#!/usr/bin/env python3
"""Pre-build .amp.json amplitude sidecars for cached TTS MP3 files."""

from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(APP_DIR))
AUDIO_DIR = APP_DIR / "assets" / "audio_cache"


def main() -> int:
    if not AUDIO_DIR.is_dir():
        print(f"Audio cache not found: {AUDIO_DIR}")
        return 1

    from voice.audio_envelope import build_all_sidecars

    count = build_all_sidecars(AUDIO_DIR)
    print(f"Built {count} new envelope sidecar(s) in {AUDIO_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
