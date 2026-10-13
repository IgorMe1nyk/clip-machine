from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from campaign_helpers import (
    black_mark,
    moment,
    transparent_mark,
    write_campaign,
    write_moments,
    write_png,
)
from clipmachine.campaign.loader import (
    Campaign,
    campaign_dir,
    load_campaign,
    load_moments,
    select_moments,
)
from clipmachine.campaign.models import Moment, parse_time
from clipmachine.config import ConfigError, load_app_config
from clipmachine.models import AppConfig

Mutate = Callable[[dict[str, Any]], None]


def app_of(project: Path) -> AppConfig:
    return load_app_config(project)


def load(project: Path, mutate: Mutate | None = None, **kwargs: Any) -> Campaign:
    write_campaign(project, mutate=mutate, **kwargs)
    return load_campaign(project, "demo", app_of(project))


def position(x: float, y: float) -> Mutate:
    def mutate(d: dict[str, Any]) -> None:
        d["watermark"]["position"] = {"x_center": x, "y_center": y}

    return mutate


# --- campaign.yaml -----------------------------------------------------------------------------


def test_a_valid_campaign_loads_and_the_example_values_are_kept(project: Path) -> None:
    campaign = load(project)
    assert campaign.slug == "demo" and campaign.folder == project / "campaigns" / "demo"
    assert campaign.config.name == "Demo Campaign"
    assert (
        campaign.config.spelling[0].source == "zorble"
        and campaign.config.spelling[0].to == "Zorb1e"
    )
    assert campaign.config.spelling[1].source == "q x v"
    assert campaign.warnings == ()
    assert campaign.config.hook.max_chars == 30


def test_the_geometry_is_worked_out_from_the_visible_box(project: Path) -> None:
    # canvas 360x96 with a bar at x 130..230, y 38..58: its centre is the canvas centre (180, 48);
    # the target centre is (0.5 * 1080, 0.18 * 1920) = (540, 345.6)
    wm = load(project).watermark
    assert wm.size == (360, 96)
    assert (wm.x, wm.y) == (360, 298)  # 540 - 180, round(345.6 - 48)
    assert (wm.rect.x1, wm.rect.y1, wm.rect.x2, wm.rect.y2) == (490, 336, 590, 356)
    assert wm.background == "transparent" and wm.blend == "normal"
    assert (wm.info.width, wm.info.height, wm.info.color_type, wm.info.has_alpha) == (
        360,
        96,
        6,
        True,
    )
    assert (
        len(wm.sha256) == 64 and wm.file == (project / "campaigns/demo/assets/mark.png").resolve()
    )


def test_the_centre_is_that_of_the_visible_content_not_of_the_canvas(project: Path) -> None:
    pixels = transparent_mark(bar=(20, 38, 120, 58))  # bar centre (70, 48), canvas centre 180
    wm = load(project, png=pixels).watermark
    assert (wm.x, wm.y) == (470, 298)  # 540 - 70
    assert (wm.rect.x1, wm.rect.x2) == (490, 590)  # the same place on screen


def test_scale_half_halves_the_box(project: Path) -> None:
    def half(d: dict[str, Any]) -> None:
        d["watermark"]["scale"] = 0.5

    wm = load(project, half).watermark
    assert wm.size == (180, 48)
    # bar 65..115 x 19..29 on the scaled canvas, centre (90, 24)
    assert (wm.x, wm.y) == (450, 322)
    assert (wm.rect.x1, wm.rect.y1, wm.rect.x2, wm.rect.y2) == (515, 341, 565, 351)


@pytest.mark.parametrize("blend", ["transparent", "black"])
def test_a_canvas_larger_than_the_frame_passes_when_the_visible_box_fits(
    project: Path, blend: str
) -> None:
    bar = (540, 250, 1060, 283)
    big = transparent_mark(1600, 533, bar) if blend == "transparent" else black_mark(1600, 533, bar)

    def screen(d: dict[str, Any]) -> None:
        d["watermark"]["blend"] = "screen"

    wm = load(project, screen, png=big).watermark
    assert wm.size == (1600, 533) and wm.x < 0 and wm.x + wm.size[0] > 1080
    assert (wm.x, wm.y) == (-260, 79)  # 540 - 800; round(345.6 - 266.5)
    assert (wm.rect.x1, wm.rect.x2) == (280, 800)
    assert wm.background == ("transparent" if blend == "transparent" else "opaque_black")


