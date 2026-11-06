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
    "{room}",
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
SKIP_LABELS = {"staircase", "staircase 1", "staircase 2", "you are here"}


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower().strip()).strip("_")


def _spelling_variants(label: str) -> list[str]:
    """Return surface-form variants for map labels with non-standard spelling."""
    variants = [label]
    norm = re.sub(r"\blabratory\b", "Laboratory", label, flags=re.IGNORECASE)
    norm = re.sub(r"\blecturehall\b", "Lecture hall", norm, flags=re.IGNORECASE)
    if norm not in variants:
        variants.append(norm)
    short_lab = re.sub(r"\blab(?:o)?ratory\s*(\d+)\b", r"Lab \1", norm, flags=re.IGNORECASE)
    if short_lab not in variants:
        variants.append(short_lab)
    short_lab_zero = re.sub(r"\blab(?:o)?ratory\s*(\d)\b", r"Lab 0\1", norm, flags=re.IGNORECASE)
    if short_lab_zero not in variants:
        variants.append(short_lab_zero)
    extra_aliases = {
        "ai research laboratory": ["AI Lab", "AI Laboratory"],
        "evaluator": ["Lift", "Elevator"],
        "game zone": ["Games", "Game Stalls", "Gaming Zone"],
        "software projects": ["Software Project Stalls", "Software Stalls"],
        "research projects": ["Research Project Stalls", "Research Stalls"],
        "company stalls": ["Company Stall", "Industry Stalls"],
        "level 1 projects": ["Level 1 Project Stalls", "First Year Projects"],
        "student projects": ["Student Project Stall"],
        "photo booth": ["Photobooth", "Photo Stall"],
        "historical instrument": ["Historical Instruments", "Historical Instrument Stall"],
    }
    for extra in extra_aliases.get(label.lower().strip(), []):
        if extra not in variants:
            variants.append(extra)
    return variants


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

        utterances: list[str] = []
        seen_utts: set[str] = set()
        for var in _spelling_variants(clean):
            for t in UTTERANCE_TEMPLATES:
                u = t.format(room=var)
                if u.lower() not in seen_utts:
                    seen_utts.add(u.lower())
                    utterances.append(u)

        display_label = re.sub(r"\blabratory\b", "Laboratory", clean, flags=re.IGNORECASE)
        display_label = re.sub(r"\blecturehall\b", "Lecture Hall", display_label, flags=re.IGNORECASE)
        if display_label and display_label[0].islower():
            display_label = display_label[0].upper() + display_label[1:]

        intents.append({
            "id": f"navigate_{slug}",
            "button_label": display_label,
            "utterances": utterances,
            "response_text": f"Let me show you the way to {display_label}.",
            "audio_file": None,
            "action": {"action": "navigate", "destination": clean},
        })

    intents.append({
        "id": "navigate_generic_restroom",
        "button_label": "Washrooms",
        "utterances": [
            "Where is the restroom?",
            "Where are the restrooms?",
            "I need to use the restroom.",
            "Where is the nearest toilet?",
            "Where is the washroom?",
            "Where are the washrooms?",
            "I need to go to the bathroom.",
        ],
        "response_text": "Both Male and Female Washrooms are on this 4th floor along the top-center corridor.",
        "audio_file": None,
        "action": {"action": "speak"},
    })

    return intents


def main():
    intents = build_navigate_intents()
    out_path = APP_DIR / "voice" / "event_db" / "navigate_intents.json"
    out_path.write_text(json.dumps(intents, indent=2), encoding="utf-8")
    print(f"Wrote {len(intents)} navigate intents to {out_path}")
    for intent in intents:
        print(f"  {intent['id']} -> {intent['action'].get('destination', intent.get('button_label', ''))}")


if __name__ == "__main__":
    main()
