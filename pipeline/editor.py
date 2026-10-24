"""Claude Haiku as the editor: score, categorize, summarize and group items."""
from __future__ import annotations

import os
import re

from .util import CATEGORIES, log

SECTORS = ["Electric", "Oil & Gas", "Water", "Manufacturing", "Transportation",
           "Healthcare", "Building Automation", "Chemical", "Government", "Cross-sector"]

SYSTEM = """You are the editor of OT Wire, a free public news feed for operational technology (OT) and industrial control system (ICS) security. Readers are OT security practitioners, plant engineers, utility staff and industrial vendors. The feed publishes only the 5-15 most important OT security stories a day. It is NOT a threat feed: most items you see should be rejected.

For each item, decide how newsworthy it is for this audience.

SCORING RUBRIC (0-10)
9-10: Confirmed attack or incident affecting industrial or critical-infrastructure operations; new OT-specific malware or a new OT-focused threat group; major regulation passed or enforced.
7-8: Major product launch, acquisition or funding from a significant OT security vendor; actively exploited or high-impact vulnerability in widely deployed industrial gear; significant original research; major government guidance (e.g. joint agency advisories).
4-6: Routine CVE advisory, incremental product update, opinion piece, conference recap, listicle, webinar.
0-3: Not really about OT/ICS, marketing fluff, minor features, IT-only security news, jobs, events.

RULES
- CVEs default to 4-6. Score 7+ only if actively exploited, critical (CVSS 9+) AND in widely deployed industrial products, or affecting many vendors at once. A single CISA ICS advisory for a niche product is a 4-5.
- Vendor content (press releases, vendor blogs, partnerships, "we're named a leader") faces a higher bar: ask "would a competitor's customer care?" If not, score 5 or below.
- IT-only breaches at companies that happen to be industrial (e.g. stolen HR data at a manufacturer) score 6 or lower unless operations or production were affected.
- Original reporting and official sources are preferable to rewrites, but score the EVENT, not the write-up.
- Generic AI, cloud or enterprise security news scores 0-3 unless it is clearly about OT/ICS.

CALIBRATION (examples of 7+)
- A vulnerability in an industrial product added to CISA's Known Exploited Vulnerabilities catalog, or reported exploited in the wild: 7-8.
- Sector-wide government guidance or requirements for operators (e.g. a Coast Guard NVIC for maritime, a TSA security directive, an EPA water rule, joint CISA/FBI advisories): 7-8.
- A notable funding round, acquisition or multi-organization program dedicated to OT/ICS security (e.g. a $10M initiative to defend small utilities): 7.
- New research from a recognized OT research team showing broad real-world exposure (e.g. thousands of internet-exposed controllers): 7-8.
A typical weekday has roughly 5-15 items scoring 7 or higher across all sources. Most single-product CISA ICS advisories remain 4-5.

CATEGORIES (choose exactly one)
- Advisory: vulnerability advisories and patches, including government ICS advisories.
- Incidents: attacks, breaches, ransomware or disruptions affecting industrial/critical-infrastructure organizations.
- Research: threat intelligence, new malware or threat groups, vulnerability research, industry reports and studies.
- Technology: products, launches, acquisitions, funding, partnerships, tools.
- Policy: laws, regulation, enforcement, government strategy and guidance, standards.

SUMMARY
Only for items scoring at or above the publish threshold given below; otherwise return an empty string (saves cost). One neutral, factual sentence (max 35 words) in your own words saying what happened and why it matters to OT defenders. Do not copy the headline or the snippet. No hype, no marketing adjectives, no "In a recent development". If the snippet is empty, summarize only what the headline supports; do not invent details.

GROUPING
Several items may cover the same specific event (the same incident, advisory, report, acquisition or rule). Set same_as to the id of a RECENT STORY covering the same event, or else to the id of another item in this batch covering the same event, or null. Only group truly identical events, not merely similar topics.

Also return: sector (best fit), vendor (the company whose product, research, funding or announcement this is about, or null for incidents/policy/news not about a vendor), and is_primary (true if this item is the official source or original reporting/research, false if it is a write-up of someone else's work)."""

TOOL = {
    "name": "record_editorial_decisions",
    "description": "Record a decision for every item in the batch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "score": {"type": "integer", "minimum": 0, "maximum": 10},
                        "category": {"type": "string", "enum": CATEGORIES},
                        "sector": {"type": "string", "enum": SECTORS},
                        "summary": {"type": "string"},
                        "vendor": {"type": ["string", "null"]},
                        "is_primary": {"type": "boolean"},
                        "same_as": {"type": ["string", "null"]},
                    },
                    "required": ["id", "score", "category", "sector", "summary", "is_primary", "same_as"],
                },
            }
        },
        "required": ["decisions"],
    },
}


