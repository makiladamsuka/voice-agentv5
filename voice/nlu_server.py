"""NLU WebSocket Server — LiveKit-free voice pipeline for kiosk.

Replaces voice_service.py (LiveKit) entirely.
The Next.js frontend captures microphone audio, sends it to Deepgram directly,
and then forwards the final transcript to this server via a local WebSocket.
This server runs NLU intent matching and replies with cached audio paths
and UI actions back to the frontend.

Pi4 process isolation:
  - Runs in its own daemon thread (started from start_robot.py).
  - Uses asyncio internally but only on its own private event loop.
  - Never blocks the main process, servo loop, or face tracker.

Note on FastAPI vs Starlette:
  FastAPI 0.139 introduced new router internals that cause WebSocket 403
  responses on some setups. We use plain Starlette here which works reliably
  and is sufficient for a simple WS endpoint + health check.
"""

from __future__ import annotations

import asyncio
import json
import logging
import hashlib
import threading
import time
import random
import re
import urllib.request
import os
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv
from openai import OpenAI

from voice.sentiment import clear_conv_emotion, write_conv_emotion
from core.speech_sync_service import get_speech_sync_service

if TYPE_CHECKING:
    from core.blackboard import Blackboard

APP_DIR = Path(__file__).resolve().parent.parent
load_dotenv(APP_DIR / ".env")

log = logging.getLogger("NluServer")

# ── Module-level state ────────────────────────────────────────────────────────
_bb: "Blackboard | None" = None
_runtime = None  # OfflineVoiceRuntime, lazy-loaded on first connection
_wayfinder = None  # Wayfinder, injected from start_robot.py (or lazy-loaded)
_nlu_ready: bool = False
_nlu_error: bool = False


_nlu_lock = threading.Lock()

def _get_runtime():
    """Load or return the NLU runtime (SentenceTransformer weights pre-warmed)."""
    global _runtime, _nlu_ready
    with _nlu_lock:
        if _runtime is None:
            from voice.offline_voice.runtime import OfflineVoiceRuntime
            log.info("Loading NLU runtime (NumPy fast-path matcher + SentenceTransformer weights)...")
            _runtime = OfflineVoiceRuntime(_bb)
            _nlu_ready = True
            log.info("NLU runtime ready.")
        return _runtime

def prewarm_nlu_runtime():
    """Warm up the NLU runtime safely at server startup."""
    try:
        _get_runtime()
    except Exception as e:
        log.warning(f"NLU runtime pre-warming deferred to first request: {e}")


def _get_wayfinder():
    """Return the injected Wayfinder, lazy-loading one if none was provided."""
    global _wayfinder
    if _wayfinder is None:
        try:
            from voice.wayfinding import Wayfinder
            log.info("Loading Wayfinder (no instance was injected)...")
            _wayfinder = Wayfinder()
        except Exception as exc:
            log.error(f"Wayfinder unavailable: {exc}")
    return _wayfinder

def reload_nlu_runtime():
    """Clear the cached runtime and system prompt so they reload from disk on the next request."""
    global _runtime, _campus_system_prompt
    _runtime = None
    _campus_system_prompt = None
    log.info("NLU runtime cache cleared (will reload on next request).")


# ── Starlette WebSocket application ──────────────────────────────────────────

def get_dest_category(d: str) -> str:
    dl = d.lower()
    if "auditorium" in dl:
        return "auditorium"
    if "laboratory" in dl or "labratory" in dl or "lab" in dl:
        return "laboratory"
    if "lecture hall" in dl or "lecturehall" in dl or "lh" in dl:
        return "lecture hall"
    if "washroom" in dl or "toilet" in dl or "bathroom" in dl:
        return "washroom"
    if "department" in dl or "departement" in dl:
        return "department"
    if "office" in dl:
        return "office"
    return "location"


_NEW_TOPIC_RE = re.compile(
    r"\b("
    r"what|who|when|why|how|tell me|explain|describe"
    r"|fit expo|anniversary|team nema|nema|university|faculty|uom|fit"
    r"|hi|hello|hey|thanks|thank you|bye|goodbye|joke|fact"
    r")\b",
    re.IGNORECASE,
)


def _should_apply_ambiguity_context(pending_cat: str | None, user_text: str) -> bool:
    """Return True only if *user_text* looks like a short disambiguation answer (e.g. '1', 'two', 'male')
    rather than a brand-new question."""
    if not pending_cat or not user_text:
        return False
    stripped = user_text.strip()
    words = stripped.split()
    if len(words) > 4:
        return False
    if _NEW_TOPIC_RE.search(stripped):
        return False
    return True


