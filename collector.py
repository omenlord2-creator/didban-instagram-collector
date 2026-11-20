#!/usr/bin/env python3
"""Collect publicly available Instagram posts through RSSHub-compatible feeds.

No Instagram login, password, session cookie, private profile, or CAPTCHA bypass.
A post is publish_ready only when an exact Instagram permalink and media URL exist.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import hashlib
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

DEFAULT_INSTANCES = [
    "https://rsshub.cmyr.dev",
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
]
INSTANCES = [
    x.strip().rstrip("/")
    for x in os.getenv("RSSHUB_INSTANCES", ",".join(DEFAULT_INSTANCES)).split(",")
    if x.strip()
]
MAX_ITEMS = int(os.getenv("MAX_ITEMS_PER_ACCOUNT", "12"))
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
HEADERS = {
    "User-Agent": "DIDBAN-Public-Feed-Collector/1.0 (+https://github.com/omenlord2-creator/didban-instagram-collector)",
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

def find_media(node):
    candidates = []
    for child in node.iter():
        name = local_name(child.tag)
        url = child.attrib.get("url") or child.attrib.get("href") or ""
        if name in {"content", "thumbnail", "enclosure", "image"} and url:
            candidates.append(url.strip())
        elif name in {"img"}:
            src = child.attrib.get("src", "")
            if src:
                candidates.append(src.strip())
    for url in candidates:
        if url.startswith("https://") and any(x in url.lower() for x in ("cdninstagram", "fbcdn", "instagram")):
            return url
    return candidates[0] if candidates else ""

def exact_instagram_url(*values):
    for value in values:
        if not value:
            continue
        match = re.search(r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/[A-Za-z0-9_-]+/?(?:\?[^\s]*)?", value)
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
        media_url = find_media(node)
        published = child_text(node, {"pubdate", "published", "updated", "date"})
        caption = re.sub(r"<[^>]+>", " ", description or title)
        caption = re.sub(r"\s+", " ", caption).strip()
        if not permalink:
            continue
        fingerprint = hashlib.sha256(permalink.encode("utf-8")).hexdigest()
        posts.append({
            "id": fingerprint,
            "source": username,
            "post_url": permalink,
            "caption": caption[:5000],
            "media_url": media_url,
            "published_at": published,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "publish_ready": bool(permalink and media_url),
            "status": "ready" if media_url else "needs_media_url",
        })
    return posts

def fetch_account(username):
    errors = []
    for base in INSTANCES:
        url = f"{base}/instagram/2/user/{username}"
        try:
            response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if response.status_code != 200:
                errors.append(f"{base}: HTTP {response.status_code}")
                continue
            content_type = response.headers.get("content-type", "").lower()
            if "html" in content_type and "<rss" not in response.text[:500].lower():
                errors.append(f"{base}: returned HTML, not a feed")
                continue
            posts = parse_feed(response.text, username)
            if posts:
                return posts, None
            errors.append(f"{base}: feed returned no usable Instagram permalinks")
        except (requests.RequestException, ET.ParseError) as exc:
            errors.append(f"{base}: {type(exc).__name__}: {exc}")
        time.sleep(1)
    return [], " | ".join(errors) or "No feed instances configured"

def main():
    DATA.mkdir(parents=True, exist_ok=True)
    state = load_json(STATE_PATH, {"seen_ids": []})
    seen = set(state.get("seen_ids", []))
    new_items, account_results, errors = [], {}, []

    for username, label in ACCOUNTS.items():
        posts, error = fetch_account(username)
        fresh = []
        for post in posts:
            if post["id"] not in seen:
                seen.add(post["id"])
                fresh.append(post)
                new_items.append(post)
        account_results[username] = {
            "label": label,
            "fetched": len(posts),
            "new": len(fresh),
            "ready": sum(1 for p in fresh if p["publish_ready"]),
            "needs_media_url": sum(1 for p in fresh if not p["publish_ready"]),
            "error": error,
        }
        if error:
            errors.append({"account": username, "error": error})

    new_items.sort(key=lambda p: p.get("published_at") or p["collected_at"], reverse=True)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_mode": "public_rss_only",
        "publishing_enabled": False,
        "summary": {
            "accounts": len(ACCOUNTS),
            "new_posts": len(new_items),
            "publish_ready": sum(1 for p in new_items if p["publish_ready"]),
            "blocked_or_incomplete": len(errors),
        },
        "accounts": account_results,
        "errors": errors,
        "items": new_items,
    }
    LATEST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STATE_PATH.write_text(json.dumps({"seen_ids": sorted(seen)[-10000:]}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False))
    for username, result in account_results.items():
        print(f"{username}: fetched={result['fetched']} new={result['new']} ready={result['ready']} error={bool(result['error'])}")

    # Do not fail the whole workflow if public services block a source.
    # The report records the failure and publishing remains disabled.
    return 0

if __name__ == "__main__":
    sys.exit(main())