def test_the_same_opaque_black_file_with_blend_normal_fails_the_frame_check(
    project: Path,
) -> None:
    big = black_mark(1600, 533, (540, 250, 1060, 283))
    with pytest.raises(ConfigError, match=r"watermark\.position.*outside the 1080x1920 frame"):
        load(project, png=big)


def test_blend_normal_on_an_opaque_black_background_warns(project: Path) -> None:
    campaign = load(project, png=black_mark(200, 60, (50, 20, 150, 40)))
    assert len(campaign.warnings) == 1
    assert "blend: screen" in campaign.warnings[0]


def test_blend_screen_on_a_transparent_mark_has_no_warning(project: Path) -> None:
    def screen(d: dict[str, Any]) -> None:
        d["watermark"]["blend"] = "screen"

    assert load(project, screen).warnings == ()


def test_a_mark_with_no_visible_pixels_is_an_error(project: Path) -> None:
    with pytest.raises(ConfigError, match="no visible pixels"):
        load(project, png=np.zeros((20, 40, 4), np.uint8))

    def screen(d: dict[str, Any]) -> None:
        d["watermark"]["blend"] = "screen"

    with pytest.raises(ConfigError, match="no visible pixels with blend screen"):
        load(project, screen, png=black_mark(40, 20, (0, 0, 0, 0)))


@pytest.mark.parametrize(
    ("x", "y", "corner"),
    [
        (0.1, 0.05, "top left"),
        (0.9, 0.05, "top right"),
        (0.1, 0.95, "bottom left"),
        (0.9, 0.95, "bottom right"),
    ],
)
def test_each_corner_zone_is_refused(project: Path, x: float, y: float, corner: str) -> None:
    pixels = transparent_mark(bar=(130, 38, 230, 58))
    with pytest.raises(ConfigError, match=f"watermark.position.*{corner} corner zone"):
        load(project, position(x, y), png=pixels)


def test_a_mark_outside_the_frame_is_refused(project: Path) -> None:
    with pytest.raises(ConfigError, match=r"watermark\.position.*outside the 1080x1920 frame"):
        load(project, position(0.0, 0.5))


@pytest.mark.parametrize(
    ("x", "y"),
    [(0.08, 0.5), (0.95, 0.5), (0.5, 0.1), (0.5, 0.8)],  # left, right, top, bottom
)
def test_each_side_of_the_safe_area_is_enforced(project: Path, x: float, y: float) -> None:
    with pytest.raises(ConfigError, match=r"watermark\.position.*outside the safe area"):
        load(project, position(x, y))


def test_the_corner_zone_comes_from_the_app_config(project: Path) -> None:
    from helpers import edit_yaml

    edit_yaml(project / "config" / "default.yaml", lambda d: d["campaign"].update(corner_zone=0.4))
    write_campaign(project, mutate=position(0.5, 0.18))
    # the top-left zone is now 432 wide and 768 tall; x 490..590 starts at 490 > 432
    assert load_campaign(project, "demo", app_of(project)).watermark.rect.x1 == 490
    edit_yaml(project / "config" / "default.yaml", lambda d: d["campaign"].update(corner_zone=0.49))
    with pytest.raises(ConfigError, match="corner zone"):
        load_campaign(project, "demo", app_of(project))


def test_duplicate_closers_and_spelling_entries_are_refused(project: Path) -> None:
    def closers(d: dict[str, Any]) -> None:
        d["post"]["closers"] = ["same line", " same line "]

    with pytest.raises(ConfigError, match="closers must be different"):
        load(project, closers)

    def spelling(d: dict[str, Any]) -> None:
        d["spelling"] = [{"from": "Zorble", "to": "A"}, {"from": " zorble ", "to": "B"}]

    with pytest.raises(ConfigError, match="same from"):
        load(project, spelling)