async def _voice_ws_endpoint(websocket) -> None:
    """
    WebSocket endpoint for the browser voice agent.

    Browser sends:
        {"type": "transcript", "text": "Where is the library?"}
        {"type": "ping"}

    Server replies:
        {
            "type": "response",
            "reply_text": "The library is on floor 2.",
            "audio_url": "/audio_cache/event_3_poster.mp3",  # or null
            "action": {"action": "show_event_poster", "target": "poster.jpg"}
        }
        {"type": "state", "conv_state": "listening"}
    """
    await websocket.accept()
    log.info("Browser connected to NLU voice server.")
    
    speculative_cache = {}
    pending_ambiguity_category = None
    last_discussed_topic = None
    conv_history: list[dict[str, str]] = []
    # ── Echo / duplicate suppression ──────────────────────────────────────
    # Use a mutable dict so reassignment works correctly in all scopes.
    import time as _time
    _echo = {
        "norm": "",
        "recent_replies": [],
        "ts": 0.0,
        "suppress_sec": 10.0,
        "speaking": False,
    }
    # ── Speculative throttle ──────────────────────────────────────────────
    _spec = {"last_norm": "", "last_ts": 0.0, "min_interval": 1.0}

    def _normalize_text(t: str) -> str:
        cleaned = re.sub(r'[^a-z0-9 ]+', '', (t or "").lower())
        return ' '.join(cleaned.split())

    if _bb is not None:
        _bb.write(voice_session_active=True, conv_state="listening")

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            if msg_type == "speculative_transcript":
                if not _nlu_ready or _nlu_error:
                    continue  # Ignore interim transcripts while weights are still loading
                user_text = msg.get("text", "").strip()
                if user_text:
                    if _should_apply_ambiguity_context(pending_ambiguity_category, user_text):
                        cat_lower = pending_ambiguity_category.lower()
                        orig_lower = user_text.lower().strip()
                        if not (orig_lower.startswith(cat_lower) or cat_lower in orig_lower):
                            user_text = f"{pending_ambiguity_category} {user_text}"
                    norm_text = _normalize_text(user_text)

                    # ── Tier-1 Check Only for Speculative Transcripts (0.01ms cost) ──
                    # Skip expensive multi-domain ChromaDB vector searches on interim fragments.
                    runtime = _get_runtime()
                    exact_intent = runtime.matcher.match_exact(user_text)
                    if not exact_intent:
                        continue

                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(
                        None, _match_intent, runtime, user_text, conv_history
                    )
                    
                    # Prevent caching fallbacks for partial transcripts!
                    is_fallback = result and result.get("is_fallback")
                    if result and not is_fallback:
                        speculative_cache[norm_text] = result
                        if len(speculative_cache) > 50:
                            speculative_cache.clear()
                        
                        # Generate TTS in the background so it's ready for the final turn
                        reply_text = result.get("reply_text")
                        audio_url = result.get("audio_url")
                        
                        if not audio_url and reply_text:
                            audio_file = await loop.run_in_executor(
                                None, _generate_dynamic_tts, reply_text
                            )
                            if audio_file:
                                audio_url = f"/assets/audio_cache/{audio_file}"
                                
                        if audio_url:
                            await websocket.send_text(json.dumps({
                                "type": "speculative_preload",
                                "audio_url": audio_url
                            }))
                continue

            elif msg_type == "transcript":
                # Wait asynchronously if the server is still loading weights in the background
                if not _nlu_ready:
                    print("[NLU] Waiting for background NLU pre-warm to finish...")
                while not _nlu_ready:
                    if _nlu_error:
                        await websocket.send_text(json.dumps({"type": "state", "conv_state": "error"}))
                        break
                    await websocket.send_text(json.dumps({"type": "state", "conv_state": "connecting"}))
                    await asyncio.sleep(0.5)
                
                if _nlu_error:
                    continue

                original_text = msg.get("text", "").strip()
                if not original_text:
                    continue

                # ── Duplicate / echo suppression ──────────────────────────
                # If the robot is currently speaking OR we are in the echo suppression window,
                # reject incoming transcripts that match the previous user query or the robot's reply.
                _norm_incoming = _normalize_text(original_text)
                _now = _time.monotonic()
                _dt = _now - _echo["ts"]
                is_duplicate_prompt = (_norm_incoming == _echo.get("norm", ""))
                
                recent_replies = _echo.get("recent_replies", [])
                is_reply_echo = False
                for rep in recent_replies:
                    if not rep or len(_norm_incoming) < 3: continue
                    if _norm_incoming in rep or rep in _norm_incoming:
                        is_reply_echo = True
                        break
                    inc_words = set(_norm_incoming.split())
                    rep_words = set(rep.split())
                    if len(inc_words) >= 2 and (len(inc_words & rep_words) / len(inc_words)) >= 0.6:
                        is_reply_echo = True
                        break

                if _echo["speaking"] or (_dt < _echo["suppress_sec"]):
                    if is_duplicate_prompt or is_reply_echo:
                        print(f"[NLU] Echo suppressed (speaking={_echo['speaking']}, {_dt:.1f}s < {_echo['suppress_sec']:.1f}s): '{original_text}'")
                        await websocket.send_text(json.dumps({"type": "state", "conv_state": "speaking" if _echo["speaking"] else "listening"}))
                        continue

                _echo["norm"] = _norm_incoming
                _echo["ts"] = _now
                # Short dedupe only until the reply sets the real playback suppress window.
                # Avoid attaching a stale long suppress_sec to this new accept timestamp.
                _echo["suppress_sec"] = 0.8

                log.info(f"[NLU] Transcript: '{original_text}'")
                print(f"[NLU] Transcript: '{original_text}'")

                user_text = original_text
                if _should_apply_ambiguity_context(pending_ambiguity_category, original_text):
                    cat_lower = pending_ambiguity_category.lower()
                    orig_lower = original_text.lower().strip()
                    if orig_lower.startswith(cat_lower) or cat_lower in orig_lower:
                        user_text = original_text
                    else:
                        user_text = f"{pending_ambiguity_category} {original_text}"
                    print(f"  [Context injected] Rewrote query to: '{user_text}'")
                else:
                    pending_ambiguity_category = None
                    if last_discussed_topic:
                        replaced = re.sub(r'\b(it|there|that)\b', last_discussed_topic, original_text, flags=re.IGNORECASE)
                        if replaced.lower() != original_text.lower():
                            user_text = replaced
                            print(f"  [Context injected] Rewrote pronoun query to: '{user_text}'")

                # Update blackboard — show "thinking" on robot face
                think_start_ts = _time.monotonic()
                if _bb is not None:
                    _bb.write(conv_state="thinking", user_speaking=False, conv_emotion=None)
                await websocket.send_text(
                    json.dumps({"type": "state", "conv_state": "thinking"})
                )

                norm_text = _normalize_text(user_text)
                loop = asyncio.get_running_loop()
                
                # Only use exact matches for speculative cache to avoid substring bugs
                # (e.g., 'laboratory 1' in 'laboratory 10')
                result = speculative_cache.pop(norm_text, None)
                speculative_cache.clear()

                if result:
                    log.info("  [NLU] Speculative cache hit!")
                    print("  [NLU] Speculative cache hit!")
                else:
                    # Run NLU in a thread so we don't block the asyncio event loop
                    runtime = _get_runtime()
                    result = await loop.run_in_executor(
                        None, _match_intent, runtime, user_text, conv_history
                    )

                if result:
                    is_fallback = result.get("is_fallback")
                    
                    if is_fallback and not pending_ambiguity_category and last_discussed_topic:
                        retry_text = f"{last_discussed_topic} {original_text}"
                        print(f"  [Context retry] Retrying fallback query as: '{retry_text}'")
                        runtime = _get_runtime()
                        loop = asyncio.get_running_loop()
                        retry_result = await loop.run_in_executor(
                            None, _match_intent, runtime, retry_text, conv_history
                        )
                        if retry_result and not retry_result.get("is_fallback"):
                            result = retry_result
                            speculative_cache[_normalize_text(original_text)] = result

                    pending_ambiguity_category = result.get("ambiguity_category")
                    
                    action = result.get("action", {})
                    if action.get("action") == "navigate" and action.get("destination"):
                        last_discussed_topic = action.get("destination")
                    elif not is_fallback:
                        # Clear context if user successfully changed topics
                        last_discussed_topic = None

                if not result:
                    log.warning("[NLU] _match_intent returned None — skipping response.")
                    await websocket.send_text(json.dumps({"type": "state", "conv_state": "listening"}))
                    continue

                reply_str = result.get("reply_text", "")
                if original_text and reply_str:
                    conv_history.append({"role": "user", "content": original_text})
                    conv_history.append({"role": "assistant", "content": reply_str})
                    if len(conv_history) > 6:
                        conv_history[:] = conv_history[-6:]

                print(
                    f"[NLU] Reply: '{reply_str[:80]}' "
                    f"audio={result.get('audio_url')}"
                )

                # Ensure dynamic TTS audio is generated if missing
                if not result.get("audio_url") and result.get("reply_text"):
                    audio_file = await loop.run_in_executor(
                        None, _generate_dynamic_tts, result["reply_text"]
                    )
                    if audio_file:
                        result["audio_url"] = f"/assets/audio_cache/{audio_file}"
                        result["audio_path"] = str(APP_DIR / "assets" / "audio_cache" / audio_file)

                # Minimal 80ms transition so UI registers state change without adding lag
                elapsed_think = _time.monotonic() - think_start_ts
                if elapsed_think < 0.08:
                    await asyncio.sleep(0.08 - elapsed_think)

                write_conv_emotion(
                    _bb,
                    result.get("reply_text", ""),
                    is_agent=True,
                    log_prefix="Vader NLU",
                )

                utterance_id = ""
                duration_ms = 0
                sync = get_speech_sync_service()
                if sync is not None:
                    utterance_id, duration_ms = await loop.run_in_executor(
                        None,
                        lambda: sync.arm_utterance(
                            reply_text=result.get("reply_text", ""),
                            audio_path=result.get("audio_path"),
                        ),
                    )

                # Immediately set Blackboard conv_state="speaking" so robot face switches to talking emotions
                if _bb is not None:
                    _bb.write(conv_state="speaking", agent_speaking=True)

                _dur_sec = (duration_ms / 1000.0) if duration_ms else 6.0
                _reply_norm = _normalize_text(result.get("reply_text", ""))
                if _reply_norm:
                    _echo["recent_replies"] = [_reply_norm] + _echo.get("recent_replies", [])[:2]
                _echo["ts"] = _time.monotonic()
                # Suppress window = audio duration + 2s tail, minimum 4s.
                _echo["suppress_sec"] = max(4.0, _dur_sec + 2.0)
                _echo["speaking"] = True

                await websocket.send_text(json.dumps({
                    "type": "response",
                    "reply_text": result["reply_text"],
                    "audio_url": result.get("audio_url"),
                    "action": result.get("action"),
                    "utterance_id": utterance_id,
                    "duration_ms": duration_ms,
                }))

                await websocket.send_text(
                    json.dumps({"type": "state", "conv_state": "speaking"})
                )

            elif msg_type == "playback_start":
                _echo["speaking"] = True
                _echo["ts"] = _time.monotonic()
                utterance_id = str(msg.get("utterance_id", "") or "")
                sync = get_speech_sync_service()
                if sync is not None:
                    sync.begin_playback(utterance_id)
                if _bb is not None:
                    _bb.write(conv_state="speaking", agent_speaking=True)

            elif msg_type == "tts_done":
                _echo["speaking"] = False
                _echo["ts"] = _time.monotonic()
                _echo["suppress_sec"] = 1.5
                sync = get_speech_sync_service()
                if sync is not None:
                    sync.end_playback()
                if _bb is not None:
                    _bb.write(conv_state="waiting", agent_speaking=False, user_speaking=False)
                await websocket.send_text(
                    json.dumps({"type": "state", "conv_state": "waiting"})
                )

            elif msg_type == "user_speaking":
                speculative_cache.clear()
                # Ignore browser VAD user_speaking while agent is speaking or in echo suppression
                if _echo.get("speaking") or (_bb and _bb.read("conv_state").get("conv_state") == "speaking"):
                    print("[NLU] Ignoring user_speaking during agent speech (speaker echo suppression)")
                    continue
                # Browser VAD detected speech start — update robot face
                if _bb is not None:
                    _bb.write(conv_state="listening", user_speaking=True)

    except Exception as exc:
        # WebSocketDisconnect or any other error
        if "disconnect" in type(exc).__name__.lower() or "1000" in str(exc) or "1001" in str(exc):
            log.info("Browser disconnected from NLU server.")
        else:
            log.error(f"NLU WebSocket error: {exc}")
    finally:
        sync = get_speech_sync_service()
        if sync is not None:
            sync.end_playback()
        if _bb is not None:
            _bb.write(
                voice_session_active=False,
                conv_state="idle",
                agent_speaking=False,
                user_speaking=False,
            )
            clear_conv_emotion(_bb)


