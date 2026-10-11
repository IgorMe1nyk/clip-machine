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
    per_block_max: int = Field(default=8, gt=0)
    max_output_tokens: int = Field(default=2000, gt=0)
    snap_padding_seconds: float = Field(default=0.2, ge=0)
    dedupe_overlap: float = Field(default=0.5, ge=0, le=1)
    # Haiku 5.5 thinks by default and thinking counts toward max_tokens; the cheapest valid
    # setting is thinking off at low effort. Sonnet 5.5 rejects "disabled" (Brief 5).
    thinking: Literal["disabled", "adaptive"] = "disabled"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    # A segment edge is a sentence boundary only after this many unpunctuated words.
    fallback_max_words: int = Field(default=40, gt=0)
    # The length the model is asked for and snapping grows short moments toward. None = the
    # defaults below, clamped into clip.min_seconds..max_seconds (see AppConfig.preferred_range).
    preferred_min_seconds: float | None = Field(default=None, gt=0)
    preferred_max_seconds: float | None = Field(default=None, gt=0)


class ScoreConfig(StrictModel):
    """The scoring stage. Every key has a default so run folders made before it existed load."""

    max_per_call: int = Field(default=20, gt=0)  # candidates judged together in one call
    max_output_tokens: int = Field(default=8000, gt=0)
    # Sonnet 5.5 rejects thinking "disabled"; between_tools is the lowest setting it accepts and
    # only works at low, medium or high effort.
    thinking: Literal["between_tools", "adaptive"] = "between_tools"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    min_score: int = Field(default=6, ge=1, le=10)
    repair_attempts: int = Field(default=1, ge=0, le=1)  # the client makes at most one repair call
    # Brief 9: context sentences shown before/after a clip, which the judge may extend into.
    # Defaults keep run folders made before it loadable.
    extend_before: int = Field(default=4, ge=0, le=10)
    extend_after: int = Field(default=3, ge=0, le=10)

    @model_validator(mode="after")
    def _check_thinking_and_effort(self) -> "ScoreConfig":
        if self.thinking == "between_tools" and self.effort in ("xhigh", "max"):
            raise ValueError(
                f"thinking 'between_tools' is not accepted at effort {self.effort!r}: use "
                "'adaptive' or a lower effort"
            )
        return self


DEFAULT_PREFERRED_MIN = 30.0
DEFAULT_PREFERRED_MAX = 50.0


def clamp_preferred(
    preferred_min: float, preferred_max: float, min_seconds: float, max_seconds: float
) -> tuple[float, float]:
    """Move a preferred range into min_seconds..max_seconds; the whole range if none is left."""
    low = max(preferred_min, min_seconds)
    high = min(preferred_max, max_seconds)
    if low >= high:
        return min_seconds, max_seconds
    return low, high


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
    # "speaker" is analysed by `clipmachine track` (Brief 8 Part A); the render stage does not
    # use it yet and refuses it.
    crop: Literal["center", "speaker"]

    @field_validator("crop", mode="before")
    @classmethod
    def _face_was_never_implemented(cls, value: object) -> object:
        if value == "face":
            raise ValueError("'face' was never implemented; use 'center' or 'speaker'")
        return value


class SpeakerConfig(StrictModel):
    """Speaker-following crop analysis (Brief 8). Every key has a default so run folders made
    before this section existed still load."""

    sample_fps: float = Field(default=10, gt=0, le=30)  # frames analysed per second of clip
    analysis_width: int = Field(default=640, ge=64)  # unused since Brief 8.1; old files carry it
    detect_width: int = Field(default=960, ge=64)  # pixels; frames are scaled to this for YuNet
    # Frames are decoded at this width (never above the source's); mouth crops are cut from them.
    landmark_width: int = Field(default=1920, ge=64)
    detect_score: float = Field(default=0.7, gt=0, lt=1)  # YuNet score a face needs
    face_crop_scale: float = Field(default=2.0, ge=1, le=6)  # crop around a face, x its box
    # Picture change between two samples (0-1) that counts as a cut in the source's edit.
    cut_threshold: float = Field(default=0.12, gt=0, le=1)
    max_faces: int = Field(default=4, ge=1, le=8)
    window_seconds: float = Field(default=0.5, gt=0)  # the speaker is chosen per window
    min_shot_seconds: float = Field(default=1.5, gt=0)  # no shot is shorter than this
    # A challenger's mouth-movement variance must beat the speaker's by this factor ...
    switch_ratio: float = Field(default=1.5, ge=1)
    confirm_windows: int = Field(default=2, ge=1)  # ... for this many windows in a row
    dead_zone: float = Field(default=0.08, ge=0, le=1)  # fraction of the crop width
    smoothing: float = Field(default=0.25, gt=0, le=1)  # exponential factor; 1 = no smoothing
    # Fraction of the frame height; smaller faces are ignored, and a clip where nearly all are
    # smaller falls back to the centre crop.
    min_face_height: float = Field(default=0.08, gt=0, le=1)
    link_max_distance: float = Field(default=0.12, gt=0, le=1)  # fraction of the frame width
    min_speech_words_per_window: int = Field(default=1, ge=1)
    debug_preview: bool = False  # also write clips/<id>.track.preview.mp4

    @model_validator(mode="after")
    def _check_relations(self) -> "SpeakerConfig":
        if self.min_shot_seconds < self.window_seconds:
            raise ValueError(
                f"min_shot_seconds ({self.min_shot_seconds}) must not be less than "
                f"window_seconds ({self.window_seconds})"
            )
        return self


