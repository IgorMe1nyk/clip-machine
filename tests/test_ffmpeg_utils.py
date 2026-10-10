import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from clipmachine.models import FetchConfig, MediaInfo
from clipmachine.utils import ffmpeg
from clipmachine.utils.ffmpeg import (
    MediaError,
    check_source,
    parse_probe,
    probe,
    probe_args,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ffprobe"
LIMITS = FetchConfig()  # 60 s .. 240 min


def fixture_json(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def info(name: str) -> MediaInfo:
    return parse_probe(fixture_json(name), fallback_size=0)


class FakeRun:
    """Stands in for subprocess.run and records how it was called."""

    def __init__(
        self,
        stdout: str = "",
        returncode: int = 0,
        stderr: str = "",
        raises: BaseException | None = None,
    ) -> None:
        self.result = subprocess.CompletedProcess([], returncode, stdout, stderr)
        self.raises = raises
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result


def install(monkeypatch: pytest.MonkeyPatch, fake: FakeRun) -> FakeRun:
    monkeypatch.setattr(ffmpeg.subprocess, "run", fake)
    return fake


# --- parsing real ffprobe output ---------------------------------------------------------


def test_parse_mp4() -> None:
    media = info("mp4")
    assert media.format_name == "mov,mp4,m4a,3gp,3g2,mj2"
    assert media.duration_seconds == 2.0
    assert media.size_bytes == 46362
    assert media.video is not None
    assert (media.video.codec, media.video.width, media.video.height) == ("mpeg4", 160, 120)
    assert (media.video.avg_frame_rate, media.video.r_frame_rate) == ("10/1", "10/1")
    assert media.video.rotation == 0
    assert media.video.variable_frame_rate is False
    assert media.audio is not None
    assert (media.audio.codec, media.audio.sample_rate, media.audio.channels) == ("aac", 44100, 1)


@pytest.mark.parametrize(
    ("name", "video_codec", "audio_codec", "duration"),
    [("mkv", "mpeg4", "aac", 2.023), ("webm", "vp8", "vorbis", 2.003)],
)
def test_parse_matroska_family(
    name: str, video_codec: str, audio_codec: str, duration: float
) -> None:
    media = info(name)
    assert media.format_name == "matroska,webm"
    assert media.duration_seconds == duration
    assert media.video is not None and media.video.codec == video_codec
    assert media.audio is not None and media.audio.codec == audio_codec


def test_parse_rotation_is_clockwise_degrees() -> None:
    # ffprobe's Display Matrix says 90 (counter-clockwise); clockwise that is 270.
    video = info("rotated").video
    assert video is not None
    assert video.rotation == 270


def test_parse_variable_frame_rate() -> None:
    video = info("vfr").video
    assert video is not None
    assert (video.avg_frame_rate, video.r_frame_rate) == ("8/1", "10/1")
    assert video.variable_frame_rate is True


def test_parse_no_audio_stream() -> None:
    media = info("no_audio")
    assert media.video is not None
    assert media.audio is None


def test_parse_audio_only_has_no_video() -> None:
    media = info("audio_only")
    assert media.video is None
    assert media.audio is not None


def test_cover_art_is_not_a_video_stream() -> None:
    media = info("audio_cover")
    assert media.video is None
    assert media.audio is not None


def test_parse_wav() -> None:
    media = info("wav")
    assert media.format_name == "wav"
    assert media.video is None
    assert media.audio is not None
    assert (media.audio.codec, media.audio.sample_rate, media.audio.channels) == (
        "pcm_s16le",
        16000,
        1,
    )


@pytest.mark.parametrize(
    ("side_data", "tags", "expected"),
    [
        ([{"side_data_type": "Display Matrix", "rotation": -90}], None, 90),
        ([{"side_data_type": "Display Matrix", "rotation": 90.0}], None, 270),
        ([{"side_data_type": "Display Matrix", "rotation": 180}], None, 180),
        (None, {"rotate": "90"}, 90),
        (None, {"rotate": "270"}, 270),
        (None, {"rotate": "nonsense"}, 0),
        ([{"side_data_type": "Other"}], None, 0),
        (None, None, 0),
    ],
)
def test_rotation_sources(
    side_data: list[dict[str, Any]] | None, tags: dict[str, str] | None, expected: int
) -> None:
    data = fixture_json("mp4")
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    video.pop("tags", None)
    if side_data is not None:
        video["side_data_list"] = side_data
    if tags is not None:
        video["tags"] = tags
    parsed = parse_probe(data, fallback_size=0).video
    assert parsed is not None
    assert parsed.rotation == expected


def test_equal_rates_written_differently_are_not_variable() -> None:
    data = fixture_json("mp4")
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    video["avg_frame_rate"] = "30000/1000"
    video["r_frame_rate"] = "30/1"
    parsed = parse_probe(data, fallback_size=0).video
    assert parsed is not None
    assert parsed.variable_frame_rate is False


def test_unknown_rate_is_not_variable() -> None:
    data = fixture_json("mp4")
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    video["avg_frame_rate"] = "0/0"
    parsed = parse_probe(data, fallback_size=0).video
    assert parsed is not None
    assert parsed.variable_frame_rate is False


def test_missing_duration_and_size() -> None:
    data = fixture_json("mp4")
    del data["format"]["duration"]
    data["format"]["size"] = "N/A"
    media = parse_probe(data, fallback_size=123)
    assert media.duration_seconds is None
    assert media.size_bytes == 123


@pytest.mark.parametrize("bad", ["N/A", "nan", "inf", "-5", "abc", ""])
def test_unusable_duration_is_none(bad: str) -> None:
    data = fixture_json("mp4")
    data["format"]["duration"] = bad
    assert parse_probe(data, fallback_size=0).duration_seconds is None


@pytest.mark.parametrize("data", [[], "text", 5, None, {}, {"format": "x"}, {"streams": []}])
def test_non_ffprobe_json_is_not_readable(data: object) -> None:
    with pytest.raises(MediaError, match="^not a readable media file$"):
        parse_probe(data, fallback_size=0)


def test_garbage_streams_do_not_crash() -> None:
    data = {"format": {"format_name": "mp4"}, "streams": ["x", 1, None, {"codec_type": "video"}]}
    media = parse_probe(data, fallback_size=0)
    assert media.video is None and media.audio is None


# --- rejection rules ----------------------------------------------------------------------


def test_valid_media_passes_with_short_minimum() -> None:
    assert check_source(info("mp4"), FetchConfig(min_duration_seconds=1)) == 2.0


@pytest.mark.parametrize("name", ["mp4", "mkv", "webm"])
def test_allowed_containers(name: str) -> None:
    check_source(info(name), FetchConfig(min_duration_seconds=1))


def test_container_not_in_allow_list() -> None:
    media = info("mp4").model_copy(update={"format_name": "avi"})
    with pytest.raises(MediaError, match="unsupported container 'avi'"):
        check_source(media, FetchConfig(min_duration_seconds=1))


def test_container_with_no_name_is_rejected() -> None:
    media = info("mp4").model_copy(update={"format_name": ""})
    with pytest.raises(MediaError, match="unsupported container"):
        check_source(media, FetchConfig(min_duration_seconds=1))


def test_container_match_is_per_token_not_substring() -> None:
    media = info("mp4").model_copy(update={"format_name": "mp4a,mov2"})
    with pytest.raises(MediaError, match="unsupported container"):
        check_source(media, FetchConfig(min_duration_seconds=1))


def test_reject_no_video() -> None:
    with pytest.raises(MediaError, match="^no video stream"):
        check_source(info("audio_only"), FetchConfig(min_duration_seconds=1))


def test_reject_no_audio() -> None:
    with pytest.raises(MediaError, match="^no audio stream"):
        check_source(info("no_audio"), FetchConfig(min_duration_seconds=1))


def test_reject_missing_duration() -> None:
    media = info("mp4").model_copy(update={"duration_seconds": None})
    with pytest.raises(MediaError, match="duration is missing"):
        check_source(media, LIMITS)


def test_duration_boundaries() -> None:
    limits = FetchConfig(min_duration_seconds=60, max_duration_minutes=240)
    base = info("mp4")
    check_source(base.model_copy(update={"duration_seconds": 60.0}), limits)
    check_source(base.model_copy(update={"duration_seconds": 240 * 60.0}), limits)
    with pytest.raises(MediaError, match="too short: 59.9 s"):
        check_source(base.model_copy(update={"duration_seconds": 59.9}), limits)
    with pytest.raises(MediaError, match="too long: 240.0 min"):
        check_source(base.model_copy(update={"duration_seconds": 240 * 60.0 + 1}), limits)


# --- the exact commands --------------------------------------------------------------------


def test_probe_argument_list_is_exact(tmp_path: Path) -> None:
    target = tmp_path / "my video.mp4"
    assert probe_args(target) == [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-protocol_whitelist",
        "file",
        "-i",
        f"file:{target.resolve()}",
    ]


def test_input_url_is_absolute_even_for_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    url = ffmpeg.input_url(Path("clip.mp4"))
    assert url == f"file:{(tmp_path / 'clip.mp4').resolve()}"
    assert Path(url.removeprefix("file:")).is_absolute()


def test_probe_runs_safely_and_parses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x" * 10)
    fake = install(monkeypatch, FakeRun(stdout=json.dumps(fixture_json("mp4"))))

    result = probe(media)

    assert result.format_name.startswith("mov,mp4")
    ((args, kwargs),) = fake.calls
    assert args == probe_args(media)
    assert isinstance(args, list)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 60
    assert kwargs["capture_output"] is True
    assert kwargs.get("shell") in (None, False)


def test_probe_size_falls_back_to_file_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x" * 10)
    data = fixture_json("mp4")
    del data["format"]["size"]
    install(monkeypatch, FakeRun(stdout=json.dumps(data)))
    assert probe(media).size_bytes == 10


@pytest.mark.parametrize(
    "fake",
    [
        FakeRun(returncode=1, stderr="Invalid data found when processing input"),
        FakeRun(stdout="not json at all"),
        FakeRun(stdout=""),
        FakeRun(stdout="[1, 2, 3]"),
    ],
)
def test_probe_failures_are_one_clear_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake: FakeRun
) -> None:
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    install(monkeypatch, fake)
    with pytest.raises(MediaError, match="^not a readable media file$"):
        probe(media)


def test_probe_never_echoes_ffprobe_stderr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    install(monkeypatch, FakeRun(returncode=1, stderr="SECRET-LOOKING-DETAIL\nsecond line"))
    with pytest.raises(MediaError) as excinfo:
        probe(media)
    assert "SECRET" not in str(excinfo.value)


def test_probe_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    install(monkeypatch, FakeRun(raises=subprocess.TimeoutExpired("ffprobe", 60)))
    with pytest.raises(MediaError, match="ffprobe timed out after 60 s"):
        probe(media)


def test_probe_tool_cannot_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    install(monkeypatch, FakeRun(raises=FileNotFoundError(2, "No such file or directory")))
    with pytest.raises(MediaError, match="could not run ffprobe"):
        probe(media)


def test_probe_oversized_output_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    install(monkeypatch, FakeRun(stdout=" " * (ffmpeg.MAX_PROBE_OUTPUT_CHARS + 1)))
    with pytest.raises(MediaError, match="not a readable media file"):
        probe(media)


# --- audio extraction ---------------------------------------------------------------------


def test_extract_argument_list_is_exact(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    temp = tmp_path / ".audio.wav.abc.tmp"
    assert ffmpeg.extract_args(source, temp) == [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-i",
        f"file:{source.resolve()}",
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "-y",
        str(temp.resolve()),
    ]


@pytest.mark.parametrize(("duration", "expected"), [(0.0, 120.0), (60.0, 150.0), (3600.0, 1920.0)])
def test_extract_timeout(duration: float, expected: float) -> None:
    assert ffmpeg.extract_timeout(duration) == expected


def test_extract_runs_safely(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = install(monkeypatch, FakeRun())
    ffmpeg.extract_audio(tmp_path / "source.mp4", tmp_path / "t.tmp", 600.0)
    ((args, kwargs),) = fake.calls
    assert args == ffmpeg.extract_args(tmp_path / "source.mp4", tmp_path / "t.tmp")
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 420.0
    assert kwargs["capture_output"] is True
    assert kwargs.get("shell") in (None, False)


def test_extract_failure_reports_last_stderr_line_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    noisy = "first\nsecond\n" + "x" * 500 + "\n\n"
    install(monkeypatch, FakeRun(returncode=1, stderr=noisy))
    with pytest.raises(MediaError) as excinfo:
        ffmpeg.extract_audio(tmp_path / "s.mp4", tmp_path / "t.tmp", 10.0)
    message = str(excinfo.value)
    assert message.startswith("audio extraction failed: xxx")
    assert "\n" not in message
    assert len(message) <= len("audio extraction failed: ") + 200


def test_extract_timeout_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install(monkeypatch, FakeRun(raises=subprocess.TimeoutExpired("ffmpeg", 125)))
    with pytest.raises(MediaError, match="ffmpeg timed out after 125 s"):
        ffmpeg.extract_audio(tmp_path / "s.mp4", tmp_path / "t.tmp", 10.0)


def test_ffmpeg_version_is_first_line(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakeRun(stdout="ffmpeg version 9.0.1 Copyright\nbuilt with gcc\n"))
    assert ffmpeg.ffmpeg_version() == "ffmpeg version 9.0.1 Copyright"
    ((args, kwargs),) = fake.calls
    assert args == ["ffmpeg", "-version"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 10


def test_ffmpeg_version_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeRun(returncode=1))
    with pytest.raises(MediaError, match="ffmpeg version"):
        ffmpeg.ffmpeg_version()


def test_real_wav_fixture_verifies() -> None:
    ffmpeg.verify_audio(info("wav"), source_duration_seconds=2.0)


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("mp4", "not a plain audio file"),  # has video
        ("audio_only", "aac 44100 Hz 1 ch"),  # wrong codec and rate
    ],
)
def test_verify_audio_rejects_wrong_shape(name: str, message: str) -> None:
    with pytest.raises(MediaError, match=message):
        ffmpeg.verify_audio(info(name), source_duration_seconds=2.0)


def test_verify_audio_rejects_wrong_channels_and_rate() -> None:
    stereo = info("wav")
    assert stereo.audio is not None
    stereo = stereo.model_copy(update={"audio": stereo.audio.model_copy(update={"channels": 2})})
    with pytest.raises(MediaError, match="pcm_s16le 16000 Hz 2 ch"):
        ffmpeg.verify_audio(stereo, source_duration_seconds=2.0)
    other_rate = stereo.model_copy(
        update={"audio": stereo.audio.model_copy(update={"channels": 1, "sample_rate": 44100})}
    )
    with pytest.raises(MediaError, match="44100 Hz"):
        ffmpeg.verify_audio(other_rate, source_duration_seconds=2.0)


def test_tolerance_is_max_of_half_second_and_0_2_percent() -> None:
    assert ffmpeg.duration_tolerance(10) == 0.5
    assert ffmpeg.duration_tolerance(250) == 0.5  # 0.2 % is exactly 0.5 here
    assert ffmpeg.duration_tolerance(1000) == pytest.approx(2.0)
    assert ffmpeg.duration_tolerance(7200) == pytest.approx(14.4)


@pytest.mark.parametrize(
    ("source", "audio", "ok"),
    [
        # short source: flat 0.5 s
        (60.0, 60.0, True),
        (60.0, 60.5, True),
        (60.0, 59.5, True),
        (60.0, 60.6, False),
        (60.0, 59.4, False),
        # long source: 0.2 % wins (3600 s -> 7.2 s)
        (3600.0, 3607.0, True),
        (3600.0, 3592.8, True),
        (3600.0, 3607.5, False),
        (3600.0, 3592.0, False),
    ],
)
def test_audio_duration_tolerance_boundaries(source: float, audio: float, ok: bool) -> None:
    media = info("wav").model_copy(update={"duration_seconds": audio})
    if ok:
        ffmpeg.verify_audio(media, source_duration_seconds=source)
    else:
        with pytest.raises(MediaError, match="allowed difference"):
            ffmpeg.verify_audio(media, source_duration_seconds=source)


def test_verify_audio_requires_a_duration() -> None:
    media = info("wav").model_copy(update={"duration_seconds": None})
    with pytest.raises(MediaError, match="no duration"):
        ffmpeg.verify_audio(media, source_duration_seconds=2.0)
