"""Command line interface. Exit codes: 0 ok, 1 a stage failed, 2 bad usage or environment."""

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from clipmachine import tracking
from clipmachine.config import ConfigError, load_resolved_file
from clipmachine.pipeline import (
    RESOLVED_CONFIG,
    STAGE_NAMES,
    is_run_folder,
    run_pipeline,
    start_new_run,
)
from clipmachine.preflight import MissingToolError
from clipmachine.runs import RunError
from clipmachine.stages.report import REPORT_HTML
from clipmachine.utils import faces
from clipmachine.utils.faces import FacesError
from clipmachine.utils.ffmpeg import MediaError

EXIT_OK = 0
EXIT_STAGE_FAILED = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipmachine",
        description="Turn a long video into short vertical clips.",
    )
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="{run,report,track,fetch-face-model}"
    )

    run = sub.add_parser(
        "run",
        help="start a new run from a media file, or resume a run folder",
        description=(
            "Start a new run: run <input-file> --client <name>\n"
            "Resume a run:    run <run-folder> [--from-stage <stage>]"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument("target", metavar="<input-file | run-folder>")
    run.add_argument("--client", help="client name (new runs only); a folder under clients/")
    run.add_argument(
        "--from-stage",
        choices=STAGE_NAMES,
        help="resume only: reset this stage and every later one, then run",
    )

    report = sub.add_parser("report", help="print the path of a run folder's report.html")
    report.add_argument("run_folder", type=Path)

    track = sub.add_parser(
        "track",
        help="find the active speaker in a run's kept clips (writes clips/<id>.track.json)",
    )
    track.add_argument("run_folder", type=Path)
    track.add_argument(
        "--preview",
        action="store_true",
        help="also write clips/<id>.track.preview.mp4 (or set speaker.debug_preview)",
    )

    sub.add_parser(
        "fetch-face-model",
        help="download the face models still missing: the MediaPipe Face Landmarker into "
        "models/mediapipe/ and the YuNet face detector into models/yunet/ (once)",
    )
    return parser


def _configure_logging() -> None:
    logger = logging.getLogger("clipmachine")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)


def _cmd_run(args: argparse.Namespace, root: Path) -> int:
    target = Path(args.target)
    if is_run_folder(target):
        if args.client is not None:
            raise RunError("--client is only for new runs; this run folder already has a client")
        run_dir = target
    else:
        if target.is_dir():
            raise RunError(
                f"{target} is a folder without run.json; pass a media file to start a new run"
            )
        if args.from_stage is not None:
            raise RunError("--from-stage is only for resuming an existing run folder")
        if args.client is None:
            raise RunError("--client is required when starting a new run")
        run_dir = start_new_run(root, target, args.client)
        print(f"run folder: {run_dir}")

    outcome = run_pipeline(run_dir, from_stage=args.from_stage, project_root=root)
    if not outcome.ok:
        print(
            f"error: stage {outcome.failed_stage} failed: {outcome.error}. "
            f"Fix the cause, then rerun: clipmachine run {run_dir}",
            file=sys.stderr,
        )
        return EXIT_STAGE_FAILED
    if not outcome.ran:
        print("nothing to do: every stage is already done")
    else:
        print(f"done: ran {', '.join(outcome.ran)}")
    report_path = run_dir / REPORT_HTML
    if report_path.is_file():
        print(f"report: {report_path}")
    return EXIT_OK


def _cmd_report(args: argparse.Namespace) -> int:
    run_folder: Path = args.run_folder
    if not is_run_folder(run_folder):
        raise RunError(f"{run_folder} is not a run folder (no run.json)")
    report_path = run_folder / REPORT_HTML
    if not report_path.is_file():
        raise RunError(
            f"no {REPORT_HTML} in {run_folder}; run: "
            f"clipmachine run {run_folder} --from-stage report"
        )
    print(report_path)
    return EXIT_OK


def _cmd_track(args: argparse.Namespace, root: Path) -> int:
    """Analyse the kept clips; run.json and the stage states are not touched."""
    run_folder: Path = args.run_folder
    if not is_run_folder(run_folder):
        raise RunError(f"{run_folder} is not a run folder (no run.json)")
    app = load_resolved_file(run_folder / RESOLVED_CONFIG).app
    results = tracking.run_track(
        run_folder, root, app, preview=bool(args.preview) or app.speaker.debug_preview
    )
    if not results:
        print("no kept clips in this run; nothing to track")
        return EXIT_OK
    for result in results:
        print(
            f"{result.clip_id}: {result.fallback_reason or 'ok'}, "
            f"faces {result.faces_seen}, switches {result.switches}, "
            f"face time {result.face_time_pct:.0f}%, {result.analysis_seconds:.1f} s"
        )
    return EXIT_OK


def _cmd_fetch_face_model(root: Path) -> int:
    """The only place the face models are downloaded; the track command never does it.

    A file that is already there and has the right hash is left alone; one with the wrong hash is
    an error."""
    for spec in (faces.LANDMARKER, faces.DETECTOR):
        folder = faces.model_dir(root, spec)
        if (folder / spec.name).exists():
            print(f"already present: {faces.ensure_model(folder, spec=spec)}")
            continue
        downloaded = faces.download_model(folder, spec=spec)
        print(f"saved: {downloaded.path}")
        print(f"size: {downloaded.size_bytes} bytes")
        print(f"sha256: {downloaded.sha256}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None, root: Path | None = None) -> int:
    """root is the project folder holding config/, clients/ and runs/ (default: current dir)."""
    args = build_parser().parse_args(argv)
    _configure_logging()
    project_root = root if root is not None else Path.cwd()
    try:
        if args.command == "run":
            return _cmd_run(args, project_root)
        if args.command == "track":
            return _cmd_track(args, project_root)
        if args.command == "fetch-face-model":
            return _cmd_fetch_face_model(project_root)
        return _cmd_report(args)
    except (ConfigError, RunError, MissingToolError, FacesError, MediaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