async def _health_endpoint(request) -> None:
    """Simple JSON health check for use by the smoke test and monitoring."""
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "mode": "nlu", "ready": _nlu_ready})


async def _reload_nlu_endpoint(request) -> None:
    """POST /reload-nlu — clears the cached runtime so it reloads from disk.

    Called by the Knowledge Base admin page after saving new intents.
    Also deletes the numpy embedding cache so the new knowledge domain
    embeddings are rebuilt on the next request.
    """
    from starlette.responses import JSONResponse
    reload_nlu_runtime()
    # Delete numpy cache files so the matcher rebuilds with new intents
    db_dir = APP_DIR / "voice" / "event_db"
    for cache_file in ("embeddings_cache.npy", "embeddings_meta.json"):
        p = db_dir / cache_file
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass
    log.info("[NluServer] /reload-nlu: cache cleared, will rebuild on next query.")
    return JSONResponse({"status": "ok", "message": "NLU runtime reloading."})


_greet_ws_clients: set = set()
_last_processed_greet_seq: int = 0


async def _greet_ws_endpoint(websocket):
    """Dedicated WebSocket for pushing proactive face greetings independently."""
    await websocket.accept()
    _greet_ws_clients.add(websocket)
    log.info("Browser connected to NLU greeting WebSocket.")
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        _greet_ws_clients.discard(websocket)


