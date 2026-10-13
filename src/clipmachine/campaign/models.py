"""The campaign's files as models (Brief 11): campaign.yaml and one entry of moments.yaml."""

import math
import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from clipmachine.models import ClipLength, RenderedOutput, RenderedSettings, StrictModel
from clipmachine.utils.captions import clean_word

Platform = Literal["tiktok", "instagram", "youtube"]

MOMENT_ID_RE = re.compile(r"[A-Za-z0-9-]{1,24}")
_HMS_RE = re.compile(r"(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)")
_MS_RE = re.compile(r"(\d+):(\d{1,2}(?:\.\d+)?)")
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
    max_chars: int = Field(default=48, ge=1, le=200)


class WatermarkPosition(StrictModel):
    x_center: float = Field(ge=0, le=1)  # fractions of the output frame
    y_center: float = Field(ge=0, le=1)


class WatermarkConfig(StrictModel):
    file: str
    position: WatermarkPosition  # where the centre of the visible content goes
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
    closers: list[str]
    hashtags: list[str] = Field(default_factory=list)

    @field_validator("closers")
    @classmethod
    def _closers(cls, value: list[str]) -> list[str]:
        closers = [c.strip() for c in value]
        if not closers:
            raise ValueError("at least one closer is needed")
        if any(not c for c in closers):
            raise ValueError("a closer must not be empty")
        if len(set(closers)) != len(closers):
            raise ValueError("closers must be different from each other")
        return closers


class CampaignConfig(StrictModel):
    name: str = Field(min_length=1)
    source_note: str = ""
    clip_length: ClipLength
    hook: CampaignHook = Field(default_factory=CampaignHook)
    watermark: WatermarkConfig
    captions: CampaignCaptions = Field(default_factory=CampaignCaptions)
    spelling: list[SpellingRule] = Field(default_factory=list)
    names_to_check: list[str] = Field(default_factory=list)
    post: PostConfig

    @field_validator("names_to_check")
    @classmethod
    def _names(cls, value: list[str]) -> list[str]:
        names = [n.strip() for n in value]
        if any(not n for n in names):
            raise ValueError("a name must not be empty")
        return names

    @model_validator(mode="after")
    def _unique_spelling(self) -> "CampaignConfig":
        sources = [rule.source for rule in self.spelling]
        if len(set(sources)) != len(sources):
            raise ValueError("spelling: two rules have the same from")
        return self


class Moment(StrictModel):
    id: str
    start: float
    end: float | None = None  # None: the tool picks the end (`end: auto` or left out)
    hook: str
    caption: str = ""
    platform: Platform

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

    @model_validator(mode="after")
    def _order(self) -> "Moment":
        if self.end is not None and self.end <= self.start:
            raise ValueError(f"end ({self.end}) must be after start ({self.start})")
        return self

    @property
    def clip_name(self) -> str:
        return f"{self.id}_{self.platform}"


class UsedEntry(StrictModel):
    """One rendered moment in used.json. Holds times and a path, never transcript text."""

    id: str
    platform: Platform
    start: float  # final source times
    end: float
    hook: str
    closer: str
    rendered_at: str  # UTC, ISO 8601
    output: str  # the mp4, relative to the repo root, forward slashes
    source_sha256: str
    posted_at: str | None = None  # set by campaign-posted; a render is not a post


class UsedLog(StrictModel):
    schema_version: int = 1
    next_closer: int = Field(default=0, ge=0)  # where the closer rotation goes next
    last_closer: str | None = None
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
    checks: dict[str, Literal["pass", "warn", "fail"]] = Field(default_factory=dict)
    closer: str | None = None
    hook_mode: str
    hook_font_size: int
    watermark: CampaignClipWatermark
    settings: RenderedSettings | None = None
    crop_mode: Literal["center", "speaker"] | None = None
    output: RenderedOutput | None = None
    render_seconds: float | None = None
    rendered_at: str | None = None
    source_sha256: str
    api_calls: int = 0
