"""Generate navigate intents grounded in the Wayfinder map graph.

Reads every room label from Wayfinder.list_rooms() and writes
voice/event_db/navigate_intents.json. Each intent carries a
{"action": "navigate", "destination": <room>} payload; the NLU server
runs live pathfinding at match time and speaks the generated directions,
so no TTS audio is pre-recorded here.

Run directly after map edits:  python -m voice.compiler.nav_intent_builder
"""

import json
import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(APP_DIR))

from voice.wayfinding import Wayfinder  # noqa: E402

UTTERANCE_TEMPLATES = [
    "Where is {room}?",
    "Where is the {room}?",
    "Take me to {room}",
    "Take me to the {room}",
    "Directions to {room}",
    "How do I get to {room}?",
    "How do I get to the {room}?",
    "Show me the way to {room}",
    "Find the {room}",
    "I'm looking for the {room}",
    "Guide me to {room}",
]

# Rooms with no useful spoken label (auto-generated map furniture)
SKIP_LABELS = {"staircase"}


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower().strip()).strip("_")


def build_navigate_intents() -> list[dict]:
    wf = Wayfinder()
    seen: set[str] = set()
    intents: list[dict] = []

    for label in wf.list_rooms():
        clean = label.strip()
        if not clean or clean.lower() in SKIP_LABELS:
            continue
        slug = _slug(clean)
        if slug in seen:  # duplicate labels (e.g. washrooms per floor)
            continue
        seen.add(slug)

        intents.append({
            "id": f"navigate_{slug}",
            "utterances": [t.format(room=clean) for t in UTTERANCE_TEMPLATES],
            "response_text": f"Let me show you the way to {clean}.",
            "audio_file": None,
            "action": {"action": "navigate", "destination": clean},
        })

    return intents


def main():
    intents = build_navigate_intents()
    out_path = APP_DIR / "voice" / "event_db" / "navigate_intents.json"
    out_path.write_text(json.dumps(intents, indent=2), encoding="utf-8")
    print(f"Wrote {len(intents)} navigate intents to {out_path}")
    for intent in intents:
        print(f"  {intent['id']} -> {intent['action']['destination']}")


if __name__ == "__main__":
    main()
