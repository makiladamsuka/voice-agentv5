"""Ultra-fast local voice runtime for zero-latency interactions."""

import json
import os
import urllib.request
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

class IntentMatcher:
    def __init__(self, db_path: Path):
        self.client = chromadb.PersistentClient(path=str(db_path))
        self.collection = self.client.get_or_create_collection(
            name="compiled_intents",
            embedding_function=embedding_functions.DefaultEmbeddingFunction(),
        )
        self.intents_map = {}
        
    def load_cache(self, compiled_json_path: Path):
        if not compiled_json_path.exists():
            print("No compiled intents found. Run intent_compiler.py first.")
            return
            
        intents = json.loads(compiled_json_path.read_text(encoding="utf-8"))
        for intent in intents:
            self.intents_map[intent["id"]] = intent
            
        if self.collection.count() > 0:
            print(f"Loaded {len(self.intents_map)} intents from ChromaDB cache.")
            return
            
        print("Indexing intents into ChromaDB for the first time. This only happens once...")
        ids = []
        documents = []
        metadatas = []
        
        for intent in intents:
            for i, utt in enumerate(intent["utterances"]):
                ids.append(f"{intent['id']}_{i}")
                documents.append(utt)
                metadatas.append({"intent_id": intent["id"]})
                
        self.collection.add(ids=ids, documents=documents, metadatas=metadatas)
        print(f"Successfully indexed {len(ids)} utterances.")

    def match(self, text: str) -> dict | None:
        if not text.strip():
            return None
            
        results = self.collection.query(query_texts=[text], n_results=1)
        if not results["metadatas"] or not results["metadatas"][0]:
            return None
            
        distance = results["distances"][0][0]
        print(f"  [Matcher] Top match distance: {distance:.2f}")
        
        # In ChromaDB (L2 distance), lower is better. 
        # A threshold of ~1.4 is usually safe for general matching.
        if distance < 1.4:
            intent_id = results["metadatas"][0][0]["intent_id"]
            return self.intents_map.get(intent_id)
        return None


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
