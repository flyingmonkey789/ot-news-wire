"""Shared helpers: paths, config, time, URL and text normalization."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(os.environ.get("OTWIRE_ROOT", Path(__file__).resolve().parent.parent))
DATA = ROOT / "data"
DOCS = ROOT / "docs"

CATEGORIES = ["Advisory", "Incidents", "Research", "Technology", "Policy"]
SOURCE_TYPES = ["gov", "research", "media", "vendor"]


def load_yaml(name: str) -> dict:
    path = ROOT / name
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data, indent=2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


# ---------------- time ----------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def local_day(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


# ---------------- URLs ----------------

_TRACKING = re.compile(r"^(utm_|mc_|fbclid$|gclid$|ocid$|cmpid$|ref$|ref_src$|src$|_hs)", re.I)


def normalize_url(url: str) -> str:
    """Canonical form used for dedupe (not for display)."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k)])
    path = p.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def domain_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def domain_matches(domain: str, candidates) -> str | None:
    """Return the candidate that `domain` equals or is a subdomain of."""
    for c in candidates:
        c = c.lower()
        if domain == c or domain.endswith("." + c):
            return c
    return None


# ---------------- text ----------------

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean_text(s: str | None, limit: int | None = None) -> str:
    s = html.unescape(_TAG.sub(" ", s or ""))
    s = _WS.sub(" ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


def title_key(title: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", "", title.lower())
    t = _WS.sub(" ", t).strip()
    return "t:" + hashlib.sha1(t.encode()).hexdigest()[:16]


def url_key(url: str) -> str:
    return "u:" + hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]


def log(msg: str) -> None:
    print(msg, flush=True)
