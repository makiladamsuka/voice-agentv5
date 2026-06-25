"""Tests for random face greetings."""

import re

from voice.greetings import generate_random_face_greeting


def test_generate_random_face_greeting_short_plain_text():
    for _ in range(20):
        text = generate_random_face_greeting()
        assert text
        assert "name" not in text.lower()
        assert "enroll" not in text.lower()
        assert len(text) < 80
        assert re.search(r"[A-Za-z]", text)
