import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from clipmachine import pipeline
from clipmachine.cli import main
from clipmachine.context import RunContext
from clipmachine.pipeline import STAGE_NAMES
from clipmachine.preflight import MissingToolError


def make_media(project: Path, name: str = "sample.mp4") -> Path:
    media = project / name
    media.write_bytes(b"")
    return media


def run_main(project: Path, *argv: str) -> int:
    return main(list(argv), root=project)


def read_run_json(run_dir: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    return data


def only_run_dir(project: Path) -> Path:
    (run_dir,) = (project / "runs").iterdir()
    return run_dir


def test_module_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "clipmachine", "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "run" in result.stdout
    assert "report" in result.stdout


def test_new_run_end_to_end(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    media = make_media(project)
    code = run_main(project, "run", str(media), "--client", "example-client")
    assert code == 0
    run_dir = only_run_dir(project)
    assert run_dir.name == f"{date.today().isoformat()}_sample"
    assert (run_dir / "config.resolved.yaml").is_file()
    stages = read_run_json(run_dir)["stages"]
    assert isinstance(stages, dict)
    assert list(stages) == list(STAGE_NAMES)
    assert all(s["status"] == "done" for s in stages.values())
    assert (
        "done: ran fetch, transcribe, candidates, score, render, report" in capsys.readouterr().out
    )


def test_resume_with_nothing_to_do(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_main(project, "run", str(make_media(project)), "--client", "example-client")
    run_dir = only_run_dir(project)
    before = (run_dir / "run.json").read_bytes()
    capsys.readouterr()

    assert run_main(project, "run", str(run_dir)) == 0

    out = capsys.readouterr().out
    assert "nothing to do" in out
    assert "not implemented yet" not in out
    assert (run_dir / "run.json").read_bytes() == before


def test_from_stage_reruns_only_render_and_report(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(project, "run", str(make_media(project)), "--client", "example-client")
    run_dir = only_run_dir(project)
    before = read_run_json(run_dir)["stages"]
    capsys.readouterr()

    assert run_main(project, "run", str(run_dir), "--from-stage", "render") == 0

    out = capsys.readouterr().out
    assert "stage render not implemented yet" not in out  # real stage (silent in unit tests)
    assert "stage report not implemented yet" not in out  # real stage (silent in unit tests)
    assert "stage score not implemented yet" not in out
    after = read_run_json(run_dir)["stages"]
    assert isinstance(before, dict)
    assert isinstance(after, dict)
    for name in ("fetch", "transcribe", "candidates", "score"):
        assert after[name] == before[name]
    for name in ("render", "report"):
        assert after[name]["finished_at"] != before[name]["finished_at"]


def test_evil_client_rejected_and_nothing_created(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_main(project, "run", str(make_media(project)), "--client", "../evil")
    assert code == 2
    assert "invalid client name" in capsys.readouterr().err
    assert list((project / "runs").iterdir()) == []


def test_unknown_client_rejected(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_main(project, "run", str(make_media(project)), "--client", "nobody")
    assert code == 2
    assert "unknown client" in capsys.readouterr().err
    assert list((project / "runs").iterdir()) == []


def test_new_run_requires_client(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_main(project, "run", str(make_media(project))) == 2
    assert "--client is required" in capsys.readouterr().err


def test_client_flag_rejected_on_resume(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_main(project, "run", str(make_media(project)), "--client", "example-client")
    run_dir = only_run_dir(project)
    capsys.readouterr()
    assert run_main(project, "run", str(run_dir), "--client", "example-client") == 2
    assert "only for new runs" in capsys.readouterr().err


def test_from_stage_rejected_for_new_run(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_main(
        project,
        "run",
        str(make_media(project)),
        "--client",
        "example-client",
        "--from-stage",
        "render",
    )
    assert code == 2
    assert "only for resuming" in capsys.readouterr().err
    assert list((project / "runs").iterdir()) == []


def test_unsupported_input_rejected(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    notes = project / "notes.txt"
    notes.write_text("hi", encoding="utf-8")
    assert run_main(project, "run", str(notes), "--client", "example-client") == 2
    assert "unsupported input type" in capsys.readouterr().err
    assert list((project / "runs").iterdir()) == []


def test_missing_input_rejected(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_main(project, "run", str(project / "nope.mp4"), "--client", "example-client")
    assert code == 2
    assert "not found" in capsys.readouterr().err


def test_folder_without_run_json_rejected(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = project / "somefolder"
    empty.mkdir()
    assert run_main(project, "run", str(empty), "--client", "example-client") == 2
    assert "without run.json" in capsys.readouterr().err


def test_stage_failure_exits_one_with_one_line_message(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(ctx: RunContext) -> None:
        raise RuntimeError("boom")

    monkeypatch.setitem(pipeline.STAGES, "score", boom)
    code = run_main(project, "run", str(make_media(project)), "--client", "example-client")

    assert code == 1
    err = capsys.readouterr().err.strip()
    assert err.count("\n") == 0
    assert "stage score failed: RuntimeError: boom" in err
    stages = read_run_json(only_run_dir(project))["stages"]
    assert isinstance(stages, dict)
    assert stages["score"]["status"] == "failed"
    assert stages["score"]["error"] == "RuntimeError: boom"
    assert stages["render"]["status"] == "pending"


def test_missing_ffmpeg_exits_two_with_guidance(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing() -> None:
        raise MissingToolError("ffmpeg not found on PATH. On Windows: run 'winget install ...'")

    monkeypatch.setattr(pipeline, "check_ffmpeg", missing)
    code = run_main(project, "run", str(make_media(project)), "--client", "example-client")
    assert code == 2
    assert "winget" in capsys.readouterr().err
    assert list((project / "runs").iterdir()) == []


def test_report_stub(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_main(project, "run", str(make_media(project)), "--client", "example-client")
    run_dir = only_run_dir(project)
    capsys.readouterr()
    assert run_main(project, "report", str(run_dir)) == 0
    assert "not implemented yet" in capsys.readouterr().out


def test_report_rejects_non_run_folder(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_main(project, "report", str(project / "runs")) == 2
    assert "not a run folder" in capsys.readouterr().err
