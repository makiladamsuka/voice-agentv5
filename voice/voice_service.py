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
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from livekit.agents import AgentServer, WorkerOptions, NOT_GIVEN
from livekit.agents.job import JobExecutorType
from livekit.agents.voice import room_io

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, RunContext, function_tool
from livekit.plugins import openai, deepgram, silero

from voice.amplitude_tts import AmplitudeTTS, drain_to_zero
from voice.text_filters import filter_leaked_tool_syntax
from voice.media_server import ImageServer, MediaServer
from voice.image_manager import ImageManager
from voice.map_navigation import MapNavigator
from voice.wayfinding import Wayfinder
from voice.event_database import build_event_database
from voice.greetings import generate_presence_greeting
from voice.tools import TimeTools, SearchTools, ContentTools, AppearanceTools
from core.eye_themes import resolve_eye_color
from voice.speaking_flag import write_speaking_flag, clear_speaking_flag

if TYPE_CHECKING:
    from core.blackboard import Blackboard

APP_DIR = Path(__file__).resolve().parent.parent

# ── Module-level state ────────────────────────────────────────────────────────
_bb: Blackboard | None = None
_global_image_server: MediaServer | None = None
_global_map_navigator: MapNavigator | None = None
_global_wayfinder: Wayfinder | None = None
_global_event_db = None
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
        self.wayfinder = _global_wayfinder or Wayfinder()
        if image_server is not None:
            image_server.set_map_navigator(self.map_navigator)
        self._room: rtc.Room | None = None
        self.content_tools = ContentTools(
            image_manager=self.image_manager,
            image_server=self.image_server,
            room_provider=lambda: self._room,
            map_navigator=self.map_navigator,
            wayfinder=self.wayfinder,
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
        db = self.event_db or _get_event_db()
        if not db:
            return "I'm sorry, the event database is not available right now."

        results = db.query_events(question)
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
    global _global_image_server, _global_map_navigator, _global_wayfinder
    if _global_image_server is None:
        assets_dir = APP_DIR / "assets"
        assets_dir.mkdir(exist_ok=True)
        _global_map_navigator = MapNavigator()
        _global_wayfinder = Wayfinder()
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


def _get_event_db():
    """Lazy-load ChromaDB on first query (saves RAM at startup)."""
    global _global_event_db
    if _global_event_db is None:
        _build_event_db_sync()
    return _global_event_db


def prewarm(proc: agents.JobProcess) -> None:
    """Heavy init runs once per worker process before any frontend connect."""
    print("[VoiceService] Prewarming worker (media server)...")
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
    proc.userdata["vad"] = None
    proc.userdata["image_server"] = _global_image_server
    proc.userdata["event_db"] = None
    print("[VoiceService] Prewarm complete — ready for instant LiveKit connect")


async def entrypoint(ctx: agents.JobContext) -> None:
    global _thinking_task, _awkward_timer_task, _smart_wait_task, _session_live, _active_session

    print(f"[VoiceService] Job received: room={ctx.room.name}")

    llm_model = os.getenv("OPENROUTER_MODEL", "openrouter/auto").strip() or "openrouter/auto"
    max_tokens = int(os.getenv("OPENROUTER_MAX_TOKENS", "1024"))
    endpointing_ms = int(os.getenv("VOICE_ENDPOINTING_MS", "400"))
    use_local_vad = os.getenv("VOICE_USE_LOCAL_VAD", "").strip().lower() in ("1", "true", "yes")
    print(
        f"[VoiceService] LLM: {llm_model} (max_tokens={max_tokens}); "
        f"turn_detection={'vad' if use_local_vad else 'stt'}; "
        f"endpointing_ms={endpointing_ms}"
    )

    vad = None
    if use_local_vad:
        vad = ctx.proc.userdata.get("vad")
        if vad is None:
            print("[VoiceService] Loading local Silero VAD (CPU-heavy on Pi)")
            vad = silero.VAD.load(
                min_speech_duration=0.15,
                min_silence_duration=0.4,
                prefix_padding_duration=0.2,
                sample_rate=8000,
            )

    image_server = ctx.proc.userdata.get("image_server") or _global_image_server
    event_db = ctx.proc.userdata.get("event_db") or _get_event_db()

    turn_handling = agents.TurnHandlingOptions(
        turn_detection="vad" if use_local_vad else "stt",
        endpointing={"min_delay": 0.4, "max_delay": 2.5},
        interruption={"enabled": True},
    )

    session = AgentSession(
        turn_handling=turn_handling,
        stt=deepgram.STT(model="nova-3", endpointing_ms=endpointing_ms),
        tts=AmplitudeTTS(model="aura-2-luna-en", bb=_bb),
        vad=vad,
        llm=openai.LLM(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            model=llm_model,
            max_completion_tokens=max_tokens,
        ),
        tts_text_transforms=[
            "filter_markdown",
            "filter_emoji",
            filter_leaked_tool_syntax,
        ],
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

            if msg_type == "change_eye_color":
                theme = data.get("color") or data.get("theme") or "default"
                rgb = resolve_eye_color(theme)
                if _bb is not None:
                    _bb.write(eye_color=rgb)
                print(f"[VoiceService] change_eye_color: {theme!r} -> {rgb}")
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

    @session.on("agent_state_changed")
    def on_agent_state_changed(ev):
        if ev.new_state == "speaking":
            _set_conv_state("speaking")
            write_speaking_flag(True)
            if _bb is not None:
                _bb.write(agent_speaking=True)
        elif ev.new_state in ("listening", "idle"):
            drain_to_zero()
            _set_conv_state("waiting")
            write_speaking_flag(False)
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

    config_raw = os.environ.get("CONFIG_PATH", "").strip()
    config_path = (
        Path(config_raw)
        if config_raw and Path(config_raw).is_absolute()
        else APP_DIR / (config_raw or "config.yaml")
    )

    from voice.local_audio_io import (
        resolve_local_speaker,
        setup_local_audio,
        shutdown_local_audio,
    )
    from voice.local_speaker import create_audio_output

    mic_input = None
    use_local_mic = False
    use_aec_speaker = False
    use_local_speaker = resolve_local_speaker(config_path)
    try:
        mic_input, use_local_mic, use_aec_speaker = await setup_local_audio(
            asyncio.get_running_loop(),
            config_path,
        )
        if mic_input is not None:
            session.input.audio = mic_input
            if _bb is not None:
                _bb.write(local_mic_active=True)

        # LiveKit skips TTS entirely when output.audio is None — attach Pi speaker sink.
        if use_local_speaker:
            session.output.audio = create_audio_output()
            if _bb is not None:
                _bb.write(local_speaker_active=True)
            print("[VoiceService] Local speaker output attached — TTS will play on Pi")

        room_opts: dict = {}
        if use_local_mic:
            room_opts["audio_input"] = False
        if (
            use_local_mic
            or use_aec_speaker
            or resolve_local_speaker(config_path)
        ):
            room_opts["audio_output"] = False

        room_options = (
            room_io.RoomOptions(**room_opts) if room_opts else NOT_GIVEN
        )
        await session.start(room=ctx.room, agent=agent, room_options=room_options)
    except Exception:
        await shutdown_local_audio()
        if _bb is not None:
            _bb.write(local_mic_active=False)
        raise

    _session_live = True
    if _bb is not None:
        _bb.write(voice_session_active=True)

    try:
        await session.say("Oh hi! I am so happy you are talking to me!")
    except Exception as e:
        print(f"[VoiceService] Initial greeting failed: {e}")

    async def monitor_face_greetings() -> None:
        """Speak queued hellos when FaceGreetingMonitor sees a new person."""
        last_seq = 0
        if _bb is not None:
            state = _bb.read("face_greeting_seq")
            last_seq = int(state.get("face_greeting_seq", 0) or 0)
        while ctx.room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            try:
                if not _session_live:
                    await asyncio.sleep(0.25)
                    continue
                if _bb is not None:
                    state = _bb.read("face_greeting_seq", "face_greeting_text")
                    seq = int(state.get("face_greeting_seq", 0) or 0)
                    if seq > last_seq:
                        text = str(state.get("face_greeting_text", "") or "").strip()
                        last_seq = seq
                        if text:
                            print(f"[VoiceService] Face greeting: {text!r}")
                            _bb.write(conv_emotion="happy")
                            await session.say(text, allow_interruptions=True)
            except Exception as exc:
                print(f"[VoiceService] Face greeting failed: {exc}")
            await asyncio.sleep(0.25)

    greet_task = asyncio.create_task(monitor_face_greetings())

    try:
        while ctx.room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            await asyncio.sleep(1)
    finally:
        greet_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await greet_task
        _session_live = False
        _active_session = None
        write_speaking_flag(False)
        
        # Return arms to home position at end of voice session
        try:
            from core.talk_gesture_service import return_to_home_position
            return_to_home_position()
        except Exception as e:
            print(f"[VoiceService] Failed to return arms to home: {e}")
        
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
        from voice.local_audio_io import shutdown_local_audio

        await shutdown_local_audio()
        if _bb is not None:
            _bb.write(local_mic_active=False)


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

    from voice.local_audio_io import resolve_local_mic, resolve_local_speaker
    from voice.local_speaker import init_from_config, shutdown as shutdown_local_speaker

    config_raw = os.environ.get("CONFIG_PATH", "").strip()
    config_path = (
        Path(config_raw)
        if config_raw and Path(config_raw).is_absolute()
        else APP_DIR / (config_raw or "config.yaml")
    )
    try:
        if resolve_local_speaker(config_path):
            local_on = init_from_config(config_path)
            if local_on:
                bb.write(local_speaker_active=True)
                print(
                    "[VoiceService] Local speaker active — "
                    "browser agent audio should be muted"
                )
        if resolve_local_mic(config_path):
            bb.write(local_mic_active=True)
            print(
                "[VoiceService] Local mic configured — "
                "browser mic should be disabled"
            )
    except Exception as exc:
        print(f"[VoiceService] Local audio init failed: {exc}")

    if devmode:
        os.environ["LIVEKIT_DEV_MODE"] = "1"

    print("[VoiceService] Starting LiveKit agent...")
    print(f"[VoiceService] .env loaded from {env_path}")
    print(f"[VoiceService] mode={'dev' if devmode else 'start'}")

    # Pi runs face/servo/camera alongside voice. Production default load_threshold
    # (0.7) rejects dispatch when CPU is busy — frontend connects but agent never joins.
    load_threshold = float(os.getenv("LIVEKIT_LOAD_THRESHOLD", "0.95"))
    worker_port = int(os.getenv("LIVEKIT_WORKER_HTTP_PORT", "0"))
    print(
        f"[VoiceService] worker load_threshold={load_threshold}, "
        f"http_port={worker_port or 'ephemeral'}"
    )

    server = AgentServer.from_server_options(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="campus-greeting-agent",
            initialize_process_timeout=120,
            num_idle_processes=1,
            job_executor_type=JobExecutorType.THREAD,
            load_threshold=load_threshold,
            port=worker_port,
        )
    )

    @server.on("worker_registered")
    def _on_worker_registered(worker_id: str, _server_info: object) -> None:
        print(
            f"[VoiceService] Worker registered with LiveKit "
            f"(id={worker_id}, agent=campus-greeting-agent)"
        )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.slow_callback_duration = 0.1

    try:
        loop.run_until_complete(_run_voice_worker(server, bb, devmode=devmode))
    finally:
        shutdown_local_speaker()
        with contextlib.suppress(Exception):
            bb.write(local_speaker_active=False, local_mic_active=False)
        if not loop.is_closed():
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
    print("[VoiceService] Stopped.")
