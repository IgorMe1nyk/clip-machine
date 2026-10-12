"""faster-whisper wrapper: environment, GPU library loading, device resolution, model loading.

This is the only module that touches faster_whisper and ctranslate2, and it imports them inside
functions, so the CLI starts fast and unit tests can replace the three seams marked SEAM below.

Nothing here writes outside the project folder: models go to <project>/models/ and the Hugging
Face home is pointed there too. GPU libraries are made findable for this process only
(os.add_dll_directory, ctypes preload, a PATH prefix on this process's own environment).
"""

import ctypes
import gc
import glob
import importlib.util
import logging
import math
import os
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clipmachine.models import TranscribeConfig

log = logging.getLogger("clipmachine")

DISABLE_CUDA_ENV = "CLIPMACHINE_DISABLE_CUDA"
MODELS_DIR_NAME = "models"
HF_HOME_NAME = ".hf"  # inside models/: Hugging Face token lookup and Xet cache
GPU_DIR_NAME = Path("tools") / "cuda12"  # optional, git-ignored folder with CUDA 12 DLLs
GPU_PACKAGES = ("cublas", "cudnn", "cuda_nvrtc")  # nvidia/<name>/bin in the `gpu` group wheels
PRELOAD_PATTERNS = ("nvrtc64_*.dll", "cublasLt64_*.dll", "cublas64_*.dll", "cudnn*64_9.dll")
CUDA_COMPUTE_ORDER = ("int8_float16", "float16", "int8", "float32")
GPU_ONLY_COMPUTE_TYPES = frozenset({"float16", "int8_float16"})
CPU_COMPUTE_TYPE = "int8"
PROBE_SAMPLES = 16000  # one second of silence at 16 kHz
MAX_REASON_CHARS = 200


class WhisperError(Exception):
    """A transcription setup problem with a one-line message fit to show the user."""


@dataclass(frozen=True)
class RawWord:
    start: float
    end: float
    word: str
    probability: float


@dataclass(frozen=True)
class RawSegment:
    id: int
    start: float
    end: float
    text: str
    avg_logprob: float
    no_speech_prob: float
    words: list[RawWord]


@dataclass(frozen=True)
class RawInfo:
    language: str
    language_probability: float
    duration: float


@dataclass
class LoadedModel:
    model: Any
    device: str
    compute_type: str
    warnings: list[str] = field(default_factory=list)

    def close(self) -> None:
        """Drop the model so its (GPU) memory is released before the stage returns."""
        self.model = None
        gc.collect()


def _one_line(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:MAX_REASON_CHARS]


# --- environment and GPU libraries -----------------------------------------------------------

_dll_dir_handles: list[Any] = []  # add_dll_directory handles; closing one removes the directory
_dll_failures: list[str] = []
_dll_prepared: set[Path] = set()


def configure_environment(root: Path) -> Path:
    """Point Hugging Face at <root>/models and turn its telemetry off. Returns the models folder.

    huggingface_hub reads these at import time, so this runs before anything imports it.
    """
    models_dir = root / MODELS_DIR_NAME
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HOME"] = str(models_dir / HF_HOME_NAME)
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    return models_dir


def gpu_library_dirs(root: Path) -> list[Path]:
    """Folders that hold CUDA 12 DLLs: the `gpu` group's wheels, then <root>/tools/cuda12."""
    found: list[Path] = []
    try:
        spec = importlib.util.find_spec("nvidia")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.submodule_search_locations:
        for location in spec.submodule_search_locations:
            for name in GPU_PACKAGES:
                candidate = Path(location) / name / "bin"
                if candidate.is_dir():
                    found.append(candidate)
    tools = root / GPU_DIR_NAME
    for candidate in (tools, tools / "bin"):
        if candidate.is_dir() and any(candidate.glob("*.dll")):
            found.append(candidate)
    return found


