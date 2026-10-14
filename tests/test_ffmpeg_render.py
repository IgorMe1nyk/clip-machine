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


def make_args_fade(tmp_path: Path, *, duration: float, audio_fade: float) -> list[str]:
    return ffmpeg.render_args(
        tmp_path / "source.mp4",
        tmp_path / "clips" / "c01.tmp.mp4",
        start=10.0,
        duration=duration,
        vf="VF",
        crf=20,
        preset="medium",
        audio_bitrate="160k",
        audio_fade=audio_fade,
    )


def test_audio_fade_adds_a_fade_in_and_a_fade_out(tmp_path: Path) -> None:
    args = make_args_fade(tmp_path, duration=26.78, audio_fade=0.08)

    assert flag_value(args, "-af") == "afade=t=in:st=0:d=0.080,afade=t=out:st=26.700:d=0.080"
    assert args.index("-af") == args.index("-vf") + 2  # right after the -vf pair


def test_no_audio_fade_means_no_af_argument(tmp_path: Path) -> None:
    assert "-af" not in make_args(tmp_path)
    assert "-af" not in make_args_fade(tmp_path, duration=26.78, audio_fade=0.0)


@pytest.mark.parametrize("duration", [0.16, 0.1])
def test_clip_too_short_for_two_fades_has_no_af_argument(tmp_path: Path, duration: float) -> None:
    assert "-af" not in make_args_fade(tmp_path, duration=duration, audio_fade=0.08)


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


def test_crop_commands_may_address_a_named_crop_instance() -> None:
    # Brief 12.1: the fit graph has two crop filters; the lines go to the speaker's by name
    text = ffmpeg.crop_commands([(0.0, 218), (1.5, 60)], target="crop@spk")
    assert text == "1.500000 crop@spk x 60;\n"
    wide = CropBox(w=1080, h=1920, x=100, y=0)
    tight = CropBox(w=964, h=1714, x=120, y=40)
    text = ffmpeg.crop_box_commands([(0.0, wide), (1.0, tight)], target="crop@spk")
    assert text == "1.000000 crop@spk w 964, crop@spk h 1714, crop@spk x 120, crop@spk y 40;\n"


def test_fit_graph_is_plain_integers_switches_on_the_spans_and_draws_subtitles_last() -> None:
    graph = ffmpeg.fit_graph(
        CROP,
        1080,
        1920,
        "clips/c04.ass",
        "../../assets/fonts",
        "clips/c04.crop.cmd",
        frame_w=1080,
        frame_h=608,
        frame_top=449,
        spans=[(4.05, 7.98), (12.0, 15.5)],
        blur=20,
        darken=0.35,
    )
    assert graph == (
        "[0:v:0]split=3[fit_src][fit_frame][fit_fill];"
        "[fit_src]sendcmd=f=clips/c04.crop.cmd,crop@spk=202:360:218:0,scale=1080:1920,setsar=1"
        "[fit_spk];"
        "[fit_frame]scale=1080:608,setsar=1[fit_fg];"
        "[fit_fill]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=20,eq=brightness=-0.350,setsar=1[fit_bg];"
        "[fit_bg][fit_fg]overlay=x=0:y=449[fit_full];"
        "[fit_spk][fit_full]overlay=x=0:y=0:enable='between(t,4.050,7.980)+between(t,12.000,15.500)',"
        "subtitles=filename=clips/c04.ass:fontsdir=../../assets/fonts"
    )
    ffmpeg.assert_source_audio_only(["-i", "s.mp4", "-map", "0:a:0", "-filter_complex", graph])
    fixed = ffmpeg.fit_graph(
        CROP, 1080, 1920, "clips/c04.ass", "f", None,
        frame_w=1080, frame_h=608, frame_top=449, spans=[(0.0, 2.0)], blur=0, darken=0.0,
    )  # fmt: skip
    assert "sendcmd" not in fixed and "[fit_src]crop@spk=202:360:218:0," in fixed
    with pytest.raises(ffmpeg.MediaError, match="span"):
        ffmpeg.fit_graph(
            CROP, 1080, 1920, "clips/c04.ass", "f", None,
            frame_w=1080, frame_h=608, frame_top=449, spans=[], blur=0, darken=0.0,
        )  # fmt: skip
    with pytest.raises(ffmpeg.MediaError, match="subtitle file path"):
        ffmpeg.fit_graph(
            CROP, 1080, 1920, "C:/bad.ass", "f", None,
            frame_w=1080, frame_h=608, frame_top=449, spans=[(0.0, 1.0)], blur=0, darken=0.0,
        )  # fmt: skip


def test_crop_commands_are_empty_for_a_path_that_never_moves() -> None:
    assert ffmpeg.crop_commands([(0.0, 218)]) == ""
    assert ffmpeg.crop_commands([]) == ""


def test_crop_box_commands_write_size_first_when_the_window_shrinks() -> None:
    wide = CropBox(w=1080, h=1920, x=100, y=0)
    tight = CropBox(w=964, h=1714, x=120, y=40)
    text = ffmpeg.crop_box_commands([(0.0, wide), (1.0, tight)])
    assert text == "1.000000 crop w 964, crop h 1714, crop x 120, crop y 40;\n"


