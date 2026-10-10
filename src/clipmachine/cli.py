"""Command line interface. Exit codes: 0 ok, 1 a stage failed, 2 bad usage or environment."""

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from clipmachine.config import ConfigError
from clipmachine.pipeline import (
    STAGE_NAMES,
    is_run_folder,
    run_pipeline,
    start_new_run,
)
from clipmachine.preflight import MissingToolError
from clipmachine.runs import RunError

EXIT_OK = 0
EXIT_STAGE_FAILED = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipmachine",
        description="Turn a long video into short vertical clips.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{run,report}")

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

    report = sub.add_parser("report", help="show the report for a run folder (stub)")
    report.add_argument("run_folder", type=Path)
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
    return EXIT_OK


def _cmd_report(args: argparse.Namespace) -> int:
    run_folder: Path = args.run_folder
    if not is_run_folder(run_folder):
        raise RunError(f"{run_folder} is not a run folder (no run.json)")
    print("report is not implemented yet")
    return EXIT_OK


def main(argv: Sequence[str] | None = None, root: Path | None = None) -> int:
    """root is the project folder holding config/, clients/ and runs/ (default: current dir)."""
    args = build_parser().parse_args(argv)
    _configure_logging()
    project_root = root if root is not None else Path.cwd()
    try:
        if args.command == "run":
            return _cmd_run(args, project_root)
        return _cmd_report(args)
    except (ConfigError, RunError, MissingToolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
