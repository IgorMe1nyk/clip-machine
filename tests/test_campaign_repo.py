"""The repo's campaign files (Brief 11): the .gitignore lines and the synthetic example."""

from pathlib import Path

import yaml

from clipmachine.campaign.loader import load_campaign, load_moments
from clipmachine.config import load_app_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "campaigns" / "_example"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def test_gitignore_keeps_only_the_example_campaign() -> None:
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    wanted = ["campaigns/*", "!campaigns/_example/", "campaigns/_example/used.json"]
    assert all(line in lines for line in wanted)
    first, negation, used = (lines.index(line) for line in wanted)
    assert first < negation < used  # the negation must come after the pattern it undoes


def test_the_example_campaign_has_its_files() -> None:
    for name in ("README.md", "campaign.yaml", "moments.yaml", "assets/watermark.png"):
        assert (EXAMPLE / name).is_file(), name


def test_the_example_yaml_files_parse_to_a_mapping_and_a_list() -> None:
    campaign = yaml.safe_load((EXAMPLE / "campaign.yaml").read_text(encoding="utf-8"))
    moments = yaml.safe_load((EXAMPLE / "moments.yaml").read_text(encoding="utf-8"))
    assert isinstance(campaign, dict)
    assert isinstance(moments, list) and moments


def test_the_example_watermark_is_a_png_of_the_expected_size() -> None:
    data = (EXAMPLE / "assets" / "watermark.png").read_bytes()
    assert data.startswith(PNG_SIGNATURE)
    assert data[12:16] == b"IHDR"
    assert (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")) == (360, 96)


def test_the_shipped_example_loads_with_the_real_config() -> None:
    app = load_app_config(REPO_ROOT)
    campaign = load_campaign(REPO_ROOT, "_example", app)
    moments = load_moments(campaign)
    assert campaign.warnings == ()
    assert campaign.hook_font_size == app.hook.font_size
    assert campaign.watermark.blend == "normal" and campaign.watermark.background == "transparent"
    assert len(moments) == 3
    assert moments[1].end is None  # `end: auto`
    assert all(m.end is not None for m in (moments[0], moments[2]))
