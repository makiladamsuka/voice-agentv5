import { NextResponse } from "next/server";
import { exec } from "child_process";

let currentVolume = 0.85;

export async function GET() {
  return NextResponse.json({ volume: currentVolume });
}

export async function POST(req: Request) {
  try {
    const { volume } = await req.json();
    if (typeof volume === "number") {
      currentVolume = Math.max(0, Math.min(1, volume));
      const pct = Math.round(currentVolume * 100);

      // If running on Linux (Raspberry Pi), set ALSA or PulseAudio master volume
      if (process.platform === "linux") {
        exec(`amixer sset -q Master ${pct}% || pactl set-sink-volume @DEFAULT_SINK@ ${pct}%`, () => {
          // Silent callback
        });
      }
    }
    return NextResponse.json({ success: true, volume: currentVolume });
  } catch (error) {
    return NextResponse.json({ success: false, error: String(error) }, { status: 500 });
  }
}
