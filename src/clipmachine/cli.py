"""Command line interface. Exit codes: 0 ok, 1 a stage failed, 2 bad usage or environment."""

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from clipmachine import tracking
from clipmachine.campaign.assign import ASSIGNMENTS_FILE, assigned_platform
from clipmachine.campaign.init import run_init
from clipmachine.campaign.loader import campaign_dir
from clipmachine.campaign.models import PLATFORMS
from clipmachine.campaign.produce import run_produce
from clipmachine.campaign.run import run_campaign
from clipmachine.campaign.schedule import (
    load_schedule,
    rebuild_plans,
    report_backups,
    run_plan,
)
from clipmachine.campaign.scout import reject_moment, run_scout
from clipmachine.campaign.used import USED_FILE, load_used, mark_posted, relabel_posted, save_used
from clipmachine.config import ConfigError, load_app_config, load_resolved_file
from clipmachine.pipeline import (
    RESOLVED_CONFIG,
    STAGE_NAMES,
    is_run_folder,
    run_pipeline,
    start_new_run,
)
from clipmachine.preflight import MissingToolError
from clipmachine.runs import RunError
from clipmachine.stages.render import render_safe_zone_preview
from clipmachine.stages.report import REPORT_HTML
from clipmachine.utils import faces
from clipmachine.utils.claude import ClaudeError
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
        dest="command",
        required=True,
        metavar="{run,report,track,safe-zones,campaign-init,produce,campaign,campaign-posted,"
        "campaign-reject,scout,plan,fetch-face-model,fetch-text-model}",
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

    safe = sub.add_parser(
        "safe-zones",
        help="render one kept clip with the safe-zone margins drawn on it, to check placement "
        "(written under runs/_scratch/safe-zones/, never into the run folder)",
    )
    safe.add_argument("run_folder", type=Path)
    safe.add_argument("--clip", required=True, help="the id of a kept clip, e.g. c01")

    campaign = sub.add_parser(
        "campaign",
        help="render the moments listed in campaigns/<name>/moments.yaml, with the watermark "
        "and a post caption each (no Claude API calls)",
    )
    campaign.add_argument("target", metavar="<source-video | run-folder>")
    campaign.add_argument("--campaign", required=True, help="the folder name under campaigns/")
    campaign.add_argument("--only", help="render only these moment ids, e.g. A03,B05")
    campaign.add_argument(
        "--force-reuse",
        action="store_true",
        help="render moments that are already marked as posted again",
    )

    init = sub.add_parser(
        "campaign-init",
        help="draft campaigns/<name>/campaign.yaml from the campaign's brief with one model call "
        "(Brief 12.2); review every line before the first run",
    )
    init.add_argument("slug", metavar="<name>", help="the new folder name under campaigns/")
    init.add_argument(
        "--brief", required=True, type=Path, help="the brief as .txt, .md or .pdf (text only)"
    )
    init.add_argument("--watermark", type=Path, help="the campaign's watermark PNG, if required")
    init.add_argument(
        "--moments", type=Path, help="a text list of timestamped moments the campaign supplied"
    )
    init.add_argument(
        "--force", action="store_true", help="replace an existing file (the old one goes to .bak)"
    )

    produce = sub.add_parser(
        "produce",
        help="one command per episode (Brief 12.2): scout it when campaign.yaml says "
        "selection: ai, render this episode's unposted moments into the campaign folder, and "
        "rebuild the report and the posting plan; posted and rejected moments are left alone",
    )
    produce.add_argument("episode", metavar="<source-video | run-folder>")
    produce.add_argument("--campaign", required=True, help="the folder name under campaigns/")
    produce.add_argument(
        "--rerender",
        action="store_true",
        help="render again the clips whose moments.yaml entry did not change (kept by default)",
    )

    posted = sub.add_parser(
        "campaign-posted",
        help="mark a campaign moment as posted, so it is not reused by accident",
    )
    posted.add_argument("--campaign", required=True, help="the folder name under campaigns/")
    posted.add_argument("--id", required=True, help="the moment id, e.g. A03")
    posted.add_argument(
        "--platform",
        choices=PLATFORMS,
        help="the platform it went to; left out, the moment's assigned platform "
        "(campaigns/<name>/assignments.json). With --fix-platform: the label to correct",
    )
    posted.add_argument(
        "--fix-platform",
        choices=PLATFORMS,
        metavar="{tiktok,instagram,youtube}",
        help="relabel the moment's posted entry with the platform it really went to",
    )

    scout = sub.add_parser(
        "scout",
        help="let the AI propose moments for a campaign whose rules allow it (campaign.yaml "
        "selection: ai): candidates and scoring on one episode, appended to moments.yaml",
    )
    scout.add_argument("episode", metavar="<source-video>")
    scout.add_argument("--campaign", required=True, help="the folder name under campaigns/")
    scout.add_argument(
        "--dry-run",
        action="store_true",
        help="score and report, but write nothing to moments.yaml or scout.json",
    )
    scout.add_argument(
        "--reuse-scored",
        action="store_true",
        help="no API call: rebuild the proposals from this episode's last scored.json of the "
        "campaign (new ids for stretches not yet listed)",
    )

    rejected = sub.add_parser(
        "campaign-reject",
        help="reject a proposed or listed moment: never rendered again, never proposed again",
    )
    rejected.add_argument("--campaign", required=True, help="the folder name under campaigns/")
    rejected.add_argument("--id", required=True, help="the moment id, e.g. S03")

    sub.add_parser(
        "plan",
        help="rebuild the combined posting plan runs/_campaigns/posting_plan.csv from every "
        "campaign, as campaigns/schedule.yaml says (Brief 12.2)",
    )

    sub.add_parser(
        "fetch-face-model",
        help="download the face models still missing: the MediaPipe Face Landmarker into "
        "models/mediapipe/ and the YuNet face detector into models/yunet/ (once)",
    )
    sub.add_parser(
        "fetch-text-model",
        help="download the PP-OCRv3 text detector into models/textdet/ (once); the "
        "graphics-aware layout (video.graphics) needs it",
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
            f"faces {result.faces_seen}, {result.faces_per_sample:.2f} per sample, "
            f"cuts {result.cuts}, switches {result.switches}, "
            f"face time {result.face_time_pct:.0f}%, mouth read {result.mouth_read_pct:.0f}%, "
            f"windows sync {result.sync_windows}, motion {result.motion_windows}, "
            f"unscored {result.unscored_windows}, "
            f"{result.analysis_seconds:.1f} s"
        )
    return EXIT_OK


