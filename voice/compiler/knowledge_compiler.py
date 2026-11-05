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
    "what is the ",
    "what are ",
    "what are the ",
    "tell me about ",
    "tell me about the ",
    "can you tell me about ",
    "can you tell me about the ",
    "explain ",
    "who is ",
    "who are ",
    "what do you know about ",
    "i want to know about ",
    "what about ",
    "describe ",
    "give me information about ",
    "tell me more about ",
]


def _expand_utterances(aliases: list[str], button_label: str | None = None) -> list[str]:
    """Generate extra utterance variants from alias phrases."""
    seen: set[str] = set()
    result: list[str] = []

    def _add(u: str) -> None:
        u_clean = u.strip().lower()
        if u_clean and u_clean not in seen:
            seen.add(u_clean)
            result.append(u_clean)

    if button_label:
        _add(button_label)
        _add(button_label.rstrip("?.!"))

    for alias in aliases:
        if not alias:
            continue
        alias_clean = str(alias).strip()
        if not alias_clean:
            continue
        _add(alias_clean)

        # Add a plain question mark variant and stripped punctuation variant
        _add(alias_clean.rstrip("?.!"))
        _add(alias_clean.rstrip("?.!") + "?")

        # For short noun phrases, prepend question starters
        words = alias_clean.lower().split()
        if len(words) <= 6:
            base = alias_clean.rstrip("?.!")
            for prefix in _QUESTION_PREFIXES:
                if not base.lower().startswith(prefix.strip()):
                    _add(prefix + base)

    return result


def _normalize_reply_item(item) -> str:
    """Convert a YAML reply item into a clean string (handling accidental unquoted YAML colons)."""
    if isinstance(item, dict):
        parts = [f"{k}: {v}" for k, v in item.items()]
        return " ".join(" ".join(parts).split())
    return " ".join(str(item or "").split())


def _extract_replies(entry: dict) -> str | list[str]:
    """Extract either a single reply string or a list of reply variations."""
    replies_raw = entry.get("replies")
    reply_raw = entry.get("reply")

    variations: list[str] = []
    if isinstance(replies_raw, list):
        for r in replies_raw:
            s = _normalize_reply_item(r)
            if s and s not in variations:
                variations.append(s)
    elif isinstance(reply_raw, list):
        for r in reply_raw:
            s = _normalize_reply_item(r)
            if s and s not in variations:
                variations.append(s)

    if reply_raw and not isinstance(reply_raw, list):
        s = _normalize_reply_item(reply_raw)
        if s and s not in variations:
            variations.insert(0, s)

    if len(variations) > 1:
        return variations
    if len(variations) == 1:
        return variations[0]
    return ""


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
        if not isinstance(entry, dict):
            continue
        topic_id = str(entry.get("topic", "")).strip()
        if not topic_id:
            print(f"WARNING: Skipping entry with missing 'topic' field: {entry}")
            continue

        aliases: list[str] = entry.get("aliases") or []
        if not isinstance(aliases, list):
            aliases = [str(aliases)]

        response_text = _extract_replies(entry)
        if not response_text:
            print(f"WARNING: Skipping topic '{topic_id}' — no reply text.")
            continue

        button_label = str(entry.get("button_label") or "").strip()
        if not button_label:
            first_alias = str(aliases[0]).strip() if aliases else topic_id.replace("_", " ")
            button_label = first_alias[0].upper() + first_alias[1:] if first_alias else topic_id

        utterances = _expand_utterances(aliases, button_label=button_label)
        if not utterances:
            print(f"WARNING: Topic '{topic_id}' has no aliases — skipping.")
            continue

        intents.append({
            "id": f"knowledge_{topic_id}",
            "button_label": button_label,
            "utterances": utterances,
            "response_text": response_text,
            "audio_file": None,
            "action": {
                "action": "speak",
            },
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(intents, f, indent=2, ensure_ascii=False)

    print(f"[OK] Compiled {len(intents)} knowledge intents -> {out_path}")
    print("   Reload the NLU runtime or restart the robot for changes to take effect.")
    return len(intents)


if __name__ == "__main__":
    compile_knowledge_base()
