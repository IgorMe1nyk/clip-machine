"""campaign_report.html (Brief 11): the page itself from hand-built records, then through the
command. Invented words only."""

from pathlib import Path
from typing import Any

import pytest

from campaign_helpers import black_mark, moment, write_campaign
from clipmachine.campaign.loader import Campaign, load_campaign
from clipmachine.campaign.models import (
    CHECKS,
    CampaignClip,
    CampaignClipWatermark,
    Moment,
)
from clipmachine.campaign.report import CHECK_LABELS, REPORT_HTML, build_html, write_report
from clipmachine.config import load_app_config
from clipmachine.utils import ffmpeg
from test_campaign_run import HOOK, SECRET_TEXTS, Rig, make_rig, out_of
from test_render import FakeFfmpeg, invented_lists

LABELS = [
    "length within range",
    "hook on screen from 0.0 s",
    "captions on",
    "watermark present for the full duration and not overlapped",
    "source audio only",
    "closer present",
    "moment not previously used",
    "names to check listed",
]


def make_campaign(
    project: Path, *, png: Any = None, source_note: str = "invented source"
) -> Campaign:
    def note(d: dict[str, Any]) -> None:
        d["source_note"] = source_note

    write_campaign(project, mutate=note, png=png)
    return load_campaign(project, "demo", load_app_config(project))


def mom(**fields: Any) -> Moment:
    return Moment.model_validate(moment(**fields))


def clip(**fields: Any) -> CampaignClip:
    data: dict[str, Any] = {
        "id": "A01",
        "platform": "tiktok",
        "status": "rendered",
        "requested_start": 24.0,
        "requested_end": 58.0,
        "start": 24.25,
        "end": 58.3,
        "duration": 34.05,
        "end_mode": "given",
        "snap_notes": ["start moved +0.25 s to a clean sentence edge"],
        "notes": ["watermark_faces_unknown"],
        "spelling_replacements": 2,
        "names_to_check": {"Zorb1e": [65.5], "QXV": []},
        "checks": {name: "pass" for name in CHECKS},
        "closer": "closer one",
        "hook_mode": "full",
        "hook_font_size": 80,
        "watermark": CampaignClipWatermark(
            sha256="a" * 64,
            blend="normal",
            x=360,
            y=298,
            width=360,
            height=96,
            rect=(490.0, 336.0, 590.0, 356.0),
            background="transparent",
            color_type="RGBA",
        ),
        "render_seconds": 3.2,
        "source_sha256": "b" * 64,
    }
    data.update(fields)
    return CampaignClip(**data)


POST = "@demo says something invented.\ncloser one\n"


def page(
    project: Path,
    moments: list[Moment],
    clips: dict[str, CampaignClip],
    posts: Any = None,
    reviews: Any = None,
) -> str:
    return build_html(
        campaign=make_campaign(project),
        run_name="2026-10-12_run",
        moments=moments,
        clips=clips,
        posts={"A01_tiktok": POST} if posts is None else posts,
        reviews=reviews,
    )


# --- the page ------------------------------------------------------------------------------------


def test_there_is_one_card_per_moment_in_order(project: Path) -> None:
    moments = [
        mom(id="A01"),
        mom(id="B02", platform="youtube"),
        mom(id="C03", platform="instagram"),
    ]
    html = page(project, moments, {"A01_tiktok": clip()})
    assert html.count('<section class="card">') == 3
    positions = [html.index(f"<h2>{m.id} &middot; {m.platform}</h2>") for m in moments]
    assert positions == sorted(positions)


def test_the_video_source_is_the_platform_folder_and_the_id(project: Path) -> None:
    html = page(project, [mom()], {"A01_tiktok": clip()})
    assert '<video controls preload="metadata" width="360" src="tiktok/A01.mp4">' in html


def test_the_header_counts_the_moments_per_platform_and_names_the_plan(project: Path) -> None:
    moments = [mom(id="A01"), mom(id="B02", platform="youtube"), mom(id="C03")]
    html = page(project, moments, {"A01_tiktok": clip()})
    assert "Platforms: tiktok 2 &middot; instagram 0 &middot; youtube 1 moments" in html
    assert "<code>posting_plan.csv</code>" in html