FONT_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.(?:ttf|otf)")
BITRATE_RE = re.compile(r"[1-9][0-9]{1,3}k")


class CaptionsConfig(StrictModel):
    words_per_line: int = Field(gt=0)
    font: str = Field(min_length=1)
    highlight_color: str
    # added in Brief 6; defaults keep run folders made before it loadable
    font_file: str = "Montserrat-Bold.ttf"  # a file name inside assets/fonts/
    font_size: int = Field(default=80, gt=0)
    margin_v: int = Field(default=350, ge=0)  # pixels from the bottom edge
    # Brief 6.1: moves every caption event in time; positive = captions appear later
    offset_seconds: float = Field(default=0.15, ge=-1.0, le=1.0)

    @model_validator(mode="after")
    def _check_values(self) -> "CaptionsConfig":
        if not HEX_COLOR_RE.fullmatch(self.highlight_color):
            raise ValueError(
                f"highlight_color must look like #RRGGBB, got {self.highlight_color!r}"
            )
        if not FONT_FILE_RE.fullmatch(self.font_file):
            raise ValueError(
                f"font_file must be a plain .ttf or .otf file name, got {self.font_file!r}"
            )
        return self


class RenderConfig(StrictModel):
    """The render stage's encoder settings. Output size is video.width/video.height."""

    crf: int = Field(default=20, ge=0, le=51)
    preset: Literal[
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    ] = "medium"
    audio_bitrate: str = "160k"

    @model_validator(mode="after")
    def _check_bitrate(self) -> "RenderConfig":
        if not BITRATE_RE.fullmatch(self.audio_bitrate):
            raise ValueError(f"audio_bitrate must look like '160k', got {self.audio_bitrate!r}")
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
    score: ScoreConfig = Field(default_factory=ScoreConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    speaker: SpeakerConfig = Field(default_factory=SpeakerConfig)

    @model_validator(mode="after")
    def _check_preferred(self) -> "AppConfig":
        low = self.candidates.preferred_min_seconds
        high = self.candidates.preferred_max_seconds
        if (low is None) != (high is None):
            raise ValueError(
                "candidates.preferred_min_seconds and preferred_max_seconds must be set together"
            )
        if (
            low is not None
            and high is not None
            and not self.clip.min_seconds <= low < high <= self.clip.max_seconds
        ):
            raise ValueError(
                f"candidates preferred range {low:g}-{high:g} must satisfy "
                f"min_seconds ({self.clip.min_seconds:g}) <= preferred_min_seconds < "
                f"preferred_max_seconds <= max_seconds ({self.clip.max_seconds:g})"
            )
        return self

    def preferred_range(self) -> tuple[float, float]:
        """The preferred length range in effect: configured, or the defaults clamped into range."""
        low = self.candidates.preferred_min_seconds
        high = self.candidates.preferred_max_seconds
        if low is None or high is None:
            low, high = DEFAULT_PREFERRED_MIN, DEFAULT_PREFERRED_MAX
        return clamp_preferred(low, high, self.clip.min_seconds, self.clip.max_seconds)


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
    """One candidate clip moment."""

    # Docstrings and descriptions here are sent to the API as part of the schema. No numeric or
    # length constraints: the API schema may not carry them, so the candidates stage checks ranges.
    start: float = Field(description="Start of the moment in seconds, from the line timestamps")
    end: float = Field(description="End of the moment in seconds, from the line timestamps")
    hook: str = Field(description="The first sentence of the moment, quoted exactly")
    reason: str = Field(description="Why this works as a clip, at most 25 words")
    type: MomentType

    @field_validator("type", mode="before")
    @classmethod
    def _normalise_type(cls, value: object) -> object:
        # The API may change the capitalisation of enum values.
        return value.strip().lower() if isinstance(value, str) else value


class FoundMoments(StrictModel):
    """Candidate moments found in one block of a transcript."""

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
    grew_backward: int = 0  # final candidates that had to grow at the start to reach min_seconds
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


Verdict = Literal["keep", "reject"]
RejectReason = Literal[
    "no_payoff",
    "needs_context",
    "weak_hook",
    "too_long_or_rambling",
    "off_brand",
    "below_threshold",
]
REJECT_REASONS: tuple[str, ...] = RejectReason.__args__  # type: ignore[attr-defined]
DroppedBy = Literal[
    "min_score", "top_n", "trim_ignored", "adjust_ignored", "overlap"
]  # our own selection, never the model's word; trim_ignored is read from old files only
MIN_SCORE, MAX_SCORE = 1, 10


def _lower(value: object) -> object:
    # The API may change the capitalisation of enum values.
    return value.strip().lower() if isinstance(value, str) else value


class KeepSentences(StrictModel):
    # Sent to the API as part of the schema: no numeric constraints (the stage checks ranges).
    # Brief 9: numbers may be <= 0 (context before the clip) or > n (context after it)
    first: int = Field(
        description="Number of the first sentence to keep; may be 0 or negative to start earlier"
    )
    last: int = Field(
        description="Number of the last sentence to keep; may be above the clip's last number"
    )


class ClipScore(StrictModel):
    """The model's judgement of one candidate. Ranges are checked here, not by the API schema."""

    id: str = Field(description="The candidate id, copied exactly")
    score: int = Field(description="1 to 10")
    verdict: Verdict
    reject_reason: RejectReason | None = Field(description="null when the verdict is keep")
    hook_line: str = Field(description="On-screen text line, at most 10 words")
    suggested_title: str
    reason: str = Field(description="Short explanation of the verdict, at most 25 words")
    keep_sentences: KeepSentences | None = Field(
        description=(
            "Optional new range of sentence numbers (the clip's own, plus the numbered context "
            "before and after it), or null"
        )
    )

    @field_validator("verdict", "reject_reason", mode="before")
    @classmethod
    def _normalise_enum(cls, value: object) -> object:
        return _lower(value)

    @model_validator(mode="after")
    def _check_rules(self) -> "ClipScore":
        if not MIN_SCORE <= self.score <= MAX_SCORE:
            raise ValueError(f"score must be {MIN_SCORE}-{MAX_SCORE}")
        if (self.verdict == "reject") != (self.reject_reason is not None):
            raise ValueError("reject_reason must be set exactly when the verdict is reject")
        if not self.hook_line.strip() or not self.suggested_title.strip():
            raise ValueError("hook_line and suggested_title must not be empty")
        return self


class ScoreBatch(StrictModel):
    """The model's answer for one chunk of candidates."""

    clips: list[ClipScore]


class ScoredClip(StrictModel):
    id: str
    rank: int | None  # 1..N for kept clips, else None
    kept: bool
    score: int
    verdict: Verdict  # the model's, unchanged
    reject_reason: RejectReason | None  # the model's, unchanged; None when it said keep
    dropped_by: DroppedBy | None  # why a clip the model kept was still not kept
    hook_line: str
    suggested_title: str
    reason: str
    start: float  # final, after any trim
    end: float
    candidate_start: float
    candidate_end: float
    trimmed: bool
    notes: list[str]
    # added in Brief 5.1; defaults keep scored.json files from Brief 5 loadable
    start_at_segment_edge: bool = False
    end_at_segment_edge: bool = False
    sentences: int = 0  # sentences in the candidate (the "of N" of a trim)
    kept_sentences: KeepSentences | None = None  # the applied range, 1-based within the candidate
    # added in Brief 9: sentences added before the clip's first / after its last
    extended_start: int = 0
    extended_end: int = 0


class ScoredSummary(StrictModel):
    candidates: int
    kept: int
    rejected: int  # the model said reject
    dropped_min_score: int  # model said keep, score under score.min_score
    dropped_top_n: int  # model said keep, passed min_score, but outside clip.keep_top
    dropped_trim_ignored: int = 0  # old files only (Brief 9 writes adjust_ignored)
    dropped_overlap: int = 0  # overlapped a better clip by more than half of the shorter one
    extensions_applied: int = 0  # clips whose start moved earlier or end later
    adjust_ignored: int = 0  # model said keep, but the adjustment it relied on was ignored
    trimmed: int
    trims_applied: int = 0  # same number as trimmed
    trims_ignored: int = 0  # outside clip.min_seconds..max_seconds
    trims_out_of_range: int = 0  # sentence numbers outside the candidate
    reject_reasons: dict[str, int]  # the model's reasons only; every reason is present
    model: str
    effort: str
    thinking: str
    min_score: int
    keep_top: int
    chunks: int
    calls: int
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    estimated_usd: float
    actual_usd: float


class ScoredFile(StrictModel):
    """The contents of scored.json. Text fields come from the model and are untrusted data."""

    schema_version: int = 1
    settings: ScoreConfig
    summary: ScoredSummary
    clips: list[ScoredClip]
    warnings: list[str]


class ClipCropBox(StrictModel):
    w: int
    h: int
    x: int
    y: int


class RenderedSource(StrictModel):
    """The source video as the render stage saw it (from meta.json)."""

    width: int
    height: int
    rotation: int
    r_frame_rate: str
    avg_frame_rate: str
    variable_frame_rate: bool


class RenderedSettings(StrictModel):
    """Everything that shapes a clip's pixels; a change makes an old output stale."""

    width: int
    height: int
    crf: int
    preset: str
    audio_bitrate: str
    font: str
    font_file: str
    font_size: int
    margin_v: int
    words_per_line: int
    highlight_color: str
    caption_offset_seconds: float = 0.0  # added in Brief 6.1; old JSON files load as 0.0


class RenderedOutput(StrictModel):
    """The output file as ffprobe reports it."""

    width: int
    height: int
    video_codec: str
    audio_codec: str
    duration_seconds: float
    size_bytes: int


class RenderedClip(StrictModel):
    """The contents of clips/<id>.json. Holds no transcript text, hook or title."""

    schema_version: int = 1
    id: str
    rank: int | None
    score: int
    start: float
    end: float
    duration: float
    source: RenderedSource
    crop: ClipCropBox
    settings: RenderedSettings
    caption_events: int
    output: RenderedOutput
    render_seconds: float


class TrackSource(StrictModel):
    """The source video as the track analysis saw it (from meta.json)."""

    width: int
    height: int
    rotation: int
    sha256: str


class TrackFrame(StrictModel):
    """Size in pixels of the frames that were analysed."""

    w: int
    h: int


class TrackShot(StrictModel):
    start: float  # seconds from the clip start
    end: float
    track: int | None  # None: no face was followed in this shot
    face_cx: float | None  # mean face centre, 0..1 of the frame width


class TrackSample(StrictModel):
    t: float  # seconds from the clip start
    crop_x: int  # left edge of the crop in source pixels (upright frame); even


class TrackSummary(StrictModel):
    faces_seen: int  # tracks found
    switches: int  # shot changes
    face_time_pct: float  # % of samples with at least one face
    samples: int
    analysis_seconds: float
    cuts: int = 0  # shot cuts found in the source's edit
    faces_per_sample: float = 0.0
    mouth_read_pct: float = 0.0  # % of face samples whose mouth could be read


TRACK_SCHEMA_VERSION = 2  # 2: cuts_exact. load_valid_track recomputes files of any other version


class TrackFile(StrictModel):
    """The contents of clips/<id>.track.json. Numbers only: no transcript text."""

    schema_version: int = TRACK_SCHEMA_VERSION
    clip_id: str
    clip_start: float
    clip_end: float
    source: TrackSource
    settings: SpeakerConfig
    mediapipe_version: str | None
    model_sha256: str | None
    detector_sha256: str | None = None
    opencv_version: str | None = None
    frame: TrackFrame
    crop_w: int  # width of the 9:16 window in source pixels
    shots: list[TrackShot]
    samples: list[TrackSample]
    fallback_reason: str | None  # set: every crop_x is the centre crop
    summary: TrackSummary
    cuts: list[
        float
    ] = []  # shot cuts as sample times (multiples of 1 / sample_fps), from clip start
    # Parallel to `cuts`: the time, seconds from the clip start and to the frame, at which each new
    # camera shot starts. Found from the source's own frames, so it can differ from the sample time
    # by up to a few frames. Empty in files written before schema 2.
    cuts_exact: list[float] = []


CostStatus = Literal["ok", "refused", "failed", "unconfirmed"]
EstimateSource = Literal["count_tokens", "chars_div_3", "none"]


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
    estimate_source: EstimateSource = "none"
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
