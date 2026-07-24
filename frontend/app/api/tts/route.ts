import { NextRequest, NextResponse } from "next/server";

async function handleTts(text: string) {
  const apiKey = process.env.NEXT_PUBLIC_DEEPGRAM_API_KEY;

  if (!apiKey) {
    return NextResponse.json(
      { error: "Deepgram API key not configured" },
      { status: 500 }
    );
  }

  const response = await fetch(
    "https://api.deepgram.com/v1/speak?model=aura-luna-en&encoding=linear16&container=wav&sample_rate=48000",
    {
      method: "POST",
      headers: {
        Authorization: `Token ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ text }),
    }
  );

  if (!response.ok) {
    const errorText = await response.text();
    console.error("[TTS Proxy] Deepgram API Error:", response.status, errorText);
    return NextResponse.json(
      { error: `Deepgram API error: ${response.statusText}` },
      { status: response.status }
    );
  }

  return new NextResponse(response.body, {
    headers: {
      "Content-Type": "audio/wav",
      "Cache-Control": "public, max-age=3600",
    },
  });
}

export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    if (!body.text) {
      return NextResponse.json(
        { error: "Missing text in request body" },
        { status: 400 }
      );
    }
    return await handleTts(body.text);
  } catch (error: any) {
    console.error("[TTS Proxy] Internal Error:", error);
    return NextResponse.json(
      { error: error.message || "Internal server error" },
      { status: 500 }
    );
  }
}

export async function GET(req: NextRequest) {
  try {
    const { searchParams } = new URL(req.url);
    const text = searchParams.get("text");
    if (!text) {
      return NextResponse.json(
        { error: "Missing text in query parameters" },
        { status: 400 }
      );
    }
    return await handleTts(text);
  } catch (error: any) {
    console.error("[TTS Proxy] Internal Error:", error);
    return NextResponse.json(
      { error: error.message || "Internal server error" },
      { status: 500 }
    );
  }
}
