"""The campaign's files as models (Brief 11): campaign.yaml and one entry of moments.yaml."""

import math
import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from clipmachine.models import ClipLength, RenderedOutput, RenderedSettings, StrictModel
from clipmachine.utils.captions import clean_word

Platform = Literal["tiktok", "instagram", "youtube"]
PLATFORMS: tuple[Platform, ...] = ("tiktok", "instagram", "youtube")

MOMENT_ID_RE = re.compile(r"[A-Za-z0-9-]{1,24}")
_HMS_RE = re.compile(r"(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)")
_MS_RE = re.compile(r"(\d+):(\d{1,2}(?:\.\d+)?)")
_CLOCK_RE = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")  # a posting time, 24-hour HH:MM
_FORMATS = 'use "h:mm:ss" or "mm:ss" in quotes, or a number of seconds'


def parse_time(value: object) -> float:
    """Seconds from `h:mm:ss`, `mm:ss` (minutes may pass 59, seconds may have decimals) or a
    number. PyYAML reads an unquoted `1:05:26` as the integer 3926, so a number is seconds."""
    if isinstance(value, bool):
        raise ValueError(f"bad time: {_FORMATS}")
    if isinstance(value, int | float):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"bad time {value!r}: {_FORMATS}; it must not be negative")
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"bad time: {_FORMATS}")
    text = value.strip()
    shown = text if len(text) <= 40 else text[:40] + "..."
    match = _HMS_RE.fullmatch(text)
    if match:
        hours, minutes, seconds = match.groups()
        if int(minutes) > 59 or float(seconds) >= 60:
            raise ValueError(f"bad time {shown!r}: minutes and seconds go from 0 to 59")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    match = _MS_RE.fullmatch(text)
    if match:
        minutes, seconds = match.groups()
        if float(seconds) >= 60:
            raise ValueError(f"bad time {shown!r}: seconds go from 0 to 59")
        return int(minutes) * 60 + float(seconds)
    raise ValueError(f"bad time {shown!r}: {_FORMATS}")


class CampaignHook(StrictModel):
    mode: Literal["full", "intro"] = "full"  # full = on screen the whole clip; intro = R1's time
    # optional; left out, hook.font_size from the app config is used
    font_size: int | None = Field(default=None, ge=20, le=200)
    # optional, intro mode only; left out, hook.seconds from the app config is used
    seconds: float | None = Field(default=None, gt=0, le=10)
    # optional; left out, hook.position from the app config is used
    position: Literal["top", "avoid_faces"] | None = None
    max_chars: int = Field(default=48, ge=1, le=200)
    # Brief 11.1: a word from config/wordlists/flag.txt (or a slur) in the hook is a warning in
    # the report, or fails the moment. The hook is never rewritten.
    sensitive: Literal["warn", "fail"] = "warn"


class WatermarkPosition(StrictModel):
    x_center: float = Field(ge=0, le=1)  # fractions of the output frame
    y_center: float = Field(ge=0, le=1)


class WatermarkConfig(StrictModel):
    file: str
    # where the centre of the visible content goes; "auto" (Brief 11.1) lets the tool choose by
    # face overlap and background darkness, once per campaign (auto_position.json)
    position: WatermarkPosition | Literal["auto"]
    scale: float = Field(default=1.0, gt=0, le=10)  # uniform only
    blend: Literal["normal", "screen"] = "normal"


class CampaignCaptions(StrictModel):
    style: Literal["box_karaoke"] = "box_karaoke"


class SpellingRule(StrictModel):
    """A caption word fix. In the YAML the keys are `from` and `to`."""

    source: str = Field(alias="from")
    to: str

    @field_validator("source")
    @classmethod
    def _source(cls, value: str) -> str:
        value = " ".join(value.lower().split())
        if not value:
            raise ValueError("from must have at least one word")
        return value

    @field_validator("to")
    @classmethod
    def _to(cls, value: str) -> str:
        value = clean_word(value)
        if not value:
            raise ValueError("to must not be empty")
        return value


class PostConfig(StrictModel):
    prefix: dict[Platform, str] = Field(default_factory=dict)
    # Brief 12: optional; a campaign that asks for no closer line leaves it out or empty
    closers: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)

    @field_validator("closers")
    @classmethod
    def _closers(cls, value: list[str]) -> list[str]:
        closers = [c.strip() for c in value]
        if any(not c for c in closers):
            raise ValueError("a closer must not be empty")
        if len(set(closers)) != len(closers):
            raise ValueError("closers must be different from each other")
        return closers