def _cmd_safe_zones(args: argparse.Namespace, root: Path) -> int:
    """Render one clip with the margins drawn; the run folder is only read."""
    run_folder: Path = args.run_folder
    if not is_run_folder(run_folder):
        raise RunError(f"{run_folder} is not a run folder (no run.json)")
    app = load_resolved_file(run_folder / RESOLVED_CONFIG).app
    print(render_safe_zone_preview(run_folder, root, app, args.clip))
    return EXIT_OK


def _cmd_campaign(args: argparse.Namespace, root: Path) -> int:
    return run_campaign(
        root, Path(args.target), args.campaign, only=args.only, force_reuse=args.force_reuse
    )


def _cmd_campaign_posted(args: argparse.Namespace, root: Path) -> int:
    """Mark the latest rendered, unposted entry of a moment as posted in used.json, or (Brief
    11.2, --fix-platform) relabel its posted entry. Needs only the campaign folder: campaign.yaml
    and the watermark are not read."""
    folder = campaign_dir(root, args.campaign)
    if args.fix_platform is not None:
        log, before, after = relabel_posted(
            load_used(folder), args.id, args.fix_platform, args.platform
        )
        if before.platform != after.platform:
            save_used(folder, log)
            print(f"{args.id}: posted entry relabelled {before.platform} -> {after.platform}")
            _replan(root)
        else:
            print(f"{args.id}: posted entry is already {after.platform}")
        return EXIT_OK
    platform = args.platform or assigned_platform(folder, args.id)
    if platform is None:
        raise ConfigError(
            f"moment {args.id} has no platform assigned yet (no entry in {ASSIGNMENTS_FILE}); "
            "render it first, or say which with --platform"
        )
    log, _, changed = mark_posted(
        load_used(folder), args.id, platform, datetime.now(UTC).isoformat()
    )
    if changed:
        save_used(folder, log)
    state = "marked as posted" if changed else "already marked as posted"
    print(f"{args.id} {platform}: {state}")
    if changed:
        _replan(root)
    return EXIT_OK


def _replan(root: Path) -> None:
    """Brief 12.2: with a campaigns/schedule.yaml, the combined plan follows used.json."""
    if load_schedule(root) is None:
        return
    result = rebuild_plans(root, load_app_config(root), now=datetime.now())
    if result is not None:
        path, backups = result
        report_backups(backups)
        print(f"posting plan: {path}")


def _cmd_campaign_reject(args: argparse.Namespace, root: Path) -> int:
    """Mark a moment rejected in used.json (Brief 12): the render refuses it and the scout never
    proposes the same stretch again. Reads campaign.yaml and moments.yaml for the times."""
    folder = campaign_dir(root, args.campaign)
    rejected = reject_moment(root, args.campaign, args.id, datetime.now(UTC).isoformat())
    print(f"{args.id}: rejected ({rejected.start:.2f}-{rejected.end:.2f} s of its source)")
    print(f"recorded in {folder / USED_FILE}")
    return EXIT_OK


def _cmd_fetch_face_model(root: Path) -> int:
    """The only place the face models are downloaded; the track command never does it."""
    return _fetch_models(root, (faces.LANDMARKER, faces.DETECTOR))


def _cmd_fetch_text_model(root: Path) -> int:
    """The only place the text detector is downloaded (Brief 12.1)."""
    return _fetch_models(root, (faces.TEXT_DETECTOR,))


def _fetch_models(root: Path, specs: tuple[faces.ModelSpec, ...]) -> int:
    """A file that is already there and has the right hash is left alone; one with the wrong
    hash is an error."""
    for spec in specs:
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
        if args.command == "safe-zones":
            return _cmd_safe_zones(args, project_root)
        if args.command == "campaign":
            return _cmd_campaign(args, project_root)
        if args.command == "campaign-init":
            return run_init(
                project_root,
                args.slug,
                brief=args.brief,
                watermark=args.watermark,
                moments=args.moments,
                force=args.force,
            )
        if args.command == "produce":
            return run_produce(
                project_root, Path(args.episode), args.campaign, rerender=args.rerender
            )
        if args.command == "campaign-posted":
            return _cmd_campaign_posted(args, project_root)
        if args.command == "scout":
            return run_scout(
                project_root,
                Path(args.episode),
                args.campaign,
                dry_run=args.dry_run,
                reuse_scored=args.reuse_scored,
            )
        if args.command == "campaign-reject":
            return _cmd_campaign_reject(args, project_root)
        if args.command == "plan":
            return run_plan(project_root)
        if args.command == "fetch-face-model":
            return _cmd_fetch_face_model(project_root)
        if args.command == "fetch-text-model":
            return _cmd_fetch_text_model(project_root)
        return _cmd_report(args)
    except (ConfigError, RunError, MissingToolError, FacesError, MediaError, ClaudeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
