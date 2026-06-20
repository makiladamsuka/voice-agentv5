import os
import asyncio
import datetime
from pathlib import Path

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, RunContext, function_tool
from livekit.plugins import openai, deepgram, silero
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Load environment variables
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

# Import dependencies (assuming they are copied to agent/)
from agent.amplitude_tts import AmplitudeTTS, _drain_to_zero
from agent.text_filters import filter_leaked_tool_syntax
from agent.tools import TimeTools, SearchTools

# ── Shared Blackboard ──────────────────────────────────────────────────────────
_bb = None

def _set_bb_state(**kwargs):
    if _bb:
        _bb.write(**kwargs)

# ── State 1: Mood Tracking (VADER) ──────────────────────────────────────────
_analyzer = SentimentIntensityAnalyzer()

def _send_vader_emotion(text: str, is_agent: bool = False):
    """Layer 1: VADER — slow mood backdrop from utterance sentiment."""
    if not text or len(text.split()) < 2:
        return
    words = text.split()
    word_count = len(words)
    comp = _analyzer.polarity_scores(text)["compound"]
    
    emotion = "engaged" # default floor
    
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
    
    # Special context overrides
    if -0.2 <= comp <= 0.2 and word_count > 10:
        emotion = "engaged"
    if comp > 0.3 and word_count > 15 and is_agent:
        emotion = "proud"

    _set_bb_state(emotion=emotion, emotion_source="conversation")
    print(f"🤖 [Vader L1] {'Agent' if is_agent else 'User'} said: '{text[:30]}...' -> {comp:.2f} -> {emotion}")

# ── State 2: Conversation state machine ─────────────────────────────────────────────────
_thinking_task: asyncio.Task | None = None
_awkward_timer_task: asyncio.Task | None = None
_smart_wait_task: asyncio.Task | None = None

async def _thinking_cycle(word_count: int):
    """
    Stage 2 Processing:
    0 - 0.5s: nodding
    0.5s - 2.0s: thinking OR concentrating
    2.0s+: remembering
    """
    _set_conv_state("nodding", "nodding")
    await asyncio.sleep(0.5)

    base_state = "concentrating" if word_count > 15 else "thinking"
    _set_conv_state(base_state, base_state)
    
    await asyncio.sleep(1.5)
    
    _set_conv_state("remembering", "remembering")
    print(f"🧠 [ConvState L2] Transitioned to REMEMBERING...")
    
    while True:
        await asyncio.sleep(3.0)
        _set_conv_state("thinking", "thinking")
        await asyncio.sleep(3.0)
        _set_conv_state("remembering", "remembering")

_session_live = False

async def _awkward_timer():
    await asyncio.sleep(5.0)
    if not _session_live:
        return
    _set_conv_state("waiting", "cheerful")
    print("👁  [ConvState L2] Long pause -> waiting (cheerful)")

def _set_conv_state(state: str, emotion: str | None = None):
    global _thinking_task, _awkward_timer_task, _smart_wait_task
    if _thinking_task and not _thinking_task.done():
        _thinking_task.cancel()
    if _awkward_timer_task and not _awkward_timer_task.done():
        _awkward_timer_task.cancel()
    if _smart_wait_task and not _smart_wait_task.done():
        _smart_wait_task.cancel()
    
    # Map state to conversational motion overlays
    pan_off = 0.0
    tilt_off = 0.0
    if state == "nodding":
        tilt_off = 8.0  # gentle nod down
    elif state == "listening":
        tilt_off = -2.0 # tilt up slightly
        
    _set_bb_state(
        conv_state=state,
        emotion=emotion or state,
        emotion_source="conversation",
        conv_pan_offset=pan_off,
        conv_tilt_offset=tilt_off
    )
    print(f"👁  [ConvState L2] -> {state} ({emotion or state})")


def prewarm(proc: agents.JobProcess):
    silero.VAD.load(
        min_speech_duration=0.1,
        min_silence_duration=0.2,
        prefix_padding_duration=0.2
    )