class CampaignZoom(StrictModel):
    """A campaign's override of video.zoom (Brief R2): a key left out falls back to the app
    config. The bounds are those of ZoomConfig."""

    enabled: bool | None = None
    style: Literal["smooth", "punch"] | None = None
    ramp_s: float | None = Field(default=None, ge=0, le=2)
    scale: float | None = Field(default=None, gt=1, le=2)
    loud_pct: float | None = Field(default=None, ge=0, le=1)
    min_gap_s: float | None = Field(default=None, gt=0)
    max_hold_s: float | None = Field(default=None, gt=0)
    max_upscale: float | None = Field(default=None, ge=1)


class CampaignVideo(StrictModel):
    zoom: CampaignZoom = Field(default_factory=CampaignZoom)


class ScoutConfig(StrictModel):
    """`scout:` in campaign.yaml (Brief 12): how the AI proposes moments for a campaign whose
    rules allow it (`selection: ai`)."""

    per_episode: int = Field(default=8, ge=1, le=30)  # moments proposed per source video, at most
    rules: list[str] = Field(default_factory=list)  # plain-language campaign rules, as written
    # topic key -> the platform its moments go to; the scorer tags every moment with one key
    topics: dict[str, Platform | Literal["auto"]] = Field(default_factory=dict)
    host_side: Literal["left", "right"] | None = None  # the host's side of the source frame
    # what the prompts say the campaign is about; the defaults suit most podcast campaigns
    niche: str = "podcast and interview clips"
    audience: str = "general adult audience"
    tone: str = "natural, conversational"

    @field_validator("rules")
    @classmethod
    def _rules(cls, value: list[str]) -> list[str]:
        rules = [" ".join(r.split()) for r in value]
        if any(not r for r in rules):
            raise ValueError("a rule must not be empty")
        return rules

    @field_validator("topics")
    @classmethod
    def _topics(
        cls, value: dict[str, Platform | Literal["auto"]]
    ) -> dict[str, Platform | Literal["auto"]]:
        for key in value:
            if not key.strip() or not all(c.isalnum() or c in "_-" for c in key):
                raise ValueError("a topic key is letters, digits, '_' or '-'")
        return value


class ScoutRecord(StrictModel):
    """One moment the scout proposed (scout.json): what the report shows about it. Holds the
    scorer's reason, never transcript text."""

    id: str
    source_sha256: str
    source_name: str
    start: float
    end: float
    score: int
    topic: str | None
    platform: Platform | Literal["auto"]
    host_check: Literal["likely", "unsure", "not_checked"]
    reason: str
    proposed_at: str
    model: str


class ScoutFile(StrictModel):
    """campaigns/<slug>/scout.json: every moment the scout ever proposed."""

    schema_version: int = 1
    records: list[ScoutRecord] = Field(default_factory=list)


class CampaignConfig(StrictModel):
    name: str = Field(min_length=1)
    source_note: str = ""
    # Brief 12: who picks the moments; `ai` lets `scout` run (only where the campaign allows it)
    selection: Literal["human", "ai"] = "human"
    scout: ScoutConfig | None = None
    clip_length: ClipLength
    hook: CampaignHook = Field(default_factory=CampaignHook)
    # Brief 12: optional; left out, no overlay is drawn and the watermark checks read "n/a"
    watermark: WatermarkConfig | None = None
    captions: CampaignCaptions = Field(default_factory=CampaignCaptions)
    spelling: list[SpellingRule] = Field(default_factory=list)
    names_to_check: list[str] = Field(default_factory=list)
    # Brief 11.1: the correct spellings; the transcript is scanned for the ways Whisper writes
    # them, and each accepted variant becomes a spelling rule (`spelling` wins on a conflict)
    names: list[str] = Field(default_factory=list)
    post: PostConfig
    # Brief 11.1: words this campaign is fine with (taken off the mask, context and flag lists;
    # a show called "High Stakes") and words to mask on top of config/wordlists/mask.txt
    allowed: list[str] = Field(default_factory=list)
    extra_masked: list[str] = Field(default_factory=list)
    # Brief R2: may override video.zoom of the app config
    video: CampaignVideo = Field(default_factory=CampaignVideo)
    # Brief 11.2: the platforms `platform: auto` spreads the moments over, in this order, and the
    # posting times (24-hour "HH:MM", Eastern) of posting_plan.csv: one slot each per platform a day
    platforms: list[Platform] = Field(default_factory=lambda: list(PLATFORMS))
    posting_times: list[str] = Field(default_factory=lambda: ["12:30", "15:30", "18:30"])

    @field_validator("names_to_check", "names", "allowed", "extra_masked")
    @classmethod
    def _names(cls, value: list[str]) -> list[str]:
        names = [n.strip() for n in value]
        if any(not n for n in names):
            raise ValueError("an entry must not be empty")
        return names

    @field_validator("platforms")
    @classmethod
    def _platforms(cls, value: list[Platform]) -> list[Platform]:
        if not value:
            raise ValueError("at least one platform is needed")
        if len(set(value)) != len(value):
            raise ValueError("a platform is listed twice")
        return value

    @field_validator("posting_times")
    @classmethod
    def _posting_times(cls, value: list[str]) -> list[str]:
        times = [t.strip() for t in value]
        if not times:
            raise ValueError("at least one posting time is needed")
        for text in times:
            if not _CLOCK_RE.fullmatch(text):
                raise ValueError(f'bad posting time {text!r}: use 24-hour "HH:MM" in quotes')
        if len(set(times)) != len(times):
            raise ValueError("a posting time is listed twice")
        return times

    @model_validator(mode="after")
    def _unique_spelling(self) -> "CampaignConfig":
        sources = [rule.source for rule in self.spelling]
        if len(set(sources)) != len(sources):
            raise ValueError("spelling: two rules have the same from")
        if self.selection == "ai" and self.scout is None:
            raise ValueError("selection: ai needs a scout: section (rules, topics, per_episode)")
        return self


