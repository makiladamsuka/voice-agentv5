"""Ultra-fast local voice runtime for zero-latency interactions."""

import json
import os
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

import chromadb
from chromadb.utils import embedding_functions
import speech_recognition as sr

try:
    import pygame
    pygame.mixer.init()
except ImportError:
    pygame = None

APP_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(APP_DIR / ".env")

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")


# ── Domain router ─────────────────────────────────────────────────────────────
# Routes a transcript to one domain BEFORE vector retrieval so that, e.g.,
# "what is the time right now" never competes with event utterances like
# "what time is the sports meeting".

# Clock questions only — deliberately narrow so "what time is the meeting"
# still routes to events.
_TIME_RE = re.compile(
    r"\b("
    r"what time is it"
    r"|what(?:'s| is) the time"
    r"|tell me the time"
    r"|current time"
    r"|time (?:right )?now"
    r"|do you have the time"
    r"|what does the clock say"
    r")\b",
    re.IGNORECASE,
)

_NAVIGATE_RE = re.compile(
    r"\b("
    r"where(?:'s| is| are)"
    r"|take me to"
    r"|directions? to"
    r"|how (?:do|can) i (?:get|go) to"
    r"|how to (?:get|go) to"
    r"|navigate to"
    r"|find the"
    r"|show me the way"
    r"|way to the"
    r"|looking for the"
    r"|guide me to"
    r")\b",
    re.IGNORECASE,
)

_SMALLTALK_RE = re.compile(
    r"\b("
    r"hi|hello|hey|yo|sup|what's up"
    r"|good (?:morning|afternoon|evening|night)"
    r"|who are you|what are you|your name|introduce yourself|who am i talking to"
    r"|are you (?:a robot|an ai|ok|okay|there|listening|working)"
    r"|how are you|how(?:'s| is) it going|how do you feel|you good|how have you been"
    r"|what can you do|how can you help|what do you know|help me|can you help"
    r"|what can i ask|your features"
    r"|thank you|thanks|appreciate it|cheers"
    r"|bye|goodbye|see you|talk to you later|catch you later"
    r"|can you hear me|is anyone there|you there|testing|test test"
    r"|nice|cool|awesome|great|interesting|okay|ok|alright"
    r")\b",
    re.IGNORECASE,
)


def route_domain(text: str) -> str:
    """Route a transcript to: 'tool_time', 'navigate', 'smalltalk', or 'events'."""
    if _TIME_RE.search(text):
        return "tool_time"
    if _NAVIGATE_RE.search(text):
        return "navigate"
    if _SMALLTALK_RE.search(text):
        return "smalltalk"
    return "events"


def get_time_reply() -> str:
    """Dynamic reply for the get_time tool."""
    now = datetime.now().strftime("%I:%M %p").lstrip("0")
    return f"It's {now} right now."


# Per-domain acceptance thresholds (L2 distance — lower is better) and the
# minimum top-1/top-2 margin required when the two nearest hits belong to
# DIFFERENT intents (ambiguity rejection).
DOMAIN_THRESHOLDS = {
    "events": 1.2,
    "smalltalk": 1.4,
    "navigate": 1.3,
}
AMBIGUITY_MARGIN = 0.15

# Intent JSON files per domain (all live in voice/event_db/)
DOMAIN_SOURCES = {
    "events": "compiled_intents.json",
    "smalltalk": "smalltalk_intents.json",
    "navigate": "navigate_intents.json",
}


