"""Data shapes. Every model forbids unknown keys so typos fail loudly."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClipConfig(StrictModel):
    min_seconds: float = Field(gt=0)
    max_seconds: float = Field(gt=0)
    keep_top: int = Field(gt=0)
    candidates_target: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_relations(self) -> "ClipConfig":
        if self.min_seconds >= self.max_seconds:
            raise ValueError(
                f"min_seconds ({self.min_seconds}) must be less than max_seconds "
                f"({self.max_seconds})"
            )
        if self.keep_top > self.candidates_target:
            raise ValueError(
                f"keep_top ({self.keep_top}) must not exceed candidates_target "
                f"({self.candidates_target})"
            )
        return self


class ModelsConfig(StrictModel):
    find: str = Field(min_length=1)
    score: str = Field(min_length=1)


class TranscribeConfig(StrictModel):
    model: Literal["base", "small", "medium"]


class VideoConfig(StrictModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    crop: Literal["center", "face"]


class CaptionsConfig(StrictModel):
    words_per_line: int = Field(gt=0)
    font: str = Field(min_length=1)
    highlight_color: str

    @model_validator(mode="after")
    def _check_color(self) -> "CaptionsConfig":
        if not HEX_COLOR_RE.fullmatch(self.highlight_color):
            raise ValueError(
                f"highlight_color must look like #RRGGBB, got {self.highlight_color!r}"
            )
        return self


class FetchConfig(StrictModel):
    """Input limits. Defaults keep run folders made before this section existed loadable."""

    max_size_gb: float = Field(default=8, gt=0)
    max_duration_minutes: float = Field(default=240, gt=0)
    min_duration_seconds: float = Field(default=60, gt=0)

    @model_validator(mode="after")
    def _check_relations(self) -> "FetchConfig":
        if self.min_duration_seconds >= self.max_duration_minutes * 60:
            raise ValueError(
                f"min_duration_seconds ({self.min_duration_seconds}) must be less than "
                f"max_duration_minutes ({self.max_duration_minutes}) in seconds"
            )
        return self


class AppConfig(StrictModel):
    clip: ClipConfig
    models: ModelsConfig
    transcribe: TranscribeConfig
    video: VideoConfig
    captions: CaptionsConfig
    fetch: FetchConfig = Field(default_factory=FetchConfig)


StageStatus = Literal["pending", "running", "done", "failed"]


class StageState(StrictModel):
    status: StageStatus = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


class RunState(StrictModel):
    """The contents of run.json."""

    schema_version: int = 1
    input: str
    client: str
    created_at: str
    stages: dict[str, StageState]


class VideoStreamInfo(StrictModel):
    codec: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    avg_frame_rate: str  # exactly as ffprobe prints it, e.g. "30000/1001"
    r_frame_rate: str
    rotation: int = Field(default=0, ge=0, lt=360)  # clockwise degrees; 0 if none
    variable_frame_rate: bool  # avg_frame_rate differs from r_frame_rate; noted, never fixed


class AudioStreamInfo(StrictModel):
    codec: str
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)


class MediaInfo(StrictModel):
    """What ffprobe says about a file. video/audio are the first usable stream of each kind."""

    format_name: str  # e.g. "mov,mp4,m4a,3gp,3g2,mj2"
    duration_seconds: float | None
    size_bytes: int = Field(ge=0)
    video: VideoStreamInfo | None
    audio: AudioStreamInfo | None


class FetchMeta(StrictModel):
    """The contents of meta.json."""

    schema_version: int = 1
    original_filename: str  # name only, never a folder path
    source_file: str  # file name inside the run folder
    sha256: str
    size_bytes: int
    media: MediaInfo
    audio_file: str
    ffmpeg_version: str
    fetched_at: str  # UTC, ISO 8601
    fetch_seconds: float


class ClipLength(StrictModel):
    min: float = Field(gt=0)
    max: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_order(self) -> "ClipLength":
        if self.min >= self.max:
            raise ValueError(f"clip_length.min ({self.min}) must be less than max ({self.max})")
        return self


class ClientRules(StrictModel):
    """Everything in a client's rules.yaml; used by the scoring stage later."""

    name: str = Field(min_length=1)
    niche: str
    audience: str
    tone: str
    prefer: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    clip_length: ClipLength | None = None