class _MomentFields(StrictModel):
    """What moments.yaml says about one moment, apart from the platform."""

    id: str
    start: float
    end: float | None = None  # None: the tool picks the end (`end: auto` or left out)
    hook: str
    caption: str = ""
    # Brief 12: this moment's own source video (a path, relative to the repo root or absolute);
    # left out, the source given on the command line
    source: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not MOMENT_ID_RE.fullmatch(value):
            raise ValueError("id must be 1-24 letters, digits or '-'")
        return value

    @field_validator("start", mode="before")
    @classmethod
    def _start(cls, value: Any) -> float:
        return parse_time(value)

    @field_validator("end", mode="before")
    @classmethod
    def _end(cls, value: Any) -> float | None:
        if value is None or (isinstance(value, str) and value.strip().lower() == "auto"):
            return None
        return parse_time(value)

    @field_validator("hook")
    @classmethod
    def _hook(cls, value: str) -> str:
        cleaned = clean_word(value)
        if not cleaned:
            raise ValueError("hook must not be empty")
        return cleaned

    @field_validator("source")
    @classmethod
    def _source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("source must be a file path, or left out")
        return value

    @model_validator(mode="after")
    def _order(self) -> "_MomentFields":
        if self.end is not None and self.end <= self.start:
            raise ValueError(f"end ({self.end}) must be after start ({self.start})")
        return self


