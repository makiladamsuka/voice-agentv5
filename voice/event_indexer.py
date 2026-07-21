"""Poster OCR/vision indexer — scans event posters and extracts metadata."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

APP_DIR = Path(__file__).resolve().parent.parent
load_dotenv(APP_DIR / ".env")
load_dotenv(APP_DIR / ".env.local")

POSTER_CATEGORIES = ("events", "competitions", "posts")
VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def encode_image(image_path: Path) -> str:
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


def _clients() -> list[tuple[OpenAI, str]]:
    """Return a list of (client, model) pairs to try in order."""
    options: list[tuple[OpenAI, str]] = []

    # 1. Groq Vision Models (Active Instruct Models)
    if os.getenv("GROQ_API_KEY"):
        groq_client = OpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=os.getenv("GROQ_API_KEY"),
        )
        options.append((groq_client, "llama-3.2-11b-vision-instruct"))
        options.append((groq_client, "llama-3.2-90b-vision-instruct"))

    # 2. OpenAI Vision Models
    if os.getenv("OPENAI_API_KEY"):
        openai_client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
        )
        options.append((openai_client, "gpt-4o-mini"))
        options.append((openai_client, "gpt-4o"))

    # 3. Direct Google Gemini (OpenAI compatible endpoint)
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if gemini_key:
        options.append((
            OpenAI(
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                api_key=gemini_key,
            ),
            "gemini-2.0-flash",
        ))

    # 4. OpenRouter Free & Paid Vision Models
    if os.getenv("OPENROUTER_API_KEY"):
        or_client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.getenv("OPENROUTER_API_KEY"),
        )
        # Free vision models first (does not require credits)
        options.append((or_client, "meta-llama/llama-3.2-11b-vision-instruct:free"))
        options.append((or_client, "qwen/qwen-2.5-vl-72b-instruct:free"))
        options.append((or_client, "google/gemini-2.0-flash-exp:free"))
        options.append((or_client, "google/gemini-2.5-flash"))

    return options


def _extract_poster(file_path: Path, clients: list[tuple[OpenAI, str]]) -> dict | None:
    """Try each client in order until one succeeds. Returns extracted dict or None."""
    image_b64 = encode_image(file_path)
    prompt = (
        "Extract details from this event poster. "
        "Return ONLY a JSON object with keys: title, date, time, location, description. "
        "Keep values short (one line each). Use null for missing fields."
    )
    for client, model in clients:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_b64}",
                                },
                            },
                        ],
                    }
                ],
                response_format={"type": "json_object"},
                max_tokens=400,  # title+date+time+location+description needs ~80 tokens
            )
            content = response.choices[0].message.content
            if not content:
                continue
            data = json.loads(content)
            if data:
                return data
        except Exception as exc:
            err_str = str(exc)
            # 402 = out of credits on this provider — try next
            if "402" in err_str or "credits" in err_str.lower():
                print(f"   [{model}] out of credits, trying next provider...")
                continue
            # Any other error — log and try next
            print(f"   [{model}] failed: {err_str[:120]}")
            continue
    return None


def index_posters(assets_dir: Path) -> list[dict]:
    """Scan events/competitions/posts posters and extract structured metadata."""
    clients = _clients()
    if not clients:
        print("No OPENROUTER_API_KEY or GROQ_API_KEY — skipping poster indexing")
        return []

    extracted: list[dict] = []
    print(f"Scanning posters in {assets_dir} ({', '.join(POSTER_CATEGORIES)})...")

    for category in POSTER_CATEGORIES:
        cat_dir = assets_dir / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        for file_path in sorted(cat_dir.iterdir()):
            if file_path.suffix.lower() not in VALID_EXTENSIONS:
                continue
            print(f"   Processing {category}/{file_path.name}...")
            data = _extract_poster(file_path, clients)
            if data is None:
                print(f"   All providers failed for {file_path.name} — skipping.")
                continue
            data["source_file"] = file_path.name
            data["category"] = category
            extracted.append(data)
            print(f"   Extracted: {data.get('title', 'Unknown')} ({category})")

    print(f"Poster indexing complete ({len(extracted)} item(s))")
    return extracted