class IntentMatcher:
    def __init__(self, db_path: Path):
        self.client = chromadb.PersistentClient(path=str(db_path))
        self.collection = self.client.get_or_create_collection(
            name="compiled_intents",
            embedding_function=embedding_functions.DefaultEmbeddingFunction(),
        )
        self.intents_map = {}

    def _load_intent_lists(self, compiled_json_path: Path) -> list[tuple[str, dict]]:
        """Load all domain intent files. Returns a list of (domain, intent) pairs."""
        db_dir = compiled_json_path.parent
        pairs: list[tuple[str, dict]] = []
        for domain, filename in DOMAIN_SOURCES.items():
            path = db_dir / filename
            if not path.exists():
                continue
            for intent in json.loads(path.read_text(encoding="utf-8")):
                pairs.append((domain, intent))
        return pairs

    def _cache_is_current(self, expected_docs: int) -> bool:
        """True if Chroma already holds the expected docs WITH domain metadata."""
        if expected_docs == 0 or self.collection.count() != expected_docs:
            return False
        sample = self.collection.peek(limit=1)
        metadatas = sample.get("metadatas") or []
        return bool(metadatas) and "domain" in metadatas[0]

    def load_cache(self, compiled_json_path: Path):
        pairs = self._load_intent_lists(compiled_json_path)
        if not pairs:
            print("No compiled intents found. Run intent_compiler.py first.")
            return

        for _, intent in pairs:
            self.intents_map[intent["id"]] = intent

        expected_docs = sum(len(i.get("utterances") or []) for _, i in pairs)

        # Rebuild Chroma when empty, when utterance count drifted, or when the
        # index predates domain metadata.
        if self._cache_is_current(expected_docs):
            print(
                f"Loaded {len(self.intents_map)} intents "
                f"({expected_docs} utterances) from ChromaDB cache."
            )
            return

        print(
            f"Indexing {len(pairs)} intents into ChromaDB "
            f"({expected_docs} utterances)..."
        )
        try:
            self.client.delete_collection("compiled_intents")
        except Exception:
            pass
        self.collection = self.client.get_or_create_collection(
            name="compiled_intents",
            embedding_function=embedding_functions.DefaultEmbeddingFunction(),
        )

        ids = []
        documents = []
        metadatas = []
        for domain, intent in pairs:
            for i, utt in enumerate(intent.get("utterances") or []):
                ids.append(f"{intent['id']}_{i}")
                documents.append(utt)
                metadatas.append({"intent_id": intent["id"], "domain": domain})

        if ids:
            # Chroma has a batch size limit; chunk if needed
            batch = 200
            for start in range(0, len(ids), batch):
                end = start + batch
                self.collection.add(
                    ids=ids[start:end],
                    documents=documents[start:end],
                    metadatas=metadatas[start:end],
                )
        print(f"Successfully indexed {len(ids)} utterances.")

    def match(self, text: str, domain: str | None = None) -> dict | None:
        """Match a transcript against ONE domain's intents with a confidence gate.

        Accept only when the nearest utterance is below the domain threshold
        AND — if the second-nearest hit belongs to a different intent — the
        distance margin between them is at least AMBIGUITY_MARGIN.
        """
        if not text.strip():
            return None

        if domain is None:
            domain = route_domain(text)
        if domain == "tool_time":
            # Tools are handled by the caller, not by retrieval.
            return None

        results = self.collection.query(
            query_texts=[text],
            n_results=2,
            where={"domain": domain},
        )
        metadatas = results.get("metadatas") or [[]]
        distances = results.get("distances") or [[]]
        if not metadatas[0]:
            print(f"  [Matcher] domain={domain}: no candidates.")
            return None

        d1 = distances[0][0]
        top_intent = metadatas[0][0]["intent_id"]
        threshold = DOMAIN_THRESHOLDS.get(domain, 1.2)

        if len(metadatas[0]) > 1:
            d2 = distances[0][1]
            second_intent = metadatas[0][1]["intent_id"]
            margin = d2 - d1
            print(
                f"  [Matcher] domain={domain} d1={d1:.2f} d2={d2:.2f} "
                f"margin={margin:.2f} ({top_intent} vs {second_intent})"
            )
            # Two different intents too close together → ambiguous, abstain.
            if second_intent != top_intent and margin < AMBIGUITY_MARGIN:
                print("  [Matcher] Rejected: ambiguous top-2 candidates.")
                return None
        else:
            print(f"  [Matcher] domain={domain} d1={d1:.2f} (single candidate)")

        if d1 >= threshold:
            print(f"  [Matcher] Rejected: distance {d1:.2f} >= {threshold}.")
            return None

        return self.intents_map.get(top_intent)


