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
import re
import urllib.request
import os
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

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


def _get_runtime():
    """Lazy-load the NLU runtime on first WebSocket connection."""
    global _runtime
    if _runtime is None:
        from voice.offline_voice.runtime import OfflineVoiceRuntime
        log.info("Loading NLU runtime (ChromaDB intent matcher)...")
        _runtime = OfflineVoiceRuntime(_bb)
        log.info("NLU runtime ready.")
    return _runtime


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
    """Clear the cached runtime so it reloads from disk on the next request."""
    global _runtime
    _runtime = None
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
    if "office" in dl:
        return "office"
    return "location"

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
    last_discussed_category = None
    # ── Echo / duplicate suppression ──────────────────────────────────────
    # Use a mutable dict so reassignment works correctly in all scopes.
    import time as _time
    _echo = {
        "norm": "",
        "reply_norm": "",
        "ts": 0.0,
        "suppress_sec": 10.0,
        "speaking": False,
    }
    # ── Speculative throttle ──────────────────────────────────────────────
    _spec = {"last_norm": "", "last_ts": 0.0, "min_interval": 1.0}

    def _normalize_text(t: str) -> str:
        return t.lower().strip(" \t\r\n.,?!;:")

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
                user_text = msg.get("text", "").strip()
                if user_text:
                    if pending_ambiguity_category:
                        user_text = f"{pending_ambiguity_category} {user_text}"
                    norm_text = _normalize_text(user_text)
                    # ── Throttle: skip if same text or too soon ───────────
                    _snow = _time.monotonic()
                    if norm_text == _spec["last_norm"] or (_snow - _spec["last_ts"]) < _spec["min_interval"]:
                        continue
                    _spec["last_norm"] = norm_text
                    _spec["last_ts"] = _snow
                    # ─────────────────────────────────────────────────────
                    runtime = _get_runtime()
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(
                        None, _match_intent, runtime, user_text
                    )
                    
                    # Prevent caching fallbacks for partial transcripts!
                    is_fallback = result and result.get("reply_text", "").startswith("I'm NEma")
                    if result and not is_fallback:
                        speculative_cache[norm_text] = result
                continue

            if msg_type == "transcript":
                original_text = msg.get("text", "").strip()
                if not original_text:
                    continue

                # ── Duplicate / echo suppression ──────────────────────────
                # If the robot is currently speaking OR we are in the echo suppression window,
                # reject incoming transcripts that match the previous user query or the robot's reply.
                _norm_incoming = _normalize_text(original_text)
                _now = _time.monotonic()
                _dt = _now - _echo["ts"]
                is_reply_echo = (
                    bool(_echo["reply_norm"])
                    and len(_norm_incoming) >= 10
                    and len(_norm_incoming) >= 0.7 * len(_echo["reply_norm"])
                    and _norm_incoming in _echo["reply_norm"]
                )

                if _echo["speaking"] or (_dt < _echo["suppress_sec"]):
                    if is_duplicate_prompt or is_reply_echo:
                        print(f"[NLU] Echo suppressed (speaking={_echo['speaking']}, {_dt:.1f}s < {_echo['suppress_sec']:.1f}s): '{original_text}'")
                        await websocket.send_text(json.dumps({"type": "state", "conv_state": "speaking" if _echo["speaking"] else "listening"}))
                        continue

                _echo["norm"] = _norm_incoming
                _echo["ts"] = _now

                log.info(f"[NLU] Transcript: '{original_text}'")
                print(f"[NLU] Transcript: '{original_text}'")

                user_text = original_text
                if pending_ambiguity_category:
                    user_text = f"{pending_ambiguity_category} {original_text}"
                    print(f"  [Context injected] Rewrote query to: '{user_text}'")

                # Update blackboard — show "thinking" on robot face
                if _bb is not None:
                    _bb.write(conv_state="thinking", user_speaking=False)
                write_conv_emotion(_bb, original_text, is_agent=False, log_prefix="Vader NLU")
                await websocket.send_text(
                    json.dumps({"type": "state", "conv_state": "thinking"})
                )

                norm_text = _normalize_text(user_text)
                
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
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(
                        None, _match_intent, runtime, user_text
                    )

                if result:
                    is_fallback = result.get("reply_text", "").startswith("I'm NEma")
                    
                    if is_fallback and not pending_ambiguity_category and last_discussed_category:
                        retry_text = f"{last_discussed_category} {original_text}"
                        print(f"  [Context retry] Retrying fallback query as: '{retry_text}'")
                        runtime = _get_runtime()
                        loop = asyncio.get_running_loop()
                        retry_result = await loop.run_in_executor(
                            None, _match_intent, runtime, retry_text
                        )
                        if retry_result and not retry_result.get("reply_text", "").startswith("I'm NEma"):
                            result = retry_result
                            speculative_cache[_normalize_text(original_text)] = result

                    pending_ambiguity_category = result.get("ambiguity_category")
                    
                    action = result.get("action", {})
                    if action.get("action") == "navigate" and action.get("destination"):
                        last_discussed_category = get_dest_category(action.get("destination"))

                    # ── Events fallback: inject poster buttons even when NLU
                    # has no compiled event intents yet (empty compiled_intents.json).
                    # Triggered whenever the user asks about events but we returned
                    # the generic fallback reply.
                    is_fallback_reply = result.get("reply_text", "").startswith("I'm NEma")
                    if is_fallback_reply and _is_events_query(original_text):
                        buttons = _get_event_buttons()
                        if buttons:
                            from voice.offline_voice.runtime import get_time_reply
                            reply_text = "Here are the latest events on campus! Tap one to find out more."
                            audio_file = _generate_dynamic_tts(reply_text)
                            audio_url = f"/assets/audio_cache/{audio_file}" if audio_file else None
                            audio_base_path = APP_DIR / "assets" / "audio_cache"
                            result = {
                                "reply_text": reply_text,
                                "audio_url": audio_url,
                                "audio_path": str(audio_base_path / audio_file) if audio_file else None,
                                "action": {
                                    "action": "show_events",
                                    "suggested_buttons": buttons,
                                },
                            }

                print(
                    f"[NLU] Reply: '{result.get('reply_text', '')[:80]}' "
                    f"audio={result.get('audio_url')}"
                )

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
                    utterance_id, duration_ms = sync.arm_utterance(
                        reply_text=result.get("reply_text", ""),
                        audio_path=result.get("audio_path"),
                    )
                elif _bb is not None:
                    _bb.write(conv_state="speaking", agent_speaking=True)

                _dur_sec = (duration_ms / 1000.0) if duration_ms else 6.0
                _echo["reply_norm"] = _normalize_text(result.get("reply_text", ""))
                _echo["ts"] = _time.monotonic()
                _echo["suppress_sec"] = max(15.0, _dur_sec + 6.0)
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
                elif _bb is not None:
                    _bb.write(conv_state="speaking", agent_speaking=True)

            elif msg_type == "tts_done":
                _echo["speaking"] = False
                _echo["ts"] = _time.monotonic()
                _echo["suppress_sec"] = 1.5
                sync = get_speech_sync_service()
                if sync is not None:
                    sync.end_playback()
                if _bb is not None:
                    _bb.write(conv_state="listening", agent_speaking=False)
                await websocket.send_text(
                    json.dumps({"type": "state", "conv_state": "listening"})
                )

            elif msg_type == "user_speaking":
                speculative_cache.clear()
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
    return JSONResponse({"status": "ok", "mode": "nlu"})


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
            WebSocketRoute("/ws/voice", _voice_ws_endpoint),
        ],
    )


