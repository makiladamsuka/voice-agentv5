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
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from core.blackboard import Blackboard

APP_DIR = Path(__file__).resolve().parent.parent
load_dotenv(APP_DIR / ".env")

log = logging.getLogger("NluServer")

# ── Module-level state ────────────────────────────────────────────────────────
_bb: "Blackboard | None" = None
_runtime = None  # OfflineVoiceRuntime, lazy-loaded on first connection


def _get_runtime():
    """Lazy-load the NLU runtime on first WebSocket connection."""
    global _runtime
    if _runtime is None:
        from voice.offline_voice.runtime import OfflineVoiceRuntime
        log.info("Loading NLU runtime (ChromaDB intent matcher)...")
        _runtime = OfflineVoiceRuntime(_bb)
        log.info("NLU runtime ready.")
    return _runtime

def reload_nlu_runtime():
    """Clear the cached runtime so it reloads from disk on the next request."""
    global _runtime
    _runtime = None
    log.info("NLU runtime cache cleared (will reload on next request).")


# ── Starlette WebSocket application ──────────────────────────────────────────

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

            if msg_type == "transcript":
                user_text = msg.get("text", "").strip()
                if not user_text:
                    continue

                log.info(f"[NLU] Transcript: '{user_text}'")
                print(f"[NLU] Transcript: '{user_text}'")

                # Update blackboard — show "thinking" on robot face
                if _bb is not None:
                    _bb.write(conv_state="thinking", user_speaking=False)
                await websocket.send_text(
                    json.dumps({"type": "state", "conv_state": "thinking"})
                )

                # Run NLU in a thread so we don't block the asyncio event loop
                runtime = _get_runtime()
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None, _match_intent, runtime, user_text
                )

                print(
                    f"[NLU] Reply: '{result.get('reply_text', '')[:80]}' "
                    f"audio={result.get('audio_url')}"
                )

                # Write result to blackboard (only fields that exist on Blackboard)
                if _bb is not None:
                    _bb.write(conv_state="speaking", agent_speaking=True)

                # Send result back to browser
                await websocket.send_text(json.dumps({
                    "type": "response",
                    "reply_text": result["reply_text"],
                    "audio_url": result.get("audio_url"),
                    "action": result.get("action"),
                }))

                # Tell browser playback has started
                await websocket.send_text(
                    json.dumps({"type": "state", "conv_state": "speaking"})
                )

            elif msg_type == "tts_done":
                # Browser finished playing TTS audio
                if _bb is not None:
                    _bb.write(conv_state="listening", agent_speaking=False)
                await websocket.send_text(
                    json.dumps({"type": "state", "conv_state": "listening"})
                )

            elif msg_type == "user_speaking":
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
        if _bb is not None:
            _bb.write(
                voice_session_active=False,
                conv_state="idle",
                agent_speaking=False,
                user_speaking=False,
            )


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
    audio_base = APP_DIR / "assets" / "audio_cache"

    intent = runtime.matcher.match(user_text)

    if intent:
        audio_file = intent.get("audio_file")
        audio_url = f"/assets/audio_cache/{audio_file}" if audio_file else None

        # Verify the cached file actually exists on disk
        if audio_file and not (audio_base / audio_file).exists():
            log.warning(
                f"Audio file missing: {audio_file} — browser will use TTS fallback."
            )
            audio_url = None

        return {
            "reply_text": intent.get("response_text", ""),
            "audio_url": audio_url,
            "action": intent.get("action", {}),
        }

    # No intent matched — return fallback
    fallback_audio = "intent_fallback.mp3"
    fallback_url = (
        f"/assets/audio_cache/{fallback_audio}"
        if (audio_base / fallback_audio).exists()
        else None
    )
    return {
        "reply_text": "I'm a campus guide! Try asking me about events or directions.",
        "audio_url": fallback_url,
        "action": {},
    }


# ── Public entry point (called from start_robot.py thread) ───────────────────

def run_nlu_server(bb: "Blackboard", *, host: str = "0.0.0.0", port: int = 8765) -> None:
    """
    Start the NLU WebSocket server on a dedicated asyncio event loop.
    Called from a daemon thread in start_robot.py.
    Blocking — runs until the Blackboard's running flag is cleared.
    """
    global _bb
    _bb = bb

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
