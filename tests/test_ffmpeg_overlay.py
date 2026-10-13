"""The watermark graph, the render arguments with an overlay and the audio assertion (Brief 11)."""

from pathlib import Path

import pytest

from clipmachine.utils import ffmpeg
from clipmachine.utils.ffmpeg import MediaError, assert_source_audio_only, overlay_graph

FRAME = (1080, 1920)


def graph(blend: str, *, size: tuple[int, int] | None, x: int = 360, y: int = 298) -> str:
    return overlay_graph("CHAIN", x=x, y=y, size=size, blend=blend, frame=FRAME)


def test_blend_normal_scaled() -> None:
    assert graph("normal", size=(360, 96)) == (
        "[0:v:0]CHAIN[base];[1:v:0]scale=360:96[wm];"
        "[base][wm]overlay=x=360:y=298:eof_action=repeat[v]"
    )


def test_blend_normal_unscaled_has_no_scale_filter() -> None:
    text = graph("normal", size=None)
    assert text == "[0:v:0]CHAIN[base];[base][1:v:0]overlay=x=360:y=298:eof_action=repeat[v]"
    assert "scale" not in text


def test_blend_screen_scaled() -> None:
    assert graph("screen", size=(180, 48), x=-260, y=79) == (
        "[0:v:0]CHAIN,format=gbrp[base];color=black:s=1080x1920[blk];"
        "[1:v:0]scale=180:48[wm];"
        "[blk][wm]overlay=x=-260:y=79:eof_action=repeat,format=gbrp[mark];"
        "[base][mark]blend=all_mode=screen:shortest=1[v]"
    )


def test_blend_screen_unscaled_has_no_scale_filter() -> None:
    text = graph("screen", size=None, x=-260, y=79)
    assert text == (
        "[0:v:0]CHAIN,format=gbrp[base];color=black:s=1080x1920[blk];"
        "[blk][1:v:0]overlay=x=-260:y=79:eof_action=repeat,format=gbrp[mark];"
        "[base][mark]blend=all_mode=screen:shortest=1[v]"
    )
    assert "scale" not in text


@pytest.mark.parametrize("blend", ["normal", "screen"])
def test_the_image_stays_for_the_whole_clip(blend: str) -> None:
    text = graph(blend, size=(10, 10))
    assert "enable" not in text and "eof_action=repeat" in text


def test_a_negative_position_is_written_as_is() -> None:
    assert "overlay=x=-1:y=-2:" in graph("normal", size=None, x=-1, y=-2)


@pytest.mark.parametrize("blend", ["multiply", "", "Screen"])
def test_an_unknown_blend_is_refused(blend: str) -> None:
    with pytest.raises(MediaError, match="unknown watermark blend"):
        graph(blend, size=None)


# --- render_args ---------------------------------------------------------------------------------

COMMON = {"start": 12.5, "duration": 30.0, "crf": 20, "preset": "medium", "audio_bitrate": "160k"}


def overlay_args(tmp_path: Path, **extra: object) -> list[str]:
    return ffmpeg.render_args(
        tmp_path / "source.mp4",
        tmp_path / "out.tmp.mp4",
        filter_complex="GRAPH",
        overlay_image=tmp_path / "mark.png",
        audio_fade=0.08,
        **COMMON,  # type: ignore[arg-type]
        **extra,  # type: ignore[arg-type]
    )


def test_render_args_with_an_overlay_is_exactly_this(tmp_path: Path) -> None:
    assert overlay_args(tmp_path) == [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
        "-protocol_whitelist", "file",
        "-ss", "12.500",
        "-i", ffmpeg.input_url(tmp_path / "source.mp4"),
        "-protocol_whitelist", "file",
        "-i", ffmpeg.input_url(tmp_path / "mark.png"),
        "-t", "30.000",
        "-map", "[v]",
        "-map", "0:a:0",
        "-filter_complex", "GRAPH",
        "-af", "afade=t=in:st=0:d=0.080,afade=t=out:st=29.920:d=0.080",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", "-f", "mp4", "-y", str((tmp_path / "out.tmp.mp4").resolve()),
    ]  # fmt: skip


def test_the_overlay_image_is_the_second_input_and_there_is_no_vf(tmp_path: Path) -> None:
    args = overlay_args(tmp_path)
    inputs = [args[i + 1] for i, a in enumerate(args) if a == "-i"]
    assert inputs == [
        ffmpeg.input_url(tmp_path / "source.mp4"),
        ffmpeg.input_url(tmp_path / "mark.png"),
    ]
    assert "-vf" not in args
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert maps == ["[v]", "0:a:0"]


def test_the_vf_command_is_what_it_was(tmp_path: Path) -> None:
    args = ffmpeg.render_args(
        tmp_path / "source.mp4",
        tmp_path / "out.tmp.mp4",
        vf="VF",
        **COMMON,  # type: ignore[arg-type]
    )
    assert args[args.index("-map") + 1] == "0:v:0"
    assert args.count("-i") == 1 and "-filter_complex" not in args
    assert args[args.index("-vf") + 1] == "VF"