def prepare_gpu_libraries(root: Path) -> list[str]:
    """Make the CUDA DLLs findable for this process only. Returns failures (empty = all fine).

    add_dll_directory alone may not reach libraries that CTranslate2 loads late by bare name, so
    each DLL is also loaded by full path (a later load by name then finds it in the process), and
    the folders are put in front of this process's own PATH.
    """
    resolved = root.resolve()
    if resolved in _dll_prepared or sys.platform != "win32":
        return list(_dll_failures)
    _dll_prepared.add(resolved)
    dirs = gpu_library_dirs(resolved)
    if not dirs:
        return list(_dll_failures)
    for folder in dirs:
        if hasattr(os, "add_dll_directory"):
            _dll_dir_handles.append(os.add_dll_directory(str(folder)))
    os.environ["PATH"] = os.pathsep.join([*(str(d) for d in dirs), os.environ.get("PATH", "")])
    for folder in dirs:
        for pattern in PRELOAD_PATTERNS:
            for dll in sorted(glob.glob(str(folder / pattern))):
                try:
                    ctypes.WinDLL(dll)  # type: ignore[attr-defined,unused-ignore]
                except OSError as exc:
                    _dll_failures.append(f"{Path(dll).name}: {_one_line(exc)}")
    return list(_dll_failures)


# --- SEAMS: the only places that touch the libraries; unit tests replace these ------------------


def cuda_device_count() -> int:  # SEAM
    import ctranslate2

    return int(ctranslate2.get_cuda_device_count())


def supported_compute_types(device: str) -> set[str]:  # SEAM
    import ctranslate2

    return {str(t) for t in ctranslate2.get_supported_compute_types(device)}


def model_path(name: str, models_dir: Path) -> str:  # SEAM
    """The local folder of a model, downloading it into models_dir first if it is missing."""
    from faster_whisper.utils import download_model

    cache = str(models_dir)
    try:
        return str(download_model(name, local_files_only=True, cache_dir=cache))
    except Exception:
        log.info(f"transcribe: downloading model {name!r} into {models_dir} (first use only)")
    try:
        return str(download_model(name, cache_dir=cache))
    except Exception as exc:
        raise WhisperError(f"could not download model {name!r}: {_one_line(exc)}") from exc


def new_whisper_model(path: str, device: str, compute_type: str, cpu_threads: int) -> Any:  # SEAM
    from faster_whisper import WhisperModel

    return WhisperModel(path, device=device, compute_type=compute_type, cpu_threads=cpu_threads)


def new_batched_pipeline(model: Any) -> Any:  # SEAM
    from faster_whisper import BatchedInferencePipeline

    return BatchedInferencePipeline(model=model)


# --- device and compute resolution -------------------------------------------------------------


def cuda_unusable_reason(root: Path) -> str | None:
    """None if CUDA looks usable, otherwise one short reason."""
    if os.environ.get(DISABLE_CUDA_ENV):
        return f"disabled by {DISABLE_CUDA_ENV}"
    prepare_gpu_libraries(root)
    try:
        count = cuda_device_count()
    except Exception as exc:
        return f"CUDA check failed: {_one_line(exc)}"
    if count < 1:
        return "no CUDA device found"
    return None


def resolve_compute_type(
    requested: str, device: str, supported: set[str]
) -> tuple[str, str | None]:
    """The compute type to load with, plus a warning if it differs from what was asked for."""
    if device == "cpu":
        if requested == "auto":
            return CPU_COMPUTE_TYPE, None
        if requested in GPU_ONLY_COMPUTE_TYPES:
            return CPU_COMPUTE_TYPE, (
                f"compute_type {requested} needs a GPU; used {CPU_COMPUTE_TYPE} on the CPU"
            )
        return requested, None
    if requested != "auto":
        return requested, None
    for candidate in CUDA_COMPUTE_ORDER:
        if candidate in supported:
            return candidate, None
    return CUDA_COMPUTE_ORDER[0], None


def _probe(model: Any) -> None:
    """Run one second of silence through the model: this is what loads cuDNN and the kernels."""
    import numpy as np

    segments, _ = model.transcribe(
        np.zeros(PROBE_SAMPLES, dtype=np.float32), language="en", beam_size=1, vad_filter=False
    )
    for _ in segments:
        pass


