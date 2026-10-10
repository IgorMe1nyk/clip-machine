import hashlib
import json
import logging
import os
from collections import namedtuple
from collections.abc import Callable
from pathlib import Path

import pytest

from clipmachine.config import load_resolved
from clipmachine.context import RunContext
from clipmachine.models import AudioStreamInfo, MediaInfo, VideoStreamInfo
from clipmachine.runs import GIB, RunError
from clipmachine.stages import fetch
from clipmachine.utils import ffmpeg
from clipmachine.utils.ffmpeg import MediaError
from helpers import edit_yaml

DiskUsage = namedtuple("DiskUsage", "total used free")  # noqa: PYI024


def video_info(duration: float = 120.0, *, variable: bool = False, audio: bool = True) -> MediaInfo:
    return MediaInfo(
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        duration_seconds=duration,
        size_bytes=0,
        video=VideoStreamInfo(
            codec="h264",
            width=1920,
            height=1080,
            avg_frame_rate="25/1" if variable else "30/1",
            r_frame_rate="30/1",
            rotation=0,
            variable_frame_rate=variable,
        ),
        audio=AudioStreamInfo(codec="aac", sample_rate=48000, channels=2) if audio else None,
    )


def wav_info(duration: float) -> MediaInfo:
    return MediaInfo(
        format_name="wav",
        duration_seconds=duration,
        size_bytes=0,
        video=None,
        audio=AudioStreamInfo(codec="pcm_s16le", sample_rate=16000, channels=1),
    )


class FakeTools:
    """Replaces ffprobe/ffmpeg so the stage can run without them."""

    def __init__(self) -> None:
        self.source_info = video_info()
        self.audio_info: MediaInfo | None = None  # default: matches the source duration
        self.probe_error: Exception | None = None
        self.extract_error: Exception | None = None
        self.extract_sources: list[Path] = []

    def probe(self, path: Path) -> MediaInfo:
        if self.probe_error is not None:
            raise self.probe_error
        if path.name.startswith(".audio.wav"):
            if self.audio_info is not None:
                return self.audio_info
            return wav_info(self.source_info.duration_seconds or 0.0)
        return self.source_info

    def extract_audio(self, source: Path, temp_wav: Path, duration: float) -> None:
        self.extract_sources.append(source)
        temp_wav.write_bytes(b"RIFF-partial")  # leaves a half-written file if it then fails
        if self.extract_error is not None:
            raise self.extract_error
        temp_wav.write_bytes(b"RIFF-complete-wav")


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> FakeTools:
    fake = FakeTools()
    monkeypatch.setattr(ffmpeg, "probe", fake.probe)
    monkeypatch.setattr(ffmpeg, "extract_audio", fake.extract_audio)
    monkeypatch.setattr(ffmpeg, "ffmpeg_version", lambda: "ffmpeg version 0.0-test")
    monkeypatch.setattr(fetch.shutil, "disk_usage", lambda p: DiskUsage(10**12, 0, 10**12))
    return fake


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "run.json").write_text("{}", encoding="utf-8")
    (folder / "config.resolved.yaml").write_text("x: 1\n", encoding="utf-8")
    return folder


@pytest.fixture
def original(tmp_path: Path) -> Path:
    path = tmp_path / "inputs" / "My Talk.MP4"
    path.parent.mkdir()
    path.write_bytes(b"pretend-video-bytes" * 100)
    return path


def make_ctx(project: Path, run_dir: Path, original: Path) -> RunContext:
    return RunContext(
        run_dir=run_dir, config=load_resolved(project, "example-client"), input_path=original
    )


def listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def snapshot(path: Path) -> tuple[bytes, int, int]:
    stat = path.stat()
    return path.read_bytes(), stat.st_size, stat.st_mtime_ns


UNTOUCHED = ["config.resolved.yaml", "run.json"]


def test_success_writes_all_outputs(
    project: Path, run_dir: Path, original: Path, tools: FakeTools
) -> None:
    before = snapshot(original)

    fetch.run(make_ctx(project, run_dir, original))

    assert listing(run_dir) == [
        "audio.wav",
        "config.resolved.yaml",
        "meta.json",
        "run.json",
        "source.mp4",
    ]
    assert (run_dir / "source.mp4").read_bytes() == before[0]
    assert (run_dir / "audio.wav").read_bytes() == b"RIFF-complete-wav"
    assert snapshot(original) == before  # original never modified
    assert tools.extract_sources == [run_dir / "source.mp4"]  # audio comes from the copy

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert list(meta) == [
        "schema_version",
        "original_filename",
        "source_file",
        "sha256",
        "size_bytes",
        "media",
        "audio_file",
        "ffmpeg_version",
        "fetched_at",
        "fetch_seconds",
    ]
    assert meta["schema_version"] == 1
    assert meta["original_filename"] == "My Talk.MP4"  # name only, no folder
    assert meta["source_file"] == "source.mp4"  # extension lower-cased
    assert meta["sha256"] == hashlib.sha256(before[0]).hexdigest()
    assert meta["size_bytes"] == before[1]
    assert meta["audio_file"] == "audio.wav"
    assert meta["ffmpeg_version"] == "ffmpeg version 0.0-test"
    assert meta["fetched_at"].endswith("+00:00")
    assert meta["fetch_seconds"] >= 0
    assert meta["media"]["video"]["codec"] == "h264"
    assert meta["media"]["audio"]["sample_rate"] == 48000
    assert meta["media"]["duration_seconds"] == 120.0
    assert str(original.parent) not in json.dumps(meta)


