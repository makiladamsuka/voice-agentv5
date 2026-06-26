#!/usr/bin/env python3
"""
Blackboard Monitor — Debug tool to watch agent_speaking flag in real-time.

This simple script reads the Blackboard continuously and shows you the current
value of the agent_speaking flag. Use this to verify that the voice agent is
actually setting the flag when it speaks.

How to run::

    python tests/test_blackboard_monitor.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401
import sys
import time

from core.blackboard import Blackboard
from voice.speaking_flag import read_speaking_flag


def main() -> None:
    print("[Monitor] Starting Blackboard monitor...")
    print("[Monitor] This will show you the agent_speaking flag in real-time")
    print("[Monitor] Press Ctrl+C to stop\n")
    
    try:
        bb = Blackboard()
        print("[Monitor] ✓ Connected to Blackboard\n")
    except Exception as exc:
        print(f"[ERROR] Failed to connect to Blackboard: {exc}", file=sys.stderr)
        print("[ERROR] Make sure start_robot.py is running", file=sys.stderr)
        sys.exit(1)
    
    print("=" * 70)
    print("MONITORING agent_speaking FLAG")
    print("  - Blackboard (in-process)")
    print("  - File flag (cross-process)")
    print("=" * 70)
    print()
    
    last_agent_speaking_bb = None
    last_agent_speaking_file = None
    last_user_speaking = None
    last_voice_session_active = None
    
    try:
        while True:
            bb_data = bb.read()
            
            agent_speaking_bb = bb_data.get("agent_speaking", None)
            agent_speaking_file = read_speaking_flag()
            user_speaking = bb_data.get("user_speaking", None)
            voice_session_active = bb_data.get("voice_session_active", None)
            conv_state = bb_data.get("conv_state", None)
            
            # Show state changes
            if agent_speaking_file != last_agent_speaking_file:
                status = "🟢 SPEAKING" if agent_speaking_file else "🔴 SILENT"
                print(f"[AGENT-FILE] agent_speaking changed: {last_agent_speaking_file} -> {agent_speaking_file} {status}")
                last_agent_speaking_file = agent_speaking_file
            
            if agent_speaking_bb != last_agent_speaking_bb:
                status = "🟢 SPEAKING" if agent_speaking_bb else "🔴 SILENT"
                print(f"[AGENT-BB] agent_speaking changed: {last_agent_speaking_bb} -> {agent_speaking_bb} {status}")
                last_agent_speaking_bb = agent_speaking_bb
            
            if user_speaking != last_user_speaking:
                status = "🎤 SPEAKING" if user_speaking else "🔇 SILENT"
                print(f"[USER]  user_speaking changed: {last_user_speaking} -> {user_speaking} {status}")
                last_user_speaking = user_speaking
            
            if voice_session_active != last_voice_session_active:
                status = "✅ ACTIVE" if voice_session_active else "❌ INACTIVE"
                print(f"[SESSION] voice_session_active changed: {last_voice_session_active} -> {voice_session_active} {status}")
                last_voice_session_active = voice_session_active
            
            # Show periodic status
            time.sleep(0.1)
            
    except KeyboardInterrupt:
        print("\n\n[Monitor] Stopped by user")
    except Exception as exc:
        print(f"\n[ERROR] Monitor crashed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
