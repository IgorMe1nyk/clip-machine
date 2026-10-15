"""Claude API client: key loading, kill switch, budget guard, retry rules, cost log.

Every call to the API goes through ClaudeClient.call_structured. A call is made only after:
the kill switch is off, the model has a configured price, and the worst-case cost fits both the
run budget (the run's costs.json) and the day budget (the git-ignored ledger).

Secrets and untrusted text: the API key is read from <root>/.env (variable
CLIPMACHINE_ANTHROPIC_API_KEY) and handed to the SDK client explicitly; ANTHROPIC_API_KEY is never
read or set. Messages raised or logged here are built from status codes, error types and request
IDs only: never from the key, a prompt, a model answer or an API error message.

The only SEAM is new_client(): unit tests replace it, so no test touches the network.
"""

import json
import logging
import math
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from clipmachine.models import (
    AppConfig,
    CostRecord,
    CostsFile,
    CostStatus,
    CostTotals,
    EstimateSource,
    ModelPrice,
)
from clipmachine.runs import write_json_atomic

log = logging.getLogger("clipmachine")

KILL_SWITCH_ENV = "CLIPMACHINE_NO_API"
KEY_VARIABLE = "CLIPMACHINE_ANTHROPIC_API_KEY"
ENV_FILE = ".env"
COSTS_FILE = "costs.json"
LEDGER_PATH = Path("runs") / "_ledger" / "costs.jsonl"
API_URL = "https://api.anthropic.com"
REQUEST_TIMEOUT_SECONDS = 120.0


@contextmanager
def no_api() -> Iterator[None]:
    """Inside the block the kill switch is on, so every Claude call is refused before anything
    else happens (no key is read, no client is built). The variable is put back as it was
    afterwards, also when the block raises. Campaign mode runs inside it."""
    before = os.environ.get(KILL_SWITCH_ENV)
    os.environ[KILL_SWITCH_ENV] = "1"
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(KILL_SWITCH_ENV, None)
        else:
            os.environ[KILL_SWITCH_ENV] = before


MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (2.0, 8.0)  # before attempt 2 and 3 after a 5xx without retry-after
MAX_WAIT_SECONDS = 60.0  # longest retry-after we are willing to sit out
COUNT_MARGIN = 1.10  # token counts are "an estimate"
CHARS_PER_TOKEN = 3  # conservative fallback when counting is unavailable
USD_DECIMALS = 8
MAX_PROBLEM_ITEMS = 5

TRUTHY = frozenset({"1", "true", "yes", "on"})
REPAIR_NOTE = (
    "\n\nYour previous answer was rejected: {problem}. "
    "Answer again with only valid JSON that matches the schema."
)

T = TypeVar("T", bound=BaseModel)


class ClaudeError(Exception):
    """A problem with a one-line message fit to show the user."""


class ApiRefused(ClaudeError):
    """We refused to call the API (kill switch, budget, missing price or key). No network use."""


class ApiCallFailed(ClaudeError):
    """The API answered with an error we do not retry, or kept failing."""


class InvalidModelOutput(ClaudeError):
    """The answer was not valid after the one allowed retry."""


# --- API key -------------------------------------------------------------------------------------