def test_old_flat_files_are_listed_when_given(project: Path) -> None:
    campaign = make_campaign(project)
    html = build_html(
        campaign=campaign,
        run_name="r",
        moments=[mom()],
        clips={},
        posts={},
        old_files=["A01_tiktok.mp4", "A01_tiktok.post.txt"],
    )
    assert "Old files from before the per-platform folders" in html
    assert "<li>A01_tiktok.post.txt</li>" in html
    assert "Old files" not in page(project, [mom()], {"A01_tiktok": clip()})


def test_all_eight_labels_are_there_in_order(project: Path) -> None:
    html = page(project, [mom()], {"A01_tiktok": clip()})
    assert [CHECK_LABELS[name] for name in CHECKS] == LABELS
    positions = [html.index(label) for label in LABELS]
    assert positions == sorted(positions)
    assert html.count("&#10003; pass") == 8


def test_a_warning_and_a_failure_are_shown_in_words(project: Path) -> None:
    checks = {name: "pass" for name in CHECKS}
    checks["not_previously_used"] = "warn"
    checks["closer_present"] = "fail"
    html = page(project, [mom()], {"A01_tiktok": clip(checks=checks)})
    assert "&#9650; warning</span> moment not previously used" in html
    assert "&#10007; FAIL</span> closer present" in html
    assert html.count("&#10003; pass") == 6


def test_a_check_that_was_not_made_says_so(project: Path) -> None:
    html = page(project, [mom()], {"A01_tiktok": clip(checks={"length_in_range": "pass"})})
    assert html.count("not checked") == 7


def test_names_to_check_show_their_times_or_say_they_were_not_said(project: Path) -> None:
    html = page(project, [mom()], {"A01_tiktok": clip()})
    assert "<b>Zorb1e</b>: 1:05.5" in html
    assert "<b>QXV</b>: not said in this clip" in html
    assert "2 spelling fixes" in html


def test_the_times_and_the_auto_end_line(project: Path) -> None:
    auto = clip(requested_end=None, end_mode="auto", auto_end_target=40.0, duration=37.42)
    html = page(project, [mom(end="auto")], {"A01_tiktok": auto})
    assert "end: auto (target 40 s, actual 37.42 s)" in html
    assert "requested 0:24&ndash;auto" in html
    assert "24.25&ndash;58.30 s" in html
    given = page(project, [mom()], {"A01_tiktok": clip()})
    assert "end: auto" not in given and "requested 0:24&ndash;0:58 &rarr; final" in given


def test_snap_notes_warnings_and_reason_codes(project: Path) -> None:
    html = page(
        project,
        [mom()],
        {"A01_tiktok": clip(warnings=["overlaps a posted moment (B02 youtube)"])},
    )
    assert "start moved +0.25 s to a clean sentence edge" in html
    assert "overlaps a posted moment (B02 youtube)" in html
    assert '<span class="badge">watermark_faces_unknown</span>' in html


def test_a_failed_card_shows_the_reason_and_no_video(project: Path) -> None:
    failed = clip(
        status="failed",
        error="4.6 s is outside the allowed 20-60 s",
        checks={"length_in_range": "fail"},
    )
    html = page(project, [mom()], {"A01_tiktok": failed}, posts={})
    assert "Not rendered: 4.6 s is outside the allowed 20-60 s" in html
    assert "<video" not in html and "<pre" not in html
    assert "&#10007; FAIL</span> length within range" in html
    assert "1 failed" in html


def test_a_moment_without_a_json_says_not_rendered_yet(project: Path) -> None:
    html = page(project, [mom(), mom(id="B02")], {"A01_tiktok": clip()})
    assert html.count("Not rendered yet") == 1 and html.count("<video") == 1
    assert "1 rendered &middot; 0 failed &middot; 1 not rendered yet" in html


def test_the_post_text_is_in_a_pre_block(project: Path) -> None:
    html = page(project, [mom()], {"A01_tiktok": clip()})
    assert f'<pre class="post">{POST}</pre>' in html


def test_a_missing_post_file_is_said(project: Path) -> None:
    html = page(project, [mom()], {"A01_tiktok": clip()}, posts={})
    assert "The post text file is missing." in html and "<pre" not in html


