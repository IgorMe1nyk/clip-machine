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
    # Accepted but no longer used: cut times come from snap.max_start_pad_seconds and
    # snap.max_end_pad_seconds.
    snap_padding_seconds: float = Field(default=0.2, ge=0)
    dedupe_overlap: float = Field(default=0.5, ge=0, le=1)
    # Haiku 5.5 thinks by default and thinking counts toward max_tokens; the cheapest valid
    # setting is thinking off at low effort. Sonnet 5.5 rejects "disabled" (Brief 5).
    thinking: Literal["disabled", "adaptive"] = "disabled"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    # An unpunctuated run longer than this many words is split: at pauses, then its largest gap.
    fallback_max_words: int = Field(default=40, gt=0)
    # The length the model is asked for and snapping grows short moments toward. None = the
    # defaults below, clamped into clip.min_seconds..max_seconds (see AppConfig.preferred_range).
    preferred_min_seconds: float | None = Field(default=None, gt=0)
    preferred_max_seconds: float | None = Field(default=None, gt=0)


class SnapConfig(StrictModel):
    """Where a clip may be cut (Brief 10). Every key has a default so older configs load."""

    min_pause_seconds: float = Field(
        default=0.35, gt=0
    )  # an unpunctuated gap this long is a boundary
    min_cut_gap: float = Field(
        default=0.15, ge=0
    )  # never cut where the gap between words is smaller
    # The cut sits in the middle of the pause, at most this far before the first word / after the
    # last word.
    max_start_pad_seconds: float = Field(default=0.25, ge=0)
    max_end_pad_seconds: float = Field(default=0.5, ge=0)

    @model_validator(mode="after")
    def _check_relations(self) -> "SnapConfig":
        if self.min_cut_gap > self.min_pause_seconds:
            raise ValueError(
                f"min_cut_gap ({self.min_cut_gap}) must not exceed min_pause_seconds "
                f"({self.min_pause_seconds})"
            )
        return self


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
STYLE_PROMPT_MAX_CHARS = 300


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
    # Short punctuated sample sent to Whisper with every window; None = no prompt.
    style_prompt: str | None = None

    @model_validator(mode="after")
    def _check_values(self) -> "TranscribeConfig":
        if self.style_prompt is not None:
            if not self.style_prompt.strip():
                raise ValueError("style_prompt must be null or a non-empty sentence")
            if len(self.style_prompt) > STYLE_PROMPT_MAX_CHARS:
                raise ValueError(
                    f"style_prompt must be at most {STYLE_PROMPT_MAX_CHARS} characters"
                )
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in self.style_prompt):
                raise ValueError("style_prompt must be one line without control characters")
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


class ZoomConfig(StrictModel):
    """Punch-in zooms (Brief R2): an instant jump to a tighter crop on some sentence starts.
    Every key has a default so configs from before this section existed still load, with
    zoom off."""

    enabled: bool = False
    # "smooth" eases to the zoomed window over `ramp_s` (Brief R2.1); "punch" jumps (Brief R2)
    style: Literal["smooth", "punch"] = "smooth"
    ramp_s: float = Field(default=0.4, ge=0, le=2)  # length of one ease in or out
    scale: float = Field(default=1.15, gt=1, le=2)  # the crop window is this much tighter
    loud_pct: float = Field(default=0.30, ge=0, le=1)  # share of the loudest sentences
    min_gap_s: float = Field(default=4.0, gt=0)  # least time between two zoom changes
    max_hold_s: float = Field(default=6.0, gt=0)  # longest a zoom-in lasts
    # A zoom that would upscale the source more than this is skipped. A key, not a constant:
    # with a higher scale the guard has to move too, or every zoom of a 1080p source is skipped.
    max_upscale: float = Field(default=2.1, ge=1)

    @model_validator(mode="after")
    def _hold_covers_the_gap(self) -> "ZoomConfig":
        if self.max_hold_s < self.min_gap_s:
            raise ValueError("max_hold_s must be at least min_gap_s")
        if 2 * self.ramp_s > self.min_gap_s:
            raise ValueError("two ramps must fit in the shortest hold: 2 * ramp_s <= min_gap_s")
        return self


