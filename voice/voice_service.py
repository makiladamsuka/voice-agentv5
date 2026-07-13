"""VoiceService — LiveKit voice agent integrated with v5 Blackboard.

Replaces all v4 UDP communication with direct Blackboard writes.
Runs on a dedicated asyncio event loop in its own daemon thread.

Layer 2 priority: when voice_session_active=True, conv_emotion overrides
surroundings emotion in EmotionEngine, and amplitude drives EyeRenderer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import threading
from pathlib import Path

# --- DNS MONKEYPATCH (Fix for Python 3.14 / aiohttp DNS bug) ---
_old_getaddrinfo = socket.getaddrinfo
def _new_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _old_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror as e:
        if host == "api.deepgram.com":
            print(f"[DNS Patch] Bypassing flaky DNS for {host}")
            # Use known good IP for api.deepgram.com
            return [(_old_getaddrinfo.__self__.AF_INET if hasattr(_old_getaddrinfo, '__self__') else socket.AF_INET, socket.SOCK_STREAM, 6, '', ('38.68.64.131', 443))]
        raise e
socket.getaddrinfo = _new_getaddrinfo
# ---------------------------------------------------------------
from typing import TYPE_CHECKING

from livekit.agents import AgentServer, WorkerOptions
from livekit.agents.job import JobExecutorType

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, RunContext, function_tool
from livekit.plugins import openai, deepgram, silero

from voice.amplitude_tts import AmplitudeTTS, drain_to_zero
from voice.text_filters import filter_leaked_tool_syntax
from voice.media_server import ImageServer, MediaServer
from voice.image_manager import ImageManager
from voice.map_navigation import MapNavigator
from voice.event_database import build_event_database
from voice.greetings import generate_presence_greeting
from voice.tools import TimeTools, SearchTools, ContentTools, AppearanceTools
from core.eye_themes import resolve_eye_color

# Import the new offline matcher
from voice.offline_voice.runtime import IntentMatcher
try:
    import pygame
    pygame.mixer.init()
except ImportError:
    pygame = None

if TYPE_CHECKING:
    from core.blackboard import Blackboard

APP_DIR = Path(__file__).resolve().parent.parent

# ── Module-level state ────────────────────────────────────────────────────────
_bb: Blackboard | None = None
_global_image_server: MediaServer | None = None
_global_map_navigator: MapNavigator | None = None
_global_event_db = None
_global_matcher: IntentMatcher | None = None
_active_session: AgentSession | None = None

# ── VADER Sentiment ──────────────────────────────────────────────────────────
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _analyzer = SentimentIntensityAnalyzer()
except ImportError:
    _analyzer = None
    print("[VoiceService] WARNING: vaderSentiment not installed — sentiment disabled")


def _send_vader_emotion(text: str, is_agent: bool = False) -> None:
    """Derive emotion from text via VADER and write to Blackboard."""
    if _analyzer is None or _bb is None:
        return
    if not text or len(text.split()) < 2:
        return

    word_count = len(text.split())
    comp = _analyzer.polarity_scores(text)["compound"]

    emotion = "engaged"
    if comp > 0.6:
        emotion = "happy"
    elif comp > 0.2:
        emotion = "warm"
    elif comp < -0.2:
        if is_agent or "sorry" in text.lower():
            emotion = "apologetic"
        else:
            emotion = "sad"
    elif comp < -0.6:
        emotion = "angry"

    if -0.2 <= comp <= 0.2 and word_count > 10:
        emotion = "engaged"
    if comp > 0.3 and word_count > 15 and is_agent:
        emotion = "proud"

    _bb.write(conv_emotion=emotion)
    print(
        f"[Vader L2] {'Agent' if is_agent else 'User'} said: '{text[:30]}...' -> {comp:.2f} -> {emotion}"
    )


# ── Conversation state machine ─────────────────────────────────────────────

_thinking_task: asyncio.Task | None = None
_awkward_timer_task: asyncio.Task | None = None
_smart_wait_task: asyncio.Task | None = None


async def _thinking_cycle(word_count: int) -> None:
    _set_conv_state("nodding")
    await asyncio.sleep(0.5)

    base_state = "concentrating" if word_count > 15 else "thinking"
    _set_conv_state(base_state)
    await asyncio.sleep(1.5)

    _set_conv_state("remembering")
    while True:
        await asyncio.sleep(3.0)
        _set_conv_state("thinking")
        await asyncio.sleep(3.0)
        _set_conv_state("remembering")


_session_live = False


async def _awkward_timer() -> None:
    await asyncio.sleep(5.0)
    if not _session_live:
        return
    _set_conv_state("waiting")
    print("[ConvState L2] Long pause -> waiting (cheerful)")


def _set_conv_state(state: str) -> None:
    global _thinking_task, _awkward_timer_task, _smart_wait_task
    if _thinking_task and not _thinking_task.done():
        _thinking_task.cancel()
    if _awkward_timer_task and not _awkward_timer_task.done():
        _awkward_timer_task.cancel()
    if _smart_wait_task and not _smart_wait_task.done():
        _smart_wait_task.cancel()

    if _bb is not None:
        _bb.write(conv_state=state)
    print(f"[ConvState L2] -> {state}")


# ── Agent class ──────────────────────────────────────────────────────────────

class CampusAgent(Agent, TimeTools, SearchTools, AppearanceTools):
    def __init__(self, image_server: MediaServer | None, event_db=None):
        from voice.prompt import SYSTEM_INSTRUCTIONS

        assets_dir = APP_DIR / "assets"
        self.image_manager = ImageManager(assets_dir)
        self.image_server = image_server
        self.event_db = event_db
        self.map_navigator = _global_map_navigator or MapNavigator()
        if image_server is not None:
            image_server.set_map_navigator(self.map_navigator)
        self._room: rtc.Room | None = None
        self.content_tools = ContentTools(
            image_manager=self.image_manager,
            image_server=self.image_server,
            room_provider=lambda: self._room,
            map_navigator=self.map_navigator,
        )
        super().__init__(instructions=SYSTEM_INSTRUCTIONS)

    @function_tool
    async def list_available_events(
        self, filter_type: str = "all", context: RunContext = None
    ) -> str:
        """Lists all available events on campus."""
        return await self.content_tools.list_available_events(context)

    @function_tool
    async def show_event_poster(self, event_description: str, context: RunContext) -> str:
        """Displays an event poster on the frontend."""
        return await self.content_tools.show_event_poster(event_description, context)

    @function_tool
    async def show_competition_poster(
        self, competition_description: str, context: RunContext
    ) -> str:
        """Displays a competition poster on the frontend."""
        return await self.content_tools.show_competition_poster(
            competition_description, context
        )

    @function_tool
    async def show_campus_post(self, post_description: str, context: RunContext) -> str:
        """Displays a campus announcement poster on the frontend."""
        return await self.content_tools.show_campus_post(post_description, context)

    @function_tool
    async def show_location_map(self, location_query: str, context: RunContext) -> str:
        """Displays a campus location map on the frontend."""
        return await self.content_tools.show_location_map(location_query, context)

    @function_tool
    async def get_campus_directions(
        self, start_location: str, destination: str, context: RunContext
    ) -> str:
        """Gives walking directions between two campus locations using the map graph."""
        return await self.content_tools.get_campus_directions(
            start_location, destination, context
        )

    @function_tool
    async def ask_about_events(self, question: str, context: RunContext) -> str:
        """Answers questions about campus events using the vector database."""
        if not self.event_db:
            return "I'm sorry, the event database is not available right now."

        results = self.event_db.query_events(question)
        if not results:
            return "I couldn't find any specific events matching your question."

        context_str = "Found these relevant campus items:\n"
        for i, event in enumerate(results):
            category = event.get("category", "event")
            context_str += (
                f"{i + 1}. [{category}] {event.get('title', 'Item')} on "
                f"{event.get('date', 'Unknown Date')}: {event.get('description', '')}\n"
            )
        return context_str


# ── Prewarm & Entrypoint ─────────────────────────────────────────────────────

def _trigger_reindex() -> None:
    """Force event DB rebuild after poster upload or manual trigger."""
    global _global_event_db
    try:
        manifest_path = APP_DIR / "voice" / "event_db" / "event_manifest.json"
        if manifest_path.exists():
            manifest_path.unlink()
        assets_dir = APP_DIR / "assets"
        _global_event_db = build_event_database(assets_dir)
        print("[VoiceService] Event database re-indexed")
    except Exception as exc:
        print(f"[VoiceService] Re-index failed: {exc}")


def _init_image_server(
    port: int = 8080,
    kiosk_config: dict | None = None,
    blackboard: "Blackboard | None" = None,
) -> None:
    global _global_image_server, _global_map_navigator
    if _global_image_server is None:
        assets_dir = APP_DIR / "assets"
        assets_dir.mkdir(exist_ok=True)
        _global_map_navigator = MapNavigator()
        _global_image_server = MediaServer(
            assets_dir,
            app_dir=APP_DIR,
            port=port,
            kiosk_config=kiosk_config,
            map_navigator=_global_map_navigator,
            on_reindex=_trigger_reindex,
            blackboard=blackboard or _bb,
        )
        _global_image_server.start()
    elif blackboard is not None or _bb is not None:
        _global_image_server.set_blackboard(blackboard or _bb)


def ensure_media_server(bb: "Blackboard", cfg: dict | None = None) -> None:
    """Start kiosk media server early with blackboard for eye-color HTTP API."""
    if cfg is None:
        import yaml

        config_path = APP_DIR / "config.yaml"
        if config_path.is_file():
            try:
                with open(config_path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception:
                cfg = {}
        else:
            cfg = {}
    kiosk_cfg = (cfg or {}).get("kiosk", {}) or {}
    port = int(kiosk_cfg.get("port", 8080))
    _init_image_server(port=port, kiosk_config=kiosk_cfg, blackboard=bb)


def _build_event_db_sync() -> None:
    global _global_event_db
    try:
        assets_dir = APP_DIR / "assets"
        _global_event_db = build_event_database(assets_dir)
    except Exception as e:
        print(f"Event database build failed: {e}")
        _global_event_db = None


def prewarm(proc: agents.JobProcess) -> None:
    """Heavy init runs once per worker process before any frontend connect."""
    print("[VoiceService] Prewarming worker (media server, event DB, VAD)...")
    import yaml

    kiosk_cfg: dict = {}
    config_path = APP_DIR / "config.yaml"
    if config_path.is_file():
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            kiosk_cfg = cfg.get("kiosk", {}) or {}
        except Exception:
            pass
    port = int(kiosk_cfg.get("port", 8080))
    _init_image_server(port=port, kiosk_config=kiosk_cfg, blackboard=_bb)
    _build_event_db_sync()
    
    global _global_matcher
    _global_matcher = IntentMatcher(APP_DIR / "voice" / "event_db")
    _global_matcher.load_cache(APP_DIR / "voice" / "event_db" / "compiled_intents.json")
    
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=0.1,
        min_silence_duration=0.3,
        prefix_padding_duration=0.2,
    )
    proc.userdata["image_server"] = _global_image_server
    proc.userdata["event_db"] = _global_event_db
    proc.userdata["matcher"] = _global_matcher
    print("[VoiceService] Prewarm complete — ready for instant LiveKit connect")


async def entrypoint(ctx: agents.JobContext) -> None:
    global _thinking_task, _awkward_timer_task, _smart_wait_task, _session_live, _active_session

    print(f"[VoiceService] Job received: room={ctx.room.name}")

    vad = ctx.proc.userdata.get("vad")
    if vad is None:
        print("[VoiceService] Warning: VAD not prewarmed, loading on connect (slow)")
        vad = silero.VAD.load(
            min_speech_duration=0.1,
            min_silence_duration=0.3,
            prefix_padding_duration=0.2,
        )

    image_server = ctx.proc.userdata.get("image_server") or _global_image_server
    event_db = ctx.proc.userdata.get("event_db") or _global_event_db

    session = AgentSession(
        turn_handling=agents.TurnHandlingOptions(interruption={"mode": "vad"}),
        stt=deepgram.STT(model="nova-3"),
        vad=vad,
        # We disable the LLM and TTS because we are using the zero-latency offline cache now!
    )

    agent = CampusAgent(image_server, event_db)
    agent._room = ctx.room
    _active_session = session

    @ctx.room.on("data_received")
    def on_data_received(packet):
        try:
            payload = packet.data.decode("utf-8")
            data = json.loads(payload)
            msg_type = data.get("type")

            if msg_type == "theme_change":
                theme = data.get("theme", "default")
                rgb = resolve_eye_color(theme)
                if _bb is not None:
                    _bb.write(eye_color=rgb)
                print(f"[VoiceService] theme_change: {theme!r} -> {rgb}")
                return

            if msg_type != "event_focus":
                return
            event = data.get("event", {})
            title = event.get("message") or event.get("title") or "this item"
            description = event.get("description", "")
            date = event.get("extracted_date") or event.get("date", "")
            location = event.get("extracted_location") or event.get("location", "")
            category = event.get("category", "event")
            detail_parts = []
            if date:
                detail_parts.append(f"on {date}")
            if location:
                detail_parts.append(f"at {location}")
            detail_str = " ".join(detail_parts)
            desc_str = f" {description}" if description else ""
            intro = (
                f"A visitor just tapped on the '{title}' {category} news card. "
                f"Tell them about this {category} enthusiastically."
            )
            if detail_str:
                intro += f" It is {detail_str}."
            intro += f"{desc_str} Then invite them to ask follow-up questions."
            print(f"[VoiceService] Event focus received: {title} ({category})")
            asyncio.create_task(session.generate_reply(user_input=intro))
        except Exception as exc:
            print(f"[VoiceService] event_focus handler error: {exc}")

    async def _hearing_reflex():
        _set_conv_state("listening")
        if _bb is not None:
            _bb.write(user_speaking=True)
        await asyncio.sleep(0.4)

    @session.on("user_state_changed")
    def on_user_state_changed(ev):
        if ev.new_state == "speaking":
            asyncio.create_task(_hearing_reflex())
        elif ev.new_state == "listening":
            global _smart_wait_task
            if _bb is not None:
                _bb.write(user_speaking=False)

            async def _smart_wait():
                await asyncio.sleep(1.2)
                if ctx.room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
                    _set_conv_state("waiting")

            if _smart_wait_task and not _smart_wait_task.done():
                _smart_wait_task.cancel()
            _smart_wait_task = asyncio.create_task(_smart_wait())

    @session.on("user_input_transcribed")
    def on_user_input_transcribed(ev):
        global _thinking_task
        if not ev.is_final:
            return

        text = ev.transcript or ""
        junk = ["uh", "um", "ah", "er", "hmm", "okay", "so", "well"]
        clean_words = [w for w in text.lower().split() if w not in junk]
        word_count = len(clean_words)

        _thinking_task = asyncio.create_task(_thinking_cycle(word_count))

        try:
            _send_vader_emotion(text, is_agent=False)
        except Exception:
            pass
            
        # --- ZERO-LATENCY INTENT MATCHING ---
        matcher = ctx.proc.userdata.get("matcher") or _global_matcher
        if matcher:
            intent = matcher.match(text)
            if intent:
                print(f"[Offline Voice] Match Found: {intent['action']}")
                if _bb:
                    _bb.write(conv_state="speaking", agent_speaking=True)
                
                # Trigger frontend UI actions manually based on the intent
                action = intent.get("action", {})
                if action.get("action") == "show_event_poster":
                    # Manually push data to frontend via LiveKit DataChannel
                    payload = json.dumps({"type": "show_poster", "description": action.get("kwargs", {}).get("event_description", "")}).encode("utf-8")
                    if ctx.room.local_participant:
                        asyncio.create_task(ctx.room.local_participant.publish_data(payload))
                
                # Play audio LOCALLY via Pygame zero-latency
                audio_path = APP_DIR / "assets" / "audio_cache" / intent["audio_file"]
                if pygame and audio_path.exists():
                    print(f"[Offline Voice] 🔊 Playing local cached audio: {intent['audio_file']}")
                    try:
                        pygame.mixer.music.load(str(audio_path))
                        pygame.mixer.music.play()
                    except Exception as e:
                        print(f"[Offline Voice] Audio error: {e}")
                else:
                    print(f"[Offline Voice] Audio not played: pygame={pygame is not None}, path={audio_path.exists()}")
            else:
                print("[Offline Voice] No exact intent match found.")

    @session.on("agent_state_changed")
    def on_agent_state_changed(ev):
        if ev.new_state == "speaking":
            _set_conv_state("speaking")
            if _bb is not None:
                _bb.write(agent_speaking=True)
        elif ev.new_state in ("listening", "idle"):
            drain_to_zero()
            _set_conv_state("waiting")
            if _bb is not None:
                _bb.write(agent_speaking=False)

    @session.on("conversation_item_added")
    def on_conversation_item_added(ev):
        from livekit.agents.llm import ChatMessage

        if not isinstance(ev.item, ChatMessage):
            return
        text = ev.item.text_content or ""
        if ev.item.role == "assistant" and text:
            try:
                _send_vader_emotion(text, is_agent=True)
            except Exception as e:
                print(f"[VoiceService] Vader Error: {e}")

    print("[VoiceService] Starting LiveKit session...")
    await session.start(room=ctx.room, agent=agent)

    _session_live = True
    if _bb is not None:
        _bb.write(voice_session_active=True)

    print("[VoiceService] Session initialized in Offline Mode.")

    try:
        while ctx.room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            await asyncio.sleep(1)
    finally:
        _session_live = False
        _active_session = None
        if _bb is not None:
            _bb.write(
                voice_session_active=False,
                conv_state="idle",
                conv_emotion=None,
                amplitude_fast=0.0,
                amplitude_slow=0.0,
                user_speaking=False,
                agent_speaking=False,
            )


# ── Public entry point (called from start_robot.py thread) ───────────────────

logger = logging.getLogger(__name__)


async def _graceful_voice_shutdown(server: AgentServer, bb: "Blackboard") -> None:
    """Drain active voice session and worker before closing the asyncio loop."""
    global _active_session

    print("[VoiceService] Shutting down...")
    if _bb is not None:
        _bb.write(
            voice_session_active=False,
            conv_state="idle",
            conv_emotion=None,
            amplitude_fast=0.0,
            amplitude_slow=0.0,
            user_speaking=False,
            agent_speaking=False,
        )

    session = _active_session
    if session is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.drain(), timeout=4.0)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.aclose(), timeout=4.0)
        _active_session = None

    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(server.drain(timeout=15), timeout=8.0)
    with contextlib.suppress(Exception):
        await server.aclose()


async def _run_voice_worker(
    server: AgentServer,
    bb: "Blackboard",
    *,
    devmode: bool,
) -> None:
    run_task = asyncio.create_task(server.run(devmode=devmode))
    try:
        while bb.read("running")["running"] and not run_task.done():
            await asyncio.sleep(0.2)

        if not run_task.done():
            await _graceful_voice_shutdown(server, bb)
            await asyncio.wait_for(run_task, timeout=10.0)
        else:
            run_task.result()
    except asyncio.TimeoutError:
        logger.warning("[VoiceService] worker shutdown timed out")
    except Exception:
        logger.exception("[VoiceService] worker failed")


def run_voice_service(bb: "Blackboard", *, devmode: bool = True) -> None:
    """Start LiveKit voice agent on a dedicated asyncio event loop (blocking).

    Called from a daemon thread in start_robot.py. Sets the module-level
    Blackboard reference so all callbacks can write to BB directly.

    Uses AgentServer.run() directly instead of cli.run_app() because the CLI
    registers signal handlers, which only work on the main thread.
    JobExecutorType.THREAD keeps jobs in-process so Blackboard writes work.
    """
    global _bb
    _bb = bb
    ensure_media_server(bb)

    env_path = APP_DIR / ".env"
    load_dotenv(env_path)
    if devmode:
        os.environ["LIVEKIT_DEV_MODE"] = "1"

    print("[VoiceService] Starting LiveKit agent...")
    print(f"[VoiceService] .env loaded from {env_path}")
    print(f"[VoiceService] mode={'dev' if devmode else 'start'}")

    server = AgentServer.from_server_options(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="campus-greeting-agent",
            initialize_process_timeout=120,
            num_idle_processes=1,
            job_executor_type=JobExecutorType.THREAD,
        )
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.slow_callback_duration = 0.1

    try:
        loop.run_until_complete(_run_voice_worker(server, bb, devmode=devmode))
    finally:
        if not loop.is_closed():
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
    print("[VoiceService] Stopped.")
