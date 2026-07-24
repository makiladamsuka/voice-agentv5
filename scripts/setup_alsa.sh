#!/usr/bin/env bash
# setup_alsa.sh — Fix ALSA sample rate mismatch on Pi 4 for voice-agentv5
#
# Problem: PortAudio opens ALSA at 24 kHz (TTS sample rate).
#          Pi 4 BCM2835 audio hardware defaults to 48 kHz.
#          ALSA silently resamples every callback, burning 5-10% CPU.
#
# Fix: Write ~/.asoundrc to tell ALSA the default output is 24 kHz mono,
#      eliminating the software resampler.
#
# Audio output device:
#   3.5mm jack → hw:0,0   (current setup)
#   HDMI audio → hw:1,0   (uncomment HDMI block below when switching to HDMI display)
#
# Usage (run once on Pi):
#   bash scripts/setup_alsa.sh
#   # Then restart the kiosk stack.

set -euo pipefail

ASOUNDRC="${HOME}/.asoundrc"

# Backup existing config if present
if [[ -f "$ASOUNDRC" ]]; then
  cp "$ASOUNDRC" "${ASOUNDRC}.bak.$(date +%Y%m%d_%H%M%S)"
  echo "[ALSA] Backed up existing ${ASOUNDRC}"
fi

# ── 3.5mm Headphone Jack (hw:0,0) — CURRENT SETUP ────────────────────────────
cat > "$ASOUNDRC" << 'EOF'
# voice-agentv5 ALSA config — Pi 4 audio optimization
# Forces 24 kHz mono to match PortAudio TTS output.
# Eliminates software resampling (saves ~8% CPU per audio callback).
#
# Current output: 3.5mm headphone jack (hw:0,0)
# For HDMI audio: replace hw:0,0 with hw:1,0 and update rate if needed.

pcm.!default {
    type plug
    slave {
        pcm "hw:0,0"
        rate 24000
        channels 1
        format S16_LE
    }
}

ctl.!default {
    type hw
    card 0
}
EOF

# ── HDMI Audio Template (uncomment when switching to HDMI display speaker) ───
# cat > "$ASOUNDRC" << 'EOF'
# pcm.!default {
#     type plug
#     slave {
#         pcm "hw:1,0"
#         rate 48000
#         channels 2
#         format S16_LE
#     }
# }
# ctl.!default {
#     type hw
#     card 1
# }
# EOF

echo "[ALSA] Written: ${ASOUNDRC}"
echo "[ALSA] Sample rate: 24000 Hz mono (matches PortAudio TTS)"
echo ""
echo "Verifying ALSA devices:"
aplay -l 2>/dev/null || echo "  (aplay not found — install alsa-utils)"
echo ""
echo "Done. Restart ./scripts/launch-kiosk-stack.sh to apply."

