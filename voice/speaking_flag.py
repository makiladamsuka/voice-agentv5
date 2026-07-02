"""Simple file-based flag for agent_speaking state.

This allows VoiceService (running in its own thread/process) to communicate
the agent_speaking state to external scripts like test_talk_runner.py.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path


_FLAG_FILE = Path("/tmp/voice_agent_speaking.json")


def write_speaking_flag(speaking: bool) -> None:
    """Write the agent_speaking flag to a shared file.
    
    Args:
        speaking: True when agent is speaking, False otherwise.
    """
    try:
        data = {"agent_speaking": speaking}
        
        # Atomic write with file locking
        temp_file = _FLAG_FILE.with_suffix(".tmp")
        with open(temp_file, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(data, f)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        
        temp_file.replace(_FLAG_FILE)
    except Exception as e:
        # Don't crash voice service if file write fails
        print(f"[SpeakingFlag] Warning: Failed to write flag: {e}")


def read_speaking_flag() -> bool:
    """Read the agent_speaking flag from the shared file.
    
    Returns:
        True if agent is speaking, False otherwise.
    """
    try:
        if not _FLAG_FILE.exists():
            return False
        
        with open(_FLAG_FILE, "r") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return data.get("agent_speaking", False)
    except Exception:
        return False


def clear_speaking_flag() -> None:
    """Clear the speaking flag file (for cleanup)."""
    try:
        if _FLAG_FILE.exists():
            _FLAG_FILE.unlink()
    except Exception:
        pass