def test_exactly_one_of_vf_and_filter_complex(tmp_path: Path) -> None:
    out = tmp_path / "o.mp4"
    with pytest.raises(MediaError, match="exactly one"):
        ffmpeg.render_args(
            tmp_path / "s.mp4", out, vf="V", filter_complex="G", overlay_image=tmp_path / "m.png",
            **COMMON,  # type: ignore[arg-type]
        )  # fmt: skip
    with pytest.raises(MediaError, match="exactly one"):
        ffmpeg.render_args(tmp_path / "s.mp4", out, **COMMON)  # type: ignore[arg-type]


def test_filter_complex_needs_an_image_and_an_image_needs_filter_complex(tmp_path: Path) -> None:
    out = tmp_path / "o.mp4"
    with pytest.raises(MediaError, match="overlay image"):
        ffmpeg.render_args(tmp_path / "s.mp4", out, filter_complex="G", **COMMON)  # type: ignore[arg-type]
    with pytest.raises(MediaError, match="overlay image"):
        ffmpeg.render_args(
            tmp_path / "s.mp4",
            out,
            vf="V",
            overlay_image=tmp_path / "m.png",
            **COMMON,  # type: ignore[arg-type]
        )


# --- assert_source_audio_only --------------------------------------------------------------------


def good(tmp_path: Path) -> list[str]:
    return overlay_args(tmp_path)


def test_both_shapes_pass(tmp_path: Path) -> None:
    assert_source_audio_only(good(tmp_path))
    plain = ffmpeg.render_args(
        tmp_path / "source.mp4",
        tmp_path / "o.mp4",
        vf="VF",
        audio_fade=0.08,
        **COMMON,  # type: ignore[arg-type]
    )
    assert_source_audio_only(plain)


def with_extra(args: list[str], *extra: str) -> list[str]:
    return [*args[:-1], *extra, args[-1]]  # before the output name, after every option


def test_a_second_audio_map_is_refused(tmp_path: Path) -> None:
    for extra in (["-map", "0:a:0"], ["-map", "0:a:1"], ["-map", "[a]"]):
        with pytest.raises(MediaError, match="0:a:0"):
            assert_source_audio_only(with_extra(good(tmp_path), *extra))


def test_audio_from_the_image_input_is_refused(tmp_path: Path) -> None:
    args = good(tmp_path)
    args[args.index("0:a:0")] = "1:a:0"
    with pytest.raises(MediaError, match="0:a:0"):
        assert_source_audio_only(args)


def test_no_audio_map_at_all_is_refused(tmp_path: Path) -> None:
    args = good(tmp_path)
    i = args.index("0:a:0")
    del args[i - 1 : i + 1]
    with pytest.raises(MediaError, match="0:a:0"):
        assert_source_audio_only(args)


def test_a_third_input_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MediaError, match="more than two inputs"):
        assert_source_audio_only(with_extra(good(tmp_path), "-i", "file:x.png"))


def test_a_second_input_that_is_not_a_png_is_refused(tmp_path: Path) -> None:
    args = good(tmp_path)
    args[len(args) - 1 - args[::-1].index("-i") + 1] = "file:track.wav"
    with pytest.raises(MediaError, match="must be a PNG"):
        assert_source_audio_only(args)
    upper = good(tmp_path)
    upper[len(upper) - 1 - upper[::-1].index("-i") + 1] = "file:MARK.PNG"
    assert_source_audio_only(upper)  # the extension is not case sensitive


def test_an_audio_filter_other_than_a_fade_is_refused(tmp_path: Path) -> None:
    args = good(tmp_path)
    args[args.index("-af") + 1] += ",volume=2"
    with pytest.raises(MediaError, match="only be faded"):
        assert_source_audio_only(args)


@pytest.mark.parametrize(
    "token",
    ["amix=inputs=2", "amerge", "amovie=x.wav", "anullsrc", "aevalsrc=0", "sine", "[1:a:0]x"],
)
def test_a_graph_that_touches_audio_is_refused(tmp_path: Path, token: str) -> None:
    args = good(tmp_path)
    args[args.index("-filter_complex") + 1] = f"[0:v:0]CHAIN[v];{token}"
    with pytest.raises(MediaError, match="must not touch audio"):
        assert_source_audio_only(args)


def test_a_file_path_that_contains_a_filter_name_is_not_an_audio_filter(tmp_path: Path) -> None:
    args = good(tmp_path)
    args[args.index("-filter_complex") + 1] = (
        "[0:v:0]crop=1:1:0:0,subtitles=filename=campaign/business-sine/amix_tiktok.ass"
        ":fontsdir=../../assets/fonts[base];[base][1:v:0]overlay=x=1:y=2:eof_action=repeat[v]"
    )
    assert_source_audio_only(args)


def test_the_real_graphs_pass_the_audio_assertion(tmp_path: Path) -> None:
    for blend in ("normal", "screen"):
        text = overlay_graph("crop=1:1:0:0,subtitles=filename=a.ass:fontsdir=f",
                             x=1, y=2, size=(3, 4), blend=blend, frame=FRAME)  # fmt: skip
        args = ffmpeg.render_args(
            tmp_path / "s.mp4", tmp_path / "o.mp4", filter_complex=text,
            overlay_image=tmp_path / "m.png", **COMMON,  # type: ignore[arg-type]
        )  # fmt: skip
        assert_source_audio_only(args)