def parse_env_value(text: str, name: str) -> str | None:
    """The value of `name` in .env text (KEY=value lines, # comments, optional quotes), or None.

    Only the one requested variable is ever returned; the rest of the file is not looked at.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        if not sep or key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            return value[1:-1].strip()
        hash_at = value.find(" #")
        return (value[:hash_at] if hash_at >= 0 else value).strip()
    return None


def load_api_key(root: Path) -> str:
    path = root / ENV_FILE
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ApiRefused(
            f"cannot read {path} ({type(exc).__name__}): create it from .env.example and set "
            f"{KEY_VARIABLE}"
        ) from None
    value = parse_env_value(text, KEY_VARIABLE)
    if not value:
        raise ApiRefused(f"{KEY_VARIABLE} is missing or empty in {path}: set it, then rerun")
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ApiRefused(f"{KEY_VARIABLE} in {path} contains spaces or control characters")
    return value


# --- cost maths ----------------------------------------------------------------------------------


def cost_usd(price: ModelPrice, input_tokens: int, output_tokens: int) -> float:
    """USD for a call. Cache tokens are priced at the input rate (caching is not used yet)."""
    total = input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok
    return round(total / 1_000_000, USD_DECIMALS)


def _usd(amount: float) -> str:
    return "$" + f"{amount:.6f}".rstrip("0").rstrip(".")


def budget_refusal(
    *, spent_run: float, spent_day: float, amount: float, max_run: float, max_day: float
) -> str | None:
    """A one-line reason if `amount` more would pass a limit, else None. Run limit comes first."""
    if spent_run + amount > max_run:
        return (
            f"budget guard: this call could cost up to {_usd(amount)} and the run has spent "
            f"{_usd(spent_run)}, over the limit {_usd(max_run)} (budget.max_usd_per_run)"
        )
    if spent_day + amount > max_day:
        return (
            f"budget guard: this call could cost up to {_usd(amount)} and today has spent "
            f"{_usd(spent_day)}, over the limit {_usd(max_day)} (budget.max_usd_per_day)"
        )
    return None


# --- error classification ------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorFacts:
    """Everything the retry rules look at. `status` is None for connection errors and timeouts."""

    status: int | None
    error_type: str | None = None
    message: str = ""  # the API's own text: matched on, never shown
    error_code: str | None = None
    retry_after: float | None = None  # seconds; None = header missing or not a number
    request_id: str | None = None


@dataclass(frozen=True)
class Decision:
    retry: bool
    wait_seconds: float = 0.0
    message: str = ""  # shown to the user when retry is False


def _seconds(raw: object) -> float | None:
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def facts_from_exception(exc: BaseException) -> ErrorFacts:
    if not isinstance(exc, anthropic.APIStatusError):
        return ErrorFacts(status=None)
    body = _mapping(exc.body)
    error = _mapping(body.get("error"))
    details = _mapping(error.get("details"))
    code = details.get("error_code") or error.get("error_code")
    raw_retry = exc.response.headers.get("retry-after")
    return ErrorFacts(
        status=exc.status_code,
        error_type=str(error.get("type") or exc.type or "") or None,
        message=str(error.get("message") or exc.message or ""),
        error_code=str(code) if code else None,
        retry_after=_seconds(raw_retry) if raw_retry is not None else None,
        request_id=exc.request_id,
    )


def classify(facts: ErrorFacts, attempt: int) -> Decision:
    """Retry or stop. The first matching rule wins; `attempt` counts from 1.

    Never retried (they mean money, limits or a bad key, not a hiccup): 401, 403, "credit
    balance is too low", the 400 for your own spend limit, a 429 with enforced_spend_limit_reached
    or without retry-after, and any other 4xx. Retried with backoff, up to MAX_ATTEMPTS in all:
    a 429 with a usable retry-after (at most MAX_WAIT_SECONDS) and any 5xx, including 529.
    """
    status = facts.status
    text = facts.message.lower()
    where = f" (request {facts.request_id})" if facts.request_id else ""

    if status == 401:
        return Decision(
            False, message=f"API key rejected (HTTP 401): check {KEY_VARIABLE} in .env{where}"
        )
    if status == 403:
        return Decision(
            False,
            message="API key lacks permission (HTTP 403): check the key's workspace and access "
            f"in the Console{where}",
        )
    if status == 402 or "credit balance is too low" in text:
        return Decision(
            False,
            message="out of API credits: check Console > Settings > Billing for the credit "
            f"balance (no credits are bought automatically){where}",
        )
    if status == 400 and text.startswith("you have reached your specified"):
        return Decision(
            False,
            message="your Console spend limit is reached: check Console > Settings > Billing > "
            f"Spend limits{where}",
        )
    if status == 429 and facts.error_code == "enforced_spend_limit_reached":
        return Decision(
            False,
            message="Anthropic's monthly usage cap is reached (enforced_spend_limit_reached): "
            f"check Console > Limits; access resumes next month{where}",
        )
    if status == 429 and facts.retry_after is None:
        return Decision(
            False,
            message="rate or usage limit with no retry time (HTTP 429): check your spend limit "
            f"and credits in the Console{where}",
        )
    if status == 429 and facts.retry_after is not None:
        wait = facts.retry_after
        if wait > MAX_WAIT_SECONDS:
            return Decision(
                False,
                message=f"rate limited (HTTP 429) for {wait:.0f} s: try again later{where}",
            )
        if attempt >= MAX_ATTEMPTS:
            return Decision(
                False,
                message=f"still rate limited (HTTP 429) after {MAX_ATTEMPTS} attempts: "
                f"try again later{where}",
            )
        return Decision(True, wait_seconds=wait)
    if status is not None and status >= 500:
        if attempt >= MAX_ATTEMPTS:
            return Decision(
                False,
                message=f"the API kept failing (HTTP {status}) after {MAX_ATTEMPTS} attempts: "
                f"try again later{where}",
            )
        wait = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1]
        if facts.retry_after is not None:
            wait = min(facts.retry_after, MAX_WAIT_SECONDS)
        return Decision(True, wait_seconds=wait)
    if status is not None:
        return Decision(
            False,
            message=f"request rejected (HTTP {status}, {facts.error_type or 'unknown error'}): "
            f"a problem with the request, not a limit{where}",
        )
    return Decision(
        False,
        message="no answer from the API (network error or timeout): check the connection; "
        "the call is booked at its estimated cost in case it was billed",
    )


# --- the client ----------------------------------------------------------------------------------


def new_client(api_key: str) -> Any:  # SEAM
    """The SDK client: our own retries only, explicit key and base URL (the SDK would otherwise
    read ANTHROPIC_BASE_URL from the environment)."""
    return anthropic.Anthropic(
        api_key=api_key, base_url=API_URL, max_retries=0, timeout=REQUEST_TIMEOUT_SECONDS
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def build_totals(records: list[CostRecord]) -> CostTotals:
    answered = [r for r in records if r.status != "refused"]
    return CostTotals(
        calls=len(answered),
        refusals=len(records) - len(answered),
        input_tokens=sum(r.input_tokens for r in records),
        output_tokens=sum(r.output_tokens for r in records),
        thinking_tokens=sum(r.thinking_tokens for r in records),
        estimated_usd=round(sum(r.estimated_usd for r in answered), USD_DECIMALS),
        actual_usd=round(sum(r.actual_usd for r in records), USD_DECIMALS),
        booked_usd=round(sum(r.booked_usd for r in records), USD_DECIMALS),
    )


class _CallFailure(Exception):
    """Internal: the network step ended without an answer."""

    def __init__(self, message: str, facts: ErrorFacts, attempts: int) -> None:
        super().__init__(message)
        self.facts = facts
        self.attempts = attempts


def _summarise_problem(exc: ValidationError) -> str:
    items = []
    for err in exc.errors()[:MAX_PROBLEM_ITEMS]:
        where = ".".join(str(p) for p in err["loc"]) or "(root)"
        # The error code and pydantic's message (our validators' own words); never the
        # offending value, which pydantic keeps out of `msg`.
        items.append(f"{where}: {err['type']} ({err['msg']})")
    return "; ".join(items) or "does not match the schema"


class ClaudeClient:
    """One run's gateway to the API. Appends to <run>/costs.json and the shared ledger."""

    def __init__(
        self,
        run_dir: Path,
        root: Path,
        app: AppConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.run_dir = run_dir
        self.root = root
        self.app = app
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic
        self.costs_path = run_dir / COSTS_FILE
        self.ledger_path = root / LEDGER_PATH
        self._records = self._load_records()
        self._first_new = len(self._records)

    # --- bookkeeping ---

    def _load_records(self) -> list[CostRecord]:
        if not self.costs_path.exists():
            return []
        try:
            return CostsFile.model_validate_json(
                self.costs_path.read_text(encoding="utf-8")
            ).records
        except (OSError, ValidationError):
            raise ApiRefused(
                f"{COSTS_FILE} in the run folder is unreadable, so the run's spending is "
                "unknown: fix it by hand, then rerun"
            ) from None

    @property
    def records(self) -> list[CostRecord]:
        """Every record of this run, including earlier processes."""
        return list(self._records)

    @property
    def session_records(self) -> list[CostRecord]:
        """Only the records added by this client object."""
        return self._records[self._first_new :]

    def session_totals(self) -> CostTotals:
        return build_totals(self.session_records)

    def spent_run(self) -> float:
        return sum(r.booked_usd for r in self._records)

    def spent_today(self) -> float:
        today = self._clock().date().isoformat()
        if not self.ledger_path.exists():
            return 0.0
        total = 0.0
        try:
            handle = self.ledger_path.open(encoding="utf-8")
        except OSError as exc:
            raise ApiRefused(
                f"cannot read {self.ledger_path.name} ({type(exc).__name__}): the daily budget "
                "cannot be checked"
            ) from None
        with handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    day = str(entry["day"])
                    booked = float(entry["booked_usd"])
                    if not math.isfinite(booked) or booked < 0:
                        raise ValueError(booked)
                except (ValueError, KeyError, TypeError):
                    raise ApiRefused(
                        f"{self.ledger_path.name} line {number} is unreadable, so the daily "
                        "budget cannot be checked: fix or remove that line"
                    ) from None
                if day == today:
                    total += booked
        return total

    def _append_ledger(self, record: CostRecord, now: datetime) -> None:
        entry = {
            "time": record.time,
            "day": now.date().isoformat(),
            "run": self.run_dir.name,
            "purpose": record.purpose,
            "model": record.model,
            "status": record.status,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "booked_usd": record.booked_usd,
            "request_id": record.request_id,
        }
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _book(self, record: CostRecord, now: datetime) -> None:
        """Ledger first (the cap across runs), then this run's costs.json."""
        if record.status != "refused":
            self._append_ledger(record, now)
        self._records.append(record)
        costs = CostsFile(totals=build_totals(self._records), records=self._records)
        write_json_atomic(self.costs_path, costs.model_dump(mode="json"))

    def _refuse(
        self,
        purpose: str,
        model: str,
        reason: str,
        *,
        estimated: float = 0.0,
        estimated_input_tokens: int = 0,
        source: EstimateSource = "none",
    ) -> ApiRefused:
        now = self._clock()
        self._book(
            CostRecord(
                time=now.isoformat(),
                purpose=purpose,
                model=model,
                status="refused",
                note=reason,
                estimated_input_tokens=estimated_input_tokens,
                estimate_source=source,
                estimated_usd=estimated,
            ),
            now,
        )
        log.info(f"claude: {purpose} refused: {reason}")
        return ApiRefused(reason)

    # --- the call ---

    def call_structured(
        self,
        purpose: str,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int,
        *,
        thinking: str | None = None,
        effort: str | None = None,
        check: Callable[[T], str | None] | None = None,
        soft_check: Callable[[T], str | None] | None = None,
        repairs: int = 1,
    ) -> tuple[T, CostRecord]:
        """Ask for JSON matching `schema`; return it parsed, plus the record of the call.

        An answer that is not valid (refusal, cut off, bad JSON, schema mismatch, or rejected by
        `check`) gets up to `repairs` (0 or 1) more calls that name the problem; then
        InvalidModelOutput is raised. Every call, and every refusal to call, is recorded.

        `check` sees the parsed answer and returns None when it is fine, or a one-line problem.
        The problem is sent to the model and logged, so it must never contain the model's own
        text or any transcript text. `soft_check` works the same way for a rule worth a repair
        but never a failure: its problem asks for the repair while one is left, and the last
        answer is taken whatever it says (the caller deals with what still breaks the rule). An
        answer that broke only a soft rule is kept, and returned when the repair is invalid.
        """
        if repairs not in (0, 1):
            raise ValueError("repairs must be 0 or 1")
        prompt = user
        problem = ""
        kept: tuple[T, CostRecord] | None = None  # an answer that broke only a soft rule
        for round_number in range(1 + repairs):
            label = purpose if round_number == 0 else f"{purpose}:repair"
            record, text = self._one_call(
                label, model, system, prompt, schema, max_tokens, thinking, effort
            )
            parsed, problem = self._parse(schema, text, record.stop_reason)
            if parsed is not None:
                last = round_number == repairs
                strict = check(parsed) if check is not None else None
                soft = soft_check(parsed) if soft_check is not None and not last else None
                if soft and not strict:
                    kept = (parsed, record)
                found = [p for p in (strict, soft) if p]
                if found:
                    parsed, problem = None, "; ".join(found)
            if parsed is not None:
                return parsed, record
            log.info(f"claude: {label} gave an invalid answer ({problem})")
            prompt = user + REPAIR_NOTE.format(problem=problem)
        if kept is not None:
            log.info(f"claude: {purpose}: the repair was invalid; the first answer is taken")
            return kept
        retried = "was still invalid after one retry" if repairs else "was invalid"
        raise InvalidModelOutput(f"the model's answer for {purpose} {retried}: {problem}")

    @staticmethod
    def _parse(schema: type[T], text: str, stop_reason: str | None) -> tuple[T | None, str]:
        if stop_reason == "refusal":
            return None, "the model refused to answer"
        if stop_reason == "max_tokens":
            return None, "the answer was cut off at max_tokens; give fewer, shorter items"
        try:
            return schema.model_validate_json(text), ""
        except ValidationError as exc:
            return None, _summarise_problem(exc)

    def _one_call(
        self,
        purpose: str,
        model: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int,
        thinking: str | None,
        effort: str | None,
    ) -> tuple[CostRecord, str]:
        # Kill switch first: nothing else (not even .env) is touched.
        if os.environ.get(KILL_SWITCH_ENV, "").strip().lower() in TRUTHY:
            raise self._refuse(
                purpose,
                model,
                f"API disabled: {KILL_SWITCH_ENV}=1 is set; unset it to allow Claude calls",
            )
        price = self.app.pricing.models.get(model)
        if price is None:
            raise self._refuse(
                purpose,
                model,
                f"no price for model {model!r} in config pricing: add it (from the pricing "
                "page) so the budget can be checked",
            )

        budget = self.app.budget
        spent_run = self.spent_run()
        spent_day = self.spent_today()
        floor = cost_usd(price, 0, max_tokens)  # the output alone: needs no network
        reason = budget_refusal(
            spent_run=spent_run,
            spent_day=spent_day,
            amount=floor,
            max_run=budget.max_usd_per_run,
            max_day=budget.max_usd_per_day,
        )
        if reason is not None:
            raise self._refuse(purpose, model, reason, estimated=floor)

        client = new_client(self._key())
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}
        }
        if effort is not None:
            output_config["effort"] = effort
        request: dict[str, Any] = {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }
        if thinking is not None:
            request["thinking"] = {"type": thinking}

        started = self._monotonic()
        try:
            in_worst, source = self._count(client, request, system, user, output_config)
        except _CallFailure as failure:
            raise self._fail(purpose, model, failure, 0.0, 0, "none", started) from None

        estimated = cost_usd(price, in_worst, max_tokens)
        reason = None
        if price.max_prompt_tokens is not None and in_worst > price.max_prompt_tokens:
            reason = (
                f"budget guard: the prompt could be {in_worst} tokens, over the "
                f"{price.max_prompt_tokens} the configured price covers: use smaller blocks"
            )
        else:
            reason = budget_refusal(
                spent_run=spent_run,
                spent_day=spent_day,
                amount=estimated,
                max_run=budget.max_usd_per_run,
                max_day=budget.max_usd_per_day,
            )
        if reason is not None:
            raise self._refuse(
                purpose,
                model,
                reason,
                estimated=estimated,
                estimated_input_tokens=in_worst,
                source=source,
            )

        try:
            response, attempts = self._send(client, {**request, "max_tokens": max_tokens})
        except _CallFailure as failure:
            raise self._fail(
                purpose, model, failure, estimated, in_worst, source, started
            ) from None
        return self._record_answer(
            purpose, model, price, response, attempts, estimated, in_worst, source, started
        )

    def _key(self) -> str:
        return load_api_key(self.root)

    def _count(
        self,
        client: Any,
        request: dict[str, Any],
        system: str,
        user: str,
        output_config: dict[str, Any],
    ) -> tuple[int, EstimateSource]:
        """Worst-case input tokens: the free count plus a margin, else characters / 3."""
        try:
            counted = client.messages.count_tokens(**request)
            return math.ceil(int(counted.input_tokens) * COUNT_MARGIN), "count_tokens"
        except Exception as exc:
            facts = facts_from_exception(exc)
            if facts.status in (401, 403):
                raise _CallFailure(classify(facts, 1).message, facts, 1) from None
        chars = len(system) + len(user) + len(json.dumps(output_config))
        return math.ceil(chars / CHARS_PER_TOKEN), "chars_div_3"

    def _send(self, client: Any, request: dict[str, Any]) -> tuple[Any, int]:
        attempt = 0
        while True:
            attempt += 1
            try:
                return client.messages.create(**request), attempt
            except Exception as exc:
                facts = facts_from_exception(exc)
                decision = classify(facts, attempt)
                if not decision.retry:
                    raise _CallFailure(decision.message, facts, attempt) from None
                log.info(
                    f"claude: attempt {attempt} failed (HTTP {facts.status}); "
                    f"waiting {decision.wait_seconds:.0f} s"
                )
                self._sleep(decision.wait_seconds)

    def _fail(
        self,
        purpose: str,
        model: str,
        failure: _CallFailure,
        estimated: float,
        estimated_input_tokens: int,
        source: EstimateSource,
        started: float,
    ) -> ApiCallFailed:
        """Book a call that ended without an answer. A timeout may still have been billed."""
        unconfirmed = failure.facts.status is None
        status: CostStatus = "unconfirmed" if unconfirmed else "failed"
        now = self._clock()
        self._book(
            CostRecord(
                time=now.isoformat(),
                purpose=purpose,
                model=model,
                status=status,
                note=str(failure),
                attempts=failure.attempts,
                estimated_input_tokens=estimated_input_tokens,
                estimate_source=source,
                estimated_usd=estimated,
                booked_usd=estimated if unconfirmed else 0.0,
                duration_seconds=round(self._monotonic() - started, 3),
                request_id=failure.facts.request_id,
            ),
            now,
        )
        log.info(f"claude: {purpose} failed: {failure}")
        return ApiCallFailed(str(failure))

    def _record_answer(
        self,
        purpose: str,
        model: str,
        price: ModelPrice,
        response: Any,
        attempts: int,
        estimated: float,
        estimated_input_tokens: int,
        source: EstimateSource,
        started: float,
    ) -> tuple[CostRecord, str]:
        usage = response.usage
        cache_created = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        input_tokens = int(usage.input_tokens)
        output_tokens = int(usage.output_tokens)
        details = getattr(usage, "output_tokens_details", None)
        thinking_tokens = int(getattr(details, "thinking_tokens", 0) or 0)
        total_in = input_tokens + cache_created + cache_read
        actual = cost_usd(price, total_in, output_tokens)

        notes = []
        if cache_created or cache_read:
            notes.append("cache tokens were used (caching is not planned); priced as input")
        if price.max_prompt_tokens is not None and total_in > price.max_prompt_tokens:
            notes.append("prompt was over the price tier; actual cost is understated")

        now = self._clock()
        record = CostRecord(
            time=now.isoformat(),
            purpose=purpose,
            model=model,
            status="ok",
            note="; ".join(notes) or None,
            attempts=attempts,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=thinking_tokens,
            cache_creation_tokens=cache_created,
            cache_read_tokens=cache_read,
            estimated_input_tokens=estimated_input_tokens,
            estimate_source=source,
            estimated_usd=estimated,
            actual_usd=actual,
            booked_usd=actual,
            duration_seconds=round(self._monotonic() - started, 3),
            request_id=getattr(response, "_request_id", None),
            stop_reason=getattr(response, "stop_reason", None),
        )
        self._book(record, now)  # booked before the answer is validated
        log.info(
            f"claude: {purpose} model={model} in={total_in} out={output_tokens} "
            f"cost={_usd(actual)} (estimated up to {_usd(estimated)}) "
            f"{record.duration_seconds:.1f} s request={record.request_id}"
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return record, text
