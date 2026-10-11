import re
from pathlib import Path

import pytest

from clipmachine.models import AudioStreamInfo, MediaInfo, VideoStreamInfo
from clipmachine.utils import ffmpeg
from clipmachine.utils.crop import CropBox

CROP = CropBox(w=202, h=360, x=218, y=0)
# a lone letter before a colon is a drive ("C:"); "ass:fontsdir" is the option separator
DRIVE_OR_BACKSLASH = re.compile(r"(?<![A-Za-z0-9_.])[A-Za-z]:|\\")


def make_args(tmp_path: Path, vf: str = "VF") -> list[str]:
    return ffmpeg.render_args(
        tmp_path / "source.mp4",
        tmp_path / "clips" / "c01.tmp.mp4",
        start=492.07,
        duration=26.78,
        vf=vf,
        crf=20,
        preset="medium",
        audio_bitrate="160k",
    )


def flag_value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_filter_chain_is_plain_integers_and_relative_paths() -> None:
    vf = ffmpeg.video_filter(CROP, 1080, 1920, "clips/c04.ass", "../../assets/fonts")
    assert vf == (
        "crop=202:360:218:0,scale=1080:1920,setsar=1,"
        "subtitles=filename=clips/c04.ass:fontsdir=../../assets/fonts"
    )
    assert not DRIVE_OR_BACKSLASH.search(vf)


@pytest.mark.parametrize(
    "bad",
    [
        r"C:\Users\x\clips\c04.ass",
        "C:/Users/x/c04.ass",
        "/abs/path/c04.ass",
        r"clips\c04.ass",
        "clips/it's.ass",
        "clips/a,b.ass",
        "clips/a b.ass",
        "clips/a:b.ass",
        "",
    ],
)
def test_unsafe_filter_paths_are_refused(bad: str) -> None:
    with pytest.raises(ffmpeg.MediaError, match="cannot be used inside an ffmpeg filter"):
        ffmpeg.video_filter(CROP, 1080, 1920, bad, "../../assets/fonts")
    with pytest.raises(ffmpeg.MediaError, match="cannot be used inside an ffmpeg filter"):
        ffmpeg.video_filter(CROP, 1080, 1920, "clips/c04.ass", bad)


def test_command_is_an_argument_list_that_reencodes(tmp_path: Path) -> None:
    args = make_args(tmp_path)
    assert all(isinstance(a, str) for a in args)
    assert args[0] == "ffmpeg"
    # seek before the input, duration after it
    assert args.index("-ss") < args.index("-i") < args.index("-t")
    assert flag_value(args, "-ss") == "492.070"
    assert flag_value(args, "-t") == "26.780"
    assert args.index("-protocol_whitelist") < args.index("-i")
    assert flag_value(args, "-protocol_whitelist") == "file"
    assert flag_value(args, "-i").startswith("file:")
    # re-encode flags; never a stream copy
    assert flag_value(args, "-c:v") == "libx264"
    assert flag_value(args, "-preset") == "medium"
    assert flag_value(args, "-crf") == "20"
    assert flag_value(args, "-pix_fmt") == "yuv420p"
    assert flag_value(args, "-c:a") == "aac"
    assert flag_value(args, "-b:a") == "160k"
    assert flag_value(args, "-movflags") == "+faststart"
    assert "copy" not in args
    assert args[-1].endswith("c01.tmp.mp4") and Path(args[-1]).is_absolute()


def test_filter_string_in_the_command_has_no_drive_or_backslash(tmp_path: Path) -> None:
    vf = ffmpeg.video_filter(CROP, 1080, 1920, "clips/c04.ass", "../../assets/fonts")
    args = make_args(tmp_path, vf)
    assert flag_value(args, "-vf") == vf
    assert not DRIVE_OR_BACKSLASH.search(flag_value(args, "-vf"))


def good_info(**overrides: object) -> MediaInfo:
    data: dict[str, object] = {
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration_seconds": 26.8,
        "size_bytes": 1000,
        "video": VideoStreamInfo(
            codec="h264",
            width=1080,
            height=1920,
            avg_frame_rate="30/1",
            r_frame_rate="30/1",
            rotation=0,
            variable_frame_rate=False,
        ),
        "audio": AudioStreamInfo(codec="aac", sample_rate=48000, channels=2),
    }
    data.update(overrides)
    return MediaInfo.model_validate(data)


def verify(info: MediaInfo) -> None:
    ffmpeg.verify_clip(info, width=1080, height=1920, duration=26.78)


def test_valid_clip_passes() -> None:
    verify(good_info())
    verify(good_info(duration_seconds=26.78 + 0.15))  # at the tolerance


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"duration_seconds": 27.0}, "expected 26.78 s"),
        ({"duration_seconds": 26.5}, "expected 26.78 s"),
        ({"duration_seconds": None}, "no duration"),
        ({"format_name": "matroska,webm"}, "expected mp4"),
        ({"video": None}, "video is missing"),
        ({"audio": None}, "audio is missing"),
    ],
)
def test_invalid_clips_are_refused(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ffmpeg.MediaError, match=message):
        verify(good_info(**overrides))


def test_wrong_size_and_codecs_are_refused() -> None:
    video = good_info().video
    assert video is not None
    with pytest.raises(ffmpeg.MediaError, match="720x1280"):
        verify(good_info(video=video.model_copy(update={"width": 720, "height": 1280})))
    with pytest.raises(ffmpeg.MediaError, match="hevc"):
        verify(good_info(video=video.model_copy(update={"codec": "hevc"})))
    audio = AudioStreamInfo(codec="mp3", sample_rate=44100, channels=2)
    with pytest.raises(ffmpeg.MediaError, match="mp3"):
        verify(good_info(audio=audio))


# --- the crop command file ----------------------------------------------------------------


def test_crop_commands_are_one_line_per_change_after_the_first() -> None:
    text = ffmpeg.crop_commands([(0.0, 218), (1.5, 60), (4.123456789, 380)])
    assert text == "1.500000 crop x 60;\n4.123457 crop x 380;\n"


def test_crop_commands_are_empty_for_a_path_that_never_moves() -> None:
    assert ffmpeg.crop_commands([(0.0, 218)]) == ""
    assert ffmpeg.crop_commands([]) == ""


def test_filter_chain_with_a_command_file_starts_with_sendcmd() -> None:
    plain = ffmpeg.video_filter(CROP, 1080, 1920, "clips/c04.ass", "../../assets/fonts")
    vf = ffmpeg.video_filter(
        CROP, 1080, 1920, "clips/c04.ass", "../../assets/fonts", "clips/c04.crop.cmd"
    )
    assert vf == "sendcmd=f=clips/c04.crop.cmd," + plain
    assert (
        ffmpeg.video_filter(CROP, 1080, 1920, "clips/c04.ass", "../../assets/fonts", None) == plain
    )
    assert not DRIVE_OR_BACKSLASH.search(vf)


@pytest.mark.parametrize(
    "bad", [r"C:\x\c.cmd", "C:/x/c.cmd", "/abs/c.cmd", r"clips\c.cmd", "a,b.cmd", "a b.cmd", ""]
)
def test_unsafe_command_file_paths_are_refused(bad: str) -> None:
    with pytest.raises(ffmpeg.MediaError, match="cannot be used inside an ffmpeg filter"):
        ffmpeg.video_filter(CROP, 1080, 1920, "clips/c04.ass", "../../assets/fonts", bad)
