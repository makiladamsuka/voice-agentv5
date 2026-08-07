"""Poster OCR/vision indexer — scans event posters and extracts metadata."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent
load_dotenv(APP_DIR / ".env")

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
            "nvidia/nemotron-nano-12b-v2-vl:free",
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
        print("No OPENROUTER_API_KEY or GROQ_API_KEY — using fallback metadata from filenames.")

    extracted: list[dict] = []
    print(f"Scanning posters in {assets_dir} ({', '.join(POSTER_CATEGORIES)})...")

    for category in POSTER_CATEGORIES:
        cat_dir = assets_dir / category
        cat_dir.mkdir(parents=True, exist_ok=True)
        for file_path in sorted(cat_dir.iterdir()):
            if file_path.suffix.lower() not in VALID_EXTENSIONS:
                continue
            print(f"   Processing {category}/{file_path.name}...")
            data = _extract_poster(file_path, clients) if clients else None
            if data is None:
                print(f"   AI extraction skipped/failed for {file_path.name} — creating fallback metadata.")
                stem = file_path.stem
                parts = stem.split("_")
                readable = [p for p in parts if not p.isdigit()]
                if readable:
                    derived_title = " ".join(readable).replace("-", " ").strip().title()
                else:
                    cat_map = {
                        "events": "Campus Event",
                        "competitions": "Competition",
                        "posts": "Campus Post",
                    }
                    base_name = cat_map.get(category, "Campus Highlight")
                    derived_title = f"{base_name} ({parts[-1][-4:] if parts else stem[-4:]})"
                data = {
                    "title": derived_title,
                    "date": None,
                    "time": None,
                    "location": None,
                    "description": f"Details and information regarding {derived_title}.",
                }
            data["source_file"] = file_path.name
            data["category"] = category
            extracted.append(data)
            print(f"   Extracted: {data.get('title', 'Unknown')} ({category})")

    print(f"Poster indexing complete ({len(extracted)} item(s))")
    return extracted