def test_closers_must_exist_and_not_be_empty(project: Path) -> None:
    def none(d: dict[str, Any]) -> None:
        d["post"]["closers"] = []

    with pytest.raises(ConfigError, match="at least one closer"):
        load(project, none)

    def blank(d: dict[str, Any]) -> None:
        d["post"]["closers"] = ["fine", "  "]

    with pytest.raises(ConfigError, match="must not be empty"):
        load(project, blank)


@pytest.mark.parametrize("size", [19, 201])
def test_the_hook_font_size_range(project: Path, size: int) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["hook"]["font_size"] = size

    with pytest.raises(ConfigError, match=r"hook\.font_size"):
        load(project, mutate)


def test_the_hook_font_size_falls_back_to_the_app_config_and_can_be_overridden(
    project: Path,
) -> None:
    assert load(project).hook_font_size == app_of(project).hook.font_size == 80

    def mutate(d: dict[str, Any]) -> None:
        d["hook"]["font_size"] = 120

    assert load(project, mutate).hook_font_size == 120


@pytest.mark.parametrize("seconds", [0, -1, 10.5])
def test_the_hook_seconds_range(project: Path, seconds: float) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["hook"]["seconds"] = seconds

    with pytest.raises(ConfigError, match="hook.seconds"):
        load(project, mutate)


def test_the_hook_seconds_fall_back_to_the_app_config_and_can_be_overridden(
    project: Path,
) -> None:
    assert load(project).hook_seconds == app_of(project).hook.seconds == 5.0

    def mutate(d: dict[str, Any]) -> None:
        d["hook"]["seconds"] = 3

    assert load(project, mutate).hook_seconds == 3.0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d["hook"].update(colour="red"),
        lambda d: d["watermark"].update(blend="multiply"),
        lambda d: d["captions"].update(style="legacy"),
        lambda d: d["watermark"].update(scale=0),
        lambda d: d["watermark"]["position"].update(x_center=1.5),
        lambda d: d["spelling"][0].update(source="x"),  # the YAML key is `from`
        lambda d: d["post"]["prefix"].update(myspace="@demo"),
        lambda d: d.update(name=""),
        lambda d: d["clip_length"].update(min=90),
        lambda d: d["spelling"][0].update(to="{}"),
        lambda d: d["spelling"][0].update({"from": "   "}),
    ],
)
def test_unknown_keys_and_bad_values_are_refused(project: Path, mutate: Mutate) -> None:
    with pytest.raises(ConfigError, match="campaign.yaml"):
        load(project, mutate)


def test_spelling_from_is_lowered_and_to_is_cleaned(project: Path) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["spelling"] = [{"from": "  Q   X  V ", "to": " Q{X}V "}]

    rule = load(project, mutate).config.spelling[0]
    assert (rule.source, rule.to) == ("q x v", "QXV")


@pytest.mark.parametrize(
    "name",
    ["../x.png", "assets/../../x.png", "/abs/x.png", "C:/x.png", "assets\\mark.png", "m.jpg", ""],
)
def test_bad_watermark_paths_are_refused(project: Path, name: str) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["watermark"]["file"] = name

    with pytest.raises(ConfigError, match=r"watermark\.file"):
        load(project, mutate)


def test_a_missing_watermark_file_and_an_uppercase_extension(project: Path) -> None:
    def missing(d: dict[str, Any]) -> None:
        d["watermark"]["file"] = "assets/none.png"

    with pytest.raises(ConfigError, match="not found"):
        load(project, missing)

    def upper(d: dict[str, Any]) -> None:
        d["watermark"]["file"] = "assets/logo.PNG"

    folder = write_campaign(project, mutate=upper)
    write_png(folder / "assets" / "logo.PNG", transparent_mark())
    assert load_campaign(project, "demo", app_of(project)).watermark.file.name == "logo.PNG"


