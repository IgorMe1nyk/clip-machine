"""Transcribe stage and the whisper wrapper, with WhisperModel and CUDA detection faked."""

import dataclasses
import json
import logging
import math
import wave
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from clipmachine.config import dump_resolved, load_resolved, load_resolved_file
from clipmachine.context import RunContext
from clipmachine.models import TranscribeConfig, Transcript
from clipmachine.runs import RunError
from clipmachine.stages import transcribe
from clipmachine.stages.transcribe import long_gaps, validate_segments
from clipmachine.utils import whisper
from clipmachine.utils.whisper import RawSegment, RawWord, WhisperError

SECRET_TEXT = "the quick brown fox"  # transcript text must never reach the log


# --- fakes -------------------------------------------------------------------------------------


def seg(
    sid: int,
    start: float,
    end: float,
    text: str = SECRET_TEXT,
    words: list[tuple[float, float, str]] | None = None,
) -> SimpleNamespace:
    pieces = words if words is not None else [(start, end, text.split()[0] if text else "")]
    return SimpleNamespace(
        id=sid,
        start=start,
        end=end,
        text=text,
        avg_logprob=-0.25,
        no_speech_prob=0.01,
        words=[SimpleNamespace(start=a, end=b, word=w, probability=0.9) for a, b, w in pieces],
    )


class FakeModel:
    def __init__(
        self,
        segments: list[SimpleNamespace],
        *,
        fail_on_probe: bool = False,
        fail_after: int | None = None,
    ) -> None:
        self.segments = segments
        self.fail_on_probe = fail_on_probe
        self.fail_after = fail_after
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, audio: object, **kwargs: Any) -> tuple[Iterator[SimpleNamespace], Any]:
        if not isinstance(audio, str):  # the one-second silence probe passes an array
            if self.fail_on_probe:
                raise RuntimeError("Library cudnn_ops64_9.dll is not found")
            return iter(()), SimpleNamespace(language="en", language_probability=1.0, duration=1.0)
        self.calls.append({"audio": audio, **kwargs})

        def generate() -> Iterator[SimpleNamespace]:
            for index, segment in enumerate(self.segments):
                if self.fail_after is not None and index >= self.fail_after:
                    raise RuntimeError("decoder blew up")
                yield segment

        info = SimpleNamespace(language="en", language_probability=0.987654, duration=0.0)
        return generate(), info


class FakeBatched:
    def __init__(self, model: FakeModel) -> None:
        self.model = model

    def transcribe(self, audio: str, batch_size: int, **kwargs: Any) -> Any:
        return self.model.transcribe(audio, batch_size=batch_size, **kwargs)


@dataclasses.dataclass
class Env:
    model: FakeModel
    loads: list[tuple[str, str, int]]  # (device, compute_type, cpu_threads)
    closed: list[bool]
    cuda_devices: int = 1
    cuda_types: set[str] = dataclasses.field(
        default_factory=lambda: {"float32", "float16", "int8", "int8_float16"}
    )
    cuda_load_error: Exception | None = None


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Env:
    state = Env(model=FakeModel([seg(0, 0.0, 2.0)]), loads=[], closed=[])

    def new_model(path: str, device: str, compute_type: str, cpu_threads: int) -> FakeModel:
        state.loads.append((device, compute_type, cpu_threads))
        if device == "cuda" and state.cuda_load_error is not None:
            raise state.cuda_load_error
        return state.model

    real_close = whisper.LoadedModel.close

    def spy_close(self: whisper.LoadedModel) -> None:
        state.closed.append(True)
        real_close(self)

    monkeypatch.delenv(whisper.DISABLE_CUDA_ENV, raising=False)
    monkeypatch.setattr(whisper, "cuda_device_count", lambda: state.cuda_devices)
    monkeypatch.setattr(
        whisper,
        "supported_compute_types",
        lambda device: state.cuda_types if device == "cuda" else {"int8", "float32"},
    )
    monkeypatch.setattr(whisper, "model_path", lambda name, models_dir: f"fake/{name}")
    monkeypatch.setattr(whisper, "new_whisper_model", new_model)
    monkeypatch.setattr(whisper, "new_batched_pipeline", FakeBatched)
    monkeypatch.setattr(whisper, "prepare_gpu_libraries", lambda root: [])
    monkeypatch.setattr(whisper.LoadedModel, "close", spy_close)
    monkeypatch.setattr(whisper, "configure_environment", lambda root: root / "models")
    return state