@pytest.mark.parametrize("name", ["Talk.MOV", "talk.mkv", "TALK.WebM"])
def test_source_keeps_lowercased_extension(
    project: Path, run_dir: Path, tmp_path: Path, tools: FakeTools, name: str
) -> None:
    path = tmp_path / name
    path.write_bytes(b"x" * 10)
    fetch.run(make_ctx(project, run_dir, path))
    assert f"source{Path(name).suffix.lower()}" in listing(run_dir)


def test_variable_frame_rate_is_noted_not_fixed(
    project: Path,
    run_dir: Path,
    original: Path,
    tools: FakeTools,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tools.source_info = video_info(variable=True)
    with caplog.at_level(logging.INFO, logger="clipmachine"):
        fetch.run(make_ctx(project, run_dir, original))
    assert "variable frame rate" in caplog.text
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["media"]["video"]["variable_frame_rate"] is True


# --- failures leave nothing behind -----------------------------------------------------------


def _fail_probe(tools: FakeTools) -> None:
    tools.probe_error = MediaError("not a readable media file")


def _fail_no_audio(tools: FakeTools) -> None:
    tools.source_info = video_info(audio=False)


def _fail_too_short(tools: FakeTools) -> None:
    tools.source_info = video_info(duration=5.0)


def _fail_extract(tools: FakeTools) -> None:
    tools.extract_error = MediaError("audio extraction failed: boom")


def _fail_verify(tools: FakeTools) -> None:
    tools.audio_info = wav_info(1.0)  # source is 120 s


@pytest.mark.parametrize(
    ("break_it", "message"),
    [
        (_fail_probe, "not a readable media file"),
        (_fail_no_audio, "no audio stream"),
        (_fail_too_short, "too short"),
        (_fail_extract, "audio extraction failed"),
        (_fail_verify, "allowed difference"),
    ],
)
def test_failure_leaves_no_outputs_or_temp_files(
    project: Path,
    run_dir: Path,
    original: Path,
    tools: FakeTools,
    break_it: Callable[[FakeTools], None],
    message: str,
) -> None:
    before = snapshot(original)
    break_it(tools)

    with pytest.raises(MediaError, match=message):
        fetch.run(make_ctx(project, run_dir, original))

    assert listing(run_dir) == UNTOUCHED
    assert snapshot(original) == before


def test_failure_while_writing_meta_removes_source_and_audio(
    project: Path,
    run_dir: Path,
    original: Path,
    tools: FakeTools,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(path: Path, data: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(fetch, "write_json_atomic", boom)
    with pytest.raises(OSError, match="disk full"):
        fetch.run(make_ctx(project, run_dir, original))
    assert listing(run_dir) == UNTOUCHED


def test_interrupt_also_cleans_up(
    project: Path, run_dir: Path, original: Path, tools: FakeTools
) -> None:
    tools.extract_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        fetch.run(make_ctx(project, run_dir, original))
    assert listing(run_dir) == UNTOUCHED


def test_input_missing_on_resume(
    project: Path, run_dir: Path, original: Path, tools: FakeTools
) -> None:
    ctx = make_ctx(project, run_dir, original)
    original.unlink()
    with pytest.raises(RunError, match="not found"):
        fetch.run(ctx)
    assert listing(run_dir) == UNTOUCHED


def test_audio_only_extension_rejected_in_stage(
    project: Path, run_dir: Path, tmp_path: Path, tools: FakeTools
) -> None:
    song = tmp_path / "song.mp3"
    song.write_bytes(b"x")
    with pytest.raises(RunError, match="audio-only files aren't supported yet"):
        fetch.run(make_ctx(project, run_dir, song))
    assert listing(run_dir) == UNTOUCHED


def test_size_limit_is_checked_again_in_stage(
    project: Path, run_dir: Path, original: Path, tools: FakeTools
) -> None:
    edit_yaml(project / "config" / "default.yaml", lambda d: d["fetch"].update(max_size_gb=1e-9))
    with pytest.raises(RunError, match="too large"):
        fetch.run(make_ctx(project, run_dir, original))
    assert listing(run_dir) == UNTOUCHED


# --- restart ----------------------------------------------------------------------------------


def test_rerun_replaces_outputs_and_removes_stale_temp_files(
    project: Path, run_dir: Path, original: Path, tools: FakeTools
) -> None:
    ctx = make_ctx(project, run_dir, original)
    fetch.run(ctx)
    first_meta = (run_dir / "meta.json").read_text(encoding="utf-8")
    (run_dir / ".source.old.tmp").write_bytes(b"leftover")
    (run_dir / ".audio.wav.old.tmp").write_bytes(b"leftover")
    (run_dir / ".meta.json.old.tmp").write_bytes(b"leftover")
    (run_dir / "source.mkv").write_bytes(b"from an older input")
    (run_dir / "notes.txt").write_text("keep me", encoding="utf-8")
    original.write_bytes(b"different content now")

    fetch.run(ctx)

    assert listing(run_dir) == [
        "audio.wav",
        "config.resolved.yaml",
        "meta.json",
        "notes.txt",
        "run.json",
        "source.mp4",
    ]
    assert (run_dir / "source.mp4").read_bytes() == b"different content now"
    assert (run_dir / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert (run_dir / "meta.json").read_text(encoding="utf-8") != first_meta


def test_failed_rerun_removes_previous_outputs(
    project: Path, run_dir: Path, original: Path, tools: FakeTools
) -> None:
    ctx = make_ctx(project, run_dir, original)
    fetch.run(ctx)
    tools.extract_error = MediaError("audio extraction failed: boom")
    with pytest.raises(MediaError):
        fetch.run(ctx)
    assert listing(run_dir) == UNTOUCHED


# --- disk space -------------------------------------------------------------------------------


def stub_free(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    monkeypatch.setattr(fetch.shutil, "disk_usage", lambda p: DiskUsage(10**13, 0, free))


def test_disk_rule_boundary(
    project: Path,
    run_dir: Path,
    original: Path,
    tools: FakeTools,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    size = original.stat().st_size
    needed = 2 * size + GIB

    stub_free(monkeypatch, needed - 1)
    with pytest.raises(RunError, match="not enough free disk space"):
        fetch.run(make_ctx(project, run_dir, original))
    assert listing(run_dir) == UNTOUCHED

    stub_free(monkeypatch, needed)
    fetch.run(make_ctx(project, run_dir, original))
    assert "meta.json" in listing(run_dir)


def test_disk_message_has_the_numbers(
    project: Path,
    run_dir: Path,
    original: Path,
    tools: FakeTools,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_free(monkeypatch, 3 * GIB // 2)
    with pytest.raises(RunError) as excinfo:
        fetch.check_disk_space(run_dir, 4 * GIB)
    message = str(excinfo.value)
    assert "need 9.00 GB" in message
    assert "4.00 GB file" in message
    assert "only 1.50 GB free" in message


def test_disk_is_measured_on_the_run_folder(
    project: Path,
    run_dir: Path,
    original: Path,
    tools: FakeTools,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Path] = []

    def usage(path: Path) -> DiskUsage:
        seen.append(Path(path))
        return DiskUsage(10**12, 0, 10**12)

    monkeypatch.setattr(fetch.shutil, "disk_usage", usage)
    fetch.run(make_ctx(project, run_dir, original))
    assert seen == [run_dir.resolve()]


# --- copy + hash -------------------------------------------------------------------------------


def test_copy_with_hash_is_one_pass_over_many_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch, "COPY_CHUNK_BYTES", 7)
    data = os.urandom(1000)
    src = tmp_path / "in.bin"
    src.write_bytes(data)
    dest = tmp_path / "out"
    dest.mkdir()
    before = snapshot(src)

    sha, copied = fetch.copy_with_hash(src, dest, "source.mp4", len(data))

    assert sha == hashlib.sha256(data).hexdigest()
    assert copied == len(data)
    assert (dest / "source.mp4").read_bytes() == data
    assert listing(dest) == ["source.mp4"]
    assert snapshot(src) == before


def test_copy_with_hash_empty_file(tmp_path: Path) -> None:
    src = tmp_path / "empty"
    src.write_bytes(b"")
    sha, copied = fetch.copy_with_hash(src, tmp_path, "copy", 0)
    assert sha == hashlib.sha256(b"").hexdigest()
    assert copied == 0


def test_copy_reads_the_original_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "in.bin"
    src.write_bytes(b"abc" * 10)
    opened: list[str] = []
    real_open = Path.open

    def spy(self: Path, *args: object, **kwargs: object) -> object:
        opened.append(f"{self.name}:{args[0] if args else kwargs.get('mode', 'r')}")
        return real_open(self, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(Path, "open", spy)
    fetch.copy_with_hash(src, tmp_path, "out.bin", 30)
    assert opened == ["in.bin:rb"]


def test_copy_detects_file_changing_mid_copy(tmp_path: Path) -> None:
    src = tmp_path / "in.bin"
    src.write_bytes(b"x" * 100)
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(RunError, match="changed while"):
        fetch.copy_with_hash(src, dest, "source.mp4", expected_size=99)
    assert listing(dest) == []
