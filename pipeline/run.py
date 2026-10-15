"""Hourly pipeline: fetch -> dedupe -> prefilter -> edit -> group -> cap -> publish.

Usage:
  python -m pipeline.run                 # normal run (needs ANTHROPIC_API_KEY)
  python -m pipeline.run --dry-run       # do everything, write nothing
  python -m pipeline.run --mock-editor   # heuristic scores, no API calls (for testing)
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import timedelta

import requests

from . import github
from .editor import Editor, summarize_pinned
from .fetch import fetch_all
from .gnews import resolve
from .publish import write_feed_json, write_feed_xml
from .util import (CATEGORIES, DATA, domain_matches, domain_of, iso, load_json, load_yaml,
                   local_day, log, normalize_url, now_utc, parse_iso, save_json, title_key,
                   url_key)

STATE_PATH = DATA / "state.json"
STORIES_PATH = DATA / "stories.json"
DECISIONS_PATH = DATA / "decisions.jsonl"
RUNS_PATH = DATA / "runs.jsonl"


# ---------------------------------------------------------------- helpers

class Blocklist:
    def __init__(self, cfg: dict):
        self.urls = {normalize_url(u) for u in cfg.get("urls") or []}
        self.domains = [d.lower() for d in cfg.get("domains") or []]
        self.patterns = [re.compile(p, re.I) for p in cfg.get("title_patterns") or []]

    def blocks(self, url: str, title: str, extra_domain: str = "") -> bool:
        if normalize_url(url) in self.urls:
            return True
        for d in (domain_of(url), extra_domain):
            if d and domain_matches(d, self.domains):
                return True
        return any(p.search(title) for p in self.patterns)


class Prefilter:
    def __init__(self, cfg: dict):
        self.inc = [re.compile(p, re.I) for p in cfg.get("include", [])]
        self.exc = [re.compile(p, re.I) for p in cfg.get("exclude", [])]

    def passes(self, item: dict) -> bool:
        text = f"{item['title']} {item.get('snippet', '')}"
        if any(p.search(text) for p in self.exc):
            return False
        return any(p.search(text) for p in self.inc)


def rank(source_type: str, is_primary: bool, score: int, priority: list[str]) -> tuple:
    type_rank = len(priority) - priority.index(source_type) if source_type in priority else 0
    return (bool(is_primary), type_rank, score)


def vendor_key(item: dict, decision: dict) -> str | None:
    v = decision.get("vendor")
    if not v and item["source_type"] in ("vendor", "research"):
        v = item.get("org")
    return v.strip().lower() if v else None


class UnionFind:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


# ---------------------------------------------------------------- health

def update_health(state: dict, health: dict, cfg: dict, now) -> list[str]:
    feeds = state.setdefault("feeds", {})
    limit = timedelta(hours=cfg["health"].get("failing_after_hours", 24))
    label = cfg["health"].get("issue_label", "feed-health")
    failing = []
    for origin, h in health.items():
        f = feeds.setdefault(origin, {})
        if h["ok"]:
            if f.get("issue"):
                github.close_issue(f["issue"], f"Feed `{origin}` is working again ({iso(now)}).")
            feeds[origin] = {"last_success": iso(now), "last_count": h["count"]}
            continue
        f["last_error"] = h["error"]
        f.setdefault("failing_since", iso(now))
        failing.append(origin)
        if not f.get("issue") and now - parse_iso(f["failing_since"]) >= limit:
            body = (f"`{origin}` ({h['label']}) has failed every run since **{f['failing_since']}**.\n\n"
                    f"Last error:\n```\n{h['error']}\n```\n\n"
                    f"Last success: {f.get('last_success', 'never')}\n\n"
                    "Fix the URL in `sources.yaml` / `queries.yaml`, or set `enabled: false`. "
                    "This issue closes automatically when the feed recovers.")
            f["issue"] = github.open_issue(f"Feed failing: {origin}", body, [label])
    # forget feeds removed from config
    for origin in list(feeds):
        if origin not in health:
            del feeds[origin]
    return failing


# ---------------------------------------------------------------- main

def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mock-editor", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_yaml("config.yaml")
    pub = cfg["publishing"]
    tz = cfg["site"].get("timezone", "UTC")
    priority = pub.get("source_type_priority", ["gov", "research", "media", "vendor"])
    threshold = pub.get("threshold", 7)
    blocklist = Blocklist(load_yaml("blocklist.yaml"))
    prefilter = Prefilter(cfg.get("prefilter", {}))

    state = load_json(STATE_PATH, {})
    seen: dict = state.setdefault("seen", {})
    gcache: dict = state.setdefault("gnews_cache", {})
    stories: list = load_json(STORIES_PATH, [])
    now = now_utc()
    today = local_day(now, tz)
    decisions_log: list[dict] = []
    stats = {"ts": iso(now), "fetched": 0, "new": 0, "prefiltered_out": 0, "reviewed": 0,
             "published": 0, "regrouped": 0, "failing_feeds": 0}

    def record(item, decision, outcome, story_id=None, d=None):
        decisions_log.append({
            "ts": iso(now), "day": today, "origin": item["origin"], "source": item["source"],
            "source_type": item["source_type"], "title": item["title"], "url": item["url"],
            "score": (d or decision or {}).get("score"), "category": (d or decision or {}).get("category"),
            "outcome": outcome, "story": story_id})

    # 1. Fetch
    log("Fetching sources…")
    items, health = fetch_all(load_yaml("sources.yaml"), load_yaml("queries.yaml"))
    stats["fetched"] = len(items)
    failing = update_health(state, health, cfg, now)
    stats["failing_feeds"] = len(failing)
    log(f"  {len(items)} items from {len(health)} feeds ({len(failing)} failing)")

    # 2. Dedupe, age, blocklist, prefilter
    max_age = timedelta(hours=pub.get("max_age_hours", 48))
    candidates, run_keys = [], set()
    published_urls = {normalize_url(s["url"]) for s in stories}
    for it in items:
        keys = {url_key(it["url"]), title_key(it["title"])}
        if keys & run_keys or any(k in seen for k in keys):
            continue
        run_keys |= keys
        it["keys"] = keys
        if now - it["published"] > max_age or normalize_url(it["url"]) in published_urls:
            for k in keys:
                seen[k] = iso(now)
            continue
        stats["new"] += 1
        if blocklist.blocks(it["url"], it["title"], it.get("publisher_domain", "")):
            for k in keys:
                seen[k] = iso(now)
            record(it, None, "blocked")
            continue
        if not it["ot_native"] and not prefilter.passes(it):
            for k in keys:
                seen[k] = iso(now)
            stats["prefiltered_out"] += 1
            continue
        candidates.append(it)

    candidates.sort(key=lambda x: x["published"], reverse=True)
    limit = cfg["editor"].get("max_items_per_run", 150)
    if len(candidates) > limit:
        log(f"  {len(candidates) - limit} items deferred to next run (max_items_per_run)")
        candidates = candidates[:limit]
    log(f"  {stats['new']} new, {stats['prefiltered_out']} dropped by prefilter, {len(candidates)} to the editor")

    # 3. Editor
    window = timedelta(hours=pub.get("group_window_hours", 72))
    recent = [s for s in stories if now - parse_iso(s["added_at"]) <= window]
    editor = Editor(cfg["editor"], mock=args.mock_editor, threshold=threshold)
    for i, it in enumerate(candidates):
        it["bid"] = f"a{i + 1}"
    decisions = editor.review(candidates, recent) if candidates else {}
    reviewed = [it for it in candidates if it["bid"] in decisions]
    for it in reviewed:
        for k in it["keys"]:
            seen[k] = iso(now)
    stats["reviewed"] = len(reviewed)

    # 4. Group coverage of the same event
    uf = UnionFind()
    by_bid = {it["bid"]: it for it in reviewed}
    story_ids = {s["id"] for s in stories}
    story_link = {}
    for it in reviewed:
        uf.find(it["bid"])
        ref = decisions[it["bid"]].get("same_as")
        if ref in by_bid and ref != it["bid"]:
            uf.union(it["bid"], ref)
        elif ref in story_ids:
            story_link[it["bid"]] = ref
    clusters: dict[str, list] = {}
    for it in reviewed:
        clusters.setdefault(uf.find(it["bid"]), []).append(it)

    stories_by_id = {s["id"]: s for s in stories}
    new_candidates = []
    for members in clusters.values():
        def r(it):
            d = decisions[it["bid"]]
            return rank(it["source_type"], d.get("is_primary", False), d["score"], priority)
        members.sort(key=r, reverse=True)
        best = members[0]
        top_score = max(decisions[m["bid"]]["score"] for m in members)
        linked = next((story_link[m["bid"]] for m in members if m["bid"] in story_link), None)

        if linked:
            s = stories_by_id[linked]
            bd = decisions[best["bid"]]
            better = r(best) > rank(s.get("source_type", "media"), s.get("is_primary", False),
                                    s.get("score", 0), priority)
            if better and not s.get("pinned") and bd["score"] >= threshold - 1:
                url = resolve(best["url"], gcache) if best["gnews"] else best["url"]
                if not blocklist.blocks(url, best["title"]):
                    s.update(url=url, title=best["title"], source=best["source"],
                             source_type=best["source_type"], is_primary=bd.get("is_primary", False),
                             score=max(s.get("score", 0), top_score), origin=best["origin"])
                    stats["regrouped"] += 1
                    log(f"  ↻ {linked} now links to {best['source']}: {best['title'][:70]}")
            for m in members:
                record(m, decisions[m["bid"]], "grouped", linked)
            continue

        if top_score >= threshold:
            new_candidates.append((top_score, best, members))
        else:
            for m in members:
                record(m, decisions[m["bid"]], "below_threshold")

    # 5. Caps (daily + per vendor), highest scores first
    new_candidates.sort(key=lambda c: (c[0], c[1]["published"]), reverse=True)
    added_today = [s for s in stories if local_day(parse_iso(s["added_at"]), tz) == today and not s.get("pinned")]
    vendor_counts: dict[str, int] = {}
    for s in added_today:
        if s.get("vendor_key"):
            vendor_counts[s["vendor_key"]] = vendor_counts.get(s["vendor_key"], 0) + 1
    daily_left = pub.get("daily_cap", 20) - len(added_today)
    vcap = pub.get("vendor_daily_cap", 3)
    next_id = state.get("next_id", 1)
    session = requests.Session()

    for top_score, best, members in new_candidates:
        d = dict(decisions[best["bid"]])
        # Link to the best source, but take category/summary from the highest-scored
        # write-up when the best source scored below threshold (summary left blank).
        top = max(members, key=lambda m: decisions[m["bid"]]["score"])
        if not d.get("summary", "").strip():
            td = decisions[top["bid"]]
            d.update(summary=td.get("summary", ""), category=td["category"], sector=td.get("sector"))
        vk = vendor_key(best, d)
        outcome = None
        if daily_left <= 0:
            outcome = "capped_daily"
        elif vk and vendor_counts.get(vk, 0) >= vcap:
            outcome = "capped_vendor"
        url = best["url"]
        if not outcome:
            url = resolve(url, gcache, session) if best["gnews"] else url
            if blocklist.blocks(url, best["title"]):
                outcome = "blocked"
            elif normalize_url(url) in published_urls:
                outcome = "duplicate_url"
        if outcome:
            for m in members:
                record(m, decisions[m["bid"]], outcome)
            continue
        sid = f"S{next_id}"
        next_id += 1
        story = {
            "id": sid, "url": url, "published": iso(best["published"]), "category": d["category"],
            "source": best["source"], "sector": d.get("sector", "Cross-sector"), "title": best["title"],
            "summary": d["summary"].strip(), "score": top_score, "source_type": best["source_type"],
            "is_primary": d.get("is_primary", False), "vendor_key": vk, "origin": best["origin"],
            "added_at": iso(now),
        }
        stories.append(story)
        stories_by_id[sid] = story
        published_urls.add(normalize_url(url))
        daily_left -= 1
        if vk:
            vendor_counts[vk] = vendor_counts.get(vk, 0) + 1
        stats["published"] += 1
        log(f"  + [{top_score}] {story['category']:<10} {story['source']}: {story['title'][:80]}")
        for m in members:
            record(m, decisions[m["bid"]], "published" if m is best else "grouped", sid,
                   d=decisions[m["bid"]])

    # 6. Pinned stories
    pins = load_yaml("pinned.yaml").get("pinned") or []
    pin_urls = {normalize_url(p["url"]) for p in pins if p.get("url")}
    for s in stories:  # unpinned entries become normal stories again
        if s.get("pinned") and normalize_url(s["url"]) not in pin_urls:
            s.pop("pinned")
    for p in pins:
        if not p.get("url") or not p.get("title"):
            continue
        nu = normalize_url(p["url"])
        existing = next((s for s in stories if normalize_url(s["url"]) == nu), None)
        cat = p.get("category") if p.get("category") in CATEGORIES else "Research"
        if existing:
            existing.update(pinned=True, title=p["title"], category=cat,
                            **({"summary": p["summary"]} if p.get("summary") else {}),
                            **({"source": p["source"]} if p.get("source") else {}))
            continue
        summary = p.get("summary") or summarize_pinned(editor, p)
        published = p.get("published")
        published = iso(parse_iso(str(published))) if published else iso(now)
        stories.append({
            "id": f"S{next_id}", "url": p["url"], "published": published, "category": cat,
            "source": p.get("source") or domain_of(p["url"]), "sector": p.get("sector", "Cross-sector"),
            "title": p["title"], "summary": summary, "score": 10, "source_type": "media",
            "is_primary": True, "vendor_key": None, "origin": "pinned", "added_at": iso(now), "pinned": True})
        next_id += 1
        log(f"  📌 pinned: {p['title'][:80]}")
    state["next_id"] = next_id

    # 7. Blocklist removal, sort, trim
    before = len(stories)
    stories = [s for s in stories if s.get("pinned") or not blocklist.blocks(s["url"], s["title"])]
    if len(stories) < before:
        log(f"  − removed {before - len(stories)} blocklisted stories")
    stories.sort(key=lambda s: s["published"], reverse=True)
    stories = stories[: pub.get("feed_max_items", 500)]

    # 8. Prune state
    seen_cut = now - timedelta(days=cfg["state"].get("seen_retention_days", 30))
    state["seen"] = {k: v for k, v in seen.items() if parse_iso(v) >= seen_cut}
    keep_urls = set(gcache) & {it["url"] for it in items}
    state["gnews_cache"] = {k: v for k, v in gcache.items() if k in keep_urls}

    usage = editor.usage
    stats.update(api_calls=usage["calls"], input_tokens=usage["input_tokens"],
                 output_tokens=usage["output_tokens"], feed_size=len(stories))
    log(f"Done: {stats['published']} published, {stats['regrouped']} regrouped, "
        f"{usage['calls']} API calls ({usage['input_tokens']} in / {usage['output_tokens']} out tokens)")

    if args.dry_run:
        log("(dry run: nothing written)")
        return

    save_json(STATE_PATH, state)
    save_json(STORIES_PATH, stories)
    write_feed_json(stories)
    write_feed_xml(stories, cfg["site"])
    append_jsonl(DECISIONS_PATH, decisions_log,
                 keep_days=cfg["state"].get("decisions_retention_days", 21), now=now)
    append_jsonl(RUNS_PATH, [stats], keep_days=cfg["state"].get("decisions_retention_days", 21), now=now)


def append_jsonl(path, rows, keep_days, now):
    cut = now - timedelta(days=keep_days)
    old = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            old = [json.loads(line) for line in f if line.strip()]
    keep = [r for r in old if parse_iso(r["ts"]) >= cut] + rows
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in keep:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