def write_wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * int(16000 * seconds))


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "run.json").write_text("{}", encoding="utf-8")
    (folder / "config.resolved.yaml").write_text("x: 1\n", encoding="utf-8")
    write_wav(folder / "audio.wav", 10.0)
    return folder


def make_ctx(project: Path, run_dir: Path, **settings: Any) -> RunContext:
    resolved = load_resolved(project, "example-client")
    cfg = TranscribeConfig(**{"model": "small", **settings})
    resolved = dataclasses.replace(
        resolved, app=resolved.app.model_copy(update={"transcribe": cfg})
    )
    return RunContext(
        run_dir=run_dir, config=resolved, input_path=run_dir / "x.mp4", project_root=project
    )


def listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


UNTOUCHED = ["audio.wav", "config.resolved.yaml", "run.json"]


# --- config ------------------------------------------------------------------------------------


def test_defaults_match_the_brief() -> None:
    cfg = TranscribeConfig()
    assert (cfg.model, cfg.device, cfg.compute_type, cfg.language) == (
        "small",
        "auto",
        "auto",
        None,
    )
    assert (cfg.beam_size, cfg.vad_filter, cfg.condition_on_previous_text) == (5, True, False)
    assert (cfg.batch_size, cfg.cpu_threads) == (0, 0)


def test_default_yaml_has_every_key(project: Path) -> None:
    data = yaml.safe_load((project / "config" / "default.yaml").read_text(encoding="utf-8"))
    assert set(data["transcribe"]) == set(TranscribeConfig.model_fields)