class MomentSpec(_MomentFields):
    """One entry of moments.yaml (Brief 11.2): the platform may be `auto` (or left out), and the
    assignment then decides it."""

    platform: Platform | Literal["auto"] = "auto"

    @field_validator("platform", mode="before")
    @classmethod
    def _platform(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


class Moment(_MomentFields):
    """A moment with its platform decided: what the renderer, the report and the log work with."""

    platform: Platform

    @classmethod
    def from_spec(cls, spec: MomentSpec, platform: Platform) -> "Moment":
        return cls.model_validate({**spec.model_dump(exclude={"platform"}), "platform": platform})

    @property
    def clip_name(self) -> str:
        return f"{self.id}_{self.platform}"


class Assignments(StrictModel):
    """campaigns/<slug>/assignments.json (Brief 11.2): which platform each moment belongs to.
    Written on first use and reused, so a re-render never moves a moment; delete it to reassign."""

    schema_version: int = 1
    platforms: list[Platform]  # the campaign's list when the file was written
    assigned: dict[str, Platform] = Field(default_factory=dict)  # moment id -> platform
    written_at: str


class AutoCandidate(StrictModel):
    x_center: float
    y_center: float
    score: float
    face_cost: float
    luminance: float


class AutoPosition(StrictModel):
    """campaigns/<slug>/auto_position.json: the watermark position chosen for `position: auto`,
    reused while the watermark and the frame geometry stay the same (Brief 11.1)."""

    schema_version: int = 1
    x_center: float
    y_center: float
    score: float
    face_cost: float
    luminance: float
    runner_up: AutoCandidate | None = None
    sampled: list[str]  # the moments scored, as <id>_<platform>
    candidates: int  # grid positions tried
    placeable: int  # of those, inside the frame, the safe area, off the corners and captions
    scored: int  # of those, with room for the hook in every sampled clip
    watermark_sha256: str
    scale: float
    blend: str
    frame: tuple[int, int]
    safe: tuple[int, int, int, int]  # top, bottom, left, right
    button_column: tuple[int, int] = (780, 840)  # safe.button_left, safe.button_top
    corner_zone: float
    hook_position: str
    notes: list[str] = Field(default_factory=list)
    chosen_at: str


class AutoSeen(StrictModel):
    clip: str  # <id>_<platform>
    at: float  # seconds from the start of that clip


class AutoVariant(StrictModel):
    """One way a name is written in the transcript (auto_spelling.json)."""

    text: str  # the span as spoken, lower case
    name: str
    count: int
    ratio: float
    soundex_match: bool
    # apply: a rule was made; suggest: listed only; explicit: `spelling` has this `from` already
    status: Literal["apply", "suggest", "explicit"]
    rule_to: str | None = None
    seen: list[AutoSeen] = Field(default_factory=list)  # up to 3, from the rendered moments


class AutoSpelling(StrictModel):
    """campaigns/<slug>/auto_spelling.json: what the names scan found. Local and git-ignored, it
    holds transcript words."""

    schema_version: int = 1
    names: list[str]
    apply_threshold: float
    suggest_threshold: float
    source_sha256: str
    written_at: str
    variants: list[AutoVariant]


class UsedEntry(StrictModel):
    """One rendered moment in used.json. Holds times and a path, never transcript text."""

    id: str
    platform: Platform
    start: float  # final source times
    end: float
    hook: str
    closer: str | None = None  # None: the campaign has no closers (Brief 12)
    rendered_at: str  # UTC, ISO 8601
    output: str  # the mp4, relative to the repo root, forward slashes
    source_sha256: str
    source_name: str | None = None  # the source video's file name as fetched (Brief 12)
    posted_at: str | None = None  # set by campaign-posted; a render is not a post


class CloserState(StrictModel):
    """Where one platform's closer rotation stands (Brief 11.2)."""

    next: int = Field(default=0, ge=0)
    last: str | None = None


class RejectedMoment(StrictModel):
    """A moment Igor rejected (Brief 12): never rendered again, and the scout never proposes the
    same stretch of that source again. Holds times and a path, never text."""

    id: str
    source: str | None  # the moment's `source:` as written in moments.yaml; None: the default
    start: float
    end: float
    rejected_at: str


class UsedLog(StrictModel):
    schema_version: int = 1
    rejected: list[RejectedMoment] = Field(default_factory=list)  # Brief 12
    # Brief 11: one rotation for the whole campaign; kept so old files load, no longer advanced
    next_closer: int = Field(default=0, ge=0)
    last_closer: str | None = None
    # Brief 11.2: one rotation per platform, so a platform's posts never repeat a closer in a row
    closers: dict[str, CloserState] = Field(default_factory=dict)
    entries: list[UsedEntry] = Field(default_factory=list)


CHECKS = (
    "length_in_range",
    "hook_from_start",
    "captions_on",
    "watermark_full_and_clear",
    "source_audio_only",
    "closer_present",
    "not_previously_used",
    "names_listed",
)


class CampaignClipWatermark(StrictModel):
    sha256: str
    blend: str
    position_mode: Literal["explicit", "auto"] = "explicit"
    x: int  # top-left of the scaled canvas on the output frame
    y: int
    width: int  # the scaled canvas
    height: int
    rect: tuple[float, float, float, float]  # the visible content box on the frame
    background: str
    color_type: str


class CampaignClip(StrictModel):
    """The contents of campaign/<slug>/<id>_<platform>.json. Holds no transcript text and no
    hook or caption text; the closer is the one piece of text it keeps."""

    schema_version: int = 1
    id: str
    platform: Platform
    status: Literal["rendered", "failed"]
    error: str | None = None
    requested_start: float
    requested_end: float | None
    start: float | None = None
    end: float | None = None
    duration: float | None = None
    end_mode: Literal["given", "auto"]
    auto_end_target: float | None = None  # the target length in seconds, when the end is auto
    snap_notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # reason codes from captions, hook, watermark
    spelling_replacements: int = 0
    names_to_check: dict[str, list[float]] = Field(default_factory=dict)
    # Brief 11.1: counts only; the words are in <clip>.review.txt next to the clip
    masked_words: int = 0  # in the captions, the hook and the post caption
    sensitive_words: int = 0  # flagged in the hook and the post caption
    # Brief R2: punch-in zooms; counts and times only
    zoom_count: int = 0
    zoom_spans: list[tuple[float, float]] = Field(default_factory=list)
    zoom_ramps: list[tuple[float, float]] = Field(default_factory=list)  # Brief R2.1
    # n/a (Brief 12): the campaign does not require it (no watermark, no closers)
    checks: dict[str, Literal["pass", "warn", "fail", "n/a"]] = Field(default_factory=dict)
    closer: str | None = None
    hook_mode: str
    hook_font_size: int
    hook_position: str = "avoid_faces"  # old files: what they did
    watermark: CampaignClipWatermark | None = None  # None: the campaign has no watermark
    settings: RenderedSettings | None = None
    crop_mode: Literal["center", "speaker"] | None = None
    output: RenderedOutput | None = None
    render_seconds: float | None = None
    rendered_at: str | None = None
    source_sha256: str
    api_calls: int = 0
