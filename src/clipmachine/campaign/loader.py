"""Load and check a campaign folder (Brief 11): campaign.yaml, the watermark and moments.yaml.

Everything here is a ConfigError when wrong, with the entry named. Nothing is rendered, and
no transcript text, hook or name is ever put in a log line from this module.
"""

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import ValidationError

from clipmachine.campaign.models import AutoPosition, CampaignConfig, Moment
from clipmachine.config import ConfigError, _read_yaml, _validate
from clipmachine.models import AppConfig
from clipmachine.utils.image import (
    ImageError,
    PngInfo,
    background_kind,
    load_pixels,
    png_info,
    visible_box,
)
from clipmachine.utils.safezone import Rect, safe_rect

SLUG_RE = re.compile(r"_?[a-z0-9][a-z0-9-]{0,40}")
AUTO_POSITION_FILE = "auto_position.json"


@dataclass(frozen=True)
class WatermarkAsset:
    """The watermark file as measured once at load: what does not depend on where it goes."""

    file: Path  # absolute
    blend: str
    info: PngInfo
    background: str  # transparent | opaque_black | opaque_other
    scale: float
    size: tuple[int, int]  # the scaled canvas
    box: tuple[float, float, float, float]  # the visible content box on the scaled canvas
    sha256: str

    @property
    def box_size(self) -> tuple[float, float]:
        return self.box[2] - self.box[0], self.box[3] - self.box[1]


@dataclass(frozen=True)
class Watermark:
    file: Path  # absolute
    blend: str
    info: PngInfo
    background: str  # transparent | opaque_black | opaque_other
    scale: float
    size: tuple[int, int]  # the scaled canvas
    x: int  # top-left of the scaled canvas on the output frame; may be outside it
    y: int
    rect: Rect  # the visible content box in output pixels, where ffmpeg draws it
    sha256: str
    x_center: float = 0.0  # the position it was placed at, as fractions of the frame
    y_center: float = 0.0


@dataclass(frozen=True)
class Campaign:
    slug: str
    folder: Path
    config: CampaignConfig
    asset: WatermarkAsset
    # None only with `position: auto` before the position is chosen (Brief 11.1)
    watermark: Watermark | None
    hook_font_size: int  # the campaign's own, else hook.font_size from the app config
    hook_seconds: float  # intro mode: the campaign's own, else hook.seconds from the app config
    hook_position: str  # top | avoid_faces: the campaign's own, else from the app config
    warnings: tuple[str, ...]
    auto: AutoPosition | None = None  # the lock, with `position: auto` once it is chosen

    @property
    def placed(self) -> Watermark:
        """The watermark once it has a position; a programming error before that."""
        assert self.watermark is not None, "the watermark position is not chosen yet"
        return self.watermark


def campaign_dir(root: Path, slug: str) -> Path:
    if not SLUG_RE.fullmatch(slug):
        raise ConfigError(
            f"invalid campaign name {slug!r}: use lowercase letters, digits and '-', "
            "starting with a letter or digit (a leading '_' is allowed), at most 41 characters"
        )
    folder = root / "campaigns" / slug
    if not folder.is_dir():
        raise ConfigError(f"unknown campaign {slug!r}: no folder {folder}")
    return folder


def _round(value: float) -> int:
    return math.floor(value + 0.5)


def _watermark_path(folder: Path, name: str) -> Path:
    pure = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or ":" in name
        or pure.is_absolute()
        or ".." in pure.parts
        or name.endswith("/")
    ):
        raise ConfigError(
            "campaign.yaml: watermark.file must be a relative path with forward slashes, "
            "inside the campaign folder, without '..'"
        )
    if pure.suffix.lower() != ".png":
        raise ConfigError("campaign.yaml: watermark.file must be a .png file")
    path = (folder / pure).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ConfigError("campaign.yaml: watermark.file must stay inside the campaign folder")
    if not path.is_file():
        raise ConfigError(f"campaign.yaml: watermark.file not found: {name}")
    return path


