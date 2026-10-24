"""Fetch direct RSS sources and Google News queries into normalized items."""
from __future__ import annotations

import calendar
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser
import requests

from .util import clean_text, domain_matches, domain_of, log, now_utc

UA = "OT-Wire/1.0 (+https://github.com; OT security news aggregator)"


def _entry_time(e) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(key)
        if t:
            try:
                return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
            except (OverflowError, ValueError):
                continue
    return None


def _get(url: str, timeout: int = 25) -> feedparser.FeedParserDict:
    if url.startswith("file://"):  # used by tests
        parsed = feedparser.parse(url[7:])
    else:
        r = requests.get(url, timeout=timeout, headers={
            "User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"})
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"unparseable feed ({type(parsed.bozo_exception).__name__})")
    return parsed


def gnews_url(q: str, defaults: dict) -> str:
    window = defaults.get("window", "when:1d")
    return ("https://news.google.com/rss/search?q=" + quote_plus(f"{q} {window}") +
            f"&hl={defaults.get('hl', 'en-US')}&gl={defaults.get('gl', 'US')}&ceid={defaults.get('ceid', 'US:en')}")


def _from_source(src: dict) -> list[dict]:
    feed = _get(src["url"])
    items = []
    for e in feed.entries:
        link = e.get("link") or ""
        title = clean_text(e.get("title"))
        if not link or not title:
            continue
        items.append({
            "title": title,
            "url": link,
            "snippet": clean_text(e.get("summary") or e.get("description"), 600),
            "published": _entry_time(e),
            "source": src["name"],
            "source_type": src["type"],
            "org": src.get("org", src["name"]),
            "ot_native": bool(src.get("ot_native")),
            "origin": f"feed:{src['id']}",
            "gnews": False,
        })
    return items


def _from_query(qry: dict, defaults: dict, domain_map: dict) -> list[dict]:
    feed = _get(gnews_url(qry["q"], defaults))
    items = []
    for e in feed.entries:
        src = e.get("source") or {}
        publisher = clean_text(src.get("title")) or "Unknown"
        pub_home = src.get("href") or ""
        title = clean_text(e.get("title"))
        # Google appends " - Publisher" to every headline.
        suffix = f" - {publisher}"
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
        dom = domain_of(pub_home)
        meta = domain_map.get(domain_matches(dom, domain_map) or "", {}) if dom else {}
        items.append({
            "title": title,
            "url": e.get("link") or "",
            # Google's description is just the headline again; not useful.
            "snippet": "",
            "published": _entry_time(e),
            "source": publisher,
            "source_type": meta.get("type", "media"),
            "org": meta.get("org", publisher),
            "ot_native": bool(qry.get("ot_native")),
            "origin": f"query:{qry['id']}",
            "gnews": True,
            "publisher_domain": dom,
        })
    return items


def fetch_all(sources_cfg: dict, queries_cfg: dict) -> tuple[list[dict], dict]:
    """Returns (items, health) where health maps origin -> {ok, error, count}."""
    jobs = []
    for s in sources_cfg.get("sources", []):
        if s.get("enabled", True):
            jobs.append((f"feed:{s['id']}", s.get("name"), lambda s=s: _from_source(s)))
    domain_map = {k.lower(): v for k, v in (sources_cfg.get("domains") or {}).items()}
    defaults = queries_cfg.get("defaults", {})
    for q in queries_cfg.get("queries", []):
        if q.get("enabled", True):
            jobs.append((f"query:{q['id']}", q["id"], lambda q=q: _from_query(q, defaults, domain_map)))

    items, health = [], {}

    def run(job):
        origin, label, fn = job
        try:
            got = fn()
            return origin, label, got, None
        except Exception as ex:  # noqa: BLE001 - any failure is a feed-health event
            return origin, label, [], f"{type(ex).__name__}: {ex}"[:300]

    with ThreadPoolExecutor(max_workers=8) as pool:
        for origin, label, got, err in pool.map(run, jobs):
            health[origin] = {"ok": err is None, "error": err, "count": len(got), "label": label}
            if err:
                log(f"  ! {origin}: {err}")
            items.extend(got)

    fetched_at = now_utc()
    for it in items:
        it["fetched_at"] = fetched_at
        if it["published"] is None or it["published"] > fetched_at:
            it["published"] = fetched_at
    return items, health
