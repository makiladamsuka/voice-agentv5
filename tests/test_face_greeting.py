"""Tests for random face greetings."""

import re
import time

from core.blackboard import Blackboard
from core.face_greeting import FaceGreetingMonitor
from voice.greetings import generate_random_face_greeting


def test_generate_random_face_greeting_short_plain_text():
    for _ in range(20):
        text = generate_random_face_greeting()
        assert text
        assert "name" not in text.lower()
        assert "enroll" not in text.lower()
        assert len(text) < 80
        assert re.search(r"[A-Za-z]", text)


def test_face_greeting_waits_for_voice_session():
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        face_area_ratio=0.05,
        body_detected=False,
        agent_speaking=False,
        user_speaking=False,
        voice_session_active=False,
    )
    monitor = FaceGreetingMonitor(bb)
    monitor.hold_sec = 0.0
    monitor.cooldown_sec = 0.0

    monitor._face_since = time.time() - 1.0
    state = bb.read(
        "face_detected",
        "face_area_ratio",
        "body_detected",
        "agent_speaking",
        "user_speaking",
        "voice_session_active",
    )
    person_visible = (
        state["face_detected"]
        and float(state["face_area_ratio"]) >= monitor.min_face_area_ratio
    ) or state["body_detected"]
    assert person_visible
    assert not state["voice_session_active"]

    # Should queue when person is visible and agent/user are not speaking
    should_queue = (
        not monitor._greeted_this_visit
        and (time.time() - monitor._face_since) >= monitor.hold_sec
        and not state["agent_speaking"]
        and not state["user_speaking"]
    )
    assert should_queue