def test_text_from_files_is_escaped_and_the_page_has_no_script_or_url(project: Path) -> None:
    nasty = "<script>alert(1)</script> http://example.invalid/x"
    campaign = make_campaign(project, source_note=nasty)
    moments = [mom(hook=nasty, caption=nasty)]
    clips = {
        "A01_tiktok": clip(
            closer=nasty,
            notes=[nasty],
            snap_notes=[nasty],
            warnings=[nasty],
            names_to_check={nasty: [1.0]},
        ),
        "B02_tiktok": clip(id="B02", status="failed", error=nasty),
    }
    moments.append(mom(id="B02"))
    html = build_html(
        campaign=campaign, run_name=nasty, moments=moments, clips=clips, posts={"A01_tiktok": nasty}
    )
    assert "<script" not in html and "://" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_the_header_shows_the_watermark_facts_and_zero_api_calls(project: Path) -> None:
    html = page(project, [mom()], {"A01_tiktok": clip()})
    assert "<h1>Demo Campaign</h1>" in html and "invented source" in html
    assert "Run folder: 2026-10-12_run" in html
    assert "API calls: 0" in html
    assert "Clip length 20&ndash;60 s" in html and "hook mode full, font size 80" in html
    assert "format PNG (RGBA), alpha yes, background transparent, blend normal" in html
    assert "x 490&ndash;590, y 336&ndash;356" in html
    assert "assets/mark.png" in html
    assert "Warnings" not in html


def test_the_campaign_warnings_are_listed(project: Path) -> None:
    campaign = make_campaign(project, png=black_mark(200, 60, (50, 20, 150, 40)))
    html = build_html(campaign=campaign, run_name="r", moments=[mom()], clips={}, posts={})
    assert "Warnings" in html and "blend: screen hides it" in html
    assert "background opaque black" in html


def test_it_is_an_offline_page(project: Path) -> None:
    html = page(project, [mom()], {"A01_tiktok": clip()})
    assert "<script" not in html and "://" not in html and 'src="http' not in html
    assert html.startswith("<!DOCTYPE html>") and "<style>" in html


# --- write_report ---


def test_write_report_reads_the_jsons_and_post_files_in_the_folder(project: Path) -> None:
    campaign = make_campaign(project)
    out = project / "out"
    (out / "_meta").mkdir(parents=True)
    (out / "tiktok").mkdir()
    (out / "_meta" / "A01_tiktok.json").write_text(clip().model_dump_json(), encoding="utf-8")
    (out / "tiktok" / "A01.txt").write_text(POST, encoding="utf-8")
    (out / "_meta" / "B02_tiktok.json").write_text("{not json", encoding="utf-8")  # not rendered
    (out / "C09_tiktok.mp4").write_bytes(b"old")  # the flat layout from before Brief 11.2
    (out / "C09_tiktok.post.txt").write_text("old\n", encoding="utf-8")

    path = write_report(out, campaign, [mom(), mom(id="B02")], "run")

    assert path == out / REPORT_HTML
    text = path.read_text(encoding="utf-8")
    assert 'src="tiktok/A01.mp4"' in text and POST.strip().split("\n")[0] in text
    assert text.count("Not rendered yet") == 1
    assert "<li>C09_tiktok.mp4</li><li>C09_tiktok.post.txt</li>" in text
    assert "campaign_report.html" not in text.split("Old files")[1]
    assert "\r" not in text


def test_masked_words_are_a_count_and_sensitive_words_are_named(project: Path) -> None:
    review = (
        "masked\tcaptions\tblorp\tmask\nmasked\thook\t-\tslur\nmasked\tpost\tglim\tmask\n"
        "sensitive\thook\twump\tflag\nsensitive\tpost\t-\tslur\nsensitive\tpost\tzib\tcontext\n"
    )
    html = page(
        project,
        [mom()],
        {"A01_tiktok": clip(masked_words=3, sensitive_words=3)},
        reviews={"A01_tiktok": review},
    )
    assert "<b>Masked:</b> 3 words (1 slur)" in html
    assert "<li>wump (hook)</li>" in html and "<li>a slur (post caption)</li>" in html
    assert "<li>zib (post caption)</li>" in html and "allowed:" in html
    assert "blorp" not in html and "glim" not in html  # masked words are a count only

    plain = page(project, [mom()], {"A01_tiktok": clip()})
    assert "Masked" not in plain and "Sensitive" not in plain
    one = page(project, [mom()], {"A01_tiktok": clip(masked_words=1)})
    assert "<b>Masked:</b> 1 word</p>" in one
    missing = page(project, [mom()], {"A01_tiktok": clip(sensitive_words=2)})
    assert "<b>Sensitive words:</b> 2 (the review file is missing)" in missing


