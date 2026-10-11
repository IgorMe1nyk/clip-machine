"""The frame pipe and the preview encoder. Most tests use a stand-in child process (this Python)
so they need no ffmpeg; the one marked `ffmpeg` runs the real tool on a generated source."""

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from clipmachine.utils import ffmpeg
from clipmachine.utils.ffmpeg import (
    MediaError,
    frame_args,
    frame_size_after_scale,
    preview_args,
    read_frames,
    write_preview,
)

SOURCE = Path("source.mp4")


# --- argument lists -----------------------------------------------------------------------


def test_frame_args_decode_only_the_clip_range() -> None:
    args = frame_args(SOURCE, start=12.5, duration=30.0, fps=10, width=640)
    assert args[0] == "ffmpeg"
    assert "-nostdin" in args and "-an" in args
    assert args[args.index("-protocol_whitelist") + 1] == "file"
    assert args.index("-ss") < args.index("-i") < args.index("-t")  # seek before the input
    assert args[args.index("-ss") + 1] == "12.500"
    assert args[args.index("-t") + 1] == "30.000"
    assert args[args.index("-i") + 1] == ffmpeg.input_url(SOURCE)
    assert args[args.index("-vf") + 1] == "fps=10,scale=640:-2"
    assert args[args.index("-pix_fmt") + 1] == "rgb24"
    assert args[args.index("-f") + 1] == "rawvideo"
    assert args[-1] == "-"
    assert all(isinstance(a, str) for a in args)


def test_frame_args_keep_a_fractional_rate() -> None:
    args = frame_args(SOURCE, start=0, duration=1, fps=7.5, width=320)
    assert args[args.index("-vf") + 1] == "fps=7.5,scale=320:-2"


def test_preview_args_read_frames_from_stdin_and_audio_from_the_source(tmp_path: Path) -> None:
    out = tmp_path / "x.tmp.mp4"
    args = preview_args(SOURCE, out, start=12.5, duration=30.0, fps=10, width=480, height=270)
    assert "-nostdin" not in args  # stdin carries the frames
    first_input = args.index("-i")
    assert args[first_input + 1] == "pipe:0"
    assert args[args.index("-s") + 1] == "480x270"
    assert args[args.index("-r") + 1] == "10"
    # the seek, length and whitelist belong to the source input, which comes second
    second_input = args.index("-i", first_input + 1)
    assert args[second_input + 1] == ffmpeg.input_url(SOURCE)
    for flag in ("-protocol_whitelist", "-ss", "-t", "-vn"):
        assert first_input < args.index(flag) < second_input
    assert args[args.index("-ss") + 1] == "12.500"
    assert args[args.index("-t") + 1] == "30.000"
    assert args[args.index("-map") + 1] == "0:v:0"
    assert "1:a:0?" in args
    assert "-shortest" in args
    assert args[args.index("-c:v") + 1] == "libx264"
    assert args[args.index("-c:a") + 1] == "aac"
    assert args[-1] == str(out.resolve())


@pytest.mark.parametrize(
    ("size", "rotation", "expected"),
    [
        ((1920, 1080), 0, (640, 360)),
        ((1280, 720), 0, (640, 360)),
        ((1080, 1920), 0, (640, 1138)),  # portrait: 1137.8 -> nearest even
        ((1080, 1920), 90, (640, 360)),  # stored portrait, plays landscape
        ((1920, 1080), 270, (640, 1138)),
        ((720, 480), 0, (640, 426)),  # 426.67 -> 426 (nearest even)
        ((1000, 999), 0, (640, 640)),  # 639.4 -> 640
    ],
)
def test_frame_size_after_scale(
    size: tuple[int, int], rotation: int, expected: tuple[int, int]
) -> None:
    assert frame_size_after_scale(size[0], size[1], rotation, 640) == expected


def test_frame_size_height_is_always_even() -> None:
    for width in range(300, 2000, 37):
        for height in range(200, 1100, 41):
            assert frame_size_after_scale(width, height, 0, 640)[1] % 2 == 0


# --- read_frames with a stand-in process --------------------------------------------------


def child(code: str, *argv: str) -> list[str]:
    return [sys.executable, "-c", code, *argv]


WRITE_FRAMES = (
    "import sys\n"
    "for i in range(3):\n"
    "    sys.stdout.buffer.write(bytes([i + 1]) * (4 * 2 * 3))\n"
    "sys.stdout.buffer.flush()\n"
)


