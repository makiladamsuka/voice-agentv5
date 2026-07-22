"""Facebook feed scraper with disk cache for kiosk carousel."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

FALLBACK_POSTS = [
    {
        "id": "fitmoments_1",
        "full_picture": (
            "https://images.unsplash.com/photo-1571019614242-c5c5dee9f50b"
            "?ixlib=rb-4.0.3&auto=format&fit=crop&w=1000&q=80"
        ),
        "message": (
            "Start your morning right! Join our sunrise yoga sessions every Tuesday "
            "at the main campus quad. Don't forget your mat!"
        ),
        "created_time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
    }
]

RSS_FEED_URL = "https://rss.app/feeds/PpO8cOM0sBcILogo.xml"


def _fetch_url(url: str, headers: dict | None = None, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _scrape_rss() -> list[dict]:
    xml = _fetch_url(RSS_FEED_URL)
    items = re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)
    posts: list[dict] = []
    for i, item in enumerate(items[:5]):
        title_m = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", item, re.DOTALL)
        desc_m = re.search(r"<description><!\[CDATA\[(.*?)\]\]></description>", item, re.DOTALL)
        link_m = re.search(r"<link>(.*?)</link>", item)
        date_m = re.search(r"<pubDate>(.*?)</pubDate>", item)
        img_m = re.search(r'src="(https?://[^"]+)"', item)
        message = (desc_m.group(1) if desc_m else title_m.group(1) if title_m else "").strip()
        if len(message) > 150:
            message = message[:150] + "..."
        posts.append(
            {
                "id": f"rss_{i}_{int(time.time())}",
                "full_picture": img_m.group(1) if img_m else FALLBACK_POSTS[0]["full_picture"],
                "message": message or "New update from FIT Moments!",
                "created_time": date_m.group(1) if date_m else time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()
                ),
            }
        )
    return posts


def _scrape_rapidapi() -> list[dict]:
    api_key = (os.getenv("RAPIDAPI_KEY") or "").strip()
    page_id = (os.getenv("FACEBOOK_PAGE_ID") or "fitmoments").strip()
    if not api_key:
        return []

    url = (
        "https://facebook-pages-scraper2.p.rapidapi.com/get_facebook_posts_details"
        f"?link=https%3A%2F%2Fwww.facebook.com%2F{page_id}&timezone=UTC"
    )
    body = _fetch_url(
        url,
        headers={
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": "facebook-pages-scraper2.p.rapidapi.com",
        },
    )
    data = json.loads(body)
    items = data.get("data", {}).get("posts") or []
    posts: list[dict] = []
    for index, item in enumerate(items[:5]):
        img_url = None
        nodes = (item.get("attachments") or {}).get("all_subattachments", {}).get("nodes") or []
        if nodes:
            img_url = (nodes[0].get("media") or {}).get("image", {}).get("uri")
        message = (
            (item.get("values") or {}).get("text")
            or item.get("text")
            or (item.get("basic_info") or {}).get("title")
            or (item.get("basic_info") or {}).get("text")
            or "New update from FIT Moments!"
        )
        if len(message) > 150:
            message = message[:150] + "..."
        created = (item.get("basic_info") or {}).get("created_time")
        created_time = (
            str(created) if created else time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        )
        posts.append(
            {
                "id": f"rapidapi_{(item.get('basic_info') or {}).get('post_id', index)}_{int(time.time())}",
                "full_picture": img_url or FALLBACK_POSTS[0]["full_picture"],
                "message": message,
                "created_time": created_time,
            }
        )
    return posts


def _write_cache(cache_path: Path, posts: list[dict]) -> None:
    cache_path.write_text(
        json.dumps({"timestamp": int(time.time() * 1000), "posts": posts}, indent=2),
        encoding="utf-8",
    )


def get_facebook_posts(cache_path: Path, cache_minutes: int = 10) -> list[dict]:
    """Return cached Facebook posts, refreshing when stale."""
    cache_ms = cache_minutes * 60 * 1000
    cached_posts = None
    last_fetch = 0

    if cache_path.is_file():
        try:
            parsed = json.loads(cache_path.read_text(encoding="utf-8"))
            cached_posts = parsed.get("posts")
            last_fetch = int(parsed.get("timestamp", 0))
        except Exception:
            pass

    needs_refresh = not cached_posts or (int(time.time() * 1000) - last_fetch > cache_ms)
    if needs_refresh:
        try:
            posts = _scrape_rapidapi()
            if not posts:
                posts = _scrape_rss()
            if posts:
                _write_cache(cache_path, posts)
                cached_posts = posts
                print(f"[facebook_feed] Cached {len(posts)} post(s)")
        except Exception as exc:
            print(f"[facebook_feed] Scrape failed: {exc}")

    return cached_posts or FALLBACK_POSTS
