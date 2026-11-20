#!/usr/bin/env python3
"""Collect public Instagram posts through unauthenticated public feed bridges.

No Instagram login, password, session cookie, private profile, or CAPTCHA bypass.
Publishing is deliberately disabled; each item needs an exact permalink and direct media URL.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_PATH = DATA / "state.json"
LATEST_PATH = DATA / "latest.json"

ACCOUNTS = {
    "bbcpersian": "BBC Persian",
    "dw_persian": "DW Persian",
    "iranintltv": "Iran International",
    "isna.news": "ISNA",
    "irnanews1313": "IRNA",
}
RSSHUB_INSTANCES = [
    x.strip().rstrip("/")
    for x in os.getenv(
        "RSSHUB_INSTANCES",
        "https://rsshub.cmyr.dev,https://rsshub.app,https://rsshub.rssforever.com",
    ).split(",")
    if x.strip()
]
MAX_ITEMS = int(os.getenv("MAX_ITEMS_PER_ACCOUNT", "12"))
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DIDBAN-Public-Feed-Collector/1.1)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
}


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(node, names):
    for child in node.iter():
        if local_name(child.tag) in names and child.text and child.text.strip():
            return child.text.strip()
    return ""


def is_direct_media(url: str, mime: str = "") -> bool:
    if not url.startswith("https://"):
        return False
    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path.lower()
    return (
        any(x in host for x in ("cdninstagram.com", "fbcdn.net", "instagram"))
        or path.endswith((".jpg", ".jpeg", ".png", ".webp", ".mp4", ".m4v"))
        or mime.lower().startswith(("image/", "video/"))
    )


def find_media(node):
    candidates = []
    for child in node.iter():
        name = local_name(child.tag)
        url = child.attrib.get("url") or child.attrib.get("href") or ""
        mime = child.attrib.get("type") or child.attrib.get("medium") or ""
        if name in {"content", "thumbnail", "enclosure", "image"} and url:
            candidates.append((url.strip(), mime))
        if name == "img":
            src = child.attrib.get("src", "")
            if src:
                candidates.append((src.strip(), "image"))
        if child.text and name in {"description", "summary", "encoded"}:
            for src in re.findall(r'<img[^>]+src=["\']([^"\']+)', child.text, re.I):
                candidates.append((src.strip(), "image"))
    for url, mime in candidates:
        if is_direct_media(url, mime):
            return url
    return ""


def exact_instagram_url(*values):
    for value in values:
        if not value:
            continue
        match = re.search(
            r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/[A-Za-z0-9_-]+/?(?:\?[^\s]*)?",
            value,
        )
        if match:
            return match.group(0).split("?")[0]
    return ""


def parse_feed(xml_text, username):
    root = ET.fromstring(xml_text)
    nodes = [n for n in root.iter() if local_name(n.tag) in {"item", "entry"}]
    posts = []
    for node in nodes[:MAX_ITEMS]:
        title = child_text(node, {"title"})
        description = child_text(node, {"description", "summary", "content", "encoded"})
        link = ""
        for child in node:
            if local_name(child.tag) == "link":
                link = child.attrib.get("href") or (child.text or "").strip()
                if link:
                    break
        if not link:
            link = child_text(node, {"link", "guid", "id"})
        permalink = exact_instagram_url(link, description, title)
        if not permalink:
            continue
        media_url = find_media(node)
        published = child_text(node, {"pubdate", "published", "updated", "date"})
        caption = re.sub(r"<[^>]+>", " ", description or title)
        caption = re.sub(r"\s+", " ", caption).strip()
        fingerprint = hashlib.sha256(permalink.encode("utf-8")).hexdigest()
        posts.append({
            "id": fingerprint,
            "source": username,
            "post_url": permalink,
            "caption": caption[:5000],
            "media_url": media_url,
            "published_at": published,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "publish_ready": bool(media_url),
            "status": "ready" if media_url else "needs_media_url",
        })
    return posts


def feed_candidates(username):
    # Open RSS is tried first as a separate public bridge; RSSHub remains fallback.
    urls = [f"https://openrss.org/feed/www.instagram.com/{username}"]
    urls.extend(f"{base}/instagram/2/user/{username}" for base in RSSHUB_INSTANCES)
    return urls


def fetch_account(username):
    errors = []
    best_incomplete = None
    for url in feed_candidates(username):
        try:
            response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if response.status_code != 200:
                errors.append(f"{url}: HTTP {response.status_code}")
                continue
            content_type = response.headers.get("content-type", "").lower()
            if "html" in content_type and "<rss" not in response.text[:500].lower():
                errors.append(f"{url}: returned HTML, not a feed")
                continue
            posts = parse_feed(response.text, username)
            if posts:
                if any(post["publish_ready"] for post in posts):
                    return posts, None, url
                if best_incomplete is None:
                    best_incomplete = (posts, url)
                errors.append(f"{url}: posts found but direct media URLs are missing")
            else:
                errors.append(f"{url}: no usable Instagram permalinks")
        except (requests.RequestException, ET.ParseError) as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
        time.sleep(1)
    if best_incomplete:
        return best_incomplete[0], " | ".join(errors), best_incomplete[1]
    return [], " | ".join(errors) or "No public feed providers configured", None


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    state = load_json(STATE_PATH, {"seen_ids": []})
    seen = set(state.get("seen_ids", []))
    new_items, account_results, errors = [], {}, []

    for username, label in ACCOUNTS.items():
        posts, error, provider = fetch_account(username)
        fresh = []
        for post in posts:
            if post["id"] not in seen:
                seen.add(post["id"])
                fresh.append(post)
                new_items.append(post)
        ready_count = sum(1 for p in fresh if p["publish_ready"])
        media_missing = sum(1 for p in fresh if not p["publish_ready"])
        account_results[username] = {
            "label": label,
            "provider": provider,
            "fetched": len(posts),
            "new": len(fresh),
            "ready": ready_count,
            "needs_media_url": media_missing,
            "error": error,
        }
        if error:
            errors.append({"account": username, "error": error})

    new_items.sort(key=lambda p: p.get("published_at") or p["collected_at"], reverse=True)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_mode": "public_feeds_no_login",
        "publishing_enabled": False,
        "summary": {
            "accounts": len(ACCOUNTS),
            "new_posts": len(new_items),
            "publish_ready": sum(1 for p in new_items if p["publish_ready"]),
            "blocked_or_incomplete": sum(1 for p in new_items if not p["publish_ready"]) + len(errors),
        },
        "accounts": account_results,
        "errors": errors,
        "items": new_items,
        "note": "Public feed collection only. Publishing remains disabled pending verified results and explicit approval.",
    }
    LATEST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STATE_PATH.write_text(json.dumps({"seen_ids": sorted(seen)[-10000:]}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False))
    for username, result in account_results.items():
        print(
            f"{username}: provider={result['provider']} fetched={result['fetched']} "
            f"new={result['new']} ready={result['ready']} error={bool(result['error'])}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
