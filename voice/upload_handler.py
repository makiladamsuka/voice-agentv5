"""Poster upload and upload-status helpers for the kiosk portal."""

from __future__ import annotations

import cgi
import json
import re
import time
from io import BytesIO
from pathlib import Path

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_CATEGORIES = ("events", "competitions", "posts")


def _safe_category(category: str, allowed: tuple[str, ...]) -> str:
    cat = (category or "posts").strip().lower()
    return cat if cat in allowed else "posts"


def _safe_filename(name: str) -> str:
    base = Path(name).name
    return f"{int(time.time() * 1000)}_{re.sub(r'[^a-zA-Z0-9._-]', '_', base)}"


def handle_upload_poster(
    rfile,
    headers,
    assets_dir: Path,
    allowed_categories: tuple[str, ...] = DEFAULT_CATEGORIES,
) -> tuple[int, dict]:
    """Parse multipart POST and save poster to assets/{category}/."""
    content_type = headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        return 400, {"error": "Expected multipart/form-data"}

    form = cgi.FieldStorage(
        fp=rfile,
        headers=headers,
        environ={
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": headers.get("Content-Length", "0"),
        },
    )

    file_field = form["poster"] if "poster" in form else None
    if file_field is None or not getattr(file_field, "file", None):
        return 400, {"error": "No poster file provided."}

    category = _safe_category(form.getvalue("category", "posts"), allowed_categories)
    original_name = getattr(file_field, "filename", None) or "upload.jpg"
    file_name = _safe_filename(original_name)

    target_dir = assets_dir / category
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / file_name

    data = file_field.file.read()
    file_path.write_bytes(data)

    return 200, {
        "success": True,
        "message": "Poster uploaded successfully.",
        "fileName": file_name,
        "category": category,
    }


def get_upload_status(
    assets_dir: Path,
    extracted_events_path: Path,
    allowed_categories: tuple[str, ...] = DEFAULT_CATEGORIES,
) -> dict:
    """Scan asset folders and join extracted event metadata."""
    latest_time = 0.0
    latest_file_url = ""
    latest_category = ""
    all_files: list[dict] = []

    extracted_events: list[dict] = []
    if extracted_events_path.is_file():
        try:
            extracted_events = json.loads(extracted_events_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    for category in allowed_categories:
        category_dir = assets_dir / category
        if not category_dir.is_dir():
            continue
        for file_path in category_dir.iterdir():
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in VALID_EXTENSIONS:
                continue
            mtime_ms = file_path.stat().st_mtime * 1000
            file_url = f"/api/image?path={category}/{file_path.name}"
            extracted = next(
                (e for e in extracted_events if e.get("source_file") == file_path.name),
                None,
            )
            entry = {
                "url": file_url,
                "category": category,
                "mtimeMs": mtime_ms,
                "name": file_path.name,
                "extracted": extracted,
            }
            all_files.append(entry)
            if mtime_ms > latest_time:
                latest_time = mtime_ms
                latest_file_url = file_url
                latest_category = category

    all_files.sort(key=lambda x: x["mtimeMs"], reverse=True)
    return {
        "lastUpload": latest_time,
        "latestFileUrl": latest_file_url,
        "latestCategory": latest_category,
        "allFiles": all_files,
    }


def serve_image(assets_dir: Path, query_path: str) -> tuple[int, bytes, str]:
    """Serve a single image from assets/ with path traversal protection."""
    if not query_path or ".." in query_path:
        return 403, b"Forbidden", "text/plain"

    clean = query_path.lstrip("/")
    if clean.startswith("assets/"):
        clean = clean[len("assets/") :]

    file_path = (assets_dir / clean).resolve()
    try:
        file_path.relative_to(assets_dir.resolve())
    except ValueError:
        return 403, b"Forbidden", "text/plain"

    if not file_path.is_file():
        return 404, b"Not found", "text/plain"

    suffix = file_path.suffix.lower()
    content_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")
    return 200, file_path.read_bytes(), content_type