def transcribe_audio(audio_data: bytes) -> str:
    """Send raw WAV/PCM data to Deepgram for extremely fast STT."""
    if not DEEPGRAM_API_KEY:
        print("Missing DEEPGRAM_API_KEY")
        return ""
        
    url = "https://api.deepgram.com/v1/listen?model=nova-2&smart_format=true"
    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
        "Content-Type": "audio/wav" 
    }
    req = urllib.request.Request(url, data=audio_data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10.0) as response:
            raw_response = response.read()
            result = json.loads(raw_response)
            
            # Navigate Deepgram JSON safely
            try:
                transcript = result["results"]["channels"][0]["alternatives"][0]["transcript"]
                if not transcript:
                    print(f"  [STT] Deepgram heard no words. (Raw: {result['results']})")
                
                # Strip noise pops (like "undefined", "undefine", "undefines") from transcripts
                if transcript:
                    words = transcript.strip().split()
                    cleaned = [w for w in words if w.lower().strip(".,?!;:") not in ("undefined", "undefine", "undefines")]
                    transcript = " ".join(cleaned)
                
                return transcript
            except KeyError:
                print(f"  [STT] Unexpected Deepgram JSON: {raw_response}")
                return ""
                
    except Exception as e:
        print(f"  [STT] Request Error: {e}")
        return ""


def play_audio(audio_path: Path) -> bool:
    """Plays an audio file using system players (mpv, ffplay) or pygame to avoid compilation issues."""
    import subprocess
    import shutil
    
    if not audio_path.exists():
        return False
        
    # 1. Try mpv (extremely low latency and quiet)
    if shutil.which("mpv"):
        try:
            subprocess.run(
                ["mpv", "--no-video", "--really-quiet", str(audio_path)],
                check=True
            )
            return True
        except Exception as e:
            print(f"  [Audio] mpv error: {e}")
            
    # 2. Try ffplay
    if shutil.which("ffplay"):
        try:
            subprocess.run(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(audio_path)],
                check=True
            )
            return True
        except Exception as e:
            print(f"  [Audio] ffplay error: {e}")
            
    # 3. Try pygame as a last fallback
    global pygame
    if pygame is not None:
        try:
            pygame.mixer.music.load(str(audio_path))
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.Clock().tick(10)
            return True
        except Exception as e:
            print(f"  [Audio] Pygame error: {e}")
            
    return False