_EVENTS_QUERY_RE = re.compile(
    r"\b("
    r"event|events|happening|going on|what's on|whats on"
    r"|competition|competitions|poster|posters"
    r"|activity|activities|campus news|announcement|announcements"
    r"|show me|tell me about|what is|latest|upcoming|today"
    r")\b",
    re.IGNORECASE,
)

def _is_events_query(text: str) -> bool:
    """Return True if the user's text looks like an events/discovery question."""
    return bool(_EVENTS_QUERY_RE.search(text))


def _get_event_buttons(max_buttons: int = 4) -> list:
    """Return button descriptors for the most recent uploaded posters.

    Each entry is ``{"label": str, "filename": str, "category": str}`` so the
    frontend can match by filename (exact) rather than by title (fragile).
    Falls back to plain strings when no posters exist yet.
    """
    assets_dir = APP_DIR / "assets"
    extracted_path = APP_DIR / "event_db" / "extracted_events.json"

    extracted: dict[str, dict] = {}
    try:
        if extracted_path.exists():
            import json as _json
            for item in _json.loads(extracted_path.read_text(encoding="utf-8")):
                if item.get("source_file"):
                    extracted[item["source_file"]] = item
    except Exception as exc:
        log.warning(f"Could not read extracted_events.json: {exc}")

    category_map = {
        "events": "Featured Campus Event",
        "competitions": "Upcoming Competition",
        "posts": "Campus Announcement",
    }
    image_exts = {".jpg", ".jpeg", ".png", ".webp"}

    entries: list[tuple[float, dict]] = []
    for category in ("events", "competitions", "posts"):
        cat_dir = assets_dir / category
        if not cat_dir.exists():
            continue
        for f in cat_dir.iterdir():
            if f.suffix.lower() not in image_exts:
                continue
            meta = extracted.get(f.name, {})
            title = (meta.get("title") or "").strip()
            if not title:
                stem = f.stem
                parts = stem.split("_")
                readable_parts = [p for p in parts if not p.isdigit()]
                if readable_parts:
                    title = " ".join(readable_parts).replace("-", " ").strip().title()
                if not title or not any(c.isalpha() for c in title):
                    title = category_map.get(category, "Campus Event")
            entries.append((f.stat().st_mtime, {
                "label": title,
                "filename": f.name,
                "category": category,
            }))

    if not entries:
        return ["Show all Events", "Show Competitions"]

    entries.sort(key=lambda x: x[0], reverse=True)
    seen: set[str] = set()
    buttons = []
    for _, desc in entries:
        if desc["filename"] not in seen:
            seen.add(desc["filename"])
            buttons.append(desc)
        if len(buttons) >= max_buttons:
            break
    return buttons


