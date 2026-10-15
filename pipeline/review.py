"""Weekly editorial review, posted as a GitHub issue (or printed locally).

Covers the last 7 days:
  - volume and spend
  - published stories per source, per category and per origin (feed or query)
  - query/feed yield, to find dead weight
  - near misses (scores 5-6) that were excluded, to tune the threshold
  - stories dropped by caps
  - feeds currently failing

Usage: python -m pipeline.review [--print]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import timedelta

from . import github
from .util import DATA, load_json, load_yaml, local_day, log, now_utc, parse_iso


def _read_jsonl(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows]
    return "\n".join(out)


def build_report() -> tuple[str, str]:
    cfg = load_yaml("config.yaml")
    tz = cfg["site"].get("timezone", "UTC")
    now = now_utc()
    since = now - timedelta(days=7)
    decisions = [d for d in _read_jsonl(DATA / "decisions.jsonl") if parse_iso(d["ts"]) >= since]
    runs = [r for r in _read_jsonl(DATA / "runs.jsonl") if parse_iso(r["ts"]) >= since]
    stories = [s for s in load_json(DATA / "stories.json", []) if parse_iso(s["added_at"]) >= since]
    state = load_json(DATA / "state.json", {})
    near = set(cfg.get("review", {}).get("near_miss_scores", [5, 6]))

    title = f"Weekly review — {local_day(since, tz)} to {local_day(now, tz)}"
    md = [f"# {title}", ""]

    # Overview
    tin = sum(r.get("input_tokens", 0) for r in runs)
    tout = sum(r.get("output_tokens", 0) for r in runs)
    md += ["## Overview", "",
           f"- Runs: **{len(runs)}** · items fetched: {sum(r['fetched'] for r in runs)} · "
           f"new: {sum(r['new'] for r in runs)} · dropped by prefilter: {sum(r['prefiltered_out'] for r in runs)}",
           f"- Sent to editor: **{sum(r['reviewed'] for r in runs)}** · published: **{len(stories)}** "
           f"(~{len(stories) / 7:.1f}/day) · regrouped to a better source: {sum(r.get('regrouped', 0) for r in runs)}",
           f"- Editor usage: {sum(r.get('api_calls', 0) for r in runs)} calls, {tin:,} input / {tout:,} output tokens",
           ""]
    per_day = Counter(local_day(parse_iso(s["added_at"]), tz) for s in stories)
    if per_day:
        md += ["Per day: " + ", ".join(f"{d}: {n}" for d, n in sorted(per_day.items())), ""]

    # Published by source / category
    md += ["## Published stories by source", "",
           _table(["Source", "Type", "Stories"],
                  [(src, typ, n) for (src, typ), n in
                   Counter((s["source"], s.get("source_type", "")) for s in stories).most_common()]), ""]
    md += ["**By category:** " + ", ".join(f"{c} {n}" for c, n in
                                           Counter(s["category"] for s in stories).most_common()), ""]

    # Yield per origin
    yield_rows = defaultdict(lambda: Counter())
    for d in decisions:
        y = yield_rows[d["origin"]]
        y["reviewed"] += 1
        if d.get("score") is not None and d["score"] >= cfg["publishing"]["threshold"]:
            y["7+"] += 1
        if d["outcome"] == "published":
            y["published"] += 1
        if d["outcome"] == "grouped":
            y["grouped"] += 1
    for s in stories:
        yield_rows[s.get("origin", "?")]
    rows = sorted(((o, c["reviewed"], c["7+"], c["published"], c["grouped"]) for o, c in yield_rows.items()),
                  key=lambda r: (-r[3], -r[1]))
    md += ["## Yield by feed and query", "",
           "_Reviewed = passed dedupe + prefilter and was scored. A query with many reviewed and "
           "zero published is a candidate to tighten or drop._", "",
           _table(["Origin", "Reviewed", "Scored ≥ threshold", "Published", "Merged into a story"], rows), ""]
    quiet = [f"`{o}`" for o, h in state.get("feeds", {}).items()
             if h.get("last_success") and o not in yield_rows]
    if quiet:
        md += ["Feeds with nothing reviewed this week: " + ", ".join(sorted(quiet)), ""]

    # Near misses
    misses = sorted((d for d in decisions if d["outcome"] == "below_threshold" and d.get("score") in near),
                    key=lambda d: (-d["score"], d["ts"]))
    limit = cfg.get("review", {}).get("max_near_misses", 60)
    md += [f"## Near misses (scored {', '.join(map(str, sorted(near)))}, not published)", "",
           f"{len(misses)} items" + (f", showing {limit}" if len(misses) > limit else ""), ""]
    if misses:
        md += [_table(["Score", "Category", "Source", "Headline", "Found via"],
                      [(d["score"], d.get("category", ""), d["source"], f"[{d['title'][:110]}]({d['url']})",
                        d["origin"]) for d in misses[:limit]]), ""]

    # Caps
    capped = [d for d in decisions if d["outcome"] in ("capped_daily", "capped_vendor")]
    if capped:
        md += ["## Dropped by caps", "",
               _table(["Reason", "Score", "Source", "Headline"],
                      [(d["outcome"].replace("capped_", ""), d.get("score"), d["source"],
                        f"[{d['title'][:110]}]({d['url']})") for d in capped]), ""]

    # Score distribution
    dist = Counter(d["score"] for d in decisions if d.get("score") is not None)
    if dist:
        md += ["## Score distribution", "",
               _table(["Score"] + [str(i) for i in range(11)], [["Items"] + [dist.get(i, 0) for i in range(11)]]), ""]

    # Health
    failing = {o: h for o, h in state.get("feeds", {}).items() if h.get("failing_since")}
    md += ["## Feed health", ""]
    if failing:
        md += [_table(["Feed", "Failing since", "Last error"],
                      [(o, h["failing_since"], (h.get("last_error") or "")[:120]) for o, h in sorted(failing.items())])]
    else:
        md += ["All feeds healthy."]
    md += ["", "---", "Tune: `config.yaml` (threshold, caps), `queries.yaml`, `sources.yaml`, "
           "`blocklist.yaml`, `pinned.yaml`."]
    return title, "\n".join(md)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", help="print instead of opening an issue")
    args = ap.parse_args(argv)
    title, body = build_report()
    if args.print or not github.enabled():
        print(body)
        return
    label = load_yaml("config.yaml").get("review", {}).get("issue_label", "weekly-review")
    num = github.open_issue(title, body[:65000], [label])
    log(f"Opened issue #{num}" if num else "Issue not opened")


if __name__ == "__main__":
    main()