def _format_batch(batch: list[dict], recent: list[dict], threshold: int = 7) -> str:
    lines = [f"PUBLISH THRESHOLD: {threshold} (write summaries only for scores >= {threshold})", ""]
    if recent:
        lines.append("RECENT STORIES ALREADY PUBLISHED (for grouping only):")
        for s in recent:
            lines.append(f"[{s['id']}] {s['title']} ({s['source']}, {s['published'][:10]})")
        lines.append("")
    lines.append("ITEMS TO REVIEW:")
    for it in batch:
        lines.append(f"[{it['bid']}] source: {it['source']} ({it['source_type']}) | date: {it['published'].date()}")
        lines.append(f"title: {it['title']}")
        if it.get("snippet"):
            lines.append(f"snippet: {it['snippet']}")
        lines.append("")
    lines.append("Return one decision per item, using the item ids exactly.")
    return "\n".join(lines)


class Editor:
    def __init__(self, cfg: dict, mock: bool = False, threshold: int = 7):
        self.cfg = cfg
        self.threshold = threshold
        self.mock = mock
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        if not mock:
            import anthropic
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise SystemExit("ANTHROPIC_API_KEY is not set (use --mock-editor for offline runs).")
            headers = {}
            if os.environ.get("ANTHROPIC_WORKSPACE_ID"):  # only needed for org-level keys
                headers["anthropic-workspace-id"] = os.environ["ANTHROPIC_WORKSPACE_ID"]
            self.client = anthropic.Anthropic(max_retries=4, default_headers=headers)

    def review(self, items: list[dict], recent: list[dict]) -> dict[str, dict]:
        """Returns {bid: decision}. Items whose batch failed are absent."""
        out: dict[str, dict] = {}
        errors: list[str] = []
        size = self.cfg.get("batch_size", 20)
        for i in range(0, len(items), size):
            batch = items[i:i + size]
            try:
                decisions = self._mock(batch) if self.mock else self._call(batch, recent)
            except Exception as ex:  # noqa: BLE001
                # Bad/expired key, no access, or wrong model name: fail the whole run
                # so GitHub emails a failure, instead of silently freezing the feed.
                status = getattr(ex, "status_code", None)
                if status in (401, 403, 404):
                    raise SystemExit(f"Anthropic API rejected the request ({status}): {ex}\n"
                                     "Check the ANTHROPIC_API_KEY secret and editor.model in config.yaml.")
                log(f"  ! editor batch failed, will retry next run: {type(ex).__name__}: {ex}")
                errors.append(f"{type(ex).__name__}: {ex}")
                continue
            valid = {it["bid"] for it in batch}
            for d in decisions:
                if d.get("id") in valid:
                    d["score"] = max(0, min(10, int(d.get("score", 0))))
                    if d.get("category") not in CATEGORIES:
                        d["category"] = "Research"
                    if d.get("sector") not in SECTORS:
                        d["sector"] = "Cross-sector"
                    out[d["id"]] = d
        if errors and not out:
            # Nothing got through: almost certainly a key/account problem, not bad luck.
            # Fail the run so GitHub sends a failure email instead of freezing the feed.
            raise SystemExit("Every editor call failed. First error:\n" + errors[0])
        return out

    def _call(self, batch, recent):
        resp = self.client.messages.create(
            model=self.cfg.get("model", "claude-haiku-5-5"),
            max_tokens=self.cfg.get("max_tokens", 4000),
            system=SYSTEM,
            tools=[TOOL],
            tool_choice={"type": "tool", "name": TOOL["name"]},
            messages=[{"role": "user", "content": _format_batch(batch, recent, self.threshold)}],
        )
        self.usage["calls"] += 1
        self.usage["input_tokens"] += resp.usage.input_tokens
        self.usage["output_tokens"] += resp.usage.output_tokens
        for block in resp.content:
            if block.type == "tool_use":
                return block.input.get("decisions", [])
        raise ValueError("no tool_use block in response")

    # Offline heuristic so the pipeline can be tested without an API key.
    def _mock(self, batch):
        out = []
        for it in batch:
            t = it["title"].lower()
            score = 4
            if re.search(r"attack|ransomware|disrupt|hack|breach|outage", t):
                score = 8
            if re.search(r"acquir|raises|funding|launch", t):
                score = 7
            if re.search(r"webinar|podcast|named a leader|award", t):
                score = 2
            cat = ("Incidents" if score == 8 else "Technology" if score == 7 else
                   "Advisory" if it["source_type"] == "gov" else "Research")
            out.append({"id": it["bid"], "score": score, "category": cat, "sector": "Cross-sector",
                        "summary": f"[mock] {it['title'][:120]}", "vendor": None,
                        "is_primary": it["source_type"] in ("gov", "research"), "same_as": None})
        return out


def summarize_pinned(editor: Editor, pin: dict) -> str:
    """Write a summary for a pinned story that has none."""
    if editor.mock:
        return f"[mock] {pin['title']}"
    from datetime import datetime, timezone
    editor_threshold, editor.threshold = editor.threshold, 0
    item = {"bid": "p1", "title": pin["title"], "source": pin.get("source", ""),
            "source_type": "media", "snippet": "", "published": datetime.now(timezone.utc)}
    try:
        d = editor._call([item], [])
        return d[0]["summary"]
    except Exception:  # noqa: BLE001
        return ""
    finally:
        editor.threshold = editor_threshold

