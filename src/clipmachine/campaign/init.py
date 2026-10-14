"""The `campaign-init` command (Brief 12.2): draft campaigns/<slug>/campaign.yaml from the
campaign's brief, with one Sonnet call, for Igor to review before the first run.

The brief is untrusted data: it is read as text (or extracted from a PDF, text only, with size
and page limits), cleaned, fenced between <brief> tags, and never treated as instructions. The
model's answer is validated twice: as the draft schema (API-enforced, repaired once) and then as
a CampaignConfig, so the written file always loads. Nothing here prints brief text, names,
prefixes or hooks: the console shows counts, choices and paths.

`--moments` drafts moments.yaml from a timestamped list the campaign supplied, with no model
call: one line, one moment, never an invented one.
"""

import re
import shutil
from datetime import date
from pathlib import Path
from typing import Any, Literal

import pypdf
import yaml
from pydantic import Field

from clipmachine.campaign.loader import check_slug, parse_moments
from clipmachine.campaign.models import (
    PLATFORMS,
    CampaignConfig,
    Platform,
    parse_time,
)
from clipmachine.campaign.run import campaign_out_dir
from clipmachine.config import ConfigError, _validate, load_app_config
from clipmachine.models import StrictModel
from clipmachine.runs import write_text_atomic
from clipmachine.stages.candidates import fill, load_prompt
from clipmachine.utils.claude import ClaudeClient

EXIT_OK = 0
PROMPT_FILE = Path("prompts") / "campaign_init.md"
PURPOSE = "campaign-init"
INIT_DIR = "_init"  # runs/_campaigns/<slug>/_init/: the call's costs.json
MAX_OUTPUT_TOKENS = 4000
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_BRIEF_CHARS = 60_000  # extraction stops here; a longer brief is cut, and the console says so
MAX_PNG_BYTES = 5 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
BRIEF_SUFFIXES = (".txt", ".md", ".pdf")
HEADER = "# DRAFT by campaign-init. Review every line before the first run."
MOMENT_ID_PREFIX = "M"
BRIEF_TAG_RE = re.compile(r"<\s*/?\s*brief[^>]*>?", re.IGNORECASE)
# control characters except tab and newline: a brief keeps its line breaks
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]+")
BLANK_LINES_RE = re.compile(r"\n{3,}")
TIME = r"\d{1,2}:\d{2}(?::\d{2})?|\d+(?:\.\d+)?"
MOMENT_LINE_RE = re.compile(
    rf"^\s*(?P<start>{TIME})(?:\s*(?:[-–—]|to)\s*(?P<end>{TIME}))?"
    r"\s*[-–—:|.]?\s*(?P<text>.*?)\s*$"
)


# --- the draft the model returns -----------------------------------------------------------------


class PlatformText(StrictModel):
    platform: Platform = Field(description="one of the platforms listed")
    text: str = Field(description="the prefix or handle a post on that platform must carry")


class TopicHint(StrictModel):
    key: str = Field(description="a short topic key: letters, digits, '_' or '-'")
    platform: Platform | Literal["auto"] = Field(description="the platform that suits it")


class CampaignDraft(StrictModel):
    """What campaign-init asks the model for. Every field is described because the schema is
    sent with the request; the lengths are checked in draft_check, not in the wire schema."""

    name: str = Field(description="the campaign or show name, short")
    source_note: str = Field(description="where the source videos come from, one line")
    selection: Literal["human", "ai"] = Field(description="who picks the moments")
    selection_reason: str = Field(description="one sentence: why that selection")
    platforms: list[Platform] = Field(description="the platforms clips go to")
    clip_min_s: float = Field(description="the shortest allowed clip, seconds")
    clip_max_s: float = Field(description="the longest allowed clip, seconds")
    prefix: list[PlatformText] = Field(description="per platform, the required post prefix")
    hashtags: list[str] = Field(description="required hashtags, each with its #")
    closers: list[str] = Field(description="required closing lines, verbatim")
    rules: list[str] = Field(description="the brief's content rules, one per item")
    topics: list[TopicHint] = Field(description="themes and the platform each suits")
    names: list[str] = Field(description="names whose spelling matters, as the brief spells them")
    keep_case: list[str] = Field(description="words that keep their capitalisation in titles")
    niche: str = Field(description="what the content is, a short phrase")
    audience: str = Field(description="who watches, a short phrase")
    tone: str = Field(description="how it sounds, a short phrase")
    review_notes: list[str] = Field(description="what the owner must confirm by hand")


