"""Unified Pi audio I/O with LiveKit MediaDevices AEC.

When ``voice.local_mic: true``, captures mic on the backend and feeds
``AgentSession.input.audio``. When combined with ``local_speaker``, TTS
plays through ``OutputPlayer`` so APM receives the reverse stream for
echo cancellation.

Enabled via config or env ``VOICE_LOCAL_MIC=1``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import struct
from pathlib import Path

from livekit import rtc
from livekit.agents.utils import aio
from livekit.agents.voice import io

_DEVICE_SAMPLE_RATE = 48000

_enabled_mic = False
_aec_speaker = False
_devices: rtc.MediaDevices | None = None
_input_capture = None
_output_player = None
_mic_input: LocalMicAudioInput | None = None
_pump_task: asyncio.Task | None = None


class LocalMicAudioInput(io.AudioInput):
    """AgentSession audio input fed from the Pi microphone."""

    def __init__(self) -> None:
        super().__init__(label="LocalMic")
        self._audio_ch: aio.Chan[rtc.AudioFrame] = aio.Chan()
        self._attached = True

    def push_frame(self, frame: rtc.AudioFrame) -> None:
        if not self._attached:
            return
        with contextlib.suppress(Exception):
            self._audio_ch.send_nowait(frame)

    async def __anext__(self) -> rtc.AudioFrame:
        return await self._audio_ch.__anext__()

    def on_attached(self) -> None:
        self._attached = True

    def on_detached(self) -> None:
        self._attached = False


def _env_flag(name: str) -> bool | None:
    raw = os.getenv(name, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def _from_yaml(config_path: Path, key: str) -> bool | None:
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
        if key in voice:
            return bool(voice[key])
    except Exception:
        pass
    return None


def _resolve_config_path(config_path: Path | None) -> Path:
    if config_path is not None:
        path = config_path
    else:
        raw = os.environ.get("CONFIG_PATH", "").strip()
        app_dir = Path(__file__).resolve().parent.parent
        path = Path(raw) if raw else app_dir / "config.yaml"
        if not path.is_absolute():
            path = app_dir / path
    return path


def resolve_local_mic(config_path: Path | None = None) -> bool:
    override = _env_flag("VOICE_LOCAL_MIC")
    if override is not None:
        return override
    path = _resolve_config_path(config_path)
    yaml_val = _from_yaml(path, "local_mic")
    if yaml_val is not None:
        return yaml_val
    return False


def resolve_local_speaker(config_path: Path | None = None) -> bool:
    from voice.local_speaker import resolve_enabled

    return resolve_enabled(_resolve_config_path(config_path))


def is_aec_speaker_active() -> bool:
    return _aec_speaker


def is_local_mic_active() -> bool:
    return _enabled_mic


def _upsample_24k_to_48k(pcm_24: bytes) -> bytes:
    n = len(pcm_24) // 2
    if n == 0:
        return b""
    try:
        import numpy as np

        arr = np.frombuffer(pcm_24, dtype=np.int16)
        return np.repeat(arr, 2).tobytes()
    except ImportError:
        samples = struct.unpack(f"<{n}h", pcm_24[: n * 2])
        doubled = [s for sample in samples for s in (sample, sample)]
        return struct.pack(f"<{len(doubled)}h", *doubled)


async def _pump_mic_stream(
    stream: rtc.AudioStream,
    mic_input: LocalMicAudioInput,
) -> None:
    try:
        async for event in stream:
            mic_input.push_frame(event.frame)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[LocalAudioIO] Mic pump error: {exc}")
    finally:
        with contextlib.suppress(Exception):
            await stream.aclose()


async def setup_local_audio(
    loop: asyncio.AbstractEventLoop,
    config_path: Path | None = None,
) -> tuple[LocalMicAudioInput | None, bool, bool]:
    """Open Pi mic/speaker with AEC. Returns (mic_input, use_local_mic, use_aec_speaker)."""
    global _enabled_mic, _aec_speaker, _devices, _input_capture
    global _output_player, _mic_input, _pump_task

    use_mic = resolve_local_mic(config_path)

    if not use_mic:
        return None, False, False

    try:
        _devices = rtc.MediaDevices(loop=loop)
        _input_capture = _devices.open_input(
            enable_aec=True,
            noise_suppression=True,
            high_pass_filter=True,
            auto_gain_control=True,
            queue_capacity=200,
        )
        _mic_input = LocalMicAudioInput()
        track = rtc.LocalAudioTrack.create_audio_track(
            "local_mic",
            _input_capture.source,
        )
        stream = rtc.AudioStream(
            track,
            sample_rate=_DEVICE_SAMPLE_RATE,
            num_channels=1,
        )
        _pump_task = asyncio.create_task(_pump_mic_stream(stream, _mic_input))

        # Speaker plays via local_speaker.py; optionally feed DAC into APM reverse.
        aec_reverse = True
        try:
            import yaml
            with open(config_path, encoding="utf-8") as f:
                voice_cfg = (yaml.safe_load(f) or {}).get("voice") or {}
            if "aec_reverse" in voice_cfg:
                aec_reverse = bool(voice_cfg["aec_reverse"])
        except Exception:
            pass
        env_aec = _env_flag("VOICE_AEC_REVERSE")
        if env_aec is not None:
            aec_reverse = env_aec

        if (
            aec_reverse
            and resolve_local_speaker(config_path)
            and _input_capture.apm is not None
        ):
            from voice.local_speaker import attach_aec

            attach_aec(_input_capture.apm, _input_capture.delay_estimator)
            print(
                f"[LocalAudioIO] Backend mic + AEC reverse @ {_DEVICE_SAMPLE_RATE} Hz "
                "(speaker via local_speaker.py)"
            )
        else:
            print(
                f"[LocalAudioIO] Backend mic @ {_DEVICE_SAMPLE_RATE} Hz "
                f"(aec_reverse={'on' if aec_reverse else 'off'}; "
                "speaker via local_speaker.py — mute browser mic/audio)"
            )

        _enabled_mic = True
        return _mic_input, True, False
    except Exception as exc:
        print(f"[LocalAudioIO] Setup failed, falling back to browser mic: {exc}")
        await shutdown_local_audio()
        return None, False, False

def write_speaker_pcm(data: bytes) -> None:
    """Queue 16-bit mono PCM at 24 kHz for AEC output player."""
    if not _aec_speaker or not data or _output_player is None:
        return
    upsampled = _upsample_24k_to_48k(data)
    _output_player._buffer.extend(upsampled)


def drain_speaker() -> None:
    if _output_player is not None:
        _output_player._buffer.clear()


async def shutdown_local_audio() -> None:
    global _enabled_mic, _aec_speaker, _devices, _input_capture
    global _output_player, _mic_input, _pump_task

    from voice.local_speaker import detach_aec

    detach_aec()

    if _pump_task is not None and not _pump_task.done():
        _pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _pump_task
    _pump_task = None

    if _input_capture is not None:
        with contextlib.suppress(Exception):
            await _input_capture.aclose()
    _input_capture = None

    if _output_player is not None:
        with contextlib.suppress(Exception):
            await _output_player.aclose()
    _output_player = None

    _mic_input = None
    _devices = None
    _enabled_mic = False
    _aec_speaker = False