async def _monitor_face_greetings_loop() -> None:
    """Single background loop in NLU server to handle face greetings.

    Guarantees each face_greeting_seq is processed EXACTLY ONCE.
    If a browser WebSocket is connected, pushes to the browser.
    If no browser is connected, plays audio via local hardware speaker.
    """
    global _last_processed_greet_seq
    if _bb is None:
        return
    _last_processed_greet_seq = _bb.read("face_greeting_seq").get("face_greeting_seq", 0)
    last_status_broadcast_ts = 0.0
    last_face_seen_ts = time.time()

    while _bb.read("running").get("running", True):
        await asyncio.sleep(0.3)
        try:
            # Broadcast live robot presence & TFT eye emotion to kiosk frontend every ~0.5s
            now_ts = time.time()
            if _greet_ws_clients and (now_ts - last_status_broadcast_ts >= 0.5):
                last_status_broadcast_ts = now_ts
                bb_data = _bb.read(
                    "emotion",
                    "face_detected",
                    "face_count",
                    "face_area_ratio",
                    "no_face_elapsed_sec",
                )
                face_detected = bool(bb_data.get("face_detected", False))
                if face_detected:
                    last_face_seen_ts = now_ts
                    no_face_sec = 0.0
                else:
                    no_face_bb = bb_data.get("no_face_elapsed_sec")
                    if no_face_bb is not None and float(no_face_bb) > 0.0:
                        no_face_sec = float(no_face_bb)
                    else:
                        no_face_sec = max(0.0, now_ts - last_face_seen_ts)

                status_payload = json.dumps({
                    "type": "robot_status",
                    "emotion": str(bb_data.get("emotion") or "idle"),
                    "face_detected": face_detected,
                    "face_count": int(bb_data.get("face_count", 0)),
                    "face_area": float(bb_data.get("face_area_ratio", 0.0) or 0.0),
                    "no_face_sec": float(no_face_sec),
                })
                for ws in list(_greet_ws_clients):
                    try:
                        await ws.send_text(status_payload)
                    except Exception:
                        pass
            state = _bb.read("face_greeting_seq", "face_greeting_text", "agent_speaking", "user_speaking")
            seq = state.get("face_greeting_seq", 0)
            if seq > _last_processed_greet_seq:
                _last_processed_greet_seq = seq
                text = str(state.get("face_greeting_text") or "").strip()
                if text and not state.get("agent_speaking") and not state.get("user_speaking"):
                    log.info(f"[NLU] Processing proactive face greeting (#{seq}): '{text}'")
                    print(f"[NLU] Processing proactive face greeting (#{seq}): '{text}'")

                    loop = asyncio.get_running_loop()
                    audio_file = await loop.run_in_executor(None, _generate_dynamic_tts, text)
                    if audio_file:
                        audio_url = f"/assets/audio_cache/{audio_file}"
                        audio_path = APP_DIR / "assets" / "audio_cache" / audio_file

                        greet_uid = ""
                        duration_ms = 0
                        sync = get_speech_sync_service()
                        if sync is not None:
                            greet_uid, duration_ms = await loop.run_in_executor(
                                None,
                                lambda: sync.arm_utterance(reply_text=text, audio_path=str(audio_path)),
                            )

                        if _bb is not None:
                            _bb.write(conv_state="speaking", agent_speaking=True, conv_emotion="happy")
                        if sync is not None and greet_uid:
                            sync.begin_playback(greet_uid)
                        # Let EyeRenderer (24 Mbaud SPI) & FaceTracker pause before audio DMA starts
                        await asyncio.sleep(0.08)

                        if _greet_ws_clients:
                            # Fix double voices: send audio_url only to primary active client (last connected).
                            # Other clients receive audio_url=None to update subtitles without duplicate playback.
                            primary_ws = list(_greet_ws_clients)[-1]
                            for ws in list(_greet_ws_clients):
                                try:
                                    url = audio_url if ws is primary_ws else None
                                    payload = json.dumps({"type": "greeting", "text": text, "audio_url": url})
                                    await ws.send_text(payload)
                                except Exception:
                                    pass
                        else:
                            from voice.offline_voice.runtime import play_audio
                            await loop.run_in_executor(None, play_audio, audio_path)

                        word_count = len(text.split())
                        clear_delay = max(2.2, (duration_ms / 1000.0) + 0.6 if duration_ms else word_count * 0.35 + 0.8)
                        await asyncio.sleep(clear_delay)
                        if _bb is not None:
                            if sync is not None:
                                sync.end_playback()
                            clear_conv_emotion(_bb)
                            _bb.write(conv_state="waiting", agent_speaking=False)
        except Exception as exc:
            log.warning(f"[NLU] Background face greeting loop error: {exc}")
            await asyncio.sleep(1.0)

def _build_app():
    """Build and return the Starlette ASGI app."""
    try:
        from starlette.applications import Starlette
        from starlette.routing import Route, WebSocketRoute
    except ImportError as e:
        raise ImportError(
            "Starlette not installed. Run: pip install fastapi uvicorn[standard]"
        ) from e

    return Starlette(
        debug=False,
        routes=[
            Route("/health", _health_endpoint, methods=["GET"]),
            Route("/reload-nlu", _reload_nlu_endpoint, methods=["POST"]),
            WebSocketRoute("/ws/voice", _voice_ws_endpoint),
            WebSocketRoute("/ws/greet", _greet_ws_endpoint),
        ],
    )


# ── Valid Knowledge Base & Navigation Suggestion Buttons ──────────────────────
# Every button returned by _get_valid_suggestion_buttons is verified against
# runtime.matcher.match_exact() so no false/unmatched buttons can ever appear.

_RELATED_KB_TOPICS: dict[str, list[str]] = {
    "knowledge_fit_expo_2026": [
        "knowledge_fit_25th_anniversary",
        "knowledge_team_nema",
        "knowledge_fit_expo_projects",
    ],
    "knowledge_fit_25th_anniversary": [
        "knowledge_fit_expo_2026",
        "knowledge_faculty_of_it",
        "knowledge_history",
    ],
    "knowledge_team_nema": [
        "knowledge_about_nema_robot",
        "knowledge_fit_expo_2026",
        "knowledge_faculty_of_it",
    ],
    "knowledge_about_nema_robot": [
        "knowledge_team_nema",
        "knowledge_fit_expo_2026",
        "knowledge_general_ai_robotics",
    ],
    "knowledge_fit_expo_projects": [
        "knowledge_fit_expo_2026",
        "knowledge_research",
        "knowledge_team_nema",
    ],
    "knowledge_faculty_of_it": [
        "knowledge_undergraduate_programs",
        "knowledge_fit_departments",
        "knowledge_fit_25th_anniversary",
    ],
    "knowledge_fit_departments": [
        "knowledge_faculty_of_it",
        "knowledge_undergraduate_programs",
        "knowledge_research",
    ],
    "knowledge_university_overview": [
        "knowledge_faculty_of_it",
        "knowledge_fit_expo_2026",
        "knowledge_history",
    ],
    "knowledge_undergraduate_programs": [
        "knowledge_admission",
        "knowledge_postgraduate_programs",
        "knowledge_faculty_of_it",
    ],
}

