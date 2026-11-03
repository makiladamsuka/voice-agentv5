"""Precomputed and synthetic speech amplitude envelopes for robot motion sync."""

from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ENVELOPE_SAMPLE_HZ = 50
_CHUNK_MS = 20  # 50 Hz = 20ms per frame
_PCM_SAMPLE_RATE = 24000
_BYTES_PER_SAMPLE = 2
_CHUNK_BYTES = int(_PCM_SAMPLE_RATE * _BYTES_PER_SAMPLE * (_CHUNK_MS / 1000))

_ALPHA_FAST = 0.85
_ALPHA_SLOW = 0.12


@dataclass(frozen=True)
class AudioEnvelope:
    sample_rate_hz: int
    duration_ms: int
    fast: tuple[float, ...]
    slow: tuple[float, ...]

    def sample_at_ms(self, elapsed_ms: int) -> tuple[float, float]:
        if self.duration_ms <= 0 or not self.fast:
            return 0.0, 0.0
        idx = int(elapsed_ms * self.sample_rate_hz / 1000)
        if idx < 0:
            return 0.0, 0.0
        if idx >= len(self.fast):
            return 0.0, 0.0
        return self.fast[idx], self.slow[idx]

    def to_dict(self) -> dict:
        return {
            "sample_rate_hz": self.sample_rate_hz,
            "duration_ms": self.duration_ms,
            "fast": list(self.fast),
            "slow": list(self.slow),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AudioEnvelope":
        return cls(
            sample_rate_hz=int(data.get("sample_rate_hz", ENVELOPE_SAMPLE_HZ)),
            duration_ms=int(data["duration_ms"]),
            fast=tuple(float(x) for x in data["fast"]),
            slow=tuple(float(x) for x in data["slow"]),
        )


def sidecar_path(audio_path: Path) -> Path:
    return audio_path.with_suffix(audio_path.suffix + ".amp.json")


def rms_pcm(pcm_bytes: bytes) -> float:
    """Compute RMS of 16-bit PCM bytes in [0.0, 1.0] using fast NumPy vector math."""
    if len(pcm_bytes) < 2:
        return 0.0
    n_bytes = (len(pcm_bytes) // 2) * 2
    samples = np.frombuffer(pcm_bytes[:n_bytes], dtype="<i2").astype(np.float32)
    rms = float(np.sqrt(np.mean(samples * samples)))
    normalised = min(rms / 32768.0, 1.0)
    return min(normalised * 8.0, 1.0)


def _smooth_envelope(raw_values: list[float]) -> tuple[tuple[float, ...], tuple[float, ...]]:
    fast: list[float] = []
    slow: list[float] = []
    af = 0.0
    sl = 0.0
    for raw in raw_values:
        af = _ALPHA_FAST * raw + (1.0 - _ALPHA_FAST) * af
        sl = _ALPHA_SLOW * raw + (1.0 - _ALPHA_SLOW) * sl
        fast.append(round(af, 4))
        slow.append(round(sl, 4))
    return tuple(fast), tuple(slow)


def envelope_from_pcm(pcm: bytes, *, sample_rate: int = _PCM_SAMPLE_RATE) -> AudioEnvelope:
    """Build an envelope from mono s16le PCM using vectorized NumPy slicing."""
    n_bytes = (len(pcm) // 2) * 2
    if n_bytes < 2:
        return AudioEnvelope(
            sample_rate_hz=ENVELOPE_SAMPLE_HZ,
            duration_ms=0,
            fast=(0.0,),
            slow=(0.0,),
        )

    samples_per_chunk = max(1, int(sample_rate / ENVELOPE_SAMPLE_HZ))
    all_samples = np.frombuffer(pcm[:n_bytes], dtype="<i2").astype(np.float32)
    total_samples = len(all_samples)

    # Reshape full chunks for instant vectorized RMS calculation
    n_full_chunks = total_samples // samples_per_chunk
    raw: list[float] = []
    if n_full_chunks > 0:
        trimmed = all_samples[: n_full_chunks * samples_per_chunk].reshape(n_full_chunks, samples_per_chunk)
        rms_arr = np.sqrt(np.mean(trimmed * trimmed, axis=1))
        norm_arr = np.minimum( np.minimum(rms_arr / 32768.0, 1.0) * 8.0, 1.0 )
        raw.extend(norm_arr.tolist())

    rem = all_samples[n_full_chunks * samples_per_chunk :]
    if len(rem) > 0:
        rms_tail = float(np.sqrt(np.mean(rem * rem)))
        raw.append(min(min(rms_tail / 32768.0, 1.0) * 8.0, 1.0))

    if not raw:
        raw = [0.0]

    duration_ms = int(total_samples / sample_rate * 1000)
    fast, slow = _smooth_envelope(raw)
    return AudioEnvelope(
        sample_rate_hz=ENVELOPE_SAMPLE_HZ,
        duration_ms=max(duration_ms, int(len(fast) * 1000 / ENVELOPE_SAMPLE_HZ)),
        fast=fast,
        slow=slow,
    )


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def decode_mp3_to_pcm(audio_path: Path, *, sample_rate: int = _PCM_SAMPLE_RATE) -> bytes:
    if not _ffmpeg_available():
        raise RuntimeError("ffmpeg not found — cannot decode MP3 for envelope")
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(err or f"ffmpeg failed for {audio_path}")
    return proc.stdout


def probe_duration_ms(audio_path: Path) -> int | None:
    if not shutil.which("ffprobe"):
        return None
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return None
        return int(float(proc.stdout.strip()) * 1000)
    except (ValueError, subprocess.TimeoutExpired):
        return None


def synthetic_envelope(text: str, duration_ms: int | None = None) -> AudioEnvelope:
    """Fallback envelope from text length when no audio file exists."""
    words = max(1, len(text.split()))
    if duration_ms is None:
        duration_ms = int(max(1200, words * 380 + 400))

    n = max(1, int(duration_ms * ENVELOPE_SAMPLE_HZ / 1000) + 1)
    raw: list[float] = []
    for i in range(n):
        t = i / ENVELOPE_SAMPLE_HZ
        carrier = max(0.0, math.sin(t * math.pi * 2 * 4.2)) ** 1.5
        word_period = max(0.32, duration_ms / 1000.0 / words)
        word_pulse = 0.55 + 0.45 * math.sin(t * math.pi * 2 / word_period)
        raw.append(min(1.0, carrier * word_pulse))

    fast, slow = _smooth_envelope(raw)
    return AudioEnvelope(
        sample_rate_hz=ENVELOPE_SAMPLE_HZ,
        duration_ms=duration_ms,
        fast=fast,
        slow=slow,
    )


def save_sidecar(envelope: AudioEnvelope, path: Path) -> None:
    path.write_text(json.dumps(envelope.to_dict(), indent=2), encoding="utf-8")


def load_sidecar(path: Path) -> AudioEnvelope | None:
    if not path.exists():
        return None
    try:
        return AudioEnvelope.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def build_envelope_for_audio(
    audio_path: Path,
    *,
    fallback_text: str = "",
    write_cache: bool = True,
) -> AudioEnvelope:
    """Load sidecar, or decode MP3/WAV and cache envelope next to the file."""
    sc = sidecar_path(audio_path)
    cached = load_sidecar(sc)
    if cached is not None:
        return cached

    if audio_path.exists():
        try:
            if audio_path.suffix.lower() == ".wav":
                import wave
                with wave.open(str(audio_path), "rb") as wf:
                    framerate = wf.getframerate()
                    pcm = wf.readframes(wf.getnframes())
                    envelope = envelope_from_pcm(pcm, sample_rate=framerate)
            else:
                pcm = decode_mp3_to_pcm(audio_path)
                envelope = envelope_from_pcm(pcm)
            if write_cache:
                save_sidecar(envelope, sc)
            return envelope
        except Exception as exc:
            print(f"[AudioEnvelope] Audio decode failed ({audio_path.name}): {exc}")

    duration_ms = probe_duration_ms(audio_path) if audio_path.exists() else None
    return synthetic_envelope(fallback_text, duration_ms=duration_ms)


def build_all_sidecars(audio_dir: Path) -> int:
    """Generate .amp.json sidecars for every MP3/WAV in a directory."""
    count = 0
    for audio_file in sorted(list(audio_dir.glob("*.mp3")) + list(audio_dir.glob("*.wav"))):
        sc = sidecar_path(audio_file)
        if sc.exists():
            continue
        try:
            env = build_envelope_for_audio(audio_file, write_cache=True)
            print(f"[AudioEnvelope] {audio_file.name} -> {env.duration_ms}ms ({len(env.fast)} frames)")
            count += 1
        except Exception as exc:
            print(f"[AudioEnvelope] SKIP {audio_file.name}: {exc}")
    return count
