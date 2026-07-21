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
    if os.getenv("OPENROUTER_API_KEY"):
        options.append((
            OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=os.getenv("OPENROUTER_API_KEY"),
            ),
            "google/gemini-2.5-flash",
        ))
    if os.getenv("GROQ_API_KEY"):
        # Groq free-tier vision model — good quality, no cost
        options.append((
            OpenAI(
                base_url="https://api.groq.com/openai/v1",
                api_key=os.getenv("GROQ_API_KEY"),
            ),
            "meta-llama/llama-4-scout-17b-16e-instruct",
        ))
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if gemini_key:
        options.append((
            OpenAI(
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                api_key=gemini_key,
            ),
            "gemini-2.5-flash",
        ))
    if os.getenv("OPENAI_API_KEY"):
        options.append((
            OpenAI(
                api_key=os.getenv("OPENAI_API_KEY"),
            ),
            "gpt-4o-mini",
        ))
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
        print("No vision API key found (OPENROUTER_API_KEY, GROQ_API_KEY, GEMINI_API_KEY, or OPENAI_API_KEY) in environment or .env — skipping poster indexing")
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