def _load(path: str, cfg: TranscribeConfig, device: str, probe: bool) -> LoadedModel:
    warnings: list[str] = []
    supported = supported_compute_types(device)
    compute_type, note = resolve_compute_type(cfg.compute_type, device, supported)
    if note is not None:
        log.info(f"transcribe: {note}")
        warnings.append(note)
    model = new_whisper_model(path, device, compute_type, cfg.cpu_threads)
    if probe:
        try:
            _probe(model)
        except BaseException:
            del model
            gc.collect()
            raise
    return LoadedModel(model=model, device=device, compute_type=compute_type, warnings=warnings)


def open_model(cfg: TranscribeConfig, root: Path) -> LoadedModel:
    """Resolve the device, then load the model on it. `auto` falls back to CPU with one log line."""
    models_dir = configure_environment(root)
    path = model_path(cfg.model, models_dir)
    if cfg.device == "cpu":
        return _load(path, cfg, "cpu", probe=False)

    reason = cuda_unusable_reason(root)
    if reason is None:
        try:
            return _load(path, cfg, "cuda", probe=True)
        except Exception as exc:
            reason = f"the model failed on CUDA: {_one_line(exc)}"
            if _dll_failures:
                reason += f" (library load problems: {'; '.join(_dll_failures)})"
    if cfg.device == "cuda":
        raise WhisperError(f"device is cuda but CUDA is not usable: {reason}")
    log.info(f"transcribe: not using the GPU ({reason}); using the CPU")
    loaded = _load(path, cfg, "cpu", probe=False)
    loaded.warnings.append(f"fell back to CPU: {reason}")
    return loaded


# --- transcription -----------------------------------------------------------------------------


def _finite(value: object) -> float:
    number = float(value)  # type: ignore[arg-type]
    return number if math.isfinite(number) else math.nan


def _convert(segment: Any) -> RawSegment:
    words = [
        RawWord(
            start=_finite(w.start),
            end=_finite(w.end),
            word=str(w.word),
            probability=_finite(w.probability),
        )
        for w in (segment.words or [])
    ]
    return RawSegment(
        id=int(segment.id),
        start=_finite(segment.start),
        end=_finite(segment.end),
        text=str(segment.text),
        avg_logprob=_finite(segment.avg_logprob),
        no_speech_prob=_finite(segment.no_speech_prob),
        words=words,
    )


def _iterate(segments: Iterable[Any]) -> Iterator[RawSegment]:
    for segment in segments:
        yield _convert(segment)


def transcribe(
    loaded: LoadedModel, audio: Path, cfg: TranscribeConfig
) -> tuple[RawInfo, Iterator[RawSegment]]:
    """Start transcribing. The segment iterator does the work as it is consumed."""
    if loaded.model is None:
        raise WhisperError("model is already closed")
    options: dict[str, Any] = {
        "language": cfg.language,
        "beam_size": cfg.beam_size,
        "vad_filter": cfg.vad_filter,
        "condition_on_previous_text": cfg.condition_on_previous_text,
        "word_timestamps": True,
    }
    if cfg.style_prompt is not None:
        # hotwords is put in front of every window; initial_prompt only reaches the first one
        # when conditioning is off
        options["hotwords"] = cfg.style_prompt
    try:
        if cfg.batch_size > 0:
            pipeline = new_batched_pipeline(loaded.model)
            segments, info = pipeline.transcribe(str(audio), batch_size=cfg.batch_size, **options)
        else:
            segments, info = loaded.model.transcribe(str(audio), **options)
    except Exception as exc:
        raise WhisperError(f"transcription failed to start: {_one_line(exc)}") from exc
    raw_info = RawInfo(
        language=str(info.language or ""),
        language_probability=float(info.language_probability or 0.0),
        duration=float(info.duration),
    )
    return raw_info, _iterate(segments)
