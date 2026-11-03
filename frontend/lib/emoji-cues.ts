export interface EmojiCue {
  emoji: string;
  codePoint: string; // Hex for animated WebP/GIF (e.g. "1f44b" for 👋)
  animation: "wave" | "bounce" | "pulse" | "wobble" | "float" | "tilt";
  label: string;
}

/**
 * Determine the active emoji, its Google animated codepoint, and animation cue.
 */
export function getEmojiCue(
  transcript: string,
  convState: string,
  emotion: string = "idle",
  faceCount: number = 0
): EmojiCue {
  const lower = (transcript || "").toLowerCase().trim();

  // 1. Crowd / Multi-person arrival
  if (
    faceCount >= 2 ||
    lower.includes("everyone") ||
    lower.includes("all of you") ||
    lower.includes("crew") ||
    lower.includes("friends")
  ) {
    if (lower.includes("hi") || lower.includes("hello") || lower.includes("welcome")) {
      return { emoji: "🎉", codePoint: "1f389", animation: "bounce", label: "Party" };
    }
  }

  // 2. Greetings & Salutations
  if (
    lower.startsWith("hi") ||
    lower.startsWith("hello") ||
    lower.startsWith("hey") ||
    lower.includes("good morning") ||
    lower.includes("good afternoon") ||
    lower.includes("good evening")
  ) {
    return { emoji: "👋", codePoint: "1f44b", animation: "wave", label: "Waving Hand" };
  }

  // 3. Departures
  if (
    lower.includes("bye") ||
    lower.includes("goodbye") ||
    lower.includes("see you") ||
    lower.includes("take care")
  ) {
    return { emoji: "🤚", codePoint: "1f91a", animation: "wave", label: "Raised Back of Hand" };
  }

  // 4. Gratitude / Politeness
  if (lower.includes("thank") || lower.includes("welcome") || lower.includes("pleasure")) {
    return { emoji: "🙏", codePoint: "1f64f", animation: "pulse", label: "Folded Hands" };
  }

  // 5. Questions about location, direction, places
  if (
    lower.includes("where") ||
    lower.includes("how do i get") ||
    lower.includes("direction") ||
    lower.includes("location") ||
    lower.includes("hall") ||
    lower.includes("stall") ||
    lower.includes("auditorium") ||
    lower.includes("map")
  ) {
    return { emoji: "📍", codePoint: "1f4cd", animation: "bounce", label: "Round Pushpin" };
  }

  // 6. Time, schedule, timetable
  if (
    lower.includes("time") ||
    lower.includes("schedule") ||
    lower.includes("timetable") ||
    lower.includes("when") ||
    lower.includes("start") ||
    lower.includes("close")
  ) {
    return { emoji: "⏱️", codePoint: "23f1_fe0f", animation: "pulse", label: "Stopwatch" };
  }

  // 7. Tech / Robot / AI questions
  if (
    lower.includes("robot") ||
    lower.includes("who are you") ||
    lower.includes("nema") ||
    lower.includes("ai") ||
    lower.includes("what can you do")
  ) {
    return { emoji: "🤖", codePoint: "1f916", animation: "bounce", label: "Robot" };
  }

  // 8. Jokes / Humor / Fun
  if (
    lower.includes("joke") ||
    lower.includes("haha") ||
    lower.includes("funny") ||
    lower.includes("laugh")
  ) {
    return { emoji: "😂", codePoint: "1f602", animation: "wobble", label: "Face with Tears of Joy" };
  }

  // 9. Emotional Expressions
  if (emotion === "excited") {
    return { emoji: "🤩", codePoint: "1f929", animation: "bounce", label: "Star-Struck" };
  }
  if (emotion === "happy") {
    return { emoji: "😃", codePoint: "1f603", animation: "bounce", label: "Grinning Face with Big Eyes" };
  }
  if (emotion === "curious") {
    return { emoji: "🧐", codePoint: "1f9d0", animation: "tilt", label: "Face with Monocle" };
  }

  // 10. Conversation States (Real-time feedback)
  if (convState === "thinking") {
    return { emoji: "🤔", codePoint: "1f914", animation: "pulse", label: "Thinking Face" };
  }
  if (convState === "listening") {
    return { emoji: "👂", codePoint: "1f442", animation: "pulse", label: "Ear" };
  }
  if (convState === "speaking") {
    return { emoji: "💬", codePoint: "1f4ac", animation: "pulse", label: "Speech Balloon" };
  }

  // Default idle presence
  return { emoji: "✨", codePoint: "2728", animation: "float", label: "Sparkles" };
}