def _match_intent(runtime, user_text: str) -> dict:
    """
    Synchronous NLU matching (runs in a thread via run_in_executor).
    Returns a dict with reply_text, audio_url, and action.
    """
    from voice.offline_voice.runtime import route_domain, get_time_reply

    audio_base = APP_DIR / "assets" / "audio_cache"

    def _with_audio_path(payload: dict, audio_file: str | None) -> dict:
        if audio_file and (audio_base / audio_file).exists():
            payload["audio_path"] = str(audio_base / audio_file)
        else:
            payload["audio_path"] = None
        return payload

    # ── Tool routes (dynamic replies, no retrieval) ──────────────────────────
    domain = route_domain(user_text)
    if domain == "tool_time":
        reply_text = get_time_reply()
        audio_file = _generate_dynamic_tts(reply_text)
        audio_url = f"/assets/audio_cache/{audio_file}" if audio_file else None
        return {
            "reply_text": reply_text,
            "audio_url": audio_url,
            "audio_path": str(APP_DIR / "assets" / "audio_cache" / audio_file) if audio_file else None,
            "action": {},
        }

    intent = runtime.matcher.match(user_text, domain=domain)
    
    # Fallback for STT mishearings: If the strict regex router sent it to the wrong domain,
    # the vector database will return no matches. We trust ChromaDB to find it in other domains.
    if not intent:
        for fallback_domain in ["navigate", "events", "smalltalk"]:
            if fallback_domain != domain:
                intent = runtime.matcher.match(user_text, domain=fallback_domain)
                if intent:
                    log.info(f"  [Fallback] AI matched in '{fallback_domain}' domain despite missing trigger words!")
                    break

    if intent:
        action = intent.get("action", {}) or {}

        # ── Navigate intents: run live pathfinding and enrich the action ─────
        if action.get("action") == "navigate" and action.get("destination"):
            return _navigate_response(action["destination"])

        # ── Events ambiguity: show all event buttons instead of a single event ──
        if intent.get("_events_ambiguous"):
            buttons = _get_event_buttons()
            if buttons:
                reply_text = "Here are the latest events on campus! Tap one to find out more."
                audio_file = _generate_dynamic_tts(reply_text)
                audio_url = f"/assets/audio_cache/{audio_file}" if audio_file else None
                return _with_audio_path({
                    "reply_text": reply_text,
                    "audio_url": audio_url,
                    "action": {
                        "action": "show_events",
                        "suggested_buttons": buttons,
                    },
                }, audio_file)

        audio_file = intent.get("audio_file")
        
        # Generate dynamic TTS for intents lacking precompiled audio (e.g., ambiguity prompts)
        if not audio_file and intent.get("response_text"):
            audio_file = _generate_dynamic_tts(intent["response_text"])
            
        audio_url = f"/assets/audio_cache/{audio_file}" if audio_file else None

        # Verify the cached file actually exists on disk
        if audio_file and not (audio_base / audio_file).exists():
            log.warning(
                f"Audio file missing: {audio_file} — browser will use TTS fallback."
            )
            audio_url = None

        # ── Inject dynamic buttons for smalltalk and events ─────
        if not action.get("suggested_buttons"):
            intent_id = intent.get("id", "")
            if intent_id == "smalltalk_greeting" or intent_id == "smalltalk_what_can_you_do":
                action["suggested_buttons"] = ["Where is the auditorium?", "What events are happening?"]
            elif intent_id.startswith("events_"):
                action["suggested_buttons"] = _get_event_buttons()

        return _with_audio_path({
            "reply_text": intent.get("response_text", ""),
            "audio_url": audio_url,
            "action": action,
            "ambiguity_category": intent.get("ambiguity_category"),
        }, audio_file if audio_url else None)

    # No intent matched — return fallback
    fallback_audio = "intent_fallback.mp3"
    fallback_url = (
        f"/assets/audio_cache/{fallback_audio}"
        if (audio_base / fallback_audio).exists()
        else None
    )
    return _with_audio_path({
        "reply_text": "I'm NEma, your campus guide! Say hi, ask who I am, or ask about events and directions.",
        "audio_url": fallback_url,
        "action": {},
    }, fallback_audio if fallback_url else None)


