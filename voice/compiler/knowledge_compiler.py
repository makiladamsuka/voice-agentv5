"""
knowledge_compiler.py — Compiles voice/knowledge_base.yaml into
voice/event_db/knowledge_intents.json (same schema as the other domain files).

Run this script after editing knowledge_base.yaml:
    python voice/compiler/knowledge_compiler.py

The robot picks up the new file automatically on the next startup, or
you can POST /api/reload-nlu from the admin panel to hot-reload without restart.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is not installed. Run: pip install pyyaml")
    sys.exit(1)

APP_DIR = Path(__file__).resolve().parent.parent.parent
KB_PATH = APP_DIR / "voice" / "knowledge_base.yaml"
OUT_PATH = APP_DIR / "voice" / "event_db" / "knowledge_intents.json"

# Common question prefixes to auto-expand aliases with
_QUESTION_PREFIXES = [
    "what is ",
    "what are ",
    "tell me about ",
    "can you tell me about ",
    "explain ",
    "who is ",
    "what do you know about ",
    "i want to know about ",
    "what about ",
    "describe ",
]


def _expand_utterances(aliases: list[str]) -> list[str]:
    """Generate extra utterance variants from alias phrases."""
    seen: set[str] = set()
    result: list[str] = []

    def _add(u: str) -> None:
        u = u.strip().lower()
        if u and u not in seen:
            seen.add(u)
            result.append(u)

    for alias in aliases:
        alias_clean = alias.strip()
        _add(alias_clean)

        # Add a plain question mark variant
        _add(alias_clean + "?")

        # For short noun phrases, prepend question starters
        words = alias_clean.lower().split()
        if len(words) <= 5:
            for prefix in _QUESTION_PREFIXES:
                _add(prefix + alias_clean)

    return result


def compile_knowledge_base(kb_path: Path = KB_PATH, out_path: Path = OUT_PATH) -> int:
    """Compile YAML → JSON. Returns the number of intents written."""
    if not kb_path.exists():
        print(f"ERROR: Knowledge base not found at {kb_path}")
        return 0

    with open(kb_path, encoding="utf-8") as f:
        topics = yaml.safe_load(f) or []

    if not isinstance(topics, list):
        print("ERROR: knowledge_base.yaml must be a list of topic entries.")
        return 0

    intents: list[dict] = []
    for entry in topics:
        topic_id = str(entry.get("topic", "")).strip()
        if not topic_id:
            print(f"WARNING: Skipping entry with missing 'topic' field: {entry}")
            continue

        aliases: list[str] = entry.get("aliases") or []
        if not isinstance(aliases, list):
            aliases = [str(aliases)]

        reply = str(entry.get("reply", "")).strip()
        if not reply:
            print(f"WARNING: Skipping topic '{topic_id}' — no reply text.")
            continue

        utterances = _expand_utterances(aliases)
        if not utterances:
            print(f"WARNING: Topic '{topic_id}' has no aliases — skipping.")
            continue

        intents.append({
            "id": f"knowledge_{topic_id}",
            "utterances": utterances,
            "response_text": reply,
            "audio_file": None,
            "action": {},
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(intents, f, indent=2, ensure_ascii=False)

    print(f"[OK] Compiled {len(intents)} knowledge intents -> {out_path}")
    print("   Reload the NLU runtime or restart the robot for changes to take effect.")
    return len(intents)


if __name__ == "__main__":
    compile_knowledge_base()
