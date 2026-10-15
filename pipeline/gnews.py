"""Resolve Google News RSS article links to the publisher's real URL.

Google News RSS links look like https://news.google.com/rss/articles/CBMi...
They no longer redirect with a plain HTTP 3xx. Resolution order:
  1. Legacy IDs: the publisher URL is embedded in the base64 payload.
  2. Current IDs: fetch the article page for a signature + timestamp, then ask
     Google's batchexecute endpoint to decode it (same method as the
     open-source `googlenewsdecoder` package).
  3. Fallback: follow redirects and accept the result if it left news.google.com.
If everything fails the Google link is kept — it still opens the story.

Only items that will actually be published are resolved, so this runs a
handful of times per hour.
"""
from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import quote, urlsplit

import requests

from .util import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def is_gnews(url: str) -> bool:
    return (urlsplit(url).hostname or "").endswith("news.google.com")


def _article_id(url: str) -> str | None:
    parts = urlsplit(url).path.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2] in ("articles", "read"):
        return parts[-1]
    return None


def _decode_legacy(art_id: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(art_id + "=" * (-len(art_id) % 4))
    except Exception:
        return None
    m = re.search(rb"https?://[\x21-\x7e]+", raw)
    if not m:
        return None
    candidate = m.group(0).decode("ascii", "ignore")
    # Newer IDs start with "AU_yqL" inside the payload and contain no URL.
    if "news.google.com" in candidate:
        return None
    # Strip trailing protobuf noise.
    return re.split(r"[\x00-\x20\"']", candidate)[0].rstrip("\\")


def _decode_batchexecute(art_id: str, session: requests.Session) -> str | None:
    for path in (f"https://news.google.com/articles/{art_id}",
                 f"https://news.google.com/rss/articles/{art_id}"):
        try:
            r = session.get(path, timeout=15)
        except requests.RequestException:
            continue
        sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
        if sg and ts:
            break
    else:
        return None
    inner = (
        '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,'
        'null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
        f'"{art_id}",{ts.group(1)},"{sg.group(1)}"]'
    )
    payload = "f.req=" + quote(json.dumps([[["Fbv4je", inner]]]))
    try:
        r = session.post(BATCH_URL, data=payload, timeout=15, headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        chunk = r.text.split("\n\n", 1)[1]
        data = json.loads(chunk)
        for row in data:
            if isinstance(row, list) and len(row) > 2 and row[1] == "Fbv4je" and row[2]:
                decoded = json.loads(row[2])
                if isinstance(decoded, list) and len(decoded) > 1 and decoded[1].startswith("http"):
                    return decoded[1]
    except Exception:
        return None
    return None


def _follow(url: str, session: requests.Session) -> str | None:
    try:
        r = session.get(url, timeout=15, allow_redirects=True)
        if not is_gnews(r.url):
            return r.url
    except requests.RequestException:
        pass
    return None


def resolve(url: str, cache: dict, session: requests.Session | None = None) -> str:
    if not is_gnews(url):
        return url
    if url in cache:
        return cache[url]
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", UA)
    art_id = _article_id(url)
    real = None
    if art_id:
        real = _decode_legacy(art_id) or _decode_batchexecute(art_id, session)
    real = real or _follow(url, session)
    time.sleep(0.5)  # be polite
    if real:
        cache[url] = real
        return real
    log(f"  ! could not resolve Google News link, keeping it: {url[:90]}")
    return url