def draft_check(draft: CampaignDraft) -> str | None:
    """None when the draft can become a campaign.yaml; else a one-line problem naming fields
    only (it goes back to the model and into the log)."""
    if not 1 <= draft.clip_min_s < draft.clip_max_s <= 600:
        return "clip_min_s must be at least 1 and below clip_max_s, which is at most 600"
    if not draft.platforms or len(set(draft.platforms)) != len(draft.platforms):
        return "platforms must list each platform once and at least one"
    seen = [p.platform for p in draft.prefix]
    if len(set(seen)) != len(seen):
        return "prefix must name each platform at most once"
    for key in (t.key for t in draft.topics):
        if not key.strip() or not all(c.isalnum() or c in "_-" for c in key):
            return "a topic key is letters, digits, '_' or '-'"
    if len(set(t.key for t in draft.topics)) != len(draft.topics):
        return "topics must list each key once"
    for field_name in ("rules", "names", "closers", "hashtags", "keep_case", "review_notes"):
        values = getattr(draft, field_name)
        if len(values) > 30:
            return f"{field_name} has more than 30 items"
        if any(not " ".join(v.split()) for v in values):
            return f"{field_name} has an empty item"
    if len(set(" ".join(c.split()) for c in draft.closers)) != len(draft.closers):
        return "closers must be different from each other"
    if not draft.name.strip():
        return "name is empty"
    return None


# --- reading the brief ---------------------------------------------------------------------------


def read_brief(path: Path) -> tuple[str, bool]:
    """The brief's text, cleaned, and whether it was cut at MAX_BRIEF_CHARS. A PDF gives its
    extracted text only (no images, no scripts); it must be under MAX_PDF_BYTES and
    MAX_PDF_PAGES and not encrypted. Any trouble reading it is a ConfigError."""
    suffix = path.suffix.lower()
    if suffix not in BRIEF_SUFFIXES:
        raise ConfigError(
            f"the brief must be one of {', '.join(BRIEF_SUFFIXES)}, not {path.suffix or '(none)'!r}"
        )
    if not path.is_file():
        raise ConfigError(f"brief not found: {path}")
    if suffix == ".pdf":
        raw, cut = _pdf_text(path)
    else:
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(
                f"the brief cannot be read as UTF-8 text ({type(exc).__name__})"
            ) from exc
        cut = len(raw) > MAX_BRIEF_CHARS
        raw = raw[:MAX_BRIEF_CHARS]
    text = clean_brief(raw)
    if not text:
        raise ConfigError("the brief holds no text (a scanned PDF? save it as .txt)")
    return text, cut


def _pdf_text(path: Path) -> tuple[str, bool]:
    size = path.stat().st_size
    if size > MAX_PDF_BYTES:
        raise ConfigError(
            f"the PDF is {size / (1024 * 1024):.1f} MB; the limit is "
            f"{MAX_PDF_BYTES // (1024 * 1024)} MB. Save the brief as .txt"
        )
    parts: list[str] = []
    total = 0
    cut = False
    try:
        reader = pypdf.PdfReader(str(path))
        if reader.is_encrypted:
            raise ConfigError("the PDF is encrypted; save the brief as .txt")
        pages = len(reader.pages)
        if pages > MAX_PDF_PAGES:
            raise ConfigError(f"the PDF has {pages} pages; the limit is {MAX_PDF_PAGES}")
        for page in reader.pages:
            text = page.extract_text() or ""
            parts.append(text)
            total += len(text)
            if total >= MAX_BRIEF_CHARS:
                cut = True
                break
    except ConfigError:
        raise
    except Exception as exc:  # pypdf raises many types on a malformed file; all end here
        raise ConfigError(
            f"the PDF cannot be read ({type(exc).__name__}); is it complete? Save the brief as .txt"
        ) from exc
    return "\n".join(parts)[:MAX_BRIEF_CHARS], cut