def test_a_file_that_is_not_a_png_is_a_config_error(project: Path) -> None:
    folder = write_campaign(project)
    (folder / "assets" / "mark.png").write_bytes(b"plain text, not a png")
    with pytest.raises(ConfigError, match="not a PNG"):
        load_campaign(project, "demo", app_of(project))


def test_a_watermark_that_resolves_outside_the_folder_is_refused(project: Path) -> None:
    folder = write_campaign(project)
    outside = project / "campaigns" / "other"
    outside.mkdir()
    write_png(outside / "mark.png", transparent_mark())
    link = folder / "assets" / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available here")

    def mutate(d: dict[str, Any]) -> None:
        d["watermark"]["file"] = "assets/link/mark.png"

    write_campaign(project, mutate=mutate)
    with pytest.raises(ConfigError, match="inside the campaign folder"):
        load_campaign(project, "demo", app_of(project))


def test_slugs_and_missing_folders(project: Path) -> None:
    write_campaign(project, "_ex-1")
    assert campaign_dir(project, "_ex-1").name == "_ex-1"
    for bad in ["", "-x", "Demo", "a b", "a/b", "..", "x" * 42, "__x"]:
        with pytest.raises(ConfigError, match="invalid campaign name"):
            campaign_dir(project, bad)
    with pytest.raises(ConfigError, match="unknown campaign"):
        campaign_dir(project, "nothing")


def test_a_missing_or_broken_campaign_yaml(project: Path) -> None:
    folder = project / "campaigns" / "demo"
    folder.mkdir(parents=True)
    with pytest.raises(ConfigError, match="not found"):
        load_campaign(project, "demo", app_of(project))
    (folder / "campaign.yaml").write_text("- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_campaign(project, "demo", app_of(project))


# --- times -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("1:05:26", 3926.0),
        ("0:00:07", 7.0),
        ("12:05", 725.0),
        ("75:30", 4530.0),  # minutes may pass 59 in mm:ss
        ("0:05.5", 5.5),
        ("1:02:03.25", 3723.25),
        (" 2:00 ", 120.0),
        (3926, 3926.0),  # PyYAML reads an unquoted 1:05:26 as this integer
        (12.5, 12.5),
        (0, 0.0),
    ],
)
def test_parse_time_accepts(value: object, seconds: float) -> None:
    assert parse_time(value) == seconds


@pytest.mark.parametrize(
    "value",
    ["", "abc", "1:2:3:4", "1:75:00", "1:00:60", "0:60", "-1:00", "90", "1:5x", -5, -0.5,
     True, False, None, [1], float("nan"), float("inf")],
)  # fmt: skip
def test_parse_time_rejects(value: object) -> None:
    with pytest.raises(ValueError, match="bad time"):
        parse_time(value)


def test_a_long_bad_time_is_not_echoed_in_full() -> None:
    with pytest.raises(ValueError) as info:
        parse_time("x" * 200)
    assert "x" * 100 not in str(info.value)


def test_pyyaml_reads_an_unquoted_time_as_an_integer_and_it_still_works(project: Path) -> None:
    folder = write_campaign(project)
    (folder / "moments.yaml").write_text(
        "- id: A01\n  start: 1:05:26\n  end: 1:06:05\n  hook: AN INVENTED HOOK\n"
        "  platform: tiktok\n",
        encoding="utf-8",
    )
    (m,) = load_moments(load_campaign(project, "demo", app_of(project)))
    assert (m.start, m.end) == (3926.0, 3965.0)


# --- moments.yaml ------------------------------------------------------------------------------


def moments_of(project: Path, entries: list[dict[str, Any]]) -> list[Moment]:
    campaign = load(project)
    write_moments(campaign.folder, entries)
    return load_moments(campaign)


def test_moments_parse_with_all_time_forms(project: Path) -> None:
    got = moments_of(
        project,
        [
            moment(id="A01", start="1:05:26", end="1:06:05"),
            moment(id="A02", start="12:05", end="12:48.5"),
            moment(id="A03", start=100, end=150.5),
            moment(id="A04", start="0:10", end="0:50"),
        ],
    )
    assert [(m.start, m.end) for m in got] == [
        (3926.0, 3965.0),
        (725.0, 768.5),
        (100.0, 150.5),
        (10.0, 50.0),
    ]
    assert got[0].clip_name == "A01_tiktok"


