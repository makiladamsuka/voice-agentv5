"""Time-aware greeting text for proactive hellos."""

from __future__ import annotations

import random
from datetime import datetime
from typing import Dict, List

_last_seen: Dict[str, datetime] = {}
_last_picked_by_bucket: Dict[str, str] = {}


def _choose_non_repeating(pool: List[str], bucket: str = "default") -> str:
    """Pick a random greeting from *pool* without repeating the last pick in *bucket*."""
    if not pool:
        return "Hello! Welcome to FIT EXPO!"
    if len(pool) == 1:
        return pool[0]
    last = _last_picked_by_bucket.get(bucket)
    candidates = [g for g in pool if g != last] or pool
    chosen = random.choice(candidates)
    _last_picked_by_bucket[bucket] = chosen
    return chosen


def get_time_of_day() -> str:
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def get_context(name: str) -> str:
    now = datetime.now()
    if name not in _last_seen:
        return "first_time"
    diff = (now - _last_seen[name]).total_seconds()
    if diff < 300:
        return "same_session"
    if diff < 3600:
        return "short_break"
    return "returning"


def mark_seen(name: str) -> None:
    _last_seen[name] = datetime.now()


def generate_greeting(name: str, is_known: bool = True) -> str:
    time_of_day = get_time_of_day()

    if not is_known or name == "Unknown":
        unknown_greetings = [
            "Hi there! Welcome to FIT EXPO 2026! I'm Nema!",
            "Hey! Welcome to the Faculty of IT! Come say hi!",
            "Hello! I'm Nema—welcome to FIT EXPO 2026!",
            "Hi there! I don't think we've met yet. Welcome to FIT EXPO!",
        ]
        return _choose_non_repeating(unknown_greetings, "unknown")

    context = get_context(name)
    mark_seen(name)

    if context == "same_session":
        return _choose_non_repeating(
            [
                f"Oh, {name}! Back again?",
                f"Hey {name}, you're back!",
                f"Welcome back, {name}!",
            ],
            f"same_{name}",
        )

    if context == "short_break":
        return _choose_non_repeating(
            [
                f"Hey {name}! How's it going?",
                f"Oh, {name}! Good to see you again.",
                f"{name}! What's up?",
            ],
            f"short_{name}",
        )

    if context == "returning":
        if time_of_day == "morning":
            pool = [f"Good morning, {name}!", f"Morning, {name}! How are you?"]
        elif time_of_day == "afternoon":
            pool = [f"Good afternoon, {name}!", f"Hey {name}! How's your day going?"]
        elif time_of_day == "evening":
            pool = [f"Good evening, {name}!", f"Hey {name}! Still around this evening?"]
        else:
            pool = [f"Hey {name}! Working late?", f"{name}! You're here late."]
        return _choose_non_repeating(pool, f"ret_{name}")

    if time_of_day == "morning":
        pool = [f"Good morning, {name}!", f"Hey {name}! Good morning!"]
    elif time_of_day == "afternoon":
        pool = [f"Good afternoon, {name}!", f"Hey {name}! Good to see you!"]
    elif time_of_day == "evening":
        pool = [f"Good evening, {name}!", f"Hey {name}! Nice to see you!"]
    else:
        pool = [f"Hey {name}!", f"{name}! What brings you here?"]
    return _choose_non_repeating(pool, f"first_{name}")


def generate_presence_greeting() -> str:
    """Generic greeting when someone approaches during an active voice session (no face ID)."""
    time_of_day = get_time_of_day()
    if time_of_day == "morning":
        pool = [
            "Oh! Good morning! Welcome to FIT EXPO!",
            "Hey there! Good morning! Come say hi!",
            "Morning! I see you over there—welcome!",
        ]
    elif time_of_day == "afternoon":
        pool = [
            "Hey! Good afternoon! Welcome to FIT EXPO 2026!",
            "Oh hi! I see you — come talk to me!",
            "Hello there! Good afternoon and welcome!",
        ]
    elif time_of_day == "evening":
        pool = [
            "Hey! Good evening! Want to chat?",
            "Oh, hi! I see you — welcome to FIT EXPO!",
            "Hello! Evening's a great time to talk!",
        ]
    else:
        pool = [
            "Hey! You're here late — hi!",
            "Oh hi! Welcome to the Faculty of IT!",
            "Hello there! Come say hi to Nema!",
        ]
    return _choose_non_repeating(pool, "presence")


def generate_random_face_greeting(
    face_count: int = 1,
    is_crowd_growth: bool = False,
) -> str:
    """Short generic hello when 1 or more faces appear (static strings for 100% TTS WAV cache hits)."""
    if is_crowd_growth:
        return _choose_non_repeating(
            [
                "Oh, more friends joined! Hi everyone!",
                "Look at this crowd! Welcome to FIT EXPO everyone!",
                "Whoa, more people! Welcome everyone!",
                "Hey, the whole crew is here! Hi everyone!",
                "More visitors! Welcome to our 25th anniversary expo!",
            ],
            "crowd_growth",
        )

    time_of_day = get_time_of_day()
    if face_count >= 3:
        pool = [
            "Hi everyone!",
            "Hello everyone! Welcome to FIT EXPO!",
            "Whoa, a whole group! Hi everyone!",
            "Hey everyone! Come say hi to Nema!",
            "Hello friends! Good to see you all!",
            "Welcome to the Faculty of IT, everyone!",
            "Hi everyone! Welcome to FIT EXPO 2026!",
        ]
        if time_of_day == "morning":
            pool.append("Good morning everyone! Welcome to FIT EXPO!")
        elif time_of_day == "afternoon":
            pool.append("Good afternoon everyone! Welcome to FIT EXPO!")
        elif time_of_day == "evening":
            pool.append("Good evening everyone!")
        return _choose_non_repeating(pool, "group")

    if face_count == 2:
        pool = [
            "Hey you two!",
            "Hi both of you! Welcome to FIT EXPO!",
            "Hello there, you two!",
            "Oh, hi you two! Welcome!",
            "Hey there, friends! Come say hi!",
            "Welcome to the Faculty of IT, you two!",
        ]
        if time_of_day == "morning":
            pool.append("Good morning, you two!")
        elif time_of_day == "afternoon":
            pool.append("Good afternoon, you two!")
        elif time_of_day == "evening":
            pool.append("Good evening, you two!")
        return _choose_non_repeating(pool, "pair")

    pool = [
        "Hi!",
        "Hello!",
        "Hey there!",
        "Oh, hi!",
        "Hello there!",
        "Hi there! Welcome to FIT EXPO!",
        "Hey! Good to see you!",
        "Hi! I'm Nema, come say hello!",
        "Welcome to the Faculty of IT!",
        "Hey there! Welcome to FIT EXPO 2026!",
    ]
    if time_of_day == "morning":
        pool.extend(["Good morning!", "Morning! Welcome to FIT EXPO!"])
    elif time_of_day == "afternoon":
        pool.extend(["Good afternoon!", "Hey! Good afternoon!"])
    elif time_of_day == "evening":
        pool.extend(["Good evening!", "Hey! Good evening!"])
    else:
        pool.extend(["Hey! You're up late!", "Hi! Working late?"])
    return _choose_non_repeating(pool, "single")
