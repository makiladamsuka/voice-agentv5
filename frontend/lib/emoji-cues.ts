export interface EmojiCue {
  emoji: string;
  codePoint: string; // Hex for animated WebP/GIF (e.g. "1f44b" for 👋)
  animation: "wave" | "bounce" | "pulse" | "wobble" | "float" | "tilt";
  label: string;
}

export interface EmojiCueOptions {
  faceDetected?: boolean;
  faceArea?: number;       // Ratio: < 0.035 is distant/aisle, > 0.035 is near
  noFaceSec?: number;      // Seconds since a face was last seen
  idleCycleTick?: number;  // Rotates 0, 1, 2 every 18 seconds
}

/**
 * Determine the active emoji, its Google animated codepoint, and animation cue.
 * Fully synchronized with TFT eye presets, distance presence, and idle cycle.
 */
export function getEmojiCue(
  transcript: string,
  convState: string,
  emotion: string = "idle",
  faceCount: number = 0,
  extra?: EmojiCueOptions
): EmojiCue {
  const lower = (transcript || "").toLowerCase().trim();
  const faceDetected = extra?.faceDetected ?? (faceCount > 0);
  const faceArea = extra?.faceArea ?? 0;
  const noFaceSec = extra?.noFaceSec ?? 0;
  const idleTick = extra?.idleCycleTick ?? 0;
  const emo = (emotion || "idle").toLowerCase();

  // ── 1. ACTIVE DIALOGUE & TRANSCRIPTS (Highest Priority) ──
  if (transcript && transcript.length > 0) {
    // 1a. Crowd / Multi-person greetings
    if (
      faceCount >= 2 ||
      lower.includes("everyone") ||
      lower.includes("all of you") ||
      lower.includes("you two") ||
      lower.includes("both of you") ||
      lower.includes("crowd") ||
      lower.includes("group") ||
      lower.includes("crew") ||
      lower.includes("friends")
    ) {
      if (
        lower.includes("hi") ||
        lower.includes("hello") ||
        lower.includes("welcome") ||
        lower.includes("hey")
      ) {
        return { emoji: "🎉", codePoint: "1f389", animation: "bounce", label: "Party" };
      }
    }

    // 1b. Greetings & Salutations (Auto-greet, Hi, Hello, Hey, Welcome, Morning, etc.)
    const GREETING_PATTERNS = [
      /\b(hi|hello|hey|welcome|howdy)\b/i,
      /\b(good\s+(morning|afternoon|evening))\b/i,
      /\b(morning|evening)\b/i,
    ];
    if (
      GREETING_PATTERNS.some((p) => p.test(lower)) ||
      lower.includes("good to see you") ||
      lower.includes("welcome to")
    ) {
      return { emoji: "👋", codePoint: "1f44b", animation: "wave", label: "Waving Hand" };
    }

    // 1c. Departures
    if (
      lower.includes("bye") ||
      lower.includes("goodbye") ||
      lower.includes("see you") ||
      lower.includes("take care")
    ) {
      return { emoji: "🤚", codePoint: "1f91a", animation: "wave", label: "Raised Back of Hand" };
    }

    // 1d. Gratitude / Politeness
    if (
      lower.includes("thank") ||
      lower.includes("pleasure") ||
      lower.includes("you're welcome") ||
      lower.includes("you are welcome")
    ) {
      return { emoji: "🙏", codePoint: "1f64f", animation: "pulse", label: "Folded Hands" };
    }

    // 1e. Questions about location, direction, places
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

    // 1f. Time, schedule, timetable
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

    // 1g. Tech / Robot / AI questions
    if (
      lower.includes("robot") ||
      lower.includes("who are you") ||
      lower.includes("nema") ||
      lower.includes("ai") ||
      lower.includes("what can you do")
    ) {
      return { emoji: "🤖", codePoint: "1f916", animation: "bounce", label: "Robot" };
    }

    // 1h. Jokes / Humor / Fun
    if (
      lower.includes("joke") ||
      lower.includes("haha") ||
      lower.includes("funny") ||
      lower.includes("laugh")
    ) {
      return { emoji: "😂", codePoint: "1f602", animation: "wobble", label: "Face with Tears of Joy" };
    }
  }

  // ── 2. ACTIVE CONVERSATION STATES (Thinking / Listening / Speaking) ──
  if (convState === "thinking") {
    return { emoji: "🤔", codePoint: "1f914", animation: "pulse", label: "Thinking Face" };
  }
  if (convState === "listening") {
    return { emoji: "👂", codePoint: "1f442", animation: "pulse", label: "Listening Ear" };
  }
  if (convState === "speaking") {
    return { emoji: "💬", codePoint: "1f4ac", animation: "pulse", label: "Speech Balloon" };
  }

  // ── 3. TFT EYE PRESET & ENVIRONMENT SYNCHRONIZATION (When Idle) ──

  // 3a. Long Quiet Period / Sleepy Snooze (TFT eyes drooping or > 2-3 min with no face)
  if (emo === "sleepy" || emo === "bored" || noFaceSec >= 150) {
    if (idleTick % 2 === 0) {
      return { emoji: "🥱", codePoint: "1f971", animation: "pulse", label: "Gentle Yawn" };
    }
    return { emoji: "😴", codePoint: "1f634", animation: "float", label: "Soft Snooze" };
  }

  // 3b. Snapping Awake / Face Lock Perk (TFT eyes excited / surprised / new face)
  if (emo === "excited" || emo === "surprised") {
    if (idleTick % 2 === 0) {
      return { emoji: "😳", codePoint: "1f633", animation: "bounce", label: "Wide Awake" };
    }
    return { emoji: "🤩", codePoint: "1f929", animation: "bounce", label: "Star-Struck" };
  }

  // 3c. Cheerful / Happy (TFT eyes happy)
  if (emo === "happy" || emo === "cheerful" || emo === "warm" || emo === "amused") {
    return { emoji: "😃", codePoint: "1f603", animation: "bounce", label: "Big Grin" };
  }

  // 3d. Glancing Left / Right (TFT eyes looking around)
  if (emo.includes("looking_left") || emo.includes("looking_right")) {
    return { emoji: "👀", codePoint: "1f440", animation: "tilt", label: "Looking Around" };
  }

  // ── 4. PERSON IN VIEW (Distantly in the Aisle) ──
  // Detected in camera, but far enough (faceArea < 0.035): wave or show speech bubble to invite them closer
  if (faceDetected && faceArea > 0 && faceArea < 0.035) {
    if (idleTick % 2 === 0) {
      return { emoji: "👋", codePoint: "1f44b", animation: "wave", label: "Gentle Wave" };
    }
    return { emoji: "💬", codePoint: "1f4ac", animation: "pulse", label: "Speech Bubble" };
  }

  // ── 5. IDLE BASELINE CYCLE (Gently alternates every 15-20 seconds) ──
  const baselineCycle = idleTick % 3;
  if (baselineCycle === 0) {
    return { emoji: "✨", codePoint: "2728", animation: "float", label: "Sparkles" };
  } else if (baselineCycle === 1) {
    return { emoji: "🤖", codePoint: "1f916", animation: "bounce", label: "Robot" };
  } else {
    return { emoji: "👀", codePoint: "1f440", animation: "tilt", label: "Looking around" };
  }
}