class OfflineVoiceRuntime:
    def __init__(self, bb):
        self.bb = bb
        self.db_path = APP_DIR / "voice" / "event_db"
        self.matcher = IntentMatcher(self.db_path)
        self.matcher.load_cache(self.db_path / "compiled_intents.json")
            
    def process_text_input(self, text: str):
        """Processes a transcript, matches intent, updates Blackboard, and plays audio."""
        self.bb.write(conv_state="thinking", user_text=text)

        # Tool routes (e.g. clock) bypass retrieval entirely
        if route_domain(text) == "tool_time":
            reply = get_time_reply()
            print(f"\n🕐 Tool: get_time → {reply}")
            self.bb.write(
                conv_state="speaking",
                agent_speaking=True,
                agent_text=reply,
                current_action={},
            )
            print(f"🔊 [Audio Playback] {reply}")
            self.bb.write(conv_state="listening", agent_speaking=False)
            return

        # 1. Match Intent instantly via ChromaDB
        intent = self.matcher.match(text)
        
        if intent:
            print(f"\n✅ Match Found! Action: {intent['action']}")
            
            # 2. Write UI Action to Blackboard (Frontend updates screen instantly)
            self.bb.write(
                conv_state="speaking", 
                agent_speaking=True,
                agent_text=intent.get("response_text", ""),
                current_action=intent.get("action", {})
            )
            
            # 3. Play Pre-Recorded Audio (Zero latency)
            audio_file = intent.get("audio_file")
            audio_path = APP_DIR / "assets" / "audio_cache" / audio_file if audio_file else None
            
            if audio_path and audio_path.exists():
                print(f"🔊 Playing audio: {audio_file}")
                played = play_audio(audio_path)
                if not played:
                    print(f"🔊 [Audio Fallback] {intent['response_text']}")
            else:
                print(f"🔊 [Audio Playback] {intent['response_text']}")
                
        else:
            print("\n❌ No match found. (Out of Domain)")
            self.bb.write(
                conv_state="speaking",
                agent_speaking=True,
                agent_text="I'm a campus guide! Try asking me about events or locations.",
                current_action={}
            )
            audio_path = APP_DIR / "assets" / "audio_cache" / "intent_fallback.mp3"
            if audio_path.exists():
                print("🔊 Playing fallback audio...")
                played = play_audio(audio_path)
                if not played:
                    print("🔊 [Audio Fallback] I'm a campus guide! Try asking me about events or locations.")
            else:
                print("🔊 [Audio Playback] I'm a campus guide! Try asking me about events or locations.")
            
        self.bb.write(conv_state="listening", agent_speaking=False)

    def start_hardware_loop(self):
        """Zero-latency hardware microphone loop using SpeechRecognition VAD."""
        r = sr.Recognizer()
        
        # Optimize for fast interactions
        r.energy_threshold = 1000
        r.dynamic_energy_threshold = False
        r.pause_threshold = 0.8  # Stop listening slightly faster when user pauses
        
        print("\n=== Offline Voice Runtime Started ===")
        with sr.Microphone() as source:
            print("Adjusting for ambient noise for 1 second...")
            r.adjust_for_ambient_noise(source, duration=1)
            print("\n✅ Microphone is live! Start speaking to the robot.")
            
            while True:
                try:
                    self.bb.write(conv_state="listening")
                    
                    # Listen until the user stops talking
                    audio = r.listen(source, timeout=None, phrase_time_limit=15)
                    
                    print("\n[🎙️ VAD] Speech captured! Sending to Deepgram...")
                    self.bb.write(
                        conv_state="thinking",
                        user_text="",
                        agent_text="",
                        current_action={}
                    )
                    
                    wav_data = audio.get_wav_data()
                    transcript = transcribe_audio(wav_data)
                    
                    if transcript:
                        print(f"Transcript: '{transcript}'")
                        
                        # Stop microphone stream to prevent feedback loops
                        try:
                            source.stream.stop_stream()
                        except Exception:
                            pass
                            
                        self.process_text_input(transcript)
                        
                        # Flush buffered audio frames and restart mic stream
                        try:
                            import time
                            time.sleep(0.3)  # Wait for reverb/click to settle
                            if hasattr(source.stream, "get_read_available"):
                                while source.stream.get_read_available() > 0:
                                    source.stream.read(source.CHUNK, exception_on_overflow=False)
                            source.stream.start_stream()
                        except Exception:
                            pass
                        
                except sr.WaitTimeoutError:
                    continue
                except Exception as e:
                    print(f"Audio loop error: {e}")

    def simulate_listen_loop(self):
        """For development: Type to test the Intent Matcher without a microphone."""
        print("\n=== Offline Voice Runtime Started ===")
        print("Type a question (or 'quit') to simulate voice input.")
        self.bb.write(conv_state="listening")
        
        while True:
            text = input("\nUser (Mic Transcript): ")
            if text.lower() == "quit":
                break
            self.process_text_input(text)


def start_text_loop(bb=None):
    """Text-based simulator for when audio drivers or LiveKit are broken."""
    print("\n=== Offline Voice Simulator ===")
    print("Type your message and press ENTER. Type 'quit' to exit.")
    
    runtime = OfflineVoiceRuntime(bb)
    while True:
        try:
            text = input("\n[You]: ")
            if text.lower() in ['quit', 'exit']:
                break
            if text.strip():
                runtime.process_text_input(text)
        except KeyboardInterrupt:
            break

if __name__ == "__main__":
    # Test wrapper for running the script directly
    class DummyBB:
        def write(self, **kwargs):
            print(f"[Blackboard Update] {kwargs}")
            
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "text":
        start_text_loop(DummyBB())
    else:
        print("\nStarting hardware microphone loop. (If this crashes, run: python runtime.py text)")
        runtime = OfflineVoiceRuntime(DummyBB())
        runtime.start_hardware_loop()
