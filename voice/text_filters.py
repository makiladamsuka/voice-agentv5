"""TTS text transforms to strip LLM tool-syntax leaks before speech."""

from __future__ import annotations

import re
from collections.abc import AsyncIterable

# Full <function ...>...</function> blocks (with or without '>' after the name)
_FUNCTION_TAG_RE = re.compile(
    r"<function[^<]*?</function\s*>",
    re.IGNORECASE | re.DOTALL,
)
# Bare Groq/Llama tool leaks (no leading '<'): function=name>{...} including truncated JSON
_BARE_FUNCTION_RE = re.compile(
    r"(?:^|[\s\n])function\s*=\s*[A-Za-z_][\w.]*\s*>?\s*\{[^}]*\}?",
    re.IGNORECASE,
)
# Also: function=name> with no JSON yet, or function=name alone at end of stream
_BARE_FUNCTION_NAME_RE = re.compile(
    r"(?:^|[\s\n])function\s*=\s*[A-Za-z_][\w.]*\s*>?",
    re.IGNORECASE,
)
# Leftover partial JSON tool args
_PARTIAL_TOOL_JSON_RE = re.compile(
    r'\{\s*"[^"]{0,40}"?\s*:?\s*"?[^}"]*\}?',
)
_FUNCTION_OPEN_RE = re.compile(r"<function[^<]*", re.IGNORECASE)
_FUNCTION_CLOSE_RE = re.compile(r"</function\s*>", re.IGNORECASE)
# Orphan JSON tool-args fragments
_TOOL_JSON_RE = re.compile(
    r"\{\s*\"(?:query|filter_type|event_description|competition_description|"
    r"location_name|from_location|to_location|color|theme|"
    r"question|arguments?)\"\s*:\s*\"[^\"]*\"\s*(?:,\s*\"[^\"]+\"\s*:\s*\"[^\"]*\"\s*)*\}",
    re.IGNORECASE,
)
# tool_call / invoke style leftovers
_TOOL_META_RE = re.compile(
    r"(?:tool_call|tool_calls|invoke_?tool|callable)\s*[:=]?\s*[A-Za-z_][\w.]*",
    re.IGNORECASE,
)

# Keep enough tail to match tags split across TTS token boundaries.
_TAIL_LEN = 96


def _strip_tool_syntax(text: str) -> str:
    text = _FUNCTION_TAG_RE.sub("", text)
    text = _BARE_FUNCTION_RE.sub(" ", text)
    text = _BARE_FUNCTION_NAME_RE.sub(" ", text)
    text = _FUNCTION_OPEN_RE.sub("", text)
    text = _FUNCTION_CLOSE_RE.sub("", text)
    text = _TOOL_JSON_RE.sub("", text)
    text = _PARTIAL_TOOL_JSON_RE.sub("", text)
    text = _TOOL_META_RE.sub("", text)
    # Collapse leftover punctuation/space noise after stripping
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(' \t>{}"')


async def filter_leaked_tool_syntax(text: AsyncIterable[str]) -> AsyncIterable[str]:
    """Remove leaked tool-call markup from streamed TTS text."""
    buffer = ""
    async for chunk in text:
        buffer += chunk
        if len(buffer) <= _TAIL_LEN:
            continue
        flush_to = len(buffer) - _TAIL_LEN
        out = _strip_tool_syntax(buffer[:flush_to])
        buffer = buffer[flush_to:]
        if out:
            yield out

    if buffer:
        out = _strip_tool_syntax(buffer)
        if out:
            yield out
