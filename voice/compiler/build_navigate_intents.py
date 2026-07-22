import json
import os
import sys
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(APP_DIR / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

def generate_navigation_utterances(destination: str, client: OpenAI) -> list[str]:
    prompt = f"""
You are an AI generating voice assistant training data for a university campus.
Generate a JSON object containing a list of exactly 12 completely different ways a student might ask for directions to the following location using their voice: "{destination}"

Rules:
1. Include casual phrasing.
2. Include synonyms if applicable (e.g., if it's a washroom, include toilet, restroom, loo; if it's a lab, include laboratory).
3. Use different sentence structures (e.g., "where is", "take me to", "I need to find", "directions to", "how do I get to").
4. Keep them realistic for a voice agent.

Return ONLY a valid JSON object with the key "utterances" containing the array of strings. Do not use markdown blocks.
"""
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        data = json.loads(response.choices[0].message.content)
        return data.get("utterances", [])
    except Exception as e:
        print(f"  [ERROR] LLM failed for {destination}: {e}")
        return []

def main():
    if not GROQ_API_KEY:
        print("Missing GROQ_API_KEY in .env!")
        sys.exit(1)

    client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=GROQ_API_KEY)
    
    db_path = APP_DIR / "voice" / "event_db" / "navigate_intents.json"
    if not db_path.exists():
        print(f"Could not find {db_path}")
        sys.exit(1)

    with open(db_path, "r", encoding="utf-8") as f:
        intents = json.load(f)

    # Inject a generic restroom intent to catch ambiguous queries if it doesn't exist
    has_generic = any(i.get("id") == "navigate_generic_restroom" for i in intents)
    if not has_generic:
        print("Injecting generic restroom disambiguation intent...")
        intents.append({
            "id": "navigate_generic_restroom",
            "utterances": [
                "Where is the restroom?",
                "Where are the restrooms?",
                "I need to use the restroom.",
                "Where is the nearest toilet?",
                "Where is the washroom?",
                "I need to go to the bathroom."
            ],
            "response_text": "We have both male and female washrooms nearby. Could you please specify which one you are looking for?",
            "audio_file": "intent_fallback.mp3",  # fallback generic audio for now, or null to force TTS engine
            "action": {}
        })

    print(f"Starting LLM enhancement for {len(intents)} locations...")
    
    updated = 0
    for i, intent in enumerate(intents):
        dest = intent.get("action", {}).get("destination")
        # Generic restroom or things without destination don't need LLM expansion
        if not dest or intent["id"] == "navigate_generic_restroom":
            continue
            
        print(f"[{i+1}/{len(intents)}] Generating variations for: {dest}...")
        new_utterances = generate_navigation_utterances(dest, client)
        
        if new_utterances:
            # Combine old and new, and deduplicate
            existing = set(intent.get("utterances", []))
            for u in new_utterances:
                existing.add(u)
            intent["utterances"] = list(existing)
            print(f"  -> Now has {len(intent['utterances'])} diverse utterances.")
            updated += 1
            
    with open(db_path, "w", encoding="utf-8") as f:
        json.dump(intents, f, indent=2)
        
    print(f"\nDone! Enhanced {updated} locations. You can now restart the robot to rebuild the ChromaDB index.")

if __name__ == "__main__":
    main()