def test_every_listed_model_is_known_to_the_installed_faster_whisper() -> None:
    from faster_whisper.utils import available_models

    from clipmachine.models import WHISPER_MODELS

    assert set(WHISPER_MODELS) == set(available_models())
    for name in ("tiny", "base", "small", "medium", "large-v3", "turbo", "distil-large-v3"):
        assert name in WHISPER_MODELS


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"model": "gigantic"}, "model must be one of"),
        ({"device": "tpu"}, "device"),
        ({"compute_type": "int4"}, "compute_type"),
        ({"language": "english"}, "language"),
        ({"language": "EN"}, "language"),
        ({"beam_size": 0}, "beam_size"),
        ({"batch_size": -1}, "batch_size"),
        ({"cpu_threads": -2}, "cpu_threads"),
        ({"batch_size": 8, "vad_filter": False}, "needs vad_filter"),
        ({"beam": 5}, "beam"),  # typo: unknown key
    ],
)
def test_invalid_settings_are_rejected(settings: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        TranscribeConfig(**settings)


def test_resolved_file_from_before_these_settings_still_loads(
    project: Path, tmp_path: Path
) -> None:
    target = tmp_path / "config.resolved.yaml"
    dump_resolved(load_resolved(project, "example-client"), target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    data["config"]["transcribe"] = {"model": "medium"}
    target.write_text(yaml.safe_dump(data), encoding="utf-8")

    cfg = load_resolved_file(target).app.transcribe
    assert cfg == TranscribeConfig(model="medium")


# --- compute type and device resolution --------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "device", "supported", "expected", "warns"),
    [
        ("auto", "cuda", {"int8_float16", "float16", "int8"}, "int8_float16", False),
        ("auto", "cuda", {"float16", "int8"}, "float16", False),
        ("auto", "cuda", {"int8", "float32"}, "int8", False),
        ("auto", "cpu", {"int8"}, "int8", False),
        ("float32", "cpu", {"float32"}, "float32", False),
        ("float16", "cuda", set(), "float16", False),
        ("int8_float16", "cpu", {"int8"}, "int8", True),
        ("float16", "cpu", {"int8"}, "int8", True),
    ],
)
def test_resolve_compute_type(
    requested: str, device: str, supported: set[str], expected: str, warns: bool
) -> None:
    compute, note = whisper.resolve_compute_type(requested, device, supported)
    assert compute == expected
    assert (note is not None) == warns


def test_auto_uses_cuda_when_it_works(env: Env, tmp_path: Path) -> None:
    loaded = whisper.open_model(TranscribeConfig(), tmp_path)
    assert (loaded.device, loaded.compute_type) == ("cuda", "int8_float16")
    assert env.loads == [("cuda", "int8_float16", 0)]
    assert loaded.warnings == []


def test_auto_falls_back_when_there_is_no_cuda_device(
    env: Env, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env.cuda_devices = 0
    with caplog.at_level(logging.INFO, logger="clipmachine"):
        loaded = whisper.open_model(TranscribeConfig(), tmp_path)
    assert (loaded.device, loaded.compute_type) == ("cpu", "int8")
    assert env.loads == [("cpu", "int8", 0)]
    reasons = [r.message for r in caplog.records if "not using the GPU" in r.message]
    assert len(reasons) == 1
    assert "no CUDA device found" in reasons[0]
    assert any("fell back to CPU" in w for w in loaded.warnings)


def test_auto_falls_back_when_the_model_fails_to_load_on_cuda(
    env: Env, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env.cuda_load_error = RuntimeError("CUDA failed with error out of memory")
    with caplog.at_level(logging.INFO, logger="clipmachine"):
        loaded = whisper.open_model(TranscribeConfig(), tmp_path)
    assert loaded.device == "cpu"
    assert [load[0] for load in env.loads] == ["cuda", "cpu"]
    (line,) = [r.message for r in caplog.records if "not using the GPU" in r.message]
    assert "out of memory" in line


def test_auto_falls_back_when_the_probe_fails_on_a_missing_library(
    env: Env, tmp_path: Path
) -> None:
    env.model.fail_on_probe = True
    loaded = whisper.open_model(TranscribeConfig(), tmp_path)
    assert loaded.device == "cpu"
    assert any("cudnn_ops64_9.dll" in w for w in loaded.warnings)


def test_cuda_device_fails_clearly_when_unusable(env: Env, tmp_path: Path) -> None:
    env.cuda_devices = 0
    with pytest.raises(WhisperError, match="device is cuda but CUDA is not usable.*no CUDA device"):
        whisper.open_model(TranscribeConfig(device="cuda"), tmp_path)
    assert env.loads == []


def test_cpu_device_never_looks_at_cuda(env: Env, tmp_path: Path) -> None:
    env.cuda_load_error = RuntimeError("must not be reached")
    loaded = whisper.open_model(TranscribeConfig(device="cpu", cpu_threads=4), tmp_path)
    assert env.loads == [("cpu", "int8", 4)]
    assert loaded.device == "cpu"


def test_environment_toggle_forces_cpu(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(whisper.DISABLE_CUDA_ENV, "1")
    loaded = whisper.open_model(TranscribeConfig(), tmp_path)
    assert loaded.device == "cpu"
    assert any(whisper.DISABLE_CUDA_ENV in w for w in loaded.warnings)
    with pytest.raises(WhisperError, match=whisper.DISABLE_CUDA_ENV):
        whisper.open_model(TranscribeConfig(device="cuda"), tmp_path)


def test_gpu_compute_type_on_cpu_becomes_int8_with_a_warning(env: Env, tmp_path: Path) -> None:
    loaded = whisper.open_model(TranscribeConfig(device="cpu", compute_type="float16"), tmp_path)
    assert loaded.compute_type == "int8"
    assert loaded.warnings and "needs a GPU" in loaded.warnings[0]


def test_configure_environment_stays_inside_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    for name in ("HF_HOME", "HF_HUB_DISABLE_TELEMETRY", "HF_HUB_DISABLE_SYMLINKS_WARNING"):
        monkeypatch.setenv(name, "")  # monkeypatch restores the real values afterwards
    models_dir = whisper.configure_environment(tmp_path)
    assert models_dir == tmp_path / "models"
    assert Path(os.environ["HF_HOME"]).parent == models_dir
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"


# --- the stage ---------------------------------------------------------------------------------


def test_success_writes_both_outputs(env: Env, project: Path, run_dir: Path) -> None:
    env.model.segments = [
        seg(0, 0.0, 2.5, "Hello there.", [(0.0, 0.8, "Hello"), (0.9, 2.5, "there.")]),
        seg(1, 62.0, 64.0, "  Second   line\nwith break ", [(62.0, 64.0, "Second")]),
    ]
    write_wav(run_dir / "audio.wav", 70.0)

    transcribe.run(make_ctx(project, run_dir))

    assert listing(run_dir) == [*UNTOUCHED, "transcript.json", "transcript.txt"]
    data = json.loads((run_dir / "transcript.json").read_text(encoding="utf-8"))
    assert list(data) == [
        "schema_version",
        "model",
        "device",
        "compute_type",
        "settings",
        "language",
        "language_probability",
        "audio_duration_seconds",
        "transcribe_seconds",
        "realtime_factor",
        "segments",
        "warnings",
    ]
    assert data["schema_version"] == 1
    assert (data["model"], data["device"], data["compute_type"]) == (
        "small",
        "cuda",
        "int8_float16",
    )
    assert data["settings"]["beam_size"] == 5
    assert data["language"] == "en"
    assert data["language_probability"] == 0.9877
    assert data["audio_duration_seconds"] == 70.0
    assert data["realtime_factor"] >= 0
    # the fixture's words are 59.5 s apart (2.5 s to 62.0 s)
    assert data["warnings"] == ["1 stretch(es) of 8 s or more without words (longest 59.5 s)"]
    first = data["segments"][0]
    assert list(first) == ["id", "start", "end", "text", "avg_logprob", "no_speech_prob", "words"]
    assert first["words"][1] == {"start": 0.9, "end": 2.5, "word": "there.", "probability": 0.9}
    Transcript.model_validate(data)

    assert (run_dir / "transcript.txt").read_text(encoding="utf-8") == (
        "[00:00] Hello there.\n[01:02] Second line with break\n"
    )


def test_transcription_options(env: Env, project: Path, run_dir: Path) -> None:
    transcribe.run(make_ctx(project, run_dir, language="en", beam_size=3, vad_filter=False))
    (call,) = env.model.calls
    assert call["audio"] == str(run_dir.resolve() / "audio.wav")
    assert call["word_timestamps"] is True
    assert call["language"] == "en"
    assert call["beam_size"] == 3
    assert call["vad_filter"] is False
    assert call["condition_on_previous_text"] is False


def test_batch_size_uses_the_batched_pipeline(env: Env, project: Path, run_dir: Path) -> None:
    transcribe.run(make_ctx(project, run_dir, batch_size=8))
    (call,) = env.model.calls
    assert call["batch_size"] == 8
    assert call["word_timestamps"] is True


def test_model_is_closed_after_success_and_after_failure(
    env: Env, project: Path, run_dir: Path
) -> None:
    transcribe.run(make_ctx(project, run_dir))
    assert env.closed == [True]

    env.model.fail_after = 0
    with pytest.raises(RuntimeError, match="decoder blew up"):
        transcribe.run(make_ctx(project, run_dir))
    assert env.closed == [True, True]


def test_progress_lines_every_minute_without_the_text(
    env: Env, project: Path, run_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_wav(run_dir / "audio.wav", 200.0)
    env.model.segments = [seg(i, i * 20.0, i * 20.0 + 19.0) for i in range(10)]
    with caplog.at_level(logging.INFO, logger="clipmachine"):
        transcribe.run(make_ctx(project, run_dir))
    progress = [r.message for r in caplog.records if "%" in r.message]
    assert len(progress) == 3  # after 60, 120 and 180 s of audio
    assert progress[0].startswith("transcribe: 40%")
    assert "elapsed" in progress[0]
    assert "quick" not in caplog.text


def test_stage_logs_device_and_never_the_text(
    env: Env, project: Path, run_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="clipmachine"):
        transcribe.run(make_ctx(project, run_dir))
    assert "device=cuda compute_type=int8_float16" in caplog.text
    assert "pid=" in caplog.text
    assert "quick" not in caplog.text


def test_missing_audio_fails_cleanly(env: Env, project: Path, run_dir: Path) -> None:
    (run_dir / "audio.wav").unlink()
    with pytest.raises(RunError, match="run the fetch stage first"):
        transcribe.run(make_ctx(project, run_dir))
    assert listing(run_dir) == ["config.resolved.yaml", "run.json"]


def test_unreadable_audio_fails_cleanly(env: Env, project: Path, run_dir: Path) -> None:
    (run_dir / "audio.wav").write_bytes(b"not a wav")
    with pytest.raises(RunError, match="not a readable WAV"):
        transcribe.run(make_ctx(project, run_dir))
    assert env.loads == []


# --- failures leave nothing behind -------------------------------------------------------------


def test_failure_mid_transcription_leaves_no_outputs(
    env: Env, project: Path, run_dir: Path
) -> None:
    env.model.segments = [seg(0, 0.0, 2.0), seg(1, 2.0, 4.0)]
    env.model.fail_after = 1
    with pytest.raises(RuntimeError, match="decoder blew up"):
        transcribe.run(make_ctx(project, run_dir))
    assert listing(run_dir) == UNTOUCHED


def test_failure_while_writing_json_removes_the_txt_too(
    env: Env, project: Path, run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(path: Path, data: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(transcribe, "write_json_atomic", boom)
    with pytest.raises(OSError, match="disk full"):
        transcribe.run(make_ctx(project, run_dir))
    assert listing(run_dir) == UNTOUCHED


def test_absurd_timestamps_fail_the_stage(env: Env, project: Path, run_dir: Path) -> None:
    env.model.segments = [
        seg(0, 500.0, 600.0, "far away", [(500.0, 501.0, "far"), (501.0, 502.0, "x")])
    ]
    with pytest.raises(RunError, match="timestamps are not usable"):
        transcribe.run(make_ctx(project, run_dir))
    assert listing(run_dir) == UNTOUCHED


def test_interrupt_also_cleans_up(
    env: Env, project: Path, run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(transcribe, "write_json_atomic", interrupted)
    with pytest.raises(KeyboardInterrupt):
        transcribe.run(make_ctx(project, run_dir))
    assert listing(run_dir) == UNTOUCHED


def test_rerun_replaces_outputs_and_removes_stale_temp_files(
    env: Env, project: Path, run_dir: Path
) -> None:
    ctx = make_ctx(project, run_dir)
    transcribe.run(ctx)
    first = (run_dir / "transcript.json").read_text(encoding="utf-8")
    (run_dir / ".transcript.json.old.tmp").write_bytes(b"leftover")
    (run_dir / ".transcript.txt.old.tmp").write_bytes(b"leftover")
    (run_dir / "notes.txt").write_text("keep me", encoding="utf-8")
    env.model.segments = [seg(0, 0.0, 1.0, "Different words")]

    transcribe.run(ctx)

    assert listing(run_dir) == [
        "audio.wav",
        "config.resolved.yaml",
        "notes.txt",
        "run.json",
        "transcript.json",
        "transcript.txt",
    ]
    assert (run_dir / "transcript.json").read_text(encoding="utf-8") != first
    assert (run_dir / "transcript.txt").read_text(encoding="utf-8") == "[00:00] Different words\n"
    assert (run_dir / "notes.txt").read_text(encoding="utf-8") == "keep me"


def test_failed_rerun_removes_previous_outputs(env: Env, project: Path, run_dir: Path) -> None:
    ctx = make_ctx(project, run_dir)
    transcribe.run(ctx)
    env.model.fail_after = 0
    env.model.segments = [seg(0, 0.0, 1.0)]
    with pytest.raises(RuntimeError, match="decoder blew up"):
        transcribe.run(ctx)
    assert listing(run_dir) == UNTOUCHED


def test_fallback_warning_lands_in_the_transcript(env: Env, project: Path, run_dir: Path) -> None:
    env.cuda_devices = 0
    transcribe.run(make_ctx(project, run_dir))
    data = json.loads((run_dir / "transcript.json").read_text(encoding="utf-8"))
    assert (data["device"], data["compute_type"]) == ("cpu", "int8")
    assert len(data["warnings"]) == 1
    assert "fell back to CPU" in data["warnings"][0]


# --- validation and clamping -------------------------------------------------------------------


def raw(
    sid: int, start: float, end: float, words: list[tuple[float, float]], text: str = "x"
) -> RawSegment:
    return RawSegment(
        id=sid,
        start=start,
        end=end,
        text=text,
        avg_logprob=-0.1,
        no_speech_prob=0.0,
        words=[RawWord(a, b, "w", 0.9) for a, b in words],
    )


def test_clean_transcript_is_unchanged() -> None:
    segments = [raw(0, 0.0, 2.0, [(0.0, 1.0), (1.0, 2.0)]), raw(1, 2.0, 3.0, [(2.0, 3.0)])]
    cleaned, warnings = validate_segments(segments, duration=10.0)
    assert cleaned == segments
    assert warnings == []


def test_time_slightly_past_the_end_is_allowed() -> None:
    segments = [raw(0, 9.0, 10.9, [(9.0, 10.9)])]
    cleaned, warnings = validate_segments(segments, duration=10.0)
    assert cleaned == segments
    assert warnings == []


def test_small_problems_are_clamped_with_a_warning() -> None:
    segments = [
        raw(0, -0.5, 4.0, [(-0.5, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0)]),
        raw(1, 4.0, 12.5, [(4.0, 5.0), (5.0, 12.5)]),
    ]
    cleaned, warnings = validate_segments(segments, duration=10.0)
    assert cleaned[0].start == 0.0
    assert cleaned[0].words[0].start == 0.0
    assert cleaned[1].end == 11.0
    assert cleaned[1].words[-1].end == 11.0
    assert len(warnings) == 2
    assert any("segment" in w for w in warnings) and any("word" in w for w in warnings)


def test_overlap_beyond_tolerance_is_moved_and_small_overlap_is_kept() -> None:
    segments = [raw(0, 0.0, 3.0, [(0.0, 1.0), (0.97, 2.0), (1.5, 3.0)])]
    cleaned, warnings = validate_segments(segments, duration=10.0)
    words = cleaned[0].words
    assert words[1].start == 0.97  # 0.03 s overlap is within the allowance
    assert words[2].start == 2.0  # 0.5 s overlap: moved to the previous end
    assert all(w.start <= w.end for w in words)
    assert len(warnings) == 1 and "previous word" in warnings[0]


def test_nan_times_are_treated_as_out_of_range() -> None:
    segments = [raw(0, 0.0, 5.0, [(0.0, 1.0), (math.nan, math.nan), (2.0, 3.0), (3.0, 4.0)])]
    cleaned, warnings = validate_segments(segments, duration=10.0)
    assert all(math.isfinite(w.start) and math.isfinite(w.end) for w in cleaned[0].words)
    assert warnings


def test_most_words_out_of_range_fails() -> None:
    segments = [raw(0, 0.0, 1.0, [(50.0, 51.0), (52.0, 53.0), (54.0, 55.0), (0.0, 1.0)])]
    with pytest.raises(RunError, match="3 of 4 words"):
        validate_segments(segments, duration=10.0)


def test_exactly_half_out_of_range_is_clamped_not_failed() -> None:
    segments = [raw(0, 0.0, 1.0, [(50.0, 51.0), (0.0, 1.0)])]
    cleaned, warnings = validate_segments(segments, duration=10.0)
    assert len(cleaned[0].words) == 2
    assert warnings


def test_segments_are_the_evidence_when_there_are_no_words() -> None:
    segments = [raw(0, 100.0, 101.0, []), raw(1, 102.0, 103.0, [])]
    with pytest.raises(RunError, match="2 of 2 segments"):
        validate_segments(segments, duration=10.0)


def test_empty_transcript_is_a_warning_not_a_failure() -> None:
    cleaned, warnings = validate_segments([], duration=10.0)
    assert cleaned == []
    assert warnings == ["no speech was found in the audio"]


def test_empty_transcript_writes_empty_txt(env: Env, project: Path, run_dir: Path) -> None:
    env.model.segments = []
    transcribe.run(make_ctx(project, run_dir))
    assert (run_dir / "transcript.txt").read_text(encoding="utf-8") == ""
    data = json.loads((run_dir / "transcript.json").read_text(encoding="utf-8"))
    assert data["segments"] == []
    assert data["warnings"] == ["no speech was found in the audio"]


# --- transcript.txt ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "[00:00]"), (59.9, "[00:59]"), (60, "[01:00]"), (3599, "[59:59]"), (3725, "[62:05]")],
)
def test_timestamp_format(seconds: float, expected: str) -> None:
    assert transcribe.format_timestamp(seconds) == expected


def test_txt_never_contains_a_line_break_inside_a_segment() -> None:
    from clipmachine.models import TranscriptSegment

    segment = TranscriptSegment(
        id=0,
        start=1.0,
        end=2.0,
        text="[00:00] fake\r\n[99:99] injected\x00line",
        avg_logprob=0.0,
        no_speech_prob=0.0,
        words=[],
    )
    text = transcribe.render_txt([segment])
    assert text.count("\n") == 1
    assert text == "[00:01] [00:00] fake [99:99] injected line\n"


def test_audio_duration_comes_from_the_wav_header(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    write_wav(path, 12.5)
    assert transcribe.audio_duration_seconds(path) == pytest.approx(12.5)


# --- style prompt, conditioning and long gaps (Brief 10) ---------------------------------------

PROMPT = "Okay. So, here's what happened. Does that make sense? Yes, it does."


def test_style_prompt_is_sent_as_hotwords_in_the_normal_path(
    env: Env, project: Path, run_dir: Path
) -> None:
    transcribe.run(make_ctx(project, run_dir, style_prompt=PROMPT))
    (call,) = env.model.calls
    assert call["hotwords"] == PROMPT
    assert "initial_prompt" not in call


def test_style_prompt_is_sent_as_hotwords_in_the_batched_path(
    env: Env, project: Path, run_dir: Path
) -> None:
    transcribe.run(make_ctx(project, run_dir, style_prompt=PROMPT, batch_size=8))
    (call,) = env.model.calls
    assert call["batch_size"] == 8
    assert call["hotwords"] == PROMPT


@pytest.mark.parametrize("batch_size", [0, 8])
def test_no_style_prompt_means_no_hotwords_key(
    env: Env, project: Path, run_dir: Path, batch_size: int
) -> None:
    transcribe.run(make_ctx(project, run_dir, batch_size=batch_size))
    (call,) = env.model.calls
    assert "hotwords" not in call


@pytest.mark.parametrize(
    "prompt",
    ["", "   ", "x" * 301, "two\nlines", "carriage\rreturn", "tab\there", "nul\x00byte"],
)
def test_bad_style_prompts_are_rejected(prompt: str) -> None:
    with pytest.raises(ValueError, match="style_prompt"):
        TranscribeConfig(style_prompt=prompt)


def test_style_prompt_of_300_characters_is_allowed() -> None:
    assert TranscribeConfig(style_prompt="x" * 300).style_prompt == "x" * 300


def test_style_prompt_defaults_to_none() -> None:
    assert TranscribeConfig().style_prompt is None


def test_transcript_settings_from_before_style_prompt_still_load(
    env: Env, project: Path, run_dir: Path
) -> None:
    transcribe.run(make_ctx(project, run_dir))
    path = run_dir / "transcript.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["settings"]["style_prompt"]
    transcript = Transcript.model_validate(data)
    assert transcript.settings.style_prompt is None


def test_default_yaml_turns_conditioning_on_and_has_a_style_prompt(project: Path) -> None:
    cfg = load_resolved(project, "example-client").app.transcribe
    assert cfg.condition_on_previous_text is True
    assert cfg.style_prompt is not None and cfg.style_prompt.strip()
    assert len(cfg.style_prompt) <= 300


def test_long_gaps_counts_gaps_between_words_across_segments() -> None:
    segments = [
        raw(0, 0.0, 3.0, [(0.0, 1.0), (10.0, 11.0)]),  # 9.0 s gap inside a segment
        raw(1, 12.0, 30.0, [(12.0, 13.0), (30.0, 31.0)]),  # 1.0 s, then 17.0 s
    ]
    count, longest = long_gaps(segments)
    assert count == 2
    assert longest == 17.0


def test_a_gap_of_exactly_the_limit_counts() -> None:
    assert long_gaps([raw(0, 0.0, 9.0, [(0.0, 1.0), (9.0, 10.0)])]) == (1, 8.0)


def test_a_gap_just_under_the_limit_does_not_count() -> None:
    assert long_gaps([raw(0, 0.0, 9.0, [(0.0, 1.0), (8.5, 9.0)])]) == (0, 0.0)


def test_long_gaps_without_words_is_zero() -> None:
    assert long_gaps([]) == (0, 0.0)
    assert long_gaps([raw(0, 0.0, 1.0, [])]) == (0, 0.0)


def test_long_gap_warning_has_counts_and_seconds_only(
    env: Env, project: Path, run_dir: Path
) -> None:
    env.model.segments = [
        seg(0, 0.0, 1.0, "aa", [(0.0, 1.0, "aa")]),
        seg(1, 5.0, 6.0, "bb", [(5.0, 6.0, "bb")]),  # 4 s: no warning
        seg(2, 16.5, 17.0, "cc", [(16.5, 17.0, "cc")]),  # 10.5 s
    ]
    write_wav(run_dir / "audio.wav", 20.0)
    transcribe.run(make_ctx(project, run_dir))
    data = json.loads((run_dir / "transcript.json").read_text(encoding="utf-8"))
    assert data["warnings"] == ["1 stretch(es) of 8 s or more without words (longest 10.5 s)"]


def test_no_long_gap_warning_when_there_are_none(env: Env, project: Path, run_dir: Path) -> None:
    env.model.segments = [seg(0, 0.0, 1.0, "aa"), seg(1, 2.0, 3.0, "bb")]
    transcribe.run(make_ctx(project, run_dir))
    data = json.loads((run_dir / "transcript.json").read_text(encoding="utf-8"))
    assert data["warnings"] == []