class VideoConfig(StrictModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    # "center": a fixed centre crop. "speaker": the crop follows the active speaker (the render
    # stage uses a clip's track file, or analyses the clip; needs both face model files).
    crop: Literal["center", "speaker"]

    @field_validator("crop", mode="before")
    @classmethod
    def _face_was_never_implemented(cls, value: object) -> object:
        if value == "face":
            raise ValueError("'face' was never implemented; use 'center' or 'speaker'")
        return value

    zoom: ZoomConfig = Field(default_factory=ZoomConfig)  # added in Brief R2


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
    # Face boxes are detected on every Nth sample and reused in between (mouth and motion are still
    # read on every sample).
    detect_every: int = Field(default=1, ge=1, le=10)
    window_seconds: float = Field(default=0.5, gt=0)  # the speaker is chosen per window
    # "mouth": the variance of mouth openness (the default: D-019 keeps it until wide two-person
    # shots have their own layout). "mix": per window, mouth against loudness where every main
    # face has a readable mouth, else pixel motion.
    signal: Literal["mix", "mouth"] = "mouth"
    min_shot_seconds: float = Field(default=1.5, gt=0)  # signal "mouth": no shot is shorter
    # signal "mix": the shortest stretch of a new speaker that gets a cut, in windows decided by
    # sync ...
    min_turn_seconds: float = Field(default=1.0, gt=0)
    # ... and in windows decided by pixel motion (a noisier signal)
    min_turn_motion_seconds: float = Field(default=1.5, gt=0)
    # A challenger's mouth-movement variance must beat the speaker's by this factor ...
    switch_ratio: float = Field(default=1.5, ge=1)
    confirm_windows: int = Field(default=2, ge=1)  # ... for this many windows in a row
    dead_zone: float = Field(default=0.15, ge=0, le=1)  # fraction of the crop width
    smoothing: float = Field(default=0.25, gt=0, le=1)  # unused since Brief 8.2; old files carry it
    smooth_seconds: float = Field(default=0.5, ge=0)  # centred averaging of the face path; 0 = off
    glide_seconds: float = Field(default=0.6, gt=0)  # the shortest glide of the crop in a shot
    max_speed: float = Field(default=0.8, gt=0)  # crop widths per second: the fastest glide
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
        for name in ("min_turn_seconds", "min_turn_motion_seconds"):
            if getattr(self, name) < self.window_seconds:
                raise ValueError(
                    f"{name} ({getattr(self, name)}) must not be less than "
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
    # added in Brief R1; defaults keep run folders made before it loadable and rendering as before
    style: Literal["box_karaoke", "legacy"] = "legacy"
    max_words: int = Field(default=4, ge=1, le=12)  # words on one line (box_karaoke)
    max_width_px: int = Field(default=760, gt=0)  # the whole box, padding included
    word_gap_em: float = Field(default=0.45, ge=0.1, le=2.0)  # gap between words, x font size
    letter_spacing_px: float = Field(default=1.0, ge=0, le=10)
    line_gap_em: float = Field(default=0.35, ge=0, le=2.0)  # gap between two lines, x font size
    spoken_color: str = "#FFD400"
    upcoming_color: str = "#FFFFFF"
    box_opacity: float = Field(default=0.75, ge=0, le=1)
    box_padding_px: int = Field(default=18, ge=0, le=100)
    # added in Brief 11.1: words on config/wordlists/mask.txt render as first letter + ***
    # (captions and the hook, every mode); the audio is never changed
    mask_profanity: bool = True

    @model_validator(mode="after")
    def _check_values(self) -> "CaptionsConfig":
        for name in ("highlight_color", "spoken_color", "upcoming_color"):
            if not HEX_COLOR_RE.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must look like #RRGGBB, got {getattr(self, name)!r}")
        if not FONT_FILE_RE.fullmatch(self.font_file):
            raise ValueError(
                f"font_file must be a plain .ttf or .otf file name, got {self.font_file!r}"
            )
        return self


class HookConfig(StrictModel):
    """The hook overlay (Brief R1): the clip's hook line as big text at the start."""

    enabled: bool = False
    seconds: float = Field(default=2.0, gt=0, le=10)
    fade_ms: int = Field(default=150, ge=0, le=2000)
    # added in Brief 11; the hook's own size, separate from captions.font_size
    font_size: int = Field(default=80, ge=20, le=200)
    # top = the top of the safe area, even over a face (no face note); avoid_faces = below
    # any face seen while the hook is on. Old configs without it read as top.
    position: Literal["top", "avoid_faces"] = "top"


class SafeConfig(StrictModel):
    """Pixels on the output canvas that the apps cover; captions and hook stay inside the rest."""

    top: int = Field(default=290, ge=0)
    bottom: int = Field(default=560, ge=0)
    left: int = Field(default=120, ge=0)
    right: int = Field(default=192, ge=0)
    # TikTok's button column (D-023): from this x and this y down to the bottom right
    # corner. The auto watermark never touches it (Brief 11.1); the safe-zone preview
    # draws it. Captions may still reach it: Igor judges that on real frames.
    button_left: int = Field(default=780, ge=0)
    button_top: int = Field(default=840, ge=0)


class CampaignModeConfig(StrictModel):
    """Campaign mode (Brief 11): the app-wide settings; the rules live in campaigns/<slug>/."""

    # a moment's start and end may move at most this far to a clean sentence edge
    max_snap_seconds: float = Field(default=1.5, ge=0, le=10)
    # fraction of width and height: a watermark may not touch these four corners
    corner_zone: float = Field(default=0.15, gt=0, lt=0.5)
    # `end: auto` aims for this clip length in seconds
    auto_end_target_s: float = Field(default=40, gt=0)
    # Brief 11.1: a transcript span this close to a campaign name (difflib ratio) becomes a
    # spelling rule; between the two thresholds, with the same Soundex key, it is a suggestion
    name_match_threshold: float = Field(default=0.8, ge=0.5, le=1.0)
    name_suggest_threshold: float = Field(default=0.7, ge=0.5, le=1.0)
    # Brief 11.1, `watermark.position: auto`: moments scored, frames read per moment, the two
    # weights of the score, and the grid steps as fractions of the frame
    auto_watermark_sample: int = Field(default=5, ge=1, le=50)
    auto_watermark_frames: int = Field(default=3, ge=1, le=30)
    auto_watermark_face_weight: float = Field(default=0.6, ge=0, le=1)
    auto_watermark_light_weight: float = Field(default=0.4, ge=0, le=1)
    auto_watermark_x_step: float = Field(default=0.05, gt=0, le=0.5)
    auto_watermark_y_step: float = Field(default=0.02, gt=0, le=0.5)


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
    # seconds of audio fade-in and fade-out on every clip, so a tight cut never clicks; 0 = off
    audio_fade_seconds: float = Field(default=0.08, ge=0, le=0.5)

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
    snap: SnapConfig = Field(default_factory=SnapConfig)
    score: ScoreConfig = Field(default_factory=ScoreConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    speaker: SpeakerConfig = Field(default_factory=SpeakerConfig)
    hook: HookConfig = Field(default_factory=HookConfig)
    safe: SafeConfig = Field(default_factory=SafeConfig)
    # added in Brief 11; old resolved files load with the defaults
    campaign: CampaignModeConfig = Field(default_factory=CampaignModeConfig)

    @model_validator(mode="after")
    def _check_safe_area(self) -> "AppConfig":
        safe, video, captions = self.safe, self.video, self.captions
        if safe.left + safe.right >= video.width:
            raise ValueError(
                f"safe.left ({safe.left}) + safe.right ({safe.right}) must be less than "
                f"video.width ({video.width})"
            )
        if safe.top + safe.bottom >= video.height:
            raise ValueError(
                f"safe.top ({safe.top}) + safe.bottom ({safe.bottom}) must be less than "
                f"video.height ({video.height})"
            )
        if captions.style == "box_karaoke":
            area = video.width - safe.left - safe.right
            if captions.max_width_px > area:
                raise ValueError(
                    f"captions.max_width_px ({captions.max_width_px}) must not exceed the safe "
                    f"width ({area} = video.width - safe.left - safe.right)"
                )
            if captions.max_width_px <= 2 * captions.box_padding_px:
                raise ValueError(
                    f"captions.max_width_px ({captions.max_width_px}) must be more than twice "
                    f"captions.box_padding_px ({captions.box_padding_px})"
                )
        if self.hook.enabled and captions.style != "box_karaoke":
            raise ValueError("hook.enabled needs captions.style 'box_karaoke'")
        return self

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
    # added in Brief 11: the input file's last-modified time (ns) when it was fetched, used to
    # recognise the same source again; old meta.json files load as None
    source_mtime_ns: int | None = None


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
    # The snap settings the candidates were made with; None = a file from before Brief 10.
    snap: SnapConfig | None = None
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
    # added in Brief 10: how the clip's start and end are cut; None in older files
    start_boundary: str | None = None  # end kind of the sentence before the clip, or "start"
    end_boundary: str | None = None  # end kind of the clip's last sentence
    start_gap: float | None = None  # seconds of silence before the clip's first word
    end_gap: float | None = None  # seconds of silence after its last word


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
    adjusts_on_weak_boundary: int = 0  # Brief 10: adjustment would start or end at a weak edge
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
    # added in Brief 10; old JSON files load as 0.0, which makes old outputs stale so they re-render
    audio_fade_seconds: float = 0.0
    # added in Brief R1; old JSON files load as the legacy style with no hook and no margins
    caption_style: str = "legacy"
    max_words: int = 0
    max_width_px: int = 0
    word_gap_em: float = 0.0
    letter_spacing_px: float = 0.0
    line_gap_em: float = 0.0
    spoken_color: str = ""
    upcoming_color: str = ""
    box_opacity: float = 0.0
    box_padding_px: int = 0
    hook_enabled: bool = False
    hook_seconds: float = 0.0
    hook_fade_ms: int = 0
    safe_top: int = 0
    safe_bottom: int = 0
    safe_left: int = 0
    safe_right: int = 0
    # added in Brief 11; old JSON files load as 0, which makes old outputs stale so they re-render
    hook_font_size: int = 0
    # old JSON files were rendered with faces avoided
    hook_position: Literal["top", "avoid_faces"] = "avoid_faces"
    # added in Brief 11.1. Unlike the fields above, the default is the new value on purpose: an
    # old clip with no masked word is not stale, and the chance of one is not worth re-rendering
    # every run on resume
    mask_profanity: bool = True
    # added in Brief R2. Old JSON files were rendered without zoom, so they are not stale while
    # zoom is off; turning it on makes them stale and they re-render
    zoom_enabled: bool = False
    zoom_scale: float = 0.0
    zoom_loud_pct: float = 0.0
    zoom_min_gap_s: float = 0.0
    zoom_max_hold_s: float = 0.0
    zoom_max_upscale: float = 0.0
    # added in Brief R2.1; an R2 file (a punch) reads as "" and is stale when zoom is on
    zoom_style: str = ""
    zoom_ramp_s: float = 0.0


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
    # "speaker": `crop` holds the window with the first x; the rest of the path is in
    # clips/<id>.crop.cmd. Old files have none of these and read as centre crops.
    crop_mode: Literal["center", "speaker"] = "center"
    track_sha256: str | None = None  # SHA-256 of the track.json file this clip was rendered from
    track_fallback: str | None = None  # the track's fallback reason: rendered as a centre crop
    crop_changes: int = 0  # how many times the crop moves during the clip
    # added in Brief R1; old files read as no notes and no counts
    notes: list[str] = []  # reason codes only, never text
    caption_lines: int = 0
    caption_max_words: int = 0  # most words in one caption line
    hook_shown: bool = False
    hook_moved: bool = False  # moved down to clear a face
    outside_safe_zone: int = 0  # caption/hook boxes not fully inside the safe area
    masked_words: int = 0  # Brief 11.1: caption and hook words masked (a count, never the words)
    # added in Brief R2: punch-ins as (in, out) seconds from the clip start; none = zoom off
    zoom_count: int = 0
    zoom_spans: list[tuple[float, float]] = []
    # added in Brief R2.1: the ease windows (start, end) actually used; none for a punch
    zoom_ramps: list[tuple[float, float]] = []


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
    face_cy: float | None = None  # mean face centre, 0..1 of the frame height


class TrackSample(StrictModel):
    t: float  # seconds from the clip start
    crop_x: int  # left edge of the crop in source pixels (upright frame); even


class TrackFace(StrictModel):
    """One face in one analysed sample: centre and size as 0..1 of the analysed frame."""

    t: float  # seconds from the clip start: the sample's time
    cx: float
    cy: float
    w: float
    h: float


class TrackSummary(StrictModel):
    faces_seen: int  # tracks found
    switches: int  # shot changes
    face_time_pct: float  # % of samples with at least one face
    samples: int
    analysis_seconds: float
    cuts: int = 0  # shot cuts found in the source's edit
    faces_per_sample: float = 0.0
    mouth_read_pct: float = 0.0  # % of face samples whose mouth could be read
    # speech windows decided by each signal (signal "mix"); unscored: nobody scored
    sync_windows: int = 0
    motion_windows: int = 0
    unscored_windows: int = 0


# 2: cuts_exact. 3: the mix signal. 4: faces. 5: face_cy. load_valid_track recomputes files of
# any other version
TRACK_SCHEMA_VERSION = 5


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
    # Shot cuts as sample times (multiples of 1 / sample_fps), seconds from the clip start.
    cuts: list[float] = []
    # Parallel to `cuts`: the time, seconds from the clip start and to the frame, at which each new
    # camera shot starts. Found from the source's own frames, so it can differ from the sample time
    # by up to a few frames. Empty in files written before schema 2.
    cuts_exact: list[float] = []
    # Every kept face of every sample (also when the track fell back to the centre crop), for
    # overlays that must not cover a face. Numbers only. Empty in files written before schema 4.
    faces: list[TrackFace] = []


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