def clean_brief(raw: str) -> str:
    """Control characters out (tab and newline stay), <brief> tags neutralised, blank runs
    collapsed, cut at MAX_BRIEF_CHARS."""
    text = CONTROL_RE.sub(" ", raw.replace("\r\n", "\n").replace("\r", "\n"))
    text = BRIEF_TAG_RE.sub("[tag removed]", text)
    text = BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()[:MAX_BRIEF_CHARS]


# --- from the draft to the files -----------------------------------------------------------------


def to_config(draft: CampaignDraft, *, watermark_file: str | None) -> dict[str, Any]:
    """The campaign.yaml mapping. Validated as a CampaignConfig before it is written, so the
    file is guaranteed to load. `host_side` is never drafted (Igor checks it in review)."""
    prefix = {p.platform: " ".join(p.text.split()) for p in draft.prefix}
    data: dict[str, Any] = {
        "name": " ".join(draft.name.split()),
        "source_note": " ".join(draft.source_note.split()),
        "selection": draft.selection,
        "scout": {
            "per_episode": 8,
            "rules": [" ".join(r.split()) for r in draft.rules],
            "topics": {t.key: t.platform for t in draft.topics},
            "niche": " ".join(draft.niche.split()) or "podcast and interview clips",
            "audience": " ".join(draft.audience.split()) or "general adult audience",
            "tone": " ".join(draft.tone.split()) or "natural, conversational",
        },
        "clip_length": {"min": draft.clip_min_s, "max": draft.clip_max_s},
        "hook": {"mode": "full", "max_chars": 48},
    }
    if watermark_file is not None:
        data["watermark"] = {"file": watermark_file, "position": "auto", "scale": 1.0}
    data["captions"] = {"style": "box_karaoke"}
    names = list(dict.fromkeys(" ".join(n.split()) for n in draft.names))
    data["names_to_check"] = names
    data["names"] = names
    data["platforms"] = list(draft.platforms)
    data["post"] = {
        "prefix": {p: prefix.get(p, "") for p in draft.platforms if prefix.get(p)},
        "closers": [" ".join(c.split()) for c in draft.closers],
        "hashtags": [" ".join(h.split()) for h in draft.hashtags],
    }
    _validate(CampaignConfig, data, "the campaign-init draft")
    return data


def render_yaml(data: dict[str, Any], draft: CampaignDraft, *, cut: bool) -> str:
    """The file text: the DRAFT header, the model's reasons as comments, then the mapping."""
    lines = [
        HEADER,
        f"# drafted {date.today().isoformat()} from the campaign brief; "
        "the brief is not stored here",
        f"# selection: {draft.selection} - {_one_line(draft.selection_reason)}",
    ]
    if cut:
        lines.append(f"# the brief was longer than {MAX_BRIEF_CHARS} characters and was cut")
    lines.extend(f"# confirm: {_one_line(note)}" for note in draft.review_notes)
    keep = [" ".join(k.split()) for k in draft.keep_case if k.strip()]
    if keep:
        lines.append(
            "# title case to keep (add to campaign.title_keep_case in config/default.yaml): "
            + ", ".join(keep)
        )
    lines.append("# host_side is not drafted: check who is on screen in the report (Brief 12.2)")
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    return "\n".join(lines) + "\n" + body