_DEFAULT_SUGGESTION_POOLS: list[list[str]] = [
    ["What is FIT EXPO 2026?", "Tell me about Team Nema", "25th Anniversary of FIT"],
    ["25th Anniversary of FIT", "What can I see at FIT EXPO?", "About Faculty of IT"],
    ["What is FIT EXPO 2026?", "How does Nema work?", "Undergraduate Degree Programs"],
    ["Tell me about Team Nema", "About University of Moratuwa", "Departments in FIT"],
]
_last_pool_idx = -1


def _get_valid_suggestion_buttons(runtime, intent_id: str) -> list[str]:
    """Return 2–3 verified suggestion buttons backed by the updated Knowledge Base & Wayfinder."""
    global _last_pool_idx
    matcher = getattr(runtime, "matcher", None)
    if matcher is None:
        return []

    def _is_valid_button(label: str) -> bool:
        if not label or not label.strip():
            return False
        return matcher.match_exact(label) is not None

    # 1. If this is a specific knowledge topic with curated related topics, use their button_labels
    if intent_id in _RELATED_KB_TOPICS:
        related_buttons: list[str] = []
        for rel_id in _RELATED_KB_TOPICS[intent_id]:
            rel_intent = matcher.intents_map.get(rel_id)
            if rel_intent:
                lbl = str(rel_intent.get("button_label") or "").strip()
                if lbl and _is_valid_button(lbl) and lbl not in related_buttons:
                    related_buttons.append(lbl)
        if related_buttons:
            return related_buttons[:3]

    # 2. For greetings / help / general smalltalk, rotate through verified suggestion pools
    if intent_id in ("smalltalk_greeting", "smalltalk_what_can_you_do", "smalltalk_identity", "smalltalk_how_are_you"):
        candidates = [i for i in range(len(_DEFAULT_SUGGESTION_POOLS)) if i != _last_pool_idx] or [0]
        _last_pool_idx = random.choice(candidates)
        pool = _DEFAULT_SUGGESTION_POOLS[_last_pool_idx]
        valid = [b for b in pool if _is_valid_button(b)]
        if valid:
            return valid[:3]

    return []


def _match_intent(runtime, user_text: str, conv_history: list[dict[str, str]] | None = None) -> dict:
    """
    Synchronous NLU matching (runs in a thread via run_in_executor).
    Returns a dict with reply_text, audio_url, and action.
    """
    _t0 = time.perf_counter()
    try:
        return _match_intent_internal(runtime, user_text, conv_history=conv_history)
    except Exception as exc:
        log.error(f"[NLU] Exception during _match_intent: {exc}", exc_info=True)
        fallback_text = "I'm sorry, I encountered an internal issue. How else can I help?"
        return {
            "reply_text": fallback_text,
            "audio_url": None,
            "action": {},
            "is_fallback": True,
        }
    finally:
        print(f"[NLU][timing] _match_intent total={(time.perf_counter() - _t0) * 1000:.0f}ms")


def _match_intent_internal(runtime, user_text: str, conv_history: list[dict[str, str]] | None = None) -> dict:
    from voice.offline_voice.runtime import (
        route_domain,
        get_time_reply,
        get_date_reply,
        pick_non_repeating_variant,
    )

    audio_base = APP_DIR / "assets" / "audio_cache"

    def _with_audio_path(payload: dict, audio_file: str | None) -> dict:
        if audio_file and (audio_base / audio_file).exists():
            payload["audio_path"] = str(audio_base / audio_file)
        else:
            payload["audio_path"] = None
        return payload

    # ── Tool routes (dynamic replies, no retrieval) ──────────────────────────
    domain = route_domain(user_text)
    if domain in ("tool_time", "tool_date"):
        reply_text = get_time_reply() if domain == "tool_time" else get_date_reply()
        audio_file = _generate_dynamic_tts(reply_text)
        audio_url = f"/assets/audio_cache/{audio_file}" if audio_file else None
        return {
            "reply_text": reply_text,
            "audio_url": audio_url,
            "audio_path": str(APP_DIR / "assets" / "audio_cache" / audio_file) if audio_file else None,
            "action": {},
        }

    # ── Tier-1 Exact Match lookup (0.01ms cost) ──────────────────────────────
    intent = runtime.matcher.match_exact(user_text)
    if intent:
        log.info(f"  [Exact Match] matched intent '{intent.get('id')}' instantly via hashmap!")
    else:
        intent = runtime.matcher.match(user_text, domain=domain)
        
        # Fallback for STT mishearings: If the strict regex router sent it to the wrong domain,
        # check the other domains before falling back to the General Q&A LLM.
        if not intent:
            for fallback_domain in ["knowledge", "navigate", "smalltalk", "events"]:
                if fallback_domain != domain:
                    intent = runtime.matcher.match(user_text, domain=fallback_domain)
                    if intent:
                        log.info(f"  [Fallback] AI matched in '{fallback_domain}' domain despite missing trigger words!")
                        break

    if intent:
        action = dict(intent.get("action") or {})

        # ── Navigate intents: run live pathfinding and enrich the action ─────
        if action.get("action") == "navigate" and action.get("destination"):
            return _navigate_response(action["destination"])

        response_text = intent.get("response_text", "")
        audio_file = intent.get("audio_file")
        intent_id = intent.get("id", "default")
        
        if isinstance(response_text, list) and len(response_text) > 0:
            idx, response_text = pick_non_repeating_variant(intent_id, response_text)
            if isinstance(audio_file, list) and len(audio_file) > idx:
                audio_file = audio_file[idx]
            else:
                audio_file = None
                
        # Generate dynamic TTS for intents lacking precompiled audio
        if not audio_file and response_text:
            audio_file = _generate_dynamic_tts(response_text)
            
        audio_url = f"/assets/audio_cache/{audio_file}" if audio_file else None

        # Verify the cached file actually exists on disk
        if audio_file and not (audio_base / audio_file).exists():
            log.warning(
                f"Audio file missing: {audio_file} — browser will use TTS fallback."
            )
            audio_url = None

        # ── Inject verified suggestion buttons for greetings & knowledge topics ──
        if not action.get("suggested_buttons"):
            valid_buttons = _get_valid_suggestion_buttons(runtime, intent_id)
            if valid_buttons:
                action["suggested_buttons"] = valid_buttons
        else:
            # Filter any pre-attached buttons so only valid, exact-matching buttons remain
            action["suggested_buttons"] = [
                b for b in action["suggested_buttons"]
                if isinstance(b, str) and (
                    runtime.matcher.match_exact(b) is not None
                    or (_get_wayfinder() and _get_wayfinder().find_room(b) is not None)
                )
            ]

        return _with_audio_path({
            "reply_text": response_text,
            "audio_url": audio_url,
            "action": action,
            "ambiguity_category": intent.get("ambiguity_category"),
        }, audio_file if audio_url else None)

    # Direct Wayfinder room lookup before falling back (ensures fuzzy navigation works even in NLU_LIGHTWEIGHT=1 mode)
    if domain == "navigate" or re.search(r"\b(where|navigate|direction|directions|how to go|how do i get|find|take me|locate|floor|room|lab|hall|auditorium|washroom|office)\b", user_text, re.IGNORECASE):
        wf = _get_wayfinder()
        if wf is not None:
            dest_cand = re.sub(
                r"^(can you |could you |please |where is |where are |where's |how do i get to |how can i go to |how to get to |navigate to |directions to |take me to |take me |find the |find |locate |show me the way to |guide me to )+",
                "",
                user_text.strip(),
                flags=re.IGNORECASE,
            ).strip(" ?.,!")
            if dest_cand:
                try:
                    room = wf.find_room(dest_cand)
                    if room and room.get("label"):
                        log.info(f"  [Wayfinder Direct] Matched destination '{room['label']}' from '{dest_cand}'")
                        return _navigate_response(dest_cand)
                except Exception:
                    pass

    # No intent matched — generate dynamic General Q&A reply
    fallback_text = _generate_dynamic_fallback(user_text, conv_history=conv_history)
    
    return _with_audio_path({
        "reply_text": fallback_text,
        "audio_url": None,  # Dynamic TTS will generate this automatically
        "action": {},
        "is_fallback": True,
    }, None)