class SimpleVoiceAgent(Agent, TimeTools, SearchTools):
    def __init__(self):
        try:
            from agent.prompt import SYSTEM_INSTRUCTIONS
        except ImportError:
            SYSTEM_INSTRUCTIONS = "You are a helpful assistant."
        super().__init__(instructions=SYSTEM_INSTRUCTIONS)

async def entrypoint(ctx: agents.JobContext):
    global _thinking_task, _awkward_timer_task, _smart_wait_task, _session_live

    session = AgentSession(
        turn_handling=agents.TurnHandlingOptions(
            interruption={"mode": "vad"}
        ),
        stt=deepgram.STT(model="nova-2"),
        tts=AmplitudeTTS(model="aura-2-luna-en"),
        vad=silero.VAD.load(
            min_speech_duration=0.1,
            min_silence_duration=0.2,
            prefix_padding_duration=0.2
        ),
        llm=openai.LLM(
            base_url="https://api.groq.com/openai/v1",
            api_key=os.getenv("GROQ_API_KEY"),
            model="llama-3.3-70b-versatile",
            parallel_tool_calls=True,
            _strict_tool_schema=False,
        ),
        tts_text_transforms=[
            "filter_markdown",
            "filter_emoji",
            filter_leaked_tool_syntax,
        ],
    )

    agent = SimpleVoiceAgent()
    print("🚀 Starting LiveKit session...")
    await session.start(room=ctx.room, agent=agent)
    _session_live = True
    _set_bb_state(session_active=True)
    await session.say("Oh hi! I am so happy you are talking to me!")

    async def _hearing_reflex():
        _set_conv_state("listening", "excited")
        await asyncio.sleep(0.4)
        _set_conv_state("listening", "attentive")

    @session.on("user_started_speaking")
    def on_user_started():
        asyncio.create_task(_hearing_reflex())

    @session.on("user_stopped_speaking")
    def on_user_stopped():
        global _awkward_timer_task, _smart_wait_task
        async def _smart_wait():
            await asyncio.sleep(1.2)
            if ctx.room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
                _set_conv_state("waiting", "attentive")
        
        if _smart_wait_task and not _smart_wait_task.done():
            _smart_wait_task.cancel()
        _smart_wait_task = asyncio.create_task(_smart_wait())

    @session.on("user_speech_committed")
    def on_user_speech_committed(msg):
        global _thinking_task
        text = str(msg)
        if hasattr(msg, "content"):  text = msg.content
        elif hasattr(msg, "text"):   text = msg.text
        
        junk = ["uh", "um", "ah", "er", "hmm", "okay", "so", "well"]
        clean_words = [w for w in text.lower().split() if w not in junk]
        word_count = len(clean_words)
        
        _thinking_task = asyncio.create_task(_thinking_cycle(word_count))
        
        try:
            _send_vader_emotion(text, is_agent=False)
        except Exception:
            pass

    @session.on("agent_speech_committed")
    def on_agent_speech_committed(msg):
        _set_conv_state("speaking", "engaged")
        try:
            text = str(msg)
            if hasattr(msg, "content"):  text = msg.content
            elif hasattr(msg, "text"):   text = msg.text
            _send_vader_emotion(text, is_agent=True)
        except Exception as e:
            print("Vader Error:", e)

    @session.on("agent_started_speaking")
    def on_agent_started():
        _set_conv_state("speaking", "engaged")

    @session.on("agent_stopped_speaking")
    def on_agent_stopped():
        global _awkward_timer_task
        _drain_to_zero()
        _set_conv_state("waiting", "attentive")

    try:
        while ctx.room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
            await asyncio.sleep(1)
    finally:
        _session_live = False
        _set_bb_state(session_active=False)

def start_voice_agent(bb):
    """Entry point for the unified robot architecture."""
    global _bb
    _bb = bb
    
    from livekit.agents import WorkerOptions, cli
    # Need to run in an asyncio event loop, or use cli.run_app which handles it
    # We pass empty argv to avoid conflicting with robot start arguments
    import sys
    sys.argv = [sys.argv[0], "start"]
    
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, agent_name="campus-greeting-agent"))
