"""Background compiler to generate intents and cache TTS audio for campus items."""

import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(APP_DIR / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

def generate_intents_for_event(event: dict, client: OpenAI) -> dict:
    prompt = f"""
You are configuring a voice agent for a campus kiosk. 
We have the following event/item:
Title: {event.get('title')}
Category: {event.get('category')}
Date: {event.get('date')}
Location: {event.get('location')}
Description: {event.get('description')}

Please generate a JSON object with two fields:
1. "utterances": A list of 10 to 15 different ways a user might ask about this event using their voice (e.g. "Is there a hackathon?", "What is happening at the main hall?", "Tell me about the robotics competition").
2. "response": A friendly, concise, conversational response (1-2 sentences max) that the robot will speak out loud answering those questions. Do not use emojis.

Return ONLY valid JSON.
    """
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)

def generate_tts_audio(text: str, output_path: Path):
    """Generate TTS audio using Deepgram Aura."""
    if not DEEPGRAM_API_KEY:
        raise ValueError("Missing DEEPGRAM_API_KEY in .env")
        
    url = "https://api.deepgram.com/v1/speak?model=aura-luna-en&encoding=linear16&container=wav&sample_rate=48000"
    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
        "Content-Type": "application/json"
    }
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    
    with urllib.request.urlopen(req, timeout=30.0) as response:
        output_path.write_bytes(response.read())

def build_cache():
    db_path = APP_DIR / "voice" / "event_db"
    events_file = db_path / "extracted_events.json"
    
    if not events_file.exists():
        print(f"No extracted events found at {events_file}.")
        print("Waiting for event_indexer.py to extract posters first.")
        return
        
    events = json.loads(events_file.read_text(encoding="utf-8"))
    
    audio_dir = APP_DIR / "assets" / "audio_cache"
    audio_dir.mkdir(parents=True, exist_ok=True)
    
    if not GROQ_API_KEY:
        print("Missing GROQ_API_KEY. Cannot compile intents.")
        return
        
    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=GROQ_API_KEY)
    
    compiled_data = []
    
    print(f"Starting compilation for {len(events)} events...")
    
    for i, event in enumerate(events):
        print(f"\n[{i+1}/{len(events)}] Compiling event: {event.get('title', 'Unknown')}...")
        try:
            intent_data = generate_intents_for_event(event, client)
            
            # Build the frontend action payload
            action_type = "show_event_poster"
            if event.get("category") == "competitions":
                action_type = "show_competition_poster"
            elif event.get("category") == "posts":
                action_type = "show_campus_post"
                
            action = {
                "action": action_type,
                "target": event.get("source_file")
            }
                
            # Create a safe filename for the audio
            safe_name = event.get('source_file', f'item_{i}').replace('.', '_')
            audio_filename = f"event_{i}_{safe_name}.wav"
            audio_path = audio_dir / audio_filename
            
            print(f"  Generated {len(intent_data['utterances'])} keyword intents.")
            print(f"  Response: \"{intent_data['response']}\"")
            print(f"  Generating TTS audio -> {audio_filename}")
            
            generate_tts_audio(intent_data["response"], audio_path)
            
            compiled_data.append({
                "id": f"event_{i}",
                "utterances": intent_data["utterances"],
                "response_text": intent_data["response"],
                "audio_file": audio_filename,
                "action": action
            })
            print("  Success.")
            
        except Exception as e:
            print(f"  [ERROR] Failed compiling {event.get('title')}: {e}")
            
    # Save the compiled intents database
    compiled_db_path = db_path / "compiled_intents.json"
    compiled_db_path.write_text(json.dumps(compiled_data, indent=2), encoding="utf-8")
    
    print(f"\nCompilation complete! Successfully built {len(compiled_data)} cached intents.")
    print(f"Cache saved to {compiled_db_path}")

    # Keep curated smalltalk intents in sync (TTS for any missing audio files)
    ensure_smalltalk_audio()


def ensure_smalltalk_audio():
    """Generate Deepgram TTS for smalltalk_intents.json entries missing audio files."""
    smalltalk_path = APP_DIR / "voice" / "event_db" / "smalltalk_intents.json"
    if not smalltalk_path.exists():
        print("No smalltalk_intents.json — skipping smalltalk TTS.")
        return

    audio_dir = APP_DIR / "assets" / "audio_cache"
    audio_dir.mkdir(parents=True, exist_ok=True)
    intents = json.loads(smalltalk_path.read_text(encoding="utf-8"))
    print(f"\nEnsuring TTS for {len(intents)} smalltalk intents...")
    for intent in intents:
        audio_file = intent.get("audio_file")
        text = intent.get("response_text", "")
        if not audio_file or not text:
            continue
        audio_path = audio_dir / audio_file
        if audio_path.exists():
            print(f"  OK  {audio_file}")
            continue
        try:
            print(f"  TTS {audio_file} — \"{text[:60]}...\"")
            generate_tts_audio(text, audio_path)
        except Exception as e:
            print(f"  [ERROR] {audio_file}: {e}")

if __name__ == "__main__":
    build_cache()
