"""Loudness envelope of a WAV file and its lookup. Files are tiny synthetic ones in tmp_path."""

import math
import struct
import wave
from collections.abc import Sequence
from pathlib import Path

import pytest

from clipmachine.runs import RunError
from clipmachine.utils.audio import envelope_at, loudness_envelope

RATE = 16000
FLOOR_DB = -80.0


def write_wav(
    path: Path,
    samples: Sequence[int],
    *,
    channels: int = 1,
    width: int = 2,
    rate: int = RATE,
) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        if width == 2:
            wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        else:
            wav.writeframes(bytes(samples))
    return path


def sine(seconds: float, amplitude: int = 32767, freq: float = 440.0) -> list[int]:
    count = int(seconds * RATE)
    return [round(amplitude * math.sin(2 * math.pi * freq * n / RATE)) for n in range(count)]


def test_silence_is_the_floor(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "a.wav", [0] * RATE)
    values = loudness_envelope(path, 0.2, 0.5)
    assert values == pytest.approx([FLOOR_DB] * 30)


def test_a_full_scale_sine_is_about_minus_three_db(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "a.wav", sine(1.0))
    values = loudness_envelope(path, 0.2, 0.5)
    assert values == pytest.approx([-3.0] * 30, abs=0.3)


def test_one_value_per_ten_milliseconds(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "a.wav", sine(1.0))
    assert len(loudness_envelope(path, 0.0, 0.5)) == 50
    assert len(loudness_envelope(path, 0.1, 0.355)) == math.ceil(0.255 / 0.01)
    assert loudness_envelope(path, 0.3, 0.3) == []


def test_a_range_past_the_end_of_the_file_is_silence(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "a.wav", sine(0.5))
    assert loudness_envelope(path, 2.0, 2.2) == pytest.approx([FLOOR_DB] * 20)


def test_a_range_across_the_end_goes_from_loud_to_the_floor(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "a.wav", sine(0.5))
    values = loudness_envelope(path, 0.4, 0.7)
    assert values[0] == pytest.approx(-3.0, abs=0.3)
    assert values[-1] == pytest.approx(FLOOR_DB)


def test_a_range_before_the_start_is_silence_then_sound(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "a.wav", sine(0.5))
    values = loudness_envelope(path, -0.2, 0.1)
    assert values[0] == pytest.approx(FLOOR_DB)
    assert values[-1] == pytest.approx(-3.0, abs=0.3)


def test_a_stereo_file_is_an_error(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "s.wav", [0] * 200, channels=2)
    with pytest.raises(RunError, match="mono 16-bit"):
        loudness_envelope(path, 0.0, 0.1)


def test_an_8_bit_file_is_an_error(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "e.wav", [128] * 100, width=1)
    with pytest.raises(RunError, match="mono 16-bit"):
        loudness_envelope(path, 0.0, 0.1)


def test_a_missing_or_broken_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(RunError, match="could not read"):
        loudness_envelope(tmp_path / "none.wav", 0.0, 0.1)
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav file at all")
    with pytest.raises(RunError, match="could not read"):
        loudness_envelope(bad, 0.0, 0.1)


def test_envelope_at_interpolates_between_hops() -> None:
    env = [0.0, 10.0, 20.0]
    assert envelope_at(env, 0.0) == 0.0
    assert envelope_at(env, 0.01) == pytest.approx(10.0)
    assert envelope_at(env, 0.005) == pytest.approx(5.0)
    assert envelope_at(env, 0.015) == pytest.approx(15.0)


def test_envelope_at_clamps_and_handles_empty() -> None:
    env = [3.0, 7.0]
    assert envelope_at(env, -1.0) == 3.0
    assert envelope_at(env, 5.0) == 7.0
    assert envelope_at([], 0.5) == 0.0
