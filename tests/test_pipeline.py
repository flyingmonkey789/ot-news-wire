"""Offline end-to-end test: fixture feeds + mock editor, no network or API key.

    pip install pytest && pytest -q
"""
import importlib
import json
import shutil
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

NOW = format_datetime(datetime.now(timezone.utc))
MEDIA = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Media</title>
<item><title>Ransomware attack halts production at European steel plant</title><link>https://media.example/steel?utm_source=rss</link><pubDate>{NOW}</pubDate><description>Plant control systems were disrupted.</description></item>
<item><title>Best overtime thriller in NHL history</title><link>https://media.example/hockey</link><pubDate>{NOW}</pubDate></item>
<item><title>New phone launch event recap</title><link>https://media.example/phone</link><pubDate>{NOW}</pubDate></item>
<item><title>Join our SCADA webinar next week</title><link>https://media.example/webinar</link><pubDate>{NOW}</pubDate></item>
<item><title>Old story about a water utility hack</title><link>https://media.example/old</link><pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate></item>
</channel></rss>"""
RESEARCH = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Vendor</title>
<item><title>Acme OT raises funding to expand threat hunting</title><link>https://acme.example/funding</link><pubDate>{NOW}</pubDate></item>
<item><title>Acme OT launches platform module</title><link>https://acme.example/l1</link><pubDate>{NOW}</pubDate></item>
<item><title>Acme OT launches asset visibility add-on</title><link>https://acme.example/l2</link><pubDate>{NOW}</pubDate></item>
<item><title>Acme OT launches vulnerability service</title><link>https://acme.example/l3</link><pubDate>{NOW}</pubDate></item>
</channel></rss>"""
GNEWS = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>GN</title>
<item><title>Ransomware attack halts production at European steel plant - The Record</title><link>https://news.google.com/rss/articles/CBMiT1</link><pubDate>{NOW}</pubDate><source url="https://therecord.media">The Record</source></item>
<item><title>Hackers attack water utility SCADA system in Ohio - Local News</title><link>https://news.google.com/rss/articles/CBMiT2</link><pubDate>{NOW}</pubDate><source url="https://localnews.example">Local News</source></item>
</channel></rss>"""


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    for name in ("config.yaml", "blocklist.yaml", "pinned.yaml", "queries.yaml"):
        shutil.copy(REPO / name, tmp_path / name)
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "media.xml").write_text(MEDIA)
    (fx / "research.xml").write_text(RESEARCH)
    (fx / "gnews.xml").write_text(GNEWS)
    (tmp_path / "sources.yaml").write_text(f"""
sources:
  - {{id: media, name: Media Daily, url: "file://{fx}/media.xml", type: media}}
  - {{id: acme, name: Acme OT, url: "file://{fx}/research.xml", type: vendor, ot_native: true}}
  - {{id: broken, name: Broken, url: "file://{fx}/missing.xml", type: media}}
domains:
  therecord.media: {{type: media, org: The Record}}
""")
    (tmp_path / "queries.yaml").write_text("queries:\n  - {id: test-query, q: test}\n")
    monkeypatch.setenv("OTWIRE_ROOT", str(tmp_path))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    import pipeline.util as util
    importlib.reload(util)
    import pipeline.fetch as fetch
    importlib.reload(fetch)
    monkeypatch.setattr(fetch, "gnews_url", lambda q, d: f"file://{fx}/gnews.xml")
    import pipeline.publish, pipeline.review, pipeline.run as run  # noqa: E401
    importlib.reload(pipeline.publish)
    importlib.reload(pipeline.review)
    importlib.reload(run)
    monkeypatch.setattr(run, "resolve", lambda url, cache, session=None:
                        url.replace("https://news.google.com/rss/articles/", "https://resolved.example/"))
    return tmp_path, run


def test_end_to_end(repo):
    root, run = repo
    run.main(["--mock-editor"])
    feed = json.loads((root / "docs/feed.json").read_text())
    titles = [f["title"] for f in feed]

    # schema
    assert feed and set(feed[0]) == {"url", "published", "category", "source", "sector", "title", "summary"}
    # prefilter: sports and phone news never reach the editor; webinar scores low
    assert not any("overtime" in t or "phone" in t or "webinar" in t for t in titles)
    # age filter
    assert not any("Old story" in t for t in titles)
    # same headline from direct feed and Google News is deduped to one story
    assert sum("steel plant" in t for t in titles) == 1
    # Google News items get resolved links and the publisher as source, " - Publisher" stripped
    water = next(f for f in feed if "water utility" in f["title"])
    assert water["url"].startswith("https://resolved.example/") and water["source"] == "Local News"
    assert not water["title"].endswith("Local News")
    # vendor cap: 4 publishable Acme items -> 3
    assert sum("Acme" in t for t in titles) == 3
    # RSS output
    xml = (root / "docs/feed.xml").read_text()
    assert "<rss" in xml and "steel plant" in xml
    # broken feed recorded in health
    state = json.loads((root / "data/state.json").read_text())
    assert "failing_since" in state["feeds"]["feed:broken"]

    # second run: everything already seen, nothing new published
    run.main(["--mock-editor"])
    assert len(json.loads((root / "docs/feed.json").read_text())) == len(feed)


def test_blocklist_and_pin(repo):
    root, run = repo
    run.main(["--mock-editor"])
    (root / "blocklist.yaml").write_text("title_patterns: ['steel plant']\n")
    (root / "pinned.yaml").write_text(
        "pinned:\n  - {url: 'https://gov.example/guidance', title: 'Joint OT guidance', "
        "category: Policy, source: CISA, summary: 'Agencies published OT guidance.'}\n")
    run.main(["--mock-editor"])
    feed = json.loads((root / "docs/feed.json").read_text())
    assert not any("steel plant" in f["title"] for f in feed)
    assert any(f["title"] == "Joint OT guidance" and f["source"] == "CISA" for f in feed)


def test_review_report(repo):
    root, run = repo
    run.main(["--mock-editor"])
    import pipeline.review as review
    title, body = review.build_report()
    assert "Weekly review" in title
    assert "Yield by feed and query" in body and "query:test-query" in body
    assert "feed:broken" in body


def test_regroup_to_better_source(repo, monkeypatch):
    """Later official coverage of an already-published event replaces the link."""
    root, run = repo
    run.main(["--mock-editor"])
    feed = json.loads((root / "docs/feed.json").read_text())
    stories = json.loads((root / "data/stories.json").read_text())
    steel = next(s for s in stories if "steel plant" in s["title"])

    gov = root / "fx" / "gov.xml"
    gov.write_text(f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Gov</title>
<item><title>Agency statement on steel plant ransomware incident</title><link>https://gov.example/steel</link><pubDate>{NOW}</pubDate></item>
</channel></rss>""")
    src = (root / "sources.yaml").read_text().replace(
        "domains:", f"""  - {{id: gov, name: National CERT, url: "file://{gov}", type: gov, ot_native: true}}\ndomains:""")
    (root / "sources.yaml").write_text(src)

    import pipeline.editor as editor
    orig = editor.Editor._mock

    def mock(self, batch):
        out = orig(self, batch)
        for d, it in zip(out, batch):
            if "steel plant" in it["title"]:
                d.update(score=9, is_primary=True, same_as=steel["id"])
        return out
    monkeypatch.setattr(editor.Editor, "_mock", mock)
    run.main(["--mock-editor"])
    after = json.loads((root / "docs/feed.json").read_text())
    assert len(after) == len(feed)  # merged, not added
    s = next(x for x in after if x["url"] == "https://gov.example/steel")
    assert s["source"] == "National CERT"
