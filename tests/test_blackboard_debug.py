#!/usr/bin/env python3
"""
Debug script to dump ALL Blackboard fields and their values.
This will help us see if agent_speaking is actually being written.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401
import sys
import time

from core.blackboard import Blackboard


def main() -> None:
    print("[Debug] Connecting to Blackboard...")
    try:
        bb = Blackboard()
        print("[Debug] ✓ Blackboard connected\n")
    except Exception as exc:
        print(f"[ERROR] Failed to connect: {exc}", file=sys.stderr)
        sys.exit(1)
    
    print("=" * 70)
    print("BLACKBOARD STATE DUMP")
    print("=" * 70)
    
    # Read all fields
    state = bb.read_all()
    
    # Voice-related fields
    print("\n🎤 VOICE FIELDS:")
    print(f"  voice_session_active = {state.get('voice_session_active')}")
    print(f"  agent_speaking = {state.get('agent_speaking')}")
    print(f"  user_speaking = {state.get('user_speaking')}")
    print(f"  conv_state = {state.get('conv_state')}")
    print(f"  conv_emotion = {state.get('conv_emotion')}")
    print(f"  amplitude_fast = {state.get('amplitude_fast')}")
    print(f"  amplitude_slow = {state.get('amplitude_slow')}")
    
    # Arm fields
    print("\n🤖 ARM FIELDS:")
    print(f"  arm_a0 = {state.get('arm_a0')}")
    print(f"  arm_a1 = {state.get('arm_a1')}")
    print(f"  arm_a2 = {state.get('arm_a2')}")
    print(f"  arm_a3 = {state.get('arm_a3')}")
    
    # System fields
    print("\n⚙️  SYSTEM FIELDS:")
    print(f"  running = {state.get('running')}")
    print(f"  face_detected = {state.get('face_detected')}")
    print(f"  servo_pan = {state.get('servo_pan')}")
    print(f"  servo_tilt = {state.get('servo_tilt')}")
    
    print("\n" + "=" * 70)
    print(f"Total fields: {len(state)}")
    print("=" * 70)
    
    # Now monitor changes
    print("\n\nMonitoring agent_speaking for 30 seconds...")
    print("Talk to the robot to see if agent_speaking changes!\n")
    
    last_agent_speaking = state.get('agent_speaking')
    last_voice_session = state.get('voice_session_active')
    
    for i in range(300):  # 30 seconds
        time.sleep(0.1)
        state = bb.read_all()
        
        agent_speaking = state.get('agent_speaking')
        voice_session = state.get('voice_session_active')
        
        if agent_speaking != last_agent_speaking:
            print(f"[{i/10:.1f}s] agent_speaking changed: {last_agent_speaking} -> {agent_speaking}")
            last_agent_speaking = agent_speaking
        
        if voice_session != last_voice_session:
            print(f"[{i/10:.1f}s] voice_session_active changed: {last_voice_session} -> {voice_session}")
            last_voice_session = voice_session
    
    print("\nDone monitoring.")


if __name__ == "__main__":
    main()