def _generate_dynamic_tts(text: str) -> str | None:
    """Generate TTS synchronously and cache it, avoiding Next.js API overhead."""
    filename = f"dyn_{hashlib.md5(text.encode()).hexdigest()[:10]}.mp3"
    audio_base = APP_DIR / "assets" / "audio_cache"
    output_path = audio_base / filename
    
    if output_path.exists():
        return filename
        
    api_key = os.getenv("DEEPGRAM_API_KEY") or os.getenv("NEXT_PUBLIC_DEEPGRAM_API_KEY")
    if not api_key:
        log.warning("No Deepgram API key for dynamic TTS")
        return None
        
    try:
        url = "https://api.deepgram.com/v1/speak?model=aura-luna-en"
        headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json"
        }
        data = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        
        with urllib.request.urlopen(req, timeout=5.0) as response:
            output_path.write_bytes(response.read())
            
        return filename
    except Exception as e:
        log.error(f"Dynamic TTS failed for text '{text}': {e}")
        return None


def _navigate_response(destination: str) -> dict:
    """Run Wayfinder pathfinding and build a map-ready navigate action.

    When the destination string is ambiguous (multiple rooms share the same
    keyword, e.g. "auditorium") we return a clarification response with
    ``suggested_buttons`` so the user can tap instead of speaking again.
    """
    wayfinder = _get_wayfinder()
    if wayfinder is None:
        return {
            "reply_text": "I'm sorry, my navigation system isn't available right now.",
            "audio_url": None,
            "action": {},
        }

    # ── Ambiguity check: if multiple distinct rooms match, ask which one ──────
    # First, try an exact/unambiguous single-room lookup. If the destination
    # string is specific enough (e.g. "Auditorium 1" after context injection),
    # find_room() will resolve it directly and we can skip disambiguation.
    try:
        exact_match = wayfinder.find_room(destination)
    except Exception:
        exact_match = None

    if exact_match is not None:
        # Verify it's a genuine confident match (label contains the query or vice-versa)
        dest_lc = destination.lower().strip()
        label_lc = exact_match["label"].lower()
        # Only treat as unambiguous if the destination exactly contains a number
        # (e.g. "Auditorium 1") — means user already picked a specific one
        import re as _re
        dest_has_number = bool(_re.search(r'\d', dest_lc))
        label_matches_exactly = (dest_lc in label_lc or label_lc in dest_lc)
        if dest_has_number and label_matches_exactly:
            # Route directly to navigation — no disambiguation needed
            candidates = [exact_match]
        else:
            try:
                candidates = wayfinder.find_rooms(destination)
            except Exception as exc:
                log.warning(f"find_rooms('{destination}') failed: {exc}")
                candidates = []
    else:
        try:
            candidates = wayfinder.find_rooms(destination)
        except Exception as exc:
            log.warning(f"find_rooms('{destination}') failed: {exc}")
            candidates = []

    if len(candidates) > 1:
        # Build clean button labels (e.g. "Auditorium 1", "Auditorium 2")
        buttons = [c["label"] for c in candidates]
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
            "ambiguity_category": category,
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

    app = _build_app()
    log.info(f"[NluServer] Starting on ws://{host}:{port}/ws/voice")
    print(f"[NluServer] WebSocket server starting on port {port}...")

    # Pre-build amplitude sidecars for all cached MP3s so ffmpeg is never
    # called during a live voice turn (eliminates CPU spikes on Pi).
    try:
        from voice.audio_envelope import build_all_sidecars
        audio_cache = APP_DIR / "assets" / "audio_cache"
        if audio_cache.is_dir():
            n = build_all_sidecars(audio_cache)
            if n:
                print(f"[NluServer] Pre-built {n} amplitude sidecar(s).")
    except Exception as exc:
        log.warning(f"Sidecar pre-build failed: {exc}")

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

    async def _run():
        serve_task = asyncio.create_task(server.serve())
        # Poll the Blackboard running flag so we shut down cleanly on Ctrl+C
        while bb.read("running")["running"] and not serve_task.done():
            await asyncio.sleep(0.5)
        server.should_exit = True
        await asyncio.wait_for(serve_task, timeout=5.0)

    try:
        loop.run_until_complete(_run())
    except Exception as e:
        log.error(f"[NluServer] Server error: {e}")
    finally:
        if not loop.is_closed():
            loop.close()
    print("[NluServer] Stopped.")