def test_read_frames_yields_whole_frames(tmp_path: Path) -> None:
    frames = list(read_frames(child(WRITE_FRAMES), 4, 2, 30, scratch_dir=tmp_path))
    assert len(frames) == 3
    for i, frame in enumerate(frames):
        assert frame.shape == (2, 4, 3)
        assert frame.dtype == np.uint8
        assert frame.flags.writeable
        assert (frame == i + 1).all()
    assert list(tmp_path.iterdir()) == []  # the stderr temp file is gone


def test_read_frames_without_a_scratch_dir() -> None:
    assert len(list(read_frames(child(WRITE_FRAMES), 4, 2, 30))) == 3


def test_a_partial_last_frame_is_dropped() -> None:
    code = "import sys; sys.stdout.buffer.write(b'\\x01' * (4 * 2 * 3 * 2 + 5))"
    assert len(list(read_frames(child(code), 4, 2, 30))) == 2


def test_a_failing_process_raises_with_its_last_stderr_line(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "sys.stderr.write('first\\nsecond problem\\n')\n"
        "sys.stdout.buffer.write(b'\\x01' * 24)\n"
        "sys.exit(3)\n"
    )
    with pytest.raises(MediaError, match="frame extraction failed: second problem"):
        list(read_frames(child(code), 4, 2, 30, scratch_dir=tmp_path))


def test_a_missing_program_is_a_media_error() -> None:
    with pytest.raises(MediaError, match="could not run"):
        list(read_frames(["clipmachine-no-such-program"], 4, 2, 30))


def test_an_unfinished_stream_is_killed_when_the_caller_stops() -> None:
    code = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'\\x01' * 24)\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(60)\n"
    )
    frames = read_frames(child(code), 4, 2, 30)
    assert next(frames).shape == (2, 4, 3)
    frames.close()  # runs the finally: kills the sleeping child (the test would hang otherwise)


# --- write_preview with a stand-in process ------------------------------------------------


def test_write_preview_pipes_frames_and_renames_the_result(tmp_path: Path) -> None:
    temp, out = tmp_path / "p.tmp.mp4", tmp_path / "p.mp4"
    code = "import sys; open(sys.argv[1], 'wb').write(sys.stdin.buffer.read())"
    frames = [np.full((2, 4, 3), i, dtype=np.uint8) for i in (7, 9)]

    write_preview(frames, child(code, str(temp)), temp, out, 30, scratch_dir=tmp_path)

    assert out.read_bytes() == bytes([7]) * 24 + bytes([9]) * 24
    assert not temp.exists()


def test_write_preview_failure_leaves_no_output(tmp_path: Path) -> None:
    temp, out = tmp_path / "p.tmp.mp4", tmp_path / "p.mp4"
    code = (
        "import sys\n"
        "open(sys.argv[1], 'wb').write(b'partial')\n"
        "sys.stdin.buffer.read()\n"
        "sys.stderr.write('encoder exploded\\n')\n"
        "sys.exit(1)\n"
    )
    frames = [np.zeros((2, 4, 3), dtype=np.uint8)]

    with pytest.raises(MediaError, match="preview encode failed: encoder exploded"):
        write_preview(frames, child(code, str(temp)), temp, out, 30, scratch_dir=tmp_path)

    assert not out.exists() and not temp.exists()


# --- real ffmpeg --------------------------------------------------------------------------


@pytest.mark.ffmpeg
def test_real_frame_pipe_and_preview(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    made = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            "-y", str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    assert made.returncode == 0, made.stderr
    info = ffmpeg.probe(source)
    assert info.video is not None
    width, height = frame_size_after_scale(info.video.width, info.video.height, 0, 160)
    assert (width, height) == (160, 90)

    args = frame_args(source, start=0.5, duration=2.0, fps=5, width=160)
    frames = list(read_frames(args, width, height, 60, scratch_dir=tmp_path))
    assert len(frames) == pytest.approx(2.0 * 5, abs=1)
    assert frames[0].shape == (90, 160, 3) and frames[0].dtype == np.uint8
    assert int(frames[0].max()) > 0  # a picture, not black

    out = tmp_path / "preview.mp4"
    temp = tmp_path / "preview.tmp.mp4"
    write_preview(
        frames,
        preview_args(source, temp, start=0.5, duration=2.0, fps=5, width=width, height=height),
        temp,
        out,
        60,
        scratch_dir=tmp_path,
    )
    preview = ffmpeg.probe(out)
    assert preview.video is not None and preview.video.codec == "h264"
    assert preview.audio is not None and preview.audio.codec == "aac"
    assert preview.duration_seconds == pytest.approx(2.0, abs=0.4)
