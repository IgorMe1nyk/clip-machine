import json
import os
from datetime import date
from pathlib import Path

import pytest

from clipmachine import runs
from clipmachine.runs import (
    GIB,
    RunError,
    check_input_size,
    create_run_dir,
    ensure_inside,
    slugify,
    validate_input,
    write_json_atomic,
)

TODAY = date(2026, 10, 12)


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("My Episode #12", "my-episode-12"),
        ("  --Hello__World--  ", "hello-world"),
        ("UPPER", "upper"),
        ("a...b///c", "a-b-c"),
        ("ünïcode título", "n-code-t-tulo"),
        ("???", "episode"),
        ("", "episode"),
        ("日本語", "episode"),
        ("x" * 80, "x" * 50),
        ("a" * 49 + " b", "a" * 49),  # cut lands on a separator; never ends with '-'
    ],
)
def test_slugify(stem: str, expected: str) -> None:
    assert slugify(stem) == expected


def test_run_dir_name_and_collisions(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    first = create_run_dir(root, Path("My Talk.mp4"), TODAY)
    second = create_run_dir(root, Path("My Talk.mp4"), TODAY)
    third = create_run_dir(root, Path("my talk.mov"), TODAY)
    assert first.name == "2026-10-12_my-talk"
    assert second.name == "2026-10-12_my-talk-2"
    assert third.name == "2026-10-12_my-talk-3"
    assert all(p.is_dir() for p in (first, second, third))


def test_run_dir_created_inside_runs_root(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    created = create_run_dir(root, Path("a.mp4"), TODAY)
    assert created.parent == root.resolve()


@pytest.mark.parametrize("bad_slug", ["../../evil", "/../../evil", "../evil"])
def test_path_escape_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_slug: str
) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setattr(runs, "slugify", lambda stem: bad_slug)
    with pytest.raises(RunError):
        create_run_dir(root, Path("a.mp4"), TODAY)
    assert not (tmp_path / "evil").exists()
    assert list(root.iterdir()) == []


def test_ensure_inside_rejects_other_locations(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    assert ensure_inside(root, root / "ok").parent == root.resolve()
    with pytest.raises(RunError):
        ensure_inside(root, tmp_path / "elsewhere")
    with pytest.raises(RunError):
        ensure_inside(root, root / ".." / "elsewhere")
    with pytest.raises(RunError):
        ensure_inside(root, root / "nested" / "deeper")


@pytest.mark.parametrize("name", ["a.mp4", "a.MOV", "a.mkv", "a.webm", "a.WebM"])
def test_input_extensions_allowed(tmp_path: Path, name: str) -> None:
    f = tmp_path / name
    f.write_bytes(b"")
    assert validate_input(f) == f


@pytest.mark.parametrize("name", ["a.m4a", "a.WAV", "a.mp3"])
def test_audio_only_extensions_rejected_with_specific_message(tmp_path: Path, name: str) -> None:
    f = tmp_path / name
    f.write_bytes(b"")
    with pytest.raises(
        RunError, match=r"^audio-only files aren't supported yet \(video needed for clips\)$"
    ):
        validate_input(f)


@pytest.mark.parametrize("name", ["a.txt", "a.exe", "a", "a.mp4.txt", "a.avi"])
def test_input_extensions_rejected(tmp_path: Path, name: str) -> None:
    f = tmp_path / name
    f.write_bytes(b"")
    with pytest.raises(RunError, match="unsupported"):
        validate_input(f)


def test_input_missing(tmp_path: Path) -> None:
    with pytest.raises(RunError, match="not found"):
        validate_input(tmp_path / "nope.mp4")


def test_input_directory_rejected(tmp_path: Path) -> None:
    d = tmp_path / "folder.mp4"
    d.mkdir()
    with pytest.raises(RunError, match="regular file"):
        validate_input(d)


def test_size_check_boundary(tmp_path: Path) -> None:
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x" * 1024)
    limit_gb = 1024 / GIB  # exactly the file size
    assert check_input_size(f, limit_gb) == 1024
    with pytest.raises(RunError, match="too large"):
        check_input_size(f, limit_gb * 0.99)


def test_write_json_atomic_replaces_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "run.json"
    write_json_atomic(target, {"a": 1})
    write_json_atomic(target, {"a": 2, "name": "ünï"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2, "name": "ünï"}
    assert [p.name for p in tmp_path.iterdir()] == ["run.json"]


def test_write_json_atomic_failure_keeps_old_file(tmp_path: Path) -> None:
    target = tmp_path / "run.json"
    write_json_atomic(target, {"ok": True})
    with pytest.raises(TypeError):
        write_json_atomic(target, {"bad": object()})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert [p.name for p in tmp_path.iterdir()] == ["run.json"]


def test_write_json_atomic_uses_os_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    real = os.replace

    def spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        calls.append((str(src), str(dst)))
        real(src, dst)

    monkeypatch.setattr(runs.os, "replace", spy)
    target = tmp_path / "run.json"
    write_json_atomic(target, {})
    assert len(calls) == 1
    assert Path(calls[0][0]).parent == target.parent
    assert Path(calls[0][1]) == target