_llm_clients: list[tuple[str, OpenAI, str]] | None = None
_llm_lock = threading.Lock()
_campus_system_prompt: str | None = None


def _get_campus_system_prompt() -> str:
    """Build and cache the campus & General Q&A system prompt directly from knowledge_base.yaml."""
    global _campus_system_prompt
    if _campus_system_prompt is not None:
        return _campus_system_prompt

    parts = [
        "You are Nema, a friendly, intelligent humanoid guide robot created by Team Nema from the Faculty of Information Technology (FIT) at the University of Moratuwa, Sri Lanka.",
        "Core Institutional & Event Facts:",
        "- Event: This is FIT EXPO 2026, the flagship technology exhibition of the Faculty of IT, University of Moratuwa.",
        "- Edition: FIT EXPO is being held for the second time (2nd edition) in history.",
        "- Milestone: We are celebrating the 25th Anniversary (Silver Jubilee, 2001 to 2026) of the Faculty of Information Technology.",
        "- Creators: You were designed and built by Team Nema from the Faculty of IT, University of Moratuwa.",
        "- General Q&A Role: In addition to campus, exhibition, and navigation questions, you can answer general knowledge, technology, AI, robotics, science, and Sri Lanka questions accurately and conversationally.",
        "Strict Output Rules:",
        "1. Reply in 1 to 2 short, natural spoken sentences (maximum 30 words).",
        "2. Never use markdown, asterisks, bullet points, numbered lists, or emojis.",
        "3. Never repeat garbled or misheard words from speech recognition.",
    ]

    kb_path = APP_DIR / "voice" / "knowledge_base.yaml"
    if kb_path.exists():
        try:
            import yaml
            kb_data = yaml.safe_load(kb_path.read_text(encoding="utf-8")) or []
            if isinstance(kb_data, list):
                kb_lines = [
                    f"- {item.get('topic', '').replace('_', ' ')}: {(item.get('reply') or '').strip().replace(chr(10), ' ')}"
                    for item in kb_data
                    if isinstance(item, dict) and item.get("topic") and item.get("reply")
                ]
                if kb_lines:
                    parts.append("Knowledge Base:\n" + "\n".join(kb_lines))
        except Exception:
            pass

    _campus_system_prompt = "\n".join(parts)
    return _campus_system_prompt


def _get_cloud_llm_clients() -> list[tuple[str, OpenAI, str]]:
    """Return ordered list of (provider_name, OpenAI client, model) for fast Q&A with automatic failover."""
    global _llm_clients
    with _llm_lock:
        if _llm_clients is not None:
            return _llm_clients

        clients: list[tuple[str, OpenAI, str]] = []
        groq_key = os.getenv("GROQ_API_KEY", "").strip()
        openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        google_key = os.getenv("GOOGLE_API_KEY", "").strip()
        preferred = os.getenv("LLM_PROVIDER", "").strip().lower()

        def _add_groq():
            if groq_key:
                env_model = os.getenv("GROQ_MODEL", "").strip()
                # Replace deprecated Groq Llama 3 slugs with active fast models
                if not env_model or env_model in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"):
                    models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
                else:
                    models = [env_model, "openai/gpt-oss-120b"]
                for m in models:
                    client = OpenAI(
                        base_url="https://api.groq.com/openai/v1",
                        api_key=groq_key,
                        timeout=5.0,
                        max_retries=1,
                    )
                    clients.append(("groq", client, m))

        def _add_openrouter():
            if openrouter_key:
                model = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct").strip()
                if model.endswith(":free"):
                    model = model[:-5]
                client = OpenAI(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=openrouter_key,
                    timeout=6.0,
                    max_retries=1,
                )
                clients.append(("openrouter", client, model))

        def _add_gemini():
            if google_key:
                model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite").strip()
                client = OpenAI(
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                    api_key=google_key,
                    timeout=6.0,
                    max_retries=1,
                )
                clients.append(("gemini", client, model))

        if preferred == "openrouter":
            _add_openrouter()
            _add_groq()
            _add_gemini()
        else:
            _add_groq()
            _add_openrouter()
            _add_gemini()

        _llm_clients = clients
        if clients:
            log.info(f"[NLU] Cloud LLM Q&A providers initialized: {[p for p, _, _ in clients]}")
        return _llm_clients


