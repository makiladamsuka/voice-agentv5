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
import threading
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
# Soft echo gate while agent speaks (peak level ~0..1)
_echo_gate_rms = 0.035
_agent_speaking = False  # set from VoiceService — never read Blackboard per-frame
# Jobs run on separate threads; red-mic reconnect can overlap teardown.
# Generation ownership stops an old session from closing the new mic.
_audio_lock = threading.Lock()
_audio_owner_gen = 0


def current_audio_generation() -> int:
    return _audio_owner_gen


class LocalMicAudioInput(io.AudioInput):
    """AgentSession audio input fed from the Pi microphone."""

    def __init__(self) -> None:
        super().__init__(label="LocalMic")
        # Bounded channel — drop oldest on overflow instead of crashing the capture path.
        self._audio_ch: aio.Chan[rtc.AudioFrame] = aio.Chan(maxsize=128)
        self._attached = True

    def push_frame(self, frame: rtc.AudioFrame) -> None:
        if not self._attached:
            return
        # Soft gate: while agent TTS plays, drop quiet frames (speaker bleed)
        # but keep loud frames so the user can still interrupt — no mic mute.
        if _should_drop_echo_frame(frame):
            return
        try:
            self._audio_ch.send_nowait(frame)
        except Exception:
            with contextlib.suppress(Exception):
                self._audio_ch.recv_nowait()
            with contextlib.suppress(Exception):
                self._audio_ch.send_nowait(frame)

    async def __anext__(self) -> rtc.AudioFrame:
        return await self._audio_ch.__anext__()

    def on_attached(self) -> None:
        self._attached = True

    def on_detached(self) -> None:
        self._attached = False


def _frame_peak_norm(frame: rtc.AudioFrame) -> float:
    """Cheap peak level (subsampled) — safe to run on every mic frame."""
    try:
        data = memoryview(frame.data).cast("h")
    except Exception:
        return 0.0
    if not data:
        return 0.0
    peak = 0
    for i in range(0, len(data), 8):
        v = abs(int(data[i]))
        if v > peak:
            peak = v
    return peak / 32768.0


def _should_drop_echo_frame(frame: rtc.AudioFrame) -> bool:
    """True when agent is speaking and mic energy looks like TTS bleed."""
    if _echo_gate_rms <= 0 or not _agent_speaking:
        return False
    return _frame_peak_norm(frame) < _echo_gate_rms


def set_agent_speaking(speaking: bool) -> None:
    """Called from VoiceService on agent_state_changed — O(1), no I/O."""
    global _agent_speaking
    _agent_speaking = bool(speaking)


