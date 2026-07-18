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
                utterance_id = str(msg.get("utterance_id", "") or "")
                sync = get_speech_sync_service()
                if sync is not None:
                    sync.begin_playback(utterance_id)
                elif _bb is not None:
                    _bb.write(conv_state="speaking", agent_speaking=True)

            elif msg_type == "tts_done":
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
    """Run Wayfinder pathfinding and build a map-ready navigate action."""
    wayfinder = _get_wayfinder()
    if wayfinder is None:
        return {
            "reply_text": "I'm sorry, my navigation system isn't available right now.",
            "audio_url": None,
            "action": {},
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
