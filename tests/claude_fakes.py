"""Scripted stand-ins for the Anthropic SDK client, shared by the client and stage tests.

Errors are real anthropic exception classes built from hand-made httpx2 responses; nothing here
touches the network.
"""

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2

VALID = json.dumps(
    {"moments": [{"start": 1.0, "end": 30.0, "hook": "A hook.", "reason": "why", "type": "story"}]}
)


def status_error(
    status: int,
    message: str = "boom",
    *,
    error_type: str = "api_error",
    code: str | None = None,
    retry_after: float | str | None = None,
    request_id: str = "req_test",
) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    headers = {"request-id": request_id}
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    response = httpx2.Response(status, request=request, headers=headers)
    error: dict[str, Any] = {"type": error_type, "message": message}
    if code is not None:
        error["details"] = {"error_code": code}
    body = {"type": "error", "error": error, "request_id": request_id}
    return anthropic.APIStatusError(message, response=response, body=body)


def network_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x.invalid/"))


def timeout_error() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x.invalid/"))


def answer(
    text: str = VALID,
    *,
    input_tokens: int = 1000,
    output_tokens: int = 200,
    thinking_tokens: int = 0,
    stop_reason: str = "end_turn",
    request_id: str = "req_ok",
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            output_tokens_details=SimpleNamespace(thinking_tokens=thinking_tokens),
        ),
        stop_reason=stop_reason,
        _request_id=request_id,
    )


class FakeMessages:
    def __init__(self, outcomes: list[Any], count: Any = 1000) -> None:
        self.outcomes = list(outcomes)
        self.count = count
        self.create_calls: list[dict[str, Any]] = []
        self.count_calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def count_tokens(self, **kwargs: Any) -> Any:
        self.count_calls.append(kwargs)
        if isinstance(self.count, BaseException):
            raise self.count
        return SimpleNamespace(input_tokens=self.count)