def set_echo_gate(*, bb=None, rms_threshold: float | None = None) -> None:
    global _echo_gate_rms
    # bb kept for API compat; we no longer read Blackboard per mic frame.
    if rms_threshold is not None:
        _echo_gate_rms = max(0.0, float(rms_threshold))


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
) -> tuple[LocalMicAudioInput | None, bool, bool, int]:
    """Open Pi mic. Returns (mic_input, use_local_mic, use_aec_speaker, audio_gen)."""
    global _enabled_mic, _aec_speaker, _devices, _input_capture
    global _output_player, _mic_input, _pump_task, _audio_owner_gen

    use_mic = resolve_local_mic(config_path)

    if not use_mic:
        return None, False, False, _audio_owner_gen

    # Previous session must be fully torn down — otherwise APM "handle not found"
    # and QueueFull spam on reconnect.
    if _enabled_mic or _input_capture is not None or _pump_task is not None:
        print("[LocalAudioIO] Cleaning up leftover mic capture before reopen")
        await shutdown_local_audio()
        await asyncio.sleep(0.15)

    # Read aec/echo gate config before open_input so we can skip AEC APM when unused.
    aec_reverse = True
    echo_gate = _echo_gate_rms
    try:
        import yaml

        with open(config_path, encoding="utf-8") as f:
            voice_cfg = (yaml.safe_load(f) or {}).get("voice") or {}
        if "aec_reverse" in voice_cfg:
            aec_reverse = bool(voice_cfg["aec_reverse"])
        if "echo_gate_rms" in voice_cfg:
            echo_gate = float(voice_cfg["echo_gate_rms"])
    except Exception:
        pass
    env_aec = _env_flag("VOICE_AEC_REVERSE")
    if env_aec is not None:
        aec_reverse = env_aec
    env_gate = os.getenv("VOICE_ECHO_GATE_RMS", "").strip()
    if env_gate:
        with contextlib.suppress(ValueError):
            echo_gate = float(env_gate)
    set_echo_gate(rms_threshold=echo_gate)

    try:
        devices = rtc.MediaDevices(loop=loop)
        # When reverse AEC is off, don't open WebRTC AEC — stale ApmProcessStream
        # handles cause "handle not found" floods after session end/reconnect.
        input_capture = devices.open_input(
            enable_aec=aec_reverse,
            noise_suppression=True,
            high_pass_filter=True,
            auto_gain_control=True,
            queue_capacity=400,
        )
        mic_input = LocalMicAudioInput()
        track = rtc.LocalAudioTrack.create_audio_track(
            "local_mic",
            input_capture.source,
        )
        stream = rtc.AudioStream(
            track,
            sample_rate=_DEVICE_SAMPLE_RATE,
            num_channels=1,
        )
        pump_task = asyncio.create_task(_pump_mic_stream(stream, mic_input))

        if (
            aec_reverse
            and resolve_local_speaker(config_path)
            and input_capture.apm is not None
        ):
            from voice.local_speaker import attach_aec

            attach_aec(input_capture.apm, input_capture.delay_estimator)
            print(
                f"[LocalAudioIO] Backend mic + AEC reverse @ {_DEVICE_SAMPLE_RATE} Hz "
                f"(speaker via local_speaker.py; echo_gate_rms={echo_gate})"
            )
        else:
            print(
                f"[LocalAudioIO] Backend mic @ {_DEVICE_SAMPLE_RATE} Hz "
                f"(aec_reverse={'on' if aec_reverse else 'off'}; "
                f"echo_gate_rms={echo_gate}; "
                "speaker via local_speaker.py — mute browser mic/audio)"
            )

        with _audio_lock:
            _devices = devices
            _input_capture = input_capture
            _mic_input = mic_input
            _pump_task = pump_task
            _enabled_mic = True
            _aec_speaker = False
            _audio_owner_gen += 1
            gen = _audio_owner_gen

        return mic_input, True, False, gen
    except Exception as exc:
        print(f"[LocalAudioIO] Setup failed, falling back to browser mic: {exc}")
        await shutdown_local_audio()
        return None, False, False, _audio_owner_gen


def write_speaker_pcm(data: bytes) -> None:
    """Queue 16-bit mono PCM at 24 kHz for AEC output player."""
    if not _aec_speaker or not data or _output_player is None:
        return
    upsampled = _upsample_24k_to_48k(data)
    _output_player._buffer.extend(upsampled)


def drain_speaker() -> None:
    if _output_player is not None:
        _output_player._buffer.clear()


async def shutdown_local_audio(*, owner_gen: int | None = None) -> None:
    """Tear down mic capture.

    Pass ``owner_gen`` from ``setup_local_audio`` so a stale disconnect
    cannot close a newer reconnect's devices (cross-thread job race).
    ``owner_gen=None`` forces shutdown (used before reopen).
    """
    global _enabled_mic, _aec_speaker, _devices, _input_capture
    global _output_player, _mic_input, _pump_task, _agent_speaking

    from voice.local_speaker import detach_aec

    with _audio_lock:
        if owner_gen is not None and owner_gen != _audio_owner_gen:
            print(
                f"[LocalAudioIO] Skip stale shutdown "
                f"(owner={owner_gen} current={_audio_owner_gen})"
            )
            return

        pump = _pump_task
        capture = _input_capture
        player = _output_player
        mic = _mic_input

        _agent_speaking = False
        _pump_task = None
        _input_capture = None
        _output_player = None
        _mic_input = None
        _devices = None
        _enabled_mic = False
        _aec_speaker = False

    detach_aec()

    if mic is not None:
        with contextlib.suppress(Exception):
            mic.on_detached()

    if pump is not None and not pump.done():
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump

    if capture is not None:
        with contextlib.suppress(Exception):
            await capture.aclose()

    if player is not None:
        with contextlib.suppress(Exception):
            await player.aclose()
