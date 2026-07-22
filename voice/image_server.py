"""Backward-compatible re-export — use voice.media_server.MediaServer."""

from voice.media_server import ImageServer, MediaServer

__all__ = ["ImageServer", "MediaServer"]
