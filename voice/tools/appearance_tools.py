"""LiveKit tools for robot appearance (physical eye color)."""

from __future__ import annotations

from livekit.agents import RunContext, function_tool

from core.eye_themes import resolve_eye_color


class AppearanceTools:
    @function_tool
    async def set_eye_color(self, color: str, context: RunContext = None) -> str:
        """Change the robot's physical eye color on the TFT displays.

        color: theme or color name — default, white, pistachio, coral, red, green, blue, yellow, cyan, purple, orange.
        """
        from voice import voice_service

        rgb = resolve_eye_color(color)
        if voice_service._bb is not None:
            voice_service._bb.write(eye_color=rgb)
        label = (color or "default").strip().lower() or "default"
        print(f"[AppearanceTools] set_eye_color({label}) -> {rgb}")
        return f"Eye color changed to {label}."
