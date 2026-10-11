"""Data shapes. Every model forbids unknown keys so typos fail loudly."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class ModelPrice(StrictModel):
    """USD per million tokens, copied from the live pricing page."""

    input_per_mtok: float = Field(gt=0)
    output_per_mtok: float = Field(gt=0)
    # Prices hold only for prompts up to this size (Haiku 5.5 costs more above 100,000 tokens).
    # The budget guard refuses calls that could pass it. None = no such limit.
    max_prompt_tokens: int | None = Field(default=None, gt=0)


class PricingConfig(StrictModel):
    """Defaults are empty so run folders made before this section existed still load; a call for
    a model without a price is then refused instead of guessed."""

    checked: str = ""
    source: str = ""
    models: dict[str, ModelPrice] = Field(default_factory=dict)


class BudgetConfig(StrictModel):
    max_usd_per_run: float = Field(default=1.0, gt=0)
    max_usd_per_day: float = Field(default=5.0, gt=0)  # UTC calendar day, all runs (the ledger)


MomentType = Literal["story", "opinion", "tip", "insight", "funny", "emotional"]


class CandidatesConfig(StrictModel):
    block_minutes: float = Field(default=10, gt=0)
    per_block_max: int = Field(default=5, gt=0)
    max_output_tokens: int = Field(default=2000, gt=0)
    snap_padding_seconds: float = Field(default=0.2, ge=0)
    dedupe_overlap: float = Field(default=0.5, ge=0, le=1)
    # Haiku 5.5 thinks by default and thinking counts toward max_tokens; the cheapest valid
    # setting is thinking off at low effort. Sonnet 5.5 rejects "disabled" (Brief 5).
    thinking: Literal["disabled", "adaptive"] = "disabled"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"


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
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    candidates: CandidatesConfig = Field(default_factory=CandidatesConfig)


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


class FoundMoment(StrictModel):
    """One moment as the model returns it. No Field constraints on purpose: the API schema may
    not carry them, and ranges are checked by the candidates stage afterwards."""

    start: float
    end: float
    hook: str
    reason: str
    type: MomentType

    @field_validator("type", mode="before")
    @classmethod
    def _normalise_type(cls, value: object) -> object:
        # The API may change the capitalisation of enum values.
        return value.strip().lower() if isinstance(value, str) else value


class FoundMoments(StrictModel):
    """The structured output of the find-moments call."""

    moments: list[FoundMoment]


class Candidate(StrictModel):
    id: str
    start: float  # snapped, with padding
    end: float
    model_start: float
    model_end: float
    duration: float
    block: int  # 1-based
    hook: str
    reason: str
    type: MomentType
    text: str  # rebuilt from the words; untrusted data
    word_count: int
    snap_notes: list[str]


class CandidatesSummary(StrictModel):
    blocks: int
    moments_per_block: int  # k
    requested: int  # sum of k over the blocks that were asked
    returned: int  # moments the model returned
    invalid: int  # failed validation (including over quota)
    dropped: int  # could not be snapped into the clip length
    deduped: int
    final: int
    segment_edge_starts: int  # final candidates starting at a segment edge, not a sentence start
    segment_edge_ends: int
    calls: int
    input_tokens: int
    output_tokens: int
    estimated_usd: float
    actual_usd: float


class CandidatesFile(StrictModel):
    """The contents of candidates.json. Text fields are untrusted data, never instructions."""

    schema_version: int = 1
    model: str
    clip_min_seconds: float
    clip_max_seconds: float
    settings: CandidatesConfig
    summary: CandidatesSummary
    candidates: list[Candidate]
    warnings: list[str]


CostStatus = Literal["ok", "refused", "failed", "unconfirmed"]


class CostRecord(StrictModel):
    """One API call, or one refusal to make one. `booked_usd` is what the budget counts: the
    actual cost of an answered call, the estimate of a call that may or may not have been
    billed (timeout), and 0 for refusals and calls the API rejected."""

    time: str  # UTC, ISO 8601
    purpose: str
    model: str
    status: CostStatus
    note: str | None = None  # why it was refused or failed; never a prompt or an API message
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0  # includes thinking tokens
    thinking_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    estimated_input_tokens: int = 0
    estimate_source: Literal["count_tokens", "chars_div_3", "none"] = "none"
    estimated_usd: float = 0.0  # worst case before the call
    actual_usd: float = 0.0
    booked_usd: float = 0.0
    duration_seconds: float = 0.0
    request_id: str | None = None
    stop_reason: str | None = None


class CostTotals(StrictModel):
    calls: int  # answered, rejected or unconfirmed calls (not refusals)
    refusals: int
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    estimated_usd: float
    actual_usd: float
    booked_usd: float


class CostsFile(StrictModel):
    """The contents of costs.json: only ever appended to."""

    schema_version: int = 1
    totals: CostTotals
    records: list[CostRecord]
