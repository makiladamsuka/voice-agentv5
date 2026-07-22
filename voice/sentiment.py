"""VADER sentiment → conv_emotion mapping for robot TFT expressions."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.blackboard import Blackboard

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    _analyzer = SentimentIntensityAnalyzer()
except ImportError:
    _analyzer = None


def derive_conv_emotion(text: str, *, is_agent: bool = False) -> str | None:
    """Map utterance text to a conversation emotion label."""
    if _analyzer is None or not text or len(text.split()) < 2:
        return None

    word_count = len(text.split())
    comp = _analyzer.polarity_scores(text)["compound"]

    emotion = "engaged"
    if comp > 0.6:
        emotion = "happy"
    elif comp > 0.2:
        emotion = "warm"
    elif comp < -0.2:
        if is_agent or "sorry" in text.lower():
            emotion = "apologetic"
        else:
            emotion = "sad"
    elif comp < -0.6:
        emotion = "angry"

    if -0.2 <= comp <= 0.2 and word_count > 10:
        emotion = "engaged"
    if comp > 0.3 and word_count > 15 and is_agent:
        emotion = "proud"

    return emotion


def write_conv_emotion(
    bb: "Blackboard | None",
    text: str,
    *,
    is_agent: bool = False,
    log_prefix: str = "Vader L2",
) -> str | None:
    """Derive emotion from text and write conv_emotion to the Blackboard."""
    if bb is None:
        return None

    emotion = derive_conv_emotion(text, is_agent=is_agent)
    if emotion is None:
        return None

    bb.write(conv_emotion=emotion)
    role = "Agent" if is_agent else "User"
    print(f"[{log_prefix}] {role} said: '{text[:30]}...' -> {emotion}")
    return emotion


def clear_conv_emotion(bb: "Blackboard | None") -> None:
    if bb is not None:
        bb.write(conv_emotion=None)
