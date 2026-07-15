"""Play agent TTS PCM on the Pi speakers (bypass Chromium WebRTC playout).

Enabled via config.kiosk.yaml ``voice.local_speaker: true`` or env
``VOICE_LOCAL_SPEAKER=1``. Requires::

    sudo apt install libportaudio2
    pip install sounddevice

When active, the frontend must not play agent audio (see ``/api/voice-config``).

Realtime path note
------------------
The PortAudio callback only memcpy's from ``_play_buffer``. AEC reverse-stream
processing runs on a separate worker thread so the DAC deadline is not missed
(main cause of TTS jitter at moderate CPU%).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import queue
import threading
import time
from pathlib import Path
from typing import Optional

from livekit import rtc
from livekit.agents.voice.io import AudioOutput, AudioOutputCapabilities

_SAMPLE_RATE = 24000
_CHANNELS = 1
# Prefer larger blocks + high latency on Pi (Pulse/ALSA): low-latency was underrunning
_BLOCK_FRAMES = 2048
_LATENCY = "high"
_AEC_SAMPLE_RATE = 48000
_AEC_FRAME_SAMPLES = 480  # 10 ms @ 48 kHz
# Soft cap on DAC staging buffer (~300 ms of int16 mono @ 24 kHz)
_MAX_PLAY_BYTES = int(_SAMPLE_RATE * 2 * 0.30)
_PORTAUDIO_LATENCY_FUDGE_SEC = 0.12
_OUTPUT_DEVICE = None  # sounddevice device index or None=default
_OUTPUT_DEVICE_NAME = ""

_enabled = False
_apm: rtc.AudioProcessingModule | None = None
_delay_estimator = None
_running = False
_stream = None
_writer: Optional[threading.Thread] = None
_aec_worker: Optional[threading.Thread] = None
_pcm_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=256)
_aec_queue: queue.Queue[tuple[bytes, float] | None] = queue.Queue(maxsize=64)
_play_buffer = bytearray()
_buf_lock = threading.Lock()
_lock = threading.Lock()
_drop_newest_count = 0


def is_enabled() -> bool:
    return _enabled


def buffered_seconds() -> float:
    """Queued + staged PCM still waiting to hit the DAC."""
    queued = 0
    with contextlib.suppress(Exception):
        for item in list(_pcm_queue.queue):
            if isinstance(item, (bytes, bytearray)):
                queued += len(item)
    with _buf_lock:
        staged = len(_play_buffer)
    return (queued + staged) / float(_SAMPLE_RATE * _CHANNELS * 2)


def attach_aec(
    apm: rtc.AudioProcessingModule,
    delay_estimator: object | None = None,
) -> None:
    """Wire APM reverse stream so mic AEC can cancel speaker bleed."""
    global _apm, _delay_estimator
    _apm = apm
    _delay_estimator = delay_estimator
    print("[LocalSpeaker] AEC reverse stream attached (async worker)")


def detach_aec() -> None:
    global _apm, _delay_estimator
    _apm = None
    _delay_estimator = None


def _env_override() -> bool | None:
    raw = os.getenv("VOICE_LOCAL_SPEAKER", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def _from_yaml(config_path: Path) -> bool | None:
    try:
        import yaml
    except ImportError:
        return None
    if not config_path.is_file():
        return None
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        voice = cfg.get("voice") or {}
        if "local_speaker" in voice:
            return bool(voice["local_speaker"])
    except Exception:
        pass
    return None


def resolve_enabled(config_path: Path | None = None) -> bool:
    override = _env_override()
    if override is not None:
        return override
    if config_path is not None:
        yaml_val = _from_yaml(config_path)
        if yaml_val is not None:
            return yaml_val
    return False


def _load_speaker_settings() -> None:
    """Read device/blocksize/latency from CONFIG_PATH voice: section."""
    global _BLOCK_FRAMES, _LATENCY, _OUTPUT_DEVICE, _OUTPUT_DEVICE_NAME
    raw = os.environ.get("CONFIG_PATH", "").strip()
    app_dir = Path(__file__).resolve().parent.parent
    path = Path(raw) if raw else app_dir / "config.yaml"
    if not path.is_absolute():
        path = app_dir / path
    prefer = "headphones"
    try:
        import yaml
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                voice = (yaml.safe_load(f) or {}).get("voice") or {}
            prefer = str(voice.get("speaker_device", prefer) or prefer).lower()
            if "speaker_blocksize" in voice:
                _BLOCK_FRAMES = max(256, int(voice["speaker_blocksize"]))
            lat = str(voice.get("speaker_latency", _LATENCY) or _LATENCY).lower()
            if lat in ("low", "high"):
                _LATENCY = lat
    except Exception:
        pass
    env_dev = os.getenv("VOICE_SPEAKER_DEVICE", "").strip().lower()
    if env_dev:
        prefer = env_dev
    _OUTPUT_DEVICE, _OUTPUT_DEVICE_NAME = _pick_output_device(prefer)


def _pick_output_device(prefer: str) -> tuple[int | None, str]:
    """Prefer jack headphones over PipeWire 'default' (resampling jitter)."""
    try:
        import sounddevice as sd
    except ImportError:
        return None, "default"
    devices = sd.query_devices()
    prefer = (prefer or "headphones").lower()
    scored: list[tuple[int, int, str]] = []
    for i, d in enumerate(devices):
        if int(d.get("max_output_channels") or 0) <= 0:
            continue
        name = str(d.get("name") or "")
        low = name.lower()
        score = 0
        if prefer in low or (prefer == "headphones" and "headphone" in low):
            score += 100
        if "bcm2835 headphones" in low:
            score += 50
        if "headphones" in low:
            score += 40
        if low in ("default", "pulse") or "pipewire" in low:
            score -= 50
        if "hdmi" in low:
            score -= 20
        scored.append((score, i, name))
    if not scored:
        return None, "default"
    scored.sort(reverse=True)
    best_score, best_i, best_name = scored[0]
    if best_score <= 0:
        return None, "default"
    return best_i, best_name


def init_from_config(config_path: Path | None = None) -> bool:
    """Call once at voice worker startup."""
    path = config_path
    if path is None:
        raw = os.environ.get("CONFIG_PATH", "").strip()
        app_dir = Path(__file__).resolve().parent.parent
        path = Path(raw) if raw else app_dir / "config.yaml"
        if not path.is_absolute():
            path = app_dir / path
    enabled = resolve_enabled(path)
    return configure(enabled=enabled)


def configure(*, enabled: bool, sample_rate: int = _SAMPLE_RATE) -> bool:
    global _enabled, _SAMPLE_RATE
    with _lock:
        if enabled:
            if not _enabled:
                _load_speaker_settings()
                _start(sample_rate)
            _enabled = True
        else:
            if _enabled:
                _stop()
            _enabled = False
    return _enabled


def write_pcm(data: bytes) -> None:
    """Queue 16-bit mono PCM (e.g. 24 kHz from Deepgram TTS). Non-blocking."""
    global _drop_newest_count
    if not _enabled or not data:
        return
    try:
        _pcm_queue.put_nowait(data)
    except queue.Full:
        # Drop newest (keep older continuity) — count for diagnostics
        _drop_newest_count += 1
        if _drop_newest_count % 25 == 1:
            print(f"[LocalSpeaker] PCM queue full — dropped newest ({_drop_newest_count})")


def drain() -> None:
    """Hard-flush queued speaker audio (interrupt / clear only)."""
    if not _enabled:
        return
    while True:
        try:
            _pcm_queue.get_nowait()
        except queue.Empty:
            break
    with _buf_lock:
        _play_buffer.clear()
    while True:
        try:
            _aec_queue.get_nowait()
        except queue.Empty:
            break


def shutdown() -> None:
    configure(enabled=False)


class LocalSpeakerAudioOutput(AudioOutput):
    """AgentSession audio sink — drives TTS synthesis and plays on Pi speakers."""

    def __init__(self, sample_rate: int = _SAMPLE_RATE) -> None:
        super().__init__(
            label="LocalSpeaker",
            next_in_chain=None,
            sample_rate=sample_rate,
            capabilities=AudioOutputCapabilities(pause=False),
        )
        self._pushed_duration = 0.0
        self._flush_task: asyncio.Task[None] | None = None
        self._interrupted = False
        self._started_playback = False

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        if not _enabled:
            return
        write_pcm(frame.data.tobytes())
        self._pushed_duration += frame.duration
        if not self._started_playback:
            self._started_playback = True
            self.on_playback_started(created_at=time.time())

    def flush(self) -> None:
        super().flush()
        self._started_playback = False
        if self._pushed_duration <= 0:
            return
        duration = self._pushed_duration
        self._pushed_duration = 0.0
        self._interrupted = False
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        loop = asyncio.get_event_loop()
        self._flush_task = loop.create_task(self._finish_when_drained(duration))

    def clear_buffer(self) -> None:
        self._interrupted = True
        self._started_playback = False
        self._pushed_duration = 0.0
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        drain()

    async def _finish_when_drained(self, duration: float) -> None:
        """Wait until staged PCM is actually drained (not just sleep(duration))."""
        deadline = time.monotonic() + duration + 2.0
        try:
            while time.monotonic() < deadline:
                if self._interrupted:
                    return
                if buffered_seconds() <= 0.02:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(_PORTAUDIO_LATENCY_FUDGE_SEC)
        except asyncio.CancelledError:
            return
        if not self._interrupted:
            self.on_playback_finished(
                playback_position=duration,
                interrupted=False,
            )


def create_audio_output(sample_rate: int = _SAMPLE_RATE) -> LocalSpeakerAudioOutput:
    return LocalSpeakerAudioOutput(sample_rate=sample_rate)


def _process_aec_pcm(pcm_24k: bytes, output_delay: float) -> None:
    """AEC reverse-stream — worker thread only, never PortAudio callback."""
    if _apm is None or not pcm_24k:
        return
    if _delay_estimator is not None and output_delay >= 0.0:
        with contextlib.suppress(Exception):
            _delay_estimator.set_output_delay(output_delay)
    try:
        import numpy as np

        n = len(pcm_24k) // 2
        samples_24k = np.frombuffer(pcm_24k, dtype=np.int16, count=n)
        samples_48k = np.repeat(samples_24k, 2)
        for start in range(0, len(samples_48k), _AEC_FRAME_SAMPLES):
            chunk = samples_48k[start : start + _AEC_FRAME_SAMPLES]
            if len(chunk) < _AEC_FRAME_SAMPLES:
                break
            frame = rtc.AudioFrame(
                chunk.tobytes(),
                _AEC_SAMPLE_RATE,
                _CHANNELS,
                _AEC_FRAME_SAMPLES,
            )
            with contextlib.suppress(Exception):
                _apm.process_reverse_stream(frame)
    except Exception:
        pass


def _audio_callback(outdata, frames, time_info, _status) -> None:
    """Realtime callback: memcpy only. AEC is queued for the worker."""
    bytes_needed = frames * _CHANNELS * 2
    with _buf_lock:
        if len(_play_buffer) >= bytes_needed:
            chunk = bytes(_play_buffer[:bytes_needed])
            del _play_buffer[:bytes_needed]
        elif _play_buffer:
            avail = len(_play_buffer)
            chunk = bytes(_play_buffer) + (b"\x00" * (bytes_needed - avail))
            _play_buffer.clear()
        else:
            chunk = b"\x00" * bytes_needed
    outdata[:bytes_needed] = chunk

    if _apm is not None:
        output_delay = -1.0
        if time_info is not None:
            with contextlib.suppress(Exception):
                output_delay = float(
                    time_info.outputBufferDacTime - time_info.currentTime
                )
        try:
            _aec_queue.put_nowait((chunk, output_delay))
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                _aec_queue.get_nowait()
            with contextlib.suppress(queue.Full):
                _aec_queue.put_nowait((chunk, output_delay))


def _aec_loop() -> None:
    while _running:
        try:
            item = _aec_queue.get(timeout=0.25)
        except queue.Empty:
            continue
        if item is None:
            break
        pcm, delay = item
        _process_aec_pcm(pcm, delay)


def _start(sample_rate: int) -> None:
    global _running, _stream, _writer, _aec_worker, _SAMPLE_RATE, _drop_newest_count
    if _running:
        return
    try:
        import sounddevice as sd
    except ImportError as exc:
        print(
            "[LocalSpeaker] sounddevice not installed — "
            "pip install sounddevice && sudo apt install libportaudio2"
        )
        raise RuntimeError("sounddevice required for local speaker") from exc

    _SAMPLE_RATE = sample_rate
    _drop_newest_count = 0
    device = _OUTPUT_DEVICE
    label = _OUTPUT_DEVICE_NAME or "default"
    print(
        f"[LocalSpeaker] Output: {label} (dev={device}) "
        f"@ {sample_rate} Hz mono (block={_BLOCK_FRAMES}, latency={_LATENCY})"
    )
    try:
        _stream = sd.RawOutputStream(
            samplerate=sample_rate,
            channels=_CHANNELS,
            dtype="int16",
            blocksize=_BLOCK_FRAMES,
            latency=_LATENCY,
            device=device,
            callback=_audio_callback,
        )
        _stream.start()
    except Exception as exc:
        print(f"[LocalSpeaker] Device {label!r} failed ({exc}); falling back to default")
        _stream = sd.RawOutputStream(
            samplerate=sample_rate,
            channels=_CHANNELS,
            dtype="int16",
            blocksize=_BLOCK_FRAMES,
            latency=_LATENCY,
            callback=_audio_callback,
        )
        _stream.start()
    _running = True
    _writer = threading.Thread(target=_writer_loop, name="LocalSpeaker", daemon=True)
    _writer.start()
    _aec_worker = threading.Thread(target=_aec_loop, name="LocalSpeakerAEC", daemon=True)
    _aec_worker.start()
    print("[LocalSpeaker] Enabled — mute browser agent audio to avoid doubling")


def _stop() -> None:
    global _running, _stream, _writer, _aec_worker
    detach_aec()
    _running = False
    with contextlib.suppress(queue.Full):
        _pcm_queue.put_nowait(None)
    with contextlib.suppress(queue.Full):
        _aec_queue.put_nowait(None)
    if _writer is not None and _writer.is_alive():
        _writer.join(timeout=2.0)
    _writer = None
    if _aec_worker is not None and _aec_worker.is_alive():
        _aec_worker.join(timeout=2.0)
    _aec_worker = None
    drain()
    if _stream is not None:
        with contextlib.suppress(Exception):
            _stream.stop()
            _stream.close()
        _stream = None


def _writer_loop() -> None:
    while _running:
        try:
            chunk = _pcm_queue.get(timeout=0.25)
        except queue.Empty:
            continue
        if chunk is None:
            break
        with _buf_lock:
            room = _MAX_PLAY_BYTES - len(_play_buffer)
            if room <= 0:
                # Cap staging delay — drop oldest buffered audio to make room
                drop = min(len(chunk), len(_play_buffer))
                if drop > 0:
                    del _play_buffer[:drop]
                room = _MAX_PLAY_BYTES - len(_play_buffer)
            if room > 0:
                _play_buffer.extend(chunk[:room])