def _one_line(text: str) -> str:
    return " ".join(text.split())


def parse_moment_list(text: str, max_chars: int) -> list[dict[str, Any]]:
    """moments.yaml entries from a supplied list: one moment per non-empty line that starts
    with a time (`12:05`, `1:02:10` or seconds), an optional end time after `-` or `to`, then
    the line's text, which becomes the hook (cut at `max_chars` on a word) and the caption.
    A line without a time is an error that names it. Nothing is invented."""
    entries: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = MOMENT_LINE_RE.match(line)
        if match is None:
            raise ConfigError(
                f"--moments line {number} does not start with a time (h:mm:ss, mm:ss or seconds)"
            )
        try:
            start = _time_of(match.group("start"))
            end = _time_of(match.group("end")) if match.group("end") else None
        except ValueError as exc:
            raise ConfigError(f"--moments line {number}: {exc}") from exc
        if end is not None and end <= start:
            raise ConfigError(f"--moments line {number}: the end time is not after the start")
        text_part = _one_line(match.group("text"))
        entry: dict[str, Any] = {
            "id": f"{MOMENT_ID_PREFIX}{len(entries) + 1:02d}",
            "start": _clock(start),
            "end": "auto" if end is None else _clock(end),
            "hook": _cut_words(text_part, max_chars),
            "caption": text_part,
            "platform": "auto",
        }
        entries.append(entry)
    if not entries:
        raise ConfigError("--moments holds no moment lines")
    return entries


def _time_of(token: str) -> float:
    """parse_time takes clock strings or numbers; a bare number on a line is seconds."""
    return parse_time(float(token) if ":" not in token else token)


def _clock(seconds: float) -> str:
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    frac = seconds - whole
    tail = f".{int(round(frac * 100)):02d}" if frac >= 0.005 else ""
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}{tail}"
    return f"{minutes}:{secs:02d}{tail}"


def _cut_words(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip()


def render_moments_yaml(entries: list[dict[str, Any]]) -> str:
    lines = [
        "# DRAFT by campaign-init --moments. Review every time and hook before the first run.",
        "# end: auto lets the tool choose a sentence end; platform: auto spreads the moments.",
    ]
    body = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True, width=100)
    return "\n".join(lines) + "\n" + body


# --- the watermark file --------------------------------------------------------------------------


def check_watermark(path: Path) -> None:
    """A real PNG (signature) under MAX_PNG_BYTES; anything else is refused."""
    if not path.is_file():
        raise ConfigError(f"watermark not found: {path}")
    size = path.stat().st_size
    if size > MAX_PNG_BYTES:
        raise ConfigError(
            f"the watermark is {size / (1024 * 1024):.1f} MB; the limit is "
            f"{MAX_PNG_BYTES // (1024 * 1024)} MB"
        )
    with path.open("rb") as handle:
        head = handle.read(len(PNG_SIGNATURE))
    if head != PNG_SIGNATURE:
        raise ConfigError("the watermark must be a PNG file (the file does not start like one)")


# --- the command ---------------------------------------------------------------------------------


def _backup(path: Path, force: bool) -> Path | None:
    """Refuse to overwrite without --force; with it, keep the old file as <name>.bak."""
    if not path.exists():
        return None
    if not force:
        raise ConfigError(f"{path} exists; use --force to replace it (the old file goes to .bak)")
    backup = path.with_name(path.name + ".bak")
    shutil.copyfile(path, backup)
    return backup


