"""Play agent TTS PCM on the Pi speakers (bypass Chromium WebRTC playout).

Enabled via config.kiosk.yaml ``voice.local_speaker: true`` or env
``VOICE_LOCAL_SPEAKER=1``. Requires::

    sudo apt install libportaudio2
    pip install sounddevice

When active, the frontend must not play agent audio (see ``/api/voice-config``).
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
_BLOCK_FRAMES = 2048  # ~85 ms @ 24 kHz — reduces ALSA underruns on Pi
_AEC_SAMPLE_RATE = 48000
_AEC_FRAME_SAMPLES = 480  # 10 ms @ 48 kHz (WebRTC APM frame size)

_enabled = False
_apm: rtc.AudioProcessingModule | None = None
_delay_estimator = None
_running = False
_stream = None
_writer: Optional[threading.Thread] = None
_pcm_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=512)
_play_buffer = bytearray()
_buf_lock = threading.Lock()
_lock = threading.Lock()


def is_enabled() -> bool:
    return _enabled


def attach_aec(
    apm: rtc.AudioProcessingModule,
    delay_estimator: object | None = None,
) -> None:
    """Wire APM reverse stream so mic AEC can cancel speaker bleed."""
    global _apm, _delay_estimator
    _apm = apm
    _delay_estimator = delay_estimator
    print("[LocalSpeaker] AEC reverse stream attached")


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
                _start(sample_rate)
            _enabled = True
        else:
            if _enabled:
                _stop()
            _enabled = False
    return _enabled


def write_pcm(data: bytes) -> None:
    """Queue 16-bit mono PCM (e.g. 24 kHz from Deepgram TTS). Non-blocking."""
    if not _enabled or not data:
        return
    try:
        _pcm_queue.put_nowait(data)
    except queue.Full:
        try:
            _pcm_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            _pcm_queue.put_nowait(data)
        except queue.Full:
            pass


def drain() -> None:
    """Flush queued speaker audio (call when agent stops speaking)."""
    if not _enabled:
        return
    while True:
        try:
            _pcm_queue.get_nowait()
        except queue.Empty:
            break
    with _buf_lock:
        _play_buffer.clear()


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
        self._flush_task = loop.create_task(self._finish_after(duration))

    def clear_buffer(self) -> None:
        self._interrupted = True
        self._started_playback = False
        self._pushed_duration = 0.0
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        drain()

    async def _finish_after(self, duration: float) -> None:
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            return
        if not self._interrupted:
            self.on_playback_finished(
                playback_position=duration,
                interrupted=False,
            )


def create_audio_output(sample_rate: int = _SAMPLE_RATE) -> LocalSpeakerAudioOutput:
    return LocalSpeakerAudioOutput(sample_rate=sample_rate)


def _feed_apm_reverse(outdata: memoryview, frames: int, time_info) -> None:
    """Feed speaker PCM into APM reverse path (48 kHz, 10 ms frames)."""
    if _apm is None or frames <= 0:
        return
    if _delay_estimator is not None and time_info is not None:
        with contextlib.suppress(Exception):
            output_delay = float(time_info.outputBufferDacTime - time_info.currentTime)
            _delay_estimator.set_output_delay(output_delay)
    try:
        import numpy as np

        pcm = bytes(outdata[: frames * 2])
        samples_24k = np.frombuffer(pcm, dtype=np.int16, count=frames)
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
    """Pull fixed-size blocks from the ring buffer (callback thread)."""
    bytes_needed = frames * _CHANNELS * 2
    with _buf_lock:
        if len(_play_buffer) >= bytes_needed:
            outdata[:bytes_needed] = _play_buffer[:bytes_needed]
            del _play_buffer[:bytes_needed]
        elif _play_buffer:
            avail = len(_play_buffer)
            outdata[:avail] = _play_buffer
            outdata[avail:bytes_needed] = b"\x00" * (bytes_needed - avail)
            _play_buffer.clear()
        else:
            outdata[:bytes_needed] = b"\x00" * bytes_needed
    _feed_apm_reverse(outdata, frames, time_info)


def _start(sample_rate: int) -> None:
    global _running, _stream, _writer, _SAMPLE_RATE
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
    device_info = sd.query_devices(kind="output")
    print(
        f"[LocalSpeaker] Output: {device_info.get('name', 'default')} "
        f"@ {sample_rate} Hz mono (block={_BLOCK_FRAMES})"
    )
    _stream = sd.RawOutputStream(
        samplerate=sample_rate,
        channels=_CHANNELS,
        dtype="int16",
        blocksize=_BLOCK_FRAMES,
        latency="high",
        callback=_audio_callback,
    )
    _stream.start()
    _running = True
    _writer = threading.Thread(target=_writer_loop, name="LocalSpeaker", daemon=True)
    _writer.start()
    print("[LocalSpeaker] Enabled — mute browser agent audio to avoid doubling")


def _stop() -> None:
    global _running, _stream, _writer
    detach_aec()
    _running = False
    try:
        _pcm_queue.put_nowait(None)
    except queue.Full:
        pass
    if _writer is not None and _writer.is_alive():
        _writer.join(timeout=2.0)
    _writer = None
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
            _play_buffer.extend(chunk)