def _generate_dynamic_fallback(
    transcript: str,
    conv_history: list[dict[str, str]] | None = None,
) -> str:
    """Generate a fast, smart campus or general Q&A reply using Cloud LLM providers with failover."""
    providers = _get_cloud_llm_clients()
    default_replies = [
        "I'm Nema from Team Nema! Feel free to ask me about FIT EXPO 2026, our 25th anniversary, or directions to any room!",
        "I'm not quite sure about that, but I can tell you all about FIT EXPO 2026, the Faculty of IT, or guide you around the building!",
    ]
    if not providers:
        return random.choice(default_replies)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _get_campus_system_prompt()},
    ]
    if conv_history:
        for turn in conv_history[-4:]:
            if turn.get("role") in ("user", "assistant") and turn.get("content"):
                messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": transcript})

    for provider_name, client, model in providers:
        try:
            _t0 = time.perf_counter()
            extra_kwargs = {"reasoning_effort": "low"} if "gpt-oss" in model else {}
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=300,
                temperature=0.35,
                **extra_kwargs,
            )
            reply = (response.choices[0].message.content or "").strip()
            reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
            reply = re.sub(r"[*_#`]", "", reply).strip()
            reply = re.sub(r"\s+", " ", reply)
            print(f"  [CloudLLM][timing] {provider_name}:{model}={(time.perf_counter() - _t0) * 1000:.0f}ms")
            if reply:
                return reply
        except Exception as e:
            log.warning(f"Dynamic Q&A LLM ({provider_name}:{model}) failed: {e}")
            continue

    return random.choice(default_replies)


_tts_lock = threading.Lock()
_tts_in_flight = set()
_tts_http_client = None


def _get_tts_http_client():
    """Persistent HTTP/1.1 keep-alive client so Deepgram TTS skips SSL handshake overhead (~250ms saved)."""
    global _tts_http_client
    if _tts_http_client is None:
        try:
            import httpx
            _tts_http_client = httpx.Client(timeout=5.0, http2=False)
        except ImportError:
            _tts_http_client = False
    return _tts_http_client


def _generate_dynamic_tts(text: str) -> str | None:
    """Generate TTS synchronously and cache it as 24kHz WAV + pre-built amplitude sidecar."""
    filename = f"dyn_{hashlib.md5(text.encode()).hexdigest()[:10]}.wav"
    audio_base = APP_DIR / "assets" / "audio_cache"
    audio_base.mkdir(parents=True, exist_ok=True)
    output_path = audio_base / filename

    if output_path.exists():
        return filename

    with _tts_lock:
        is_first = filename not in _tts_in_flight
        if is_first:
            _tts_in_flight.add(filename)

    if not is_first:
        for _ in range(50):
            if output_path.exists():
                return filename
            if filename not in _tts_in_flight:
                break
            time.sleep(0.05)
        return filename if output_path.exists() else None

    api_key = os.getenv("DEEPGRAM_API_KEY") or os.getenv("NEXT_PUBLIC_DEEPGRAM_API_KEY")
    if not api_key:
        log.warning("No Deepgram API key for dynamic TTS")
        with _tts_lock:
            _tts_in_flight.discard(filename)
        return None

    try:
        _t0 = time.perf_counter()
        # 24000Hz WAV is half the payload size of 48000Hz, matches _PCM_SAMPLE_RATE, and has identical voice clarity
        url = "https://api.deepgram.com/v1/speak?model=aura-luna-en&encoding=linear16&container=wav&sample_rate=24000"
        headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        }
        client = _get_tts_http_client()
        if client:
            resp = client.post(url, headers=headers, json={"text": text})
            resp.raise_for_status()
            wav_bytes = resp.content
        else:
            data = json.dumps({"text": text}).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5.0) as response:
                wav_bytes = response.read()

        output_path.write_bytes(wav_bytes)
        # Pre-compute the lip-sync sidecar immediately in this worker thread
        try:
            from voice.audio_envelope import build_envelope_for_audio
            build_envelope_for_audio(output_path, fallback_text=text, write_cache=True)
        except Exception:
            pass
        print(f"  [TTS][timing] Deepgram 24kHz WAV={(time.perf_counter() - _t0) * 1000:.0f}ms")
        return filename
    except Exception as e:
        log.error(f"Dynamic TTS failed for text '{text}': {e}")
        return None
    finally:
        with _tts_lock:
            _tts_in_flight.discard(filename)


