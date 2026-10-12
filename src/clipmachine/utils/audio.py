"""Loudness of a stretch of the run's audio, one value per 10 ms.

Reads the mono 16-bit PCM WAV the fetch stage writes, with the standard library's `wave` module,
and only the part that is asked for. numpy is imported inside the function that needs it.
"""

import math
import wave
from collections.abc import Sequence
from pathlib import Path

from clipmachine.runs import RunError

HOP_SECONDS = 0.01  # one loudness value per hop
WINDOW_SECONDS = 0.025  # the samples each value is measured over, centred on its time
RMS_FLOOR = 1e-4  # added to the RMS, so silence is -80 dB and never log(0)
SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM
FULL_SCALE = 32768.0


def loudness_envelope(path: Path, start: float, end: float) -> list[float]:
    """Loudness in dB, one value per HOP_SECONDS covering [start, end) seconds of the file.

    Value i is 20 * log10(rms + RMS_FLOOR) of the WINDOW_SECONDS of samples (scaled to -1..1)
    centred on start + i * HOP_SECONDS. Samples outside the file count as silence. Only the needed
    part of the file is read. A file that cannot be read, or is not mono 16-bit PCM, is a RunError.
    """
    import numpy as np

    count = max(0, math.ceil(round((end - start) / HOP_SECONDS, 9)))
    try:
        with wave.open(str(path), "rb") as wav:
            if (
                wav.getnchannels() != 1
                or wav.getsampwidth() != SAMPLE_WIDTH_BYTES
                or wav.getcomptype() != "NONE"
            ):
                raise RunError(f"{path.name} is not mono 16-bit PCM audio")
            rate = wav.getframerate()
            total = wav.getnframes()
            if count == 0:
                return []
            half = round(WINDOW_SECONDS / 2 * rate)
            first = round(start * rate) - half
            last = round((start + (count - 1) * HOP_SECONDS) * rate) + half + 1
            low, high = max(first, 0), min(last, total)
            if high > low:
                wav.setpos(low)
                raw = wav.readframes(high - low)
            else:
                raw = b""
    except RunError:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise RunError(f"could not read the audio file {path.name}: {type(exc).__name__}") from exc

    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / FULL_SCALE
    # zeros outside the file: pad so every window is a plain slice of `padded`
    padded = np.zeros(last - first, dtype=np.float64)
    padded[low - first : low - first + len(samples)] = samples
    out: list[float] = []
    for i in range(count):
        centre = round((start + i * HOP_SECONDS) * rate)
        a = centre - half - first
        window = padded[a : a + 2 * half + 1]
        rms = float(np.sqrt(np.mean(window * window))) if len(window) else 0.0
        out.append(20.0 * math.log10(rms + RMS_FLOOR))
    return out


def envelope_at(envelope: Sequence[float], t: float) -> float:
    """The envelope's value `t` seconds after its start, by linear interpolation between hops and
    clamped to the first and last value. 0.0 for an empty envelope."""
    if not envelope:
        return 0.0
    position = t / HOP_SECONDS
    if position <= 0:
        return envelope[0]
    index = int(math.floor(position))
    if index >= len(envelope) - 1:
        return envelope[-1]
    fraction = position - index
    return envelope[index] + (envelope[index + 1] - envelope[index]) * fraction
