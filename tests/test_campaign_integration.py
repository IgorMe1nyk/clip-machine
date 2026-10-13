"""`clipmachine campaign` against real ffmpeg: a short generated source, a campaign with a
watermark, the real font. Skipped (see conftest) when ffmpeg is not on PATH."""

import subprocess
from pathlib import Path

import pytest

from campaign_helpers import moment, write_campaign, write_moments, write_run
from clipmachine import cli
from clipmachine.campaign.models import CHECKS, CampaignClip
from clipmachine.utils import ffmpeg
from helpers import edit_yaml

pytestmark = pytest.mark.ffmpeg


def make_source(path: Path) -> None:
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=duration=12:size=480x270:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ]  # fmt: skip
    result = subprocess.run(
        args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr


def test_a_campaign_run_renders_a_real_clip(project: Path) -> None:
    edit_yaml(project / "config" / "default.yaml", lambda d: d["render"].update(preset="ultrafast"))
    run_dir = write_run(project, sentences=2, width=480, height=270).resolve()
    make_source(run_dir / "source.mp4")  # in place of the stub
    folder = write_campaign(project, mutate=lambda d: d.update(clip_length={"min": 2, "max": 10}))
    write_moments(folder, [moment(start="0:05", end="0:09")])  # sentence 1: 4.65 to 9.3 s

    code = cli.main(["campaign", str(run_dir), "--campaign", "demo"], root=project)

    assert code == 0
    out = run_dir / "campaign" / "demo"
    clip = CampaignClip.model_validate_json((out / "A01_tiktok.json").read_text(encoding="utf-8"))
    assert clip.status == "rendered" and clip.duration is not None
    assert (clip.start, clip.end) == (4.65, 9.3)
    assert list(clip.checks) == list(CHECKS)
    ffmpeg.verify_clip(
        ffmpeg.probe(out / "A01_tiktok.mp4"), width=1080, height=1920, duration=clip.duration
    )
    assert (out / "A01_tiktok.post.txt").read_text(encoding="utf-8").endswith("closer one\n")
    report = (out / "campaign_report.html").read_text(encoding="utf-8")
    assert report.startswith("<!DOCTYPE html>") and 'src="A01_tiktok.mp4"' in report
    assert not list(out.glob("*.tmp*"))