def _clean_button_room_label(label: str) -> str:
    """Format a map room label cleanly for disambiguation buttons."""
    clean = label.strip()
    clean = re.sub(r"\blabratory\b", "Laboratory", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\blecturehall\b", "Lecture hall", clean, flags=re.IGNORECASE)
    if clean.lower() == "female washroom":
        return "Female Washroom"
    if clean.lower() == "male washroom":
        return "Male Washroom"
    if clean and clean[0].islower():
        clean = clean[0].upper() + clean[1:]
    return clean


def _navigate_response(destination: str) -> dict:
    """Run Wayfinder pathfinding and build a map-ready navigate action.

    When the destination string is genuinely ambiguous (multiple rooms share the
    same generic keyword, e.g. "auditorium"), we return a clarification response
    with validated ``suggested_buttons`` so the user can tap without false buttons.
    """
    wayfinder = _get_wayfinder()
    if wayfinder is None:
        return {
            "reply_text": "I'm sorry, my navigation system isn't available right now.",
            "audio_url": None,
            "action": {},
        }

    # ── Ambiguity check: if multiple distinct rooms match, ask which one ──────
    try:
        exact_match = wayfinder.find_room(destination)
    except Exception:
        exact_match = None

    dest_lc = destination.lower().strip()
    if exact_match is not None:
        label_lc = exact_match["label"].strip().lower()
        dest_has_number = bool(re.search(r"\d", dest_lc))
        label_has_number = bool(re.search(r"\d", label_lc))
        dest_has_gender = bool(re.search(r"\b(male|female)\b", dest_lc))
        label_has_gender = bool(re.search(r"\b(male|female)\b", label_lc))
        sim_ratio = SequenceMatcher(None, dest_lc, label_lc).ratio()

        missing_qualifier = (label_has_number and not dest_has_number) or (label_has_gender and not dest_has_gender)

        # Unambiguous if exact match, high similarity (>= 0.84) without missing number/gender qualifier,
        # or contains a specific room number matching the label.
        if dest_lc == label_lc or (sim_ratio >= 0.84 and not missing_qualifier):
            candidates = [exact_match]
        elif dest_has_number and (dest_lc in label_lc or label_lc in dest_lc):
            candidates = [exact_match]
        else:
            try:
                candidates = wayfinder.find_rooms(destination)
            except Exception as exc:
                log.warning(f"find_rooms('{destination}') failed: {exc}")
                candidates = [exact_match]
    else:
        try:
            candidates = wayfinder.find_rooms(destination)
        except Exception as exc:
            log.warning(f"find_rooms('{destination}') failed: {exc}")
            candidates = []

    if len(candidates) > 1:
        # Build clean, deduplicated button labels verified against Wayfinder
        buttons: list[str] = []
        seen_btn_lc: set[str] = set()
        for c in candidates:
            clean_lbl = _clean_button_room_label(c["label"])
            if clean_lbl.lower() not in seen_btn_lc:
                seen_btn_lc.add(clean_lbl.lower())
                buttons.append(clean_lbl)

        if len(buttons) > 1:
            category = get_dest_category(destination)

            if len(buttons) == 2:
                options = f"{buttons[0]} or {buttons[1]}"
            elif len(buttons) == 3:
                options = f"{buttons[0]}, {buttons[1]}, or {buttons[2]}"
            else:
                options = ", ".join(buttons[:-1]) + f", or {buttons[-1]}"

            reply_text = f"Which {category} did you mean? {options}?"
            audio_file = _generate_dynamic_tts(reply_text)
            audio_url = f"/assets/audio_cache/{audio_file}" if audio_file else None
            audio_base = APP_DIR / "assets" / "audio_cache"
            return {
                "reply_text": reply_text,
                "audio_url": audio_url,
                "audio_path": str(audio_base / audio_file) if audio_file else None,
                "action": {
                    "action": "speak",
                    "text": reply_text,
                    "suggested_buttons": buttons,
                },
                "ambiguity_category": category if category != "location" else None,
            }

    try:
        result = wayfinder.find_path(destination)
    except Exception as exc:
        log.error(f"Wayfinder.find_path('{destination}') failed: {exc}")
        result = None

    if not result:
        return {
            "reply_text": f"I'm sorry, I couldn't find a path to {destination}.",
            "audio_url": None,
            "action": {},
        }

    if "error" in result:
        return {
            "reply_text": result["error"],
            "audio_url": None,
            "action": {},
        }

    # Same node shape LiveKit / v2 kiosk expect — include floor for the
    # multi-floor switcher and scoped ids so path_ids line up.
    nodes = [
        {
            "id": n["id"],
            "label": n["label"],
            "type": n.get("type", "room"),
            "world": n["world"],
            "building": n.get("building"),
            "size": n.get("size", [1, 1, 1]),
            "floor": n.get("floor", result.get("floor", "floor_1")),
        }
        for n in result["nodes"]
    ]

    audio_file = _generate_dynamic_tts(result["directions"])
    audio_url = f"/assets/audio_cache/{audio_file}" if audio_file else None

    return {
        "reply_text": result["directions"],
        "audio_url": audio_url,
        "audio_path": str(APP_DIR / "assets" / "audio_cache" / audio_file) if audio_file else None,
        "action": {
            "action": "navigate",
            "destination": result["destination"],
            "floor": result.get("floor", "floor_1"),
            "path": result["path_coords"],
            "path_coords": result["path_coords"],
            "path_ids": result.get("path_ids", []),
            "nodes": nodes,
            "buildings": result["buildings"],
        },
    }


# ── Public entry point (called from start_robot.py thread) ───────────────────

def run_nlu_server(
    bb: "Blackboard", *, host: str = "0.0.0.0", port: int = 8765, wayfinder=None
) -> None:
    """
    Start the NLU WebSocket server on a dedicated asyncio event loop.
    Called from a daemon thread in start_robot.py.
    Blocking — runs until the Blackboard's running flag is cleared.
    """
    global _bb, _wayfinder
    _bb = bb
    if wayfinder is not None:
        _wayfinder = wayfinder

    try:
        import uvicorn
    except ImportError:
        print("[NluServer] ERROR: uvicorn not installed. Run: pip install uvicorn[standard]")
        return

    # ── Start server IMMEDIATELY, load weights in background ──────────────
    # This ensures the port opens instantly so the frontend doesn't timeout
    # when clicking the mic button. If they speak before it's ready, it waits.
    
    app = _build_app()
    log.info(f"[NluServer] Starting on ws://{host}:{port}/ws/voice")
    print(f"[NluServer] WebSocket server starting on port {port}...")

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",   # Keep quiet — robot is busy with servos
        loop="asyncio",
        lifespan="off",
    )
    server = uvicorn.Server(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _prewarm_sidecars():
        """Pre-build amplitude sidecars for all cached MP3s so ffmpeg is never
        called during a live voice turn (eliminates CPU spikes on Pi).
        """
        try:
            from voice.audio_envelope import build_all_sidecars
            audio_cache = APP_DIR / "assets" / "audio_cache"
            if audio_cache.is_dir():
                n = build_all_sidecars(audio_cache)
                if n:
                    print(f"[NluServer] Pre-built {n} amplitude sidecar(s).")
        except Exception as exc:
            log.warning(f"Sidecar pre-build failed: {exc}")

    def _prewarm_nlu():
        """Load NLU models into RAM safely in a background thread."""
        global _nlu_error
        print("[NluServer] Pre-loading NLU runtime in background...")
        try:
            _get_runtime()
            print("[NluServer] NLU runtime ready (SentenceTransformer loaded).")
        except Exception as exc:
            _nlu_error = True
            log.warning(f"[NluServer] NLU pre-warm failed: {exc}")

    async def _run():
        serve_task = asyncio.create_task(server.serve())
        greet_task = asyncio.create_task(_monitor_face_greetings_loop())
        # Give the server a moment to bind, then pre-warm in background
        await asyncio.sleep(0.3)
        print(f"[NluServer] Port {port} open — building audio sidecars in background...")
        asyncio.get_running_loop().run_in_executor(None, _prewarm_nlu)
        asyncio.get_running_loop().run_in_executor(None, _prewarm_sidecars)
        
        # Poll the Blackboard running flag so we shut down cleanly on Ctrl+C
        while bb.read("running")["running"] and not serve_task.done():
            await asyncio.sleep(0.5)
        server.should_exit = True
        greet_task.cancel()
        await asyncio.wait_for(serve_task, timeout=5.0)

    try:
        loop.run_until_complete(_run())
    except Exception as e:
        log.error(f"[NluServer] Server error: {e}")
    finally:
        if not loop.is_closed():
            loop.close()
    print("[NluServer] Stopped.")