def measure_watermark(
    cfg: CampaignConfig, app: AppConfig, path: Path
) -> tuple[WatermarkAsset, list[str]]:
    """Read the file once: PNG facts, pixels, background and the visible box at the campaign's
    scale. Returns the asset and the warnings about it."""
    mark = cfg.watermark
    try:
        info = png_info(path)
        pixels = load_pixels(path)
    except ImageError as exc:
        raise ConfigError(f"campaign.yaml: watermark.file: {exc}") from exc
    background = background_kind(pixels)
    box = visible_box(pixels, by_brightness=mark.blend == "screen")
    if box is None:
        raise ConfigError(
            f"campaign.yaml: the watermark has no visible pixels with blend {mark.blend} "
            "(it is fully transparent" + (" or black)" if mark.blend == "screen" else ")")
        )
    size = (max(1, round(info.width * mark.scale)), max(1, round(info.height * mark.scale)))
    fx, fy = size[0] / info.width, size[1] / info.height
    scaled = (box[0] * fx, box[1] * fy, box[2] * fx, box[3] * fy)
    warnings: list[str] = []
    if mark.blend == "normal" and background == "opaque_black":
        warnings.append(
            "the watermark has an opaque black background that will be drawn on the video; "
            "blend: screen hides it"
        )
    asset = WatermarkAsset(
        file=path,
        blend=mark.blend,
        info=info,
        background=background,
        scale=mark.scale,
        size=size,
        box=scaled,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    return asset, warnings


def _rect_at(
    asset: WatermarkAsset, x_center: float, y_center: float, app: AppConfig
) -> tuple[int, int, Rect]:
    """The canvas top-left and the visible box on the frame for a centre position."""
    bx1, by1, bx2, by2 = asset.box
    # the centre of the visible content goes to the position
    x = _round(x_center * app.video.width - (bx1 + bx2) / 2)
    y = _round(y_center * app.video.height - (by1 + by2) / 2)
    return x, y, Rect(x + bx1, y + by1, x + bx2, y + by2)


def placement_problem(
    asset: WatermarkAsset, x_center: float, y_center: float, app: AppConfig
) -> str | None:
    """Why the visible box may not sit at this centre (the frame, a corner zone, the safe area),
    or None when it may."""
    width, height = app.video.width, app.video.height
    _, _, rect = _rect_at(asset, x_center, y_center, app)
    shown = f"x {rect.x1:g}..{rect.x2:g}, y {rect.y1:g}..{rect.y2:g}"
    frame = Rect(0.0, 0.0, float(width), float(height))
    if not rect.inside(frame):
        return f"puts the visible watermark ({shown}) outside the {width}x{height} frame"
    zone_w, zone_h = app.campaign.corner_zone * width, app.campaign.corner_zone * height
    corners = {
        "top left": Rect(0, 0, zone_w, zone_h),
        "top right": Rect(width - zone_w, 0, width, zone_h),
        "bottom left": Rect(0, height - zone_h, zone_w, height),
        "bottom right": Rect(width - zone_w, height - zone_h, width, height),
    }
    for name, corner in corners.items():
        if rect.intersects(corner):
            return (
                f"puts the visible watermark ({shown}) in the {name} corner zone "
                f"({app.campaign.corner_zone:.0%} of width and height)"
            )
    safe = safe_rect(width, height, app.safe.top, app.safe.bottom, app.safe.left, app.safe.right)
    if not rect.inside(safe):
        return (
            f"puts the visible watermark ({shown}) outside the safe area "
            f"(x {safe.x1:g}..{safe.x2:g}, y {safe.y1:g}..{safe.y2:g})"
        )
    return None


def place_watermark(
    asset: WatermarkAsset, x_center: float, y_center: float, app: AppConfig
) -> Watermark:
    """The watermark at a centre position; a ConfigError names the geometry check it fails."""
    problem = placement_problem(asset, x_center, y_center, app)
    if problem is not None:
        raise ConfigError(f"campaign.yaml: watermark.position {problem}")
    x, y, rect = _rect_at(asset, x_center, y_center, app)
    return Watermark(
        file=asset.file,
        blend=asset.blend,
        info=asset.info,
        background=asset.background,
        scale=asset.scale,
        size=asset.size,
        x=x,
        y=y,
        rect=rect,
        sha256=asset.sha256,
        x_center=x_center,
        y_center=y_center,
    )


def load_campaign(root: Path, slug: str, app: AppConfig) -> Campaign:
    folder = campaign_dir(root, slug)
    cfg = _validate(CampaignConfig, _read_yaml(folder / "campaign.yaml"), "campaign.yaml")
    path = _watermark_path(folder, cfg.watermark.file)
    asset, warnings = measure_watermark(cfg, app, path)
    font_size = cfg.hook.font_size if cfg.hook.font_size is not None else app.hook.font_size
    seconds = cfg.hook.seconds if cfg.hook.seconds is not None else app.hook.seconds
    position = cfg.hook.position if cfg.hook.position is not None else app.hook.position
    where = cfg.watermark.position
    watermark: Watermark | None = None
    lock: AutoPosition | None = None
    if where == "auto":
        lock, stale = _read_lock(folder, asset, app, position)
        if stale is not None:
            warnings.append(stale)
        if lock is not None:
            problem = placement_problem(asset, lock.x_center, lock.y_center, app)
            if problem is not None:
                raise ConfigError(
                    f"{AUTO_POSITION_FILE}: the locked position {problem}; delete the file to "
                    "choose again, or set an explicit watermark.position"
                )
            watermark = place_watermark(asset, lock.x_center, lock.y_center, app)
    else:
        watermark = place_watermark(asset, where.x_center, where.y_center, app)
    return Campaign(
        slug, folder, cfg, asset, watermark, font_size, seconds, position, tuple(warnings), lock
    )


def _read_lock(
    folder: Path, asset: WatermarkAsset, app: AppConfig, hook_position: str
) -> tuple[AutoPosition | None, str | None]:
    """The auto_position.json lock when it is there and still fits the watermark and the frame
    geometry: (lock, None). A stale or unreadable lock gives (None, why), so the position is
    chosen again; no file gives (None, None)."""
    path = folder / AUTO_POSITION_FILE
    if not path.is_file():
        return None, None
    try:
        lock = AutoPosition.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        return None, f"{AUTO_POSITION_FILE} cannot be read ({type(exc).__name__}); choosing again"
    current = (
        asset.sha256,
        asset.scale,
        asset.blend,
        (app.video.width, app.video.height),
        (app.safe.top, app.safe.bottom, app.safe.left, app.safe.right),
        app.campaign.corner_zone,
        hook_position,
    )
    locked = (
        lock.watermark_sha256,
        lock.scale,
        lock.blend,
        lock.frame,
        lock.safe,
        lock.corner_zone,
        lock.hook_position,
    )
    if current != locked:
        return None, (
            f"{AUTO_POSITION_FILE} is stale (the watermark file, its scale or blend, the frame, "
            "the safe margins, the corner zone or the hook position changed); choosing again"
        )
    return lock, None


def load_moments(campaign: Campaign) -> list[Moment]:
    path = campaign.folder / "moments.yaml"
    if not path.is_file():
        raise ConfigError(f"moments.yaml not found: {path}")
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"moments.yaml: invalid YAML: {exc}") from exc
    if not isinstance(data, list):
        raise ConfigError("moments.yaml: expected a list of moments at the top level")
    if not data:
        raise ConfigError("moments.yaml: the list is empty")
    limit = campaign.config.hook.max_chars
    moments: list[Moment] = []
    seen: set[tuple[str, str]] = set()
    for number, entry in enumerate(data, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"moments.yaml entry {number}: expected a mapping")
        label = f"moments.yaml entry {number} (id {str(entry.get('id'))[:24]})"
        moment = _validate(Moment, entry, label)
        if len(moment.hook) > limit:
            raise ConfigError(
                f"{label}: hook has {len(moment.hook)} characters, more than "
                f"hook.max_chars ({limit}); shorten the hook"
            )
        key = (moment.id, moment.platform)
        if key in seen:
            raise ConfigError(f"{label}: id and platform ({moment.platform}) are already used")
        seen.add(key)
        moments.append(moment)
    return moments


def select_moments(moments: list[Moment], only: str | None) -> list[Moment]:
    """The moments named in `only` (comma-separated ids), in file order; all when None."""
    if only is None:
        return list(moments)
    wanted = [part.strip() for part in only.split(",") if part.strip()]
    if not wanted:
        raise ConfigError("--only needs at least one id")
    known = {m.id for m in moments}
    missing = [w for w in wanted if w not in known]
    if missing:
        raise ConfigError(f"--only: no moment with id {', '.join(missing)}")
    return [m for m in moments if m.id in set(wanted)]