# --- through the command ---


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    fake = FakeFfmpeg()
    monkeypatch.setattr(ffmpeg, "render_clip", fake.render_clip)
    monkeypatch.setattr(ffmpeg, "probe", fake.probe)
    return fake


@pytest.fixture
def rig(project: Path, fake: FakeFfmpeg) -> Rig:
    rig = make_rig(project, fake)
    rig.moments(
        moment(id="A01", start="0:24", end="0:58"),
        moment(id="A02", start="0:34", end="1:04", platform="youtube", caption="a second one."),
    )
    return rig


def test_the_command_writes_the_report_next_to_the_clips(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rig.go() == 0

    report = rig.out / REPORT_HTML
    assert report.is_file()
    assert f"report: {report}" in capsys.readouterr().out
    text = report.read_text(encoding="utf-8")
    assert 'src="tiktok/A01.mp4"' in text and 'src="youtube/A02.mp4"' in text
    assert HOOK in text and "closer one" in text and "2 rendered" in text


def test_a_moment_not_selected_this_time_is_listed_as_not_rendered_yet(rig: Rig) -> None:
    assert rig.go("--only", "A02") == 0
    text = (rig.out / REPORT_HTML).read_text(encoding="utf-8")
    assert text.count("Not rendered yet") == 1
    assert 'src="youtube/A02.mp4"' in text and 'src="tiktok/A01.mp4"' not in text


def test_the_report_is_rewritten_on_every_run(rig: Rig) -> None:
    assert rig.go("--only", "A02") == 0
    first = (rig.out / REPORT_HTML).read_text(encoding="utf-8")
    assert rig.go("--only", "A01") == 0
    second = (rig.out / REPORT_HTML).read_text(encoding="utf-8")
    assert first != second and 'src="tiktok/A01.mp4"' in second and "Not rendered yet" not in second


def test_the_report_is_written_when_a_moment_failed(rig: Rig) -> None:
    rig.moments(
        moment(id="A01", start="0:24", end="0:30"),  # too short
        moment(id="A02", start="0:34", end="1:04", platform="youtube"),
    )
    assert rig.go() == 1
    text = (rig.out / REPORT_HTML).read_text(encoding="utf-8")
    assert "Not rendered: " in text and "outside the allowed 20-60 s" in text
    assert "1 rendered &middot; 1 failed" in text


def test_the_report_is_written_when_a_moment_was_refused(rig: Rig) -> None:
    from clipmachine import cli

    assert rig.go() == 0
    cli.main(
        ["campaign-posted", "--campaign", "demo", "--id", "A01", "--platform", "tiktok"],
        root=rig.project,
    )
    (rig.out / REPORT_HTML).unlink()
    assert rig.go() == 0
    assert 'src="tiktok/A01.mp4"' in (rig.out / REPORT_HTML).read_text(encoding="utf-8")


def test_the_console_still_shows_no_text_from_the_report(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.go()
    text = out_of(capsys)
    assert "report: " in text
    for secret in (*SECRET_TEXTS, "a second one.", "Example Campaign"):
        assert secret not in text


def test_the_report_names_the_sensitive_word_and_the_console_does_not(
    project: Path, fake: FakeFfmpeg, capsys: pytest.CaptureFixture[str]
) -> None:
    invented_lists(project)
    rig = make_rig(project, fake, overrides={(5, 2): "blorp"})
    rig.moments(moment(start="0:24", end="0:58", hook="WHY WUMP MATTERS"))

    assert rig.go() == 0

    html = (rig.out / REPORT_HTML).read_text(encoding="utf-8")
    assert "<li>WUMP (hook)</li>" in html and "<b>Masked:</b> 1 word</p>" in html
    assert "blorp" not in html
    assert "WUMP" not in out_of(capsys)
