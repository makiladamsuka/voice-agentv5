"""SpeechSyncService — play amplitude envelopes locked to utterance clock.

Arms EyeRenderer and ServoLoop with amplitude_fast / amplitude_slow during
NLU browser playback. Envelopes come from precomputed MP3 sidecars or a
synthetic fallback for live TTS text.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING

from voice.audio_envelope import AudioEnvelope, build_envelope_for_audio

if TYPE_CHECKING:
    from core.blackboard import Blackboard

_active_service: "SpeechSyncService | None" = None


def get_speech_sync_service() -> "SpeechSyncService | None":
    return _active_service


class SpeechSyncService:
    """~50 Hz envelope player driven by utterance_start_ts on the Blackboard."""

    def __init__(self, bb: "Blackboard", *, tick_hz: float = 50.0) -> None:
        self.bb = bb
        self._tick_hz = max(20.0, tick_hz)
        self._lock = threading.Lock()
        self._armed_id: str = ""
        self._armed_envelope: AudioEnvelope | None = None
        self._active_id: str = ""
        self._active_envelope: AudioEnvelope | None = None
        self._start_ts: float = 0.0

    def arm_utterance(
        self,
        *,
        reply_text: str,
        audio_path: str | None = None,
        utterance_id: str | None = None,
    ) -> tuple[str, int]:
        """Load envelope and stage utterance; playback starts on begin_playback()."""
        uid = utterance_id or uuid.uuid4().hex[:12]
        envelope: AudioEnvelope

        if audio_path:
            path = __import__("pathlib").Path(audio_path)
            envelope = build_envelope_for_audio(path, fallback_text=reply_text)
        else:
            from voice.audio_envelope import synthetic_envelope

            envelope = synthetic_envelope(reply_text)

        with self._lock:
            self._armed_id = uid
            self._armed_envelope = envelope
            self._active_id = ""
            self._active_envelope = None
            self._start_ts = 0.0

        self.bb.write(
            utterance_id=uid,
            utterance_start_ts=0.0,
            utterance_duration_ms=envelope.duration_ms,
            utterance_elapsed_ms=0,
            utterance_audio_path=audio_path or "",
            speech_sync_active=False,
            amplitude_fast=0.0,
            amplitude_slow=0.0,
        )
        return uid, envelope.duration_ms

    def begin_playback(self, utterance_id: str) -> bool:
        """Align clock to browser audio start."""
        with self._lock:
            if utterance_id and utterance_id != self._armed_id:
                print(
                    f"[SpeechSync] playback_start id mismatch "
                    f"(got {utterance_id}, armed {self._armed_id})"
                )
            if self._armed_envelope is None:
                return False
            self._active_id = self._armed_id
            self._active_envelope = self._armed_envelope
            self._start_ts = time.time()
            self._armed_id = ""
            self._armed_envelope = None

        self.bb.write(
            utterance_id=self._active_id,
            utterance_start_ts=self._start_ts,
            speech_sync_active=True,
            agent_speaking=True,
            conv_state="speaking",
        )
        print(
            f"[SpeechSync] playback started id={self._active_id} "
            f"duration={self._active_envelope.duration_ms}ms"
        )
        return True

    def end_playback(self) -> None:
        """Stop envelope playback and decay amplitude."""
        with self._lock:
            self._active_id = ""
            self._active_envelope = None
            self._start_ts = 0.0

        self.bb.write(
            speech_sync_active=False,
            agent_speaking=False,
            utterance_elapsed_ms=0,
            utterance_start_ts=0.0,
            utterance_id="",
            utterance_duration_ms=0,
            utterance_audio_path="",
            amplitude_fast=0.0,
            amplitude_slow=0.0,
        )

    def _tick(self) -> None:
        with self._lock:
            envelope = self._active_envelope
            start_ts = self._start_ts
            active_id = self._active_id

        if envelope is None or not start_ts:
            return

        elapsed_ms = int((time.time() - start_ts) * 1000)
        fast, slow = envelope.sample_at_ms(elapsed_ms)

        if elapsed_ms > envelope.duration_ms + 500:
            self.end_playback()
            return

        self.bb.write(
            utterance_id=active_id,
            utterance_elapsed_ms=elapsed_ms,
            amplitude_fast=round(fast, 4),
            amplitude_slow=round(slow, 4),
            speech_sync_active=True,
            agent_speaking=True,
        )

    def run(self) -> None:
        global _active_service
        _active_service = self
        delay = 1.0 / self._tick_hz
        print(f"[SpeechSync] Service started ({self._tick_hz:.0f} Hz)")
        try:
            while self.bb.read("running")["running"]:
                if self.bb.read("speech_sync_active")["speech_sync_active"]:
                    self._tick()
                time.sleep(delay)
        finally:
            self.end_playback()
            _active_service = None
            print("[SpeechSync] Service stopped")
