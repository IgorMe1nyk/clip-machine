"""Fetch against real ffmpeg/ffprobe. Skipped (see conftest) when they are not on PATH.

Test media is generated with lavfi sources and encoders present in both the gyan.dev essentials
build and Ubuntu's ffmpeg (mpeg4, aac).
"""

import hashlib
import json
import socket
import subprocess
from pathlib import Path

import pytest

from clipmachine.cli import main

pytestmark = pytest.mark.ffmpeg

COMMON = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y"]
VIDEO = ["-f", "lavfi", "-i", "testsrc=duration={d}:size=160x120:rate=10"]
AUDIO = ["-f", "lavfi", "-i", "sine=frequency=440:duration={d}"]


def _run(args: list[str]) -> None:
    result = subprocess.run(
        args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr


def _fmt(parts: list[str], seconds: int) -> list[str]:
    return [p.format(d=seconds) for p in parts]


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    folder = tmp_path_factory.mktemp("media")
    files = {
        "valid": folder / "valid.mp4",
        "no_audio": folder / "no_audio.mp4",
        "short": folder / "short.mp4",
        "audio_m4a": folder / "talk.m4a",
    }
    codecs = ["-c:v", "mpeg4", "-c:a", "aac"]
    _run([*COMMON, *_fmt(VIDEO, 65), *_fmt(AUDIO, 65), *codecs, "-shortest", str(files["valid"])])
    _run([*COMMON, *_fmt(VIDEO, 65), "-c:v", "mpeg4", str(files["no_audio"])])
    _run([*COMMON, *_fmt(VIDEO, 3), *_fmt(AUDIO, 3), *codecs, "-shortest", str(files["short"])])
    _run([*COMMON, *_fmt(AUDIO, 65), "-c:a", "aac", str(files["audio_m4a"])])
    return files


def run_cli(project: Path, *argv: str) -> int:
    return main(list(argv), root=project)


def only_run_dir(project: Path) -> Path:
    (run_dir,) = (project / "runs").iterdir()
    return run_dir


def listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def fingerprint(path: Path) -> tuple[int, int, str]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def ffprobe_json(path: Path) -> dict[str, object]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format"]
        + ["-i", str(path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    data: dict[str, object] = json.loads(result.stdout)
    return data


def test_valid_video_full_fetch(
    project: Path, media: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # a folder name with spaces exercises the file: prefix on a path that needs care
    spaced = tmp_path / "my videos"
    spaced.mkdir()
    original = spaced / "Episode One.MP4"
    original.write_bytes(media["valid"].read_bytes())
    before = fingerprint(original)

    code = run_cli(project, "run", str(original), "--client", "example-client")

    assert code == 0, capsys.readouterr().err
    run_dir = only_run_dir(project)
    assert {"audio.wav", "meta.json", "source.mp4", "run.json"} <= set(listing(run_dir))
    assert not [n for n in listing(run_dir) if n.endswith(".tmp")]
    assert fingerprint(original) == before
    assert (run_dir / "source.mp4").read_bytes() == original.read_bytes()

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["original_filename"] == "Episode One.MP4"
    assert meta["source_file"] == "source.mp4"
    assert meta["sha256"] == before[2]
    assert meta["size_bytes"] == before[0]
    assert meta["media"]["video"]["width"] == 160
    assert meta["media"]["audio"]["codec"] == "aac"
    assert meta["media"]["duration_seconds"] == pytest.approx(65, abs=1)
    assert meta["ffmpeg_version"].startswith("ffmpeg version")
    assert meta["fetch_seconds"] > 0

    wav = ffprobe_json(run_dir / "audio.wav")
    (stream,) = wav["streams"]  # type: ignore[misc]
    assert stream["codec_name"] == "pcm_s16le"
    assert stream["sample_rate"] == "16000"
    assert stream["channels"] == 1
    assert float(wav["format"]["duration"]) == pytest.approx(65, abs=0.5)  # type: ignore[index]


def test_from_stage_fetch_replaces_outputs(project: Path, media: dict[str, Path]) -> None:
    original = project / "talk.mp4"
    original.write_bytes(media["valid"].read_bytes())
    assert run_cli(project, "run", str(original), "--client", "example-client") == 0
    run_dir = only_run_dir(project)
    first = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))

    assert run_cli(project, "run", str(run_dir), "--from-stage", "fetch") == 0

    second = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert second["fetched_at"] != first["fetched_at"]
    assert second["sha256"] == first["sha256"]
    assert not [n for n in listing(run_dir) if n.endswith(".tmp")]
    assert run_cli(project, "run", str(run_dir)) == 0  # nothing to do


def assert_rejected(
    project: Path, original: Path, capsys: pytest.CaptureFixture[str], message: str
) -> None:
    code = run_cli(project, "run", str(original), "--client", "example-client")
    err = capsys.readouterr().err
    assert code == 1, err
    assert message in err
    assert err.strip().count("\n") == 0  # one line
    assert listing(only_run_dir(project)) == ["config.resolved.yaml", "run.json"]


def test_video_without_audio_rejected(
    project: Path, media: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert_rejected(project, media["no_audio"], capsys, "no audio stream")


def test_audio_only_m4a_rejected_by_extension(
    project: Path, media: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_cli(project, "run", str(media["audio_m4a"]), "--client", "example-client")
    assert code == 2
    assert (
        "audio-only files aren't supported yet (video needed for clips)" in capsys.readouterr().err
    )
    assert list((project / "runs").iterdir()) == []


def test_audio_only_renamed_mp4_rejected_by_content(
    project: Path, media: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    disguised = tmp_path / "talk.mp4"
    disguised.write_bytes(media["audio_m4a"].read_bytes())
    assert_rejected(project, disguised, capsys, "no video stream")


def test_text_file_renamed_mp4_rejected(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = tmp_path / "notes.mp4"
    fake.write_text("this is just text, not a video\n" * 50, encoding="utf-8")
    assert_rejected(project, fake, capsys, "not a readable media file")


def test_too_short_rejected(
    project: Path, media: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert_rejected(project, media["short"], capsys, "too short")


def test_playlist_cannot_make_ffprobe_open_a_url(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A playlist saved as .mp4 pointing at a URL: rejected, and nothing connects to it.

    Regression guard only. On ffmpeg 9.0.1 the demuxers already refuse this file with or without
    `-protocol_whitelist file` (checked by hand), so this does not prove the flag is what stops it.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(0.5)
    port = server.getsockname()[1]
    try:
        playlist = tmp_path / "evil.mp4"
        playlist.write_text(
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n"
            f"http://127.0.0.1:{port}/segment.ts\n#EXT-X-ENDLIST\n",
            encoding="utf-8",
        )
        code = run_cli(project, "run", str(playlist), "--client", "example-client")
        assert code == 1
        assert "stage fetch failed" in capsys.readouterr().err
        with pytest.raises(TimeoutError):
            server.accept()  # nobody connected
    finally:
        server.close()