@pytest.mark.parametrize("end", ["auto", "AUTO", " Auto ", None])
def test_end_auto_or_missing_means_the_tool_picks(project: Path, end: Any) -> None:
    (m,) = moments_of(project, [moment(end=end)])
    assert m.end is None


def test_a_missing_end_key_is_none(project: Path) -> None:
    entry = moment()
    del entry["end"]
    (m,) = moments_of(project, [entry])
    assert m.end is None


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"start": "abc"}, "bad time"),
        ({"start": -3}, "bad time"),
        ({"start": True}, "bad time"),
        ({"end": "nonsense"}, "bad time"),
        ({"end": "10:00"}, "must be after start"),  # equal
        ({"end": "9:59"}, "must be after start"),
        ({"id": "has space"}, "id must be"),
        ({"id": ""}, "id must be"),
        ({"id": "x" * 25}, "id must be"),
        ({"id": "a/b"}, "id must be"),
        ({"platform": "myspace"}, "platform"),
        ({"hook": " {} "}, "hook must not be empty"),
        ({"hook": ""}, "hook must not be empty"),
        ({"hook": "H" * 31}, "hook.max_chars \\(30\\)"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_bad_moments_name_the_entry(project: Path, fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=rf"moments\.yaml entry 2 \(id .*\).*{message}"):
        moments_of(project, [moment(id="B01", platform="youtube"), moment(**fields)])


def test_a_hook_of_exactly_the_limit_is_fine(project: Path) -> None:
    (m,) = moments_of(project, [moment(hook="H" * 30)])
    assert len(m.hook) == 30


def test_the_hook_limit_is_counted_after_cleaning(project: Path) -> None:
    (m,) = moments_of(project, [moment(hook="{" + "H" * 30 + "}")])
    assert m.hook == "H" * 30


def test_the_hook_error_gives_both_numbers_and_not_the_text(project: Path) -> None:
    with pytest.raises(ConfigError) as info:
        moments_of(project, [moment(hook="Q" * 31)])
    text = str(info.value)
    assert "31" in text and "30" in text and "QQQQ" not in text


def test_duplicate_id_and_platform_is_refused_but_two_platforms_are_fine(project: Path) -> None:
    with pytest.raises(ConfigError, match="entry 2 .*already used"):
        moments_of(project, [moment(), moment(start="20:00", end="20:40")])
    got = moments_of(project, [moment(platform="tiktok"), moment(platform="youtube")])
    assert [m.clip_name for m in got] == ["A01_tiktok", "A01_youtube"]


def test_an_empty_or_wrong_shaped_moments_file(project: Path) -> None:
    campaign = load(project)
    with pytest.raises(ConfigError, match="not found"):
        load_moments(campaign)
    (campaign.folder / "moments.yaml").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="list"):
        load_moments(campaign)
    (campaign.folder / "moments.yaml").write_text("[]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="empty"):
        load_moments(campaign)
    (campaign.folder / "moments.yaml").write_text("a: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="list"):
        load_moments(campaign)
    (campaign.folder / "moments.yaml").write_text("- just text\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="entry 1: expected a mapping"):
        load_moments(campaign)
    (campaign.folder / "moments.yaml").write_text("- [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_moments(campaign)


def test_select_moments_keeps_file_order_and_rejects_unknown_ids(project: Path) -> None:
    got = moments_of(
        project,
        [
            moment(id="A01"),
            moment(id="B05"),
            moment(id="A03"),
            moment(id="A01", platform="youtube"),
        ],
    )
    assert select_moments(got, None) == got
    picked = select_moments(got, "A03, A01")
    assert [m.clip_name for m in picked] == ["A01_tiktok", "A03_tiktok", "A01_youtube"]
    with pytest.raises(ConfigError, match="no moment with id Z9"):
        select_moments(got, "A01,Z9")
    with pytest.raises(ConfigError, match="at least one id"):
        select_moments(got, " , ")
