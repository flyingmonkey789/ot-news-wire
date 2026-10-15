"""Write docs/feed.json and docs/feed.xml from the story list."""
from __future__ import annotations

from email.utils import format_datetime
from xml.etree import ElementTree as ET

from .util import DOCS, parse_iso, save_json

PUBLIC_FIELDS = ["url", "published", "category", "source", "sector", "title", "summary"]


def write_feed_json(stories: list[dict]) -> None:
    save_json(DOCS / "feed.json", [{k: s.get(k) for k in PUBLIC_FIELDS} for s in stories])


def write_feed_xml(stories: list[dict], site: dict) -> None:
    ATOM = "http://www.w3.org/2005/Atom"
    ET.register_namespace("atom", ATOM)
    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")
    base = site.get("url", "").rstrip("/") + "/"
    ET.SubElement(ch, "title").text = site.get("title", "OT Wire")
    ET.SubElement(ch, "link").text = base
    ET.SubElement(ch, "description").text = site.get("description", "")
    ET.SubElement(ch, "language").text = "en-us"
    ET.SubElement(ch, f"{{{ATOM}}}link", {"href": base + "feed.xml", "rel": "self",
                                          "type": "application/rss+xml"})
    if stories:
        ET.SubElement(ch, "lastBuildDate").text = format_datetime(parse_iso(stories[0]["published"]))
    for s in stories:
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = s["title"]
        ET.SubElement(it, "link").text = s["url"]
        ET.SubElement(it, "guid", {"isPermaLink": "true"}).text = s["url"]
        ET.SubElement(it, "pubDate").text = format_datetime(parse_iso(s["published"]))
        ET.SubElement(it, "category").text = s["category"]
        ET.SubElement(it, "description").text = f"{s.get('summary', '')} (Source: {s['source']})"
    ET.indent(rss)
    DOCS.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(rss).write(DOCS / "feed.xml", encoding="utf-8", xml_declaration=True)
