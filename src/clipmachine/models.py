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


# Names faster-whisper 1.2.1 accepts (faster_whisper.utils.available_models()); a unit test
# keeps this in step with the installed version.
WHISPER_MODELS = (
    "tiny.en",
    "tiny",
    "base.en",
    "base",
    "small.en",
    "small",
    "medium.en",
    "medium",
    "large-v1",
    "large-v2",
    "large-v3",
    "large",
    "distil-large-v2",
    "distil-medium.en",
    "distil-small.en",
    "distil-large-v3",
    "distil-large-v3.5",
    "large-v3-turbo",
    "turbo",
)
LANGUAGE_CODE_RE = re.compile(r"[a-z]{2,3}")


class TranscribeConfig(StrictModel):
    """Every key but `model` has a default, so run folders made before they existed still load."""

    model: str = "small"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: Literal["auto", "float16", "int8_float16", "int8", "float32"] = "auto"
    language: str | None = None  # None = auto-detect
    beam_size: int = Field(default=5, gt=0)
    vad_filter: bool = True
    condition_on_previous_text: bool = False
    batch_size: int = Field(default=0, ge=0)  # 0 = normal transcribe; >0 = batched pipeline
    cpu_threads: int = Field(default=0, ge=0)  # 0 = library default

    @model_validator(mode="after")
    def _check_values(self) -> "TranscribeConfig":
        if self.model not in WHISPER_MODELS:
            raise ValueError(
                f"model must be one of: {', '.join(WHISPER_MODELS)}; got {self.model!r}"
            )
        if self.language is not None and not LANGUAGE_CODE_RE.fullmatch(self.language):
            raise ValueError(
                f"language must be null or an ISO code like 'en', got {self.language!r}"
            )
        if self.batch_size > 0 and not self.vad_filter:
            raise ValueError(
                "batch_size > 0 needs vad_filter: true (the batched pipeline uses VAD)"
            )
        return self


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


class TranscriptWord(StrictModel):
    start: float
    end: float
    word: str
    probability: float


class TranscriptSegment(StrictModel):
    id: int
    start: float
    end: float
    text: str
    avg_logprob: float
    no_speech_prob: float
    words: list[TranscriptWord]


class Transcript(StrictModel):
    """The contents of transcript.json. The text is untrusted data, never instructions."""

    schema_version: int = 1
    model: str
    device: str  # what was actually used
    compute_type: str  # what was actually used
    settings: TranscribeConfig
    language: str
    language_probability: float
    audio_duration_seconds: float
    transcribe_seconds: float
    realtime_factor: float  # audio seconds / processing seconds
    segments: list[TranscriptSegment]
    warnings: list[str]


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