def test_crop_box_commands_write_position_first_when_the_window_grows() -> None:
    wide = CropBox(w=1080, h=1920, x=100, y=0)
    tight = CropBox(w=964, h=1714, x=120, y=40)
    text = ffmpeg.crop_box_commands([(0.0, tight), (2.0165, wide)])
    assert text == "2.016500 crop x 100, crop y 0, crop w 1080, crop h 1920;\n"


def test_crop_box_commands_write_only_the_fields_that_changed() -> None:
    a = CropBox(w=724, h=1286, x=844, y=76)
    b = CropBox(w=724, h=1286, x=900, y=76)
    assert ffmpeg.crop_box_commands([(0.0, a), (1.0, b)]) == "1.000000 crop x 900;\n"
    c = CropBox(w=724, h=1286, x=900, y=10)
    assert ffmpeg.crop_box_commands([(0.0, b), (1.5, c)]) == "1.500000 crop y 10;\n"


def test_crop_box_commands_for_an_x_only_path_equal_crop_commands() -> None:
    xs = [(0.0, 218), (1.5, 60), (4.123456789, 380)]
    boxes = [(t, CropBox(w=202, h=360, x=x, y=0)) for t, x in xs]
    assert ffmpeg.crop_box_commands(boxes) == ffmpeg.crop_commands(xs)


def test_crop_box_commands_are_empty_for_one_box_or_none() -> None:
    assert ffmpeg.crop_box_commands([(0.0, CROP)]) == ""
    assert ffmpeg.crop_box_commands([]) == ""


def test_crop_box_commands_make_one_line_per_change_in_a_zoom_cycle() -> None:
    base = CropBox(w=810, h=1440, x=800, y=0)
    zoomed = CropBox(w=724, h=1286, x=844, y=76)
    text = ffmpeg.crop_box_commands([(0.0, zoomed), (6.0, base), (10.0, zoomed), (16.0, base)])
    assert text.splitlines() == [
        "6.000000 crop x 800, crop y 0, crop w 810, crop h 1440;",
        "10.000000 crop w 724, crop h 1286, crop x 844, crop y 76;",
        "16.000000 crop x 800, crop y 0, crop w 810, crop h 1440;",
    ]


def test_crop_box_commands_only_ever_address_the_crop_filter() -> None:
    # every command targets `crop`: the overlays (subtitles, hook, watermark) come later in the
    # chain and are never scaled by a zoom
    base = CropBox(w=810, h=1440, x=800, y=0)
    zoomed = CropBox(w=724, h=1286, x=844, y=76)
    text = ffmpeg.crop_box_commands([(0.0, base), (1.0, zoomed), (2.0, base)])
    for line in text.splitlines():
        body = line.split(" ", 1)[1].rstrip(";")
        assert all(part.startswith("crop ") for part in body.split(", "))


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


# --- the safe-zone preview's lines (Brief R1) -----------------------------------------------------

LINES_1080 = (
    "drawbox=x=0:y=290:w=1080:h=2:color=red@0.6:t=fill,"
    "drawbox=x=0:y=1246:w=1080:h=2:color=red@0.6:t=fill,"
    "drawbox=x=120:y=0:w=2:h=1920:color=red@0.6:t=fill,"
    "drawbox=x=886:y=0:w=2:h=1920:color=red@0.6:t=fill,"
    "drawbox=x=780:y=840:w=300:h=1080:color=yellow@0.5:t=2"
)


def test_safe_zone_lines_mark_the_margins_and_tiktoks_button_column() -> None:
    assert ffmpeg.safe_zone_lines(1080, 1920, 290, 672, 120, 192) == LINES_1080


def test_safe_zone_lines_scale_the_button_column_with_the_canvas() -> None:
    lines = ffmpeg.safe_zone_lines(540, 960, 145, 336, 60, 96)
    assert lines.endswith("drawbox=x=390:y=420:w=150:h=540:color=yellow@0.5:t=2")
    assert "drawbox=x=0:y=145:w=540:h=2:" in lines


def test_safe_zone_lines_are_plain_integers_and_safe_in_a_filter() -> None:
    vf = ffmpeg.safe_zone_lines(1080, 1920, 290, 672, 120, 192)
    assert not DRIVE_OR_BACKSLASH.search(vf) and " " not in vf and "'" not in vf
    for option in re.findall(r"(?:drawbox=|:)[xywh]=([^:,]+)", vf):
        assert re.fullmatch(r"\d+", option)


def test_the_extra_filter_is_appended_after_the_subtitles_and_changes_nothing_else() -> None:
    plain = ffmpeg.video_filter(CROP, 1080, 1920, "clips/c04.ass", "../../assets/fonts")
    with_lines = ffmpeg.video_filter(
        CROP, 1080, 1920, "clips/c04.ass", "../../assets/fonts", None, LINES_1080
    )
    assert with_lines == f"{plain},{LINES_1080}"
    moving = ffmpeg.video_filter(
        CROP, 1080, 1920, "clips/c04.ass", "../../assets/fonts", "clips/c04.crop.cmd", LINES_1080
    )
    assert moving.startswith("sendcmd=f=clips/c04.crop.cmd,crop=") and moving.endswith(LINES_1080)
    assert "drawbox" not in ffmpeg.video_filter(
        CROP, 1080, 1920, "clips/c04.ass", "../../assets/fonts", "clips/c04.crop.cmd"
    )