def run_init(
    root: Path,
    slug: str,
    *,
    brief: Path,
    watermark: Path | None,
    moments: Path | None,
    force: bool,
) -> int:
    root = root.resolve()
    check_slug(slug)
    if slug.startswith("_"):
        raise ConfigError("a campaign name starting with '_' is reserved for the example")
    app = load_app_config(root)
    folder = root / "campaigns" / slug
    yaml_path = folder / "campaign.yaml"
    moments_path = folder / "moments.yaml"
    if yaml_path.exists() and not force:
        raise ConfigError(
            f"{yaml_path} exists; use --force to replace it (the old file goes to .bak)"
        )
    if moments is not None and moments_path.exists() and not force:
        raise ConfigError(
            f"{moments_path} exists; use --force to replace it (the old file goes to .bak)"
        )
    if watermark is not None:
        check_watermark(watermark)
    moments_entries = None
    if moments is not None:
        try:
            listed = moments.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(
                f"--moments cannot be read as UTF-8 text ({type(exc).__name__})"
            ) from exc
        moments_entries = parse_moment_list(listed, 48)

    text, cut = read_brief(brief)
    system_template, user_template = load_prompt(root, PROMPT_FILE)
    values = {"platforms": ", ".join(PLATFORMS), "max_hook_chars": "48", "brief": text}
    init_dir = campaign_out_dir(root, slug) / INIT_DIR
    init_dir.mkdir(parents=True, exist_ok=True)
    client = ClaudeClient(init_dir, root, app)
    draft, record = client.call_structured(
        PURPOSE,
        app.models.score,
        fill(system_template, values, PROMPT_FILE),
        fill(user_template, values, PROMPT_FILE),
        CampaignDraft,
        MAX_OUTPUT_TOKENS,
        thinking=app.score.thinking,
        effort=app.score.effort,
        check=draft_check,
        repairs=1,
    )

    watermark_file = None
    if watermark is not None:
        watermark_file = f"assets/{watermark.name}"
    data = to_config(draft, watermark_file=watermark_file)

    folder.mkdir(parents=True, exist_ok=True)
    if watermark is not None:
        (folder / "assets").mkdir(exist_ok=True)
        shutil.copyfile(watermark, folder / "assets" / watermark.name)
    backup = _backup(yaml_path, force)
    write_text_atomic(yaml_path, render_yaml(data, draft, cut=cut))
    if backup is not None:
        print(f"the old campaign.yaml is {backup.name}")
    if moments_entries is not None:
        parse_moments(yaml.safe_dump(moments_entries, sort_keys=False), 48)  # it must load
        backup = _backup(moments_path, force)
        write_text_atomic(moments_path, render_moments_yaml(moments_entries))
        if backup is not None:
            print(f"the old moments.yaml is {backup.name}")

    cfg = data
    print(f"campaign.yaml: {yaml_path}")
    print(f"selection: {draft.selection} (confirm; human is the safe choice when unsure)")
    print(f"platforms: {', '.join(cfg['platforms'])}")
    prefixes = ", ".join(sorted(cfg["post"]["prefix"])) or "none"
    print(
        f"post prefixes for: {prefixes}; {len(cfg['post']['closers'])} closers, "
        f"{len(cfg['post']['hashtags'])} hashtags"
    )
    print(f"clip length: {cfg['clip_length']['min']:g}-{cfg['clip_length']['max']:g} s")
    wm = "yes (position: auto)" if watermark_file else "none (pass --watermark <png> if required)"
    print(f"watermark: {wm}")
    print(
        f"scout: {len(cfg['scout']['rules'])} rules, {len(cfg['scout']['topics'])} topics; "
        f"names: {len(cfg['names'])}; confirm notes: {len(draft.review_notes)}"
    )
    if cut:
        print(f"warning: the brief was cut at {MAX_BRIEF_CHARS} characters")
    if moments_entries is not None:
        without = sum(1 for e in moments_entries if not e["hook"])
        print(f"moments.yaml: {len(moments_entries)} moments drafted, {without} without a hook")
    print(f"API cost: ${record.actual_usd:.4f} in 1 call; see {init_dir}")
    print(
        "next: review every line of campaign.yaml, then: "
        f"uv run python -m clipmachine produce <episode> --campaign {slug}"
    )
    return EXIT_OK
