# OT Wire

The OT security news aggregator. A free, public feed of the most important
operational technology (OT) and industrial control system (ICS) security stories,
about 5–15 a day, picked by an automated editor and published as a static site.

Every hour a GitHub Action collects stories from government advisories, OT research
teams, trade press and Google News, filters them, has Claude Haiku score and summarize
them, groups duplicate coverage, and commits `docs/feed.json` and `docs/feed.xml`.
GitHub Pages serves the site from `docs/`.

OT Wire stores only the headline, its own one-sentence summary and the link.
Article text is never stored or republished.

## How it works

```
sources.yaml ─┐                                   ┌─> docs/feed.json  (site)
              ├─ fetch ─ dedupe ─ prefilter ─ Haiku editor ─ group ─ caps ─┤
queries.yaml ─┘   (state)  (keywords)  (score, category,  (same    (daily, └─> docs/feed.xml   (RSS out)
                                       summary, sector)   event)   vendor)
```

1. **Fetch** every source in `sources.yaml` and every Google News query in
   `queries.yaml` (`when:1d` appended automatically).
2. **Dedupe** against everything seen in the last 30 days (`data/state.json`), by
   normalized URL and by headline. Items older than 48 hours are ignored.
3. **Prefilter**: sources not marked `ot_native`, and all Google News results, must
   match an OT keyword pattern in `config.yaml`. Free, and cuts most of the noise
   before anything is sent to the API.
4. **Editor**: Claude Haiku scores each item 0–10 against the rubric, assigns one of
   **Advisory, Incidents, Research, Technology, Policy**, a sector, and writes a
   neutral one-sentence summary in its own words. It also flags which items cover
   the same event, including stories already published in the last 72 hours.
5. **Group**: coverage of one event becomes one story. The link goes to the best
   source: official or original reporting first, then gov > research > media > vendor.
   If better coverage arrives later (e.g. the CISA advisory after a news write-up),
   the existing story's link is upgraded instead of adding a duplicate.
6. **Publish** stories scoring ≥ 7, highest first, subject to a daily cap (20) and a
   per-vendor cap (3/day). Google News redirect links are resolved to the publisher
   URL before publishing.
7. **Write** `docs/feed.json` (newest first, latest 500) and `docs/feed.xml`.

Each feed item:

```json
{ "url": "...", "published": "2026-10-15T14:52:00Z", "category": "Incidents",
  "source": "The Record", "sector": "Water", "title": "...", "summary": "..." }
```

`sector` is an extra field the site's search uses.

## Setup (about 15 minutes)

1. **Create the repo.** New public repository on GitHub (`ot-news-wire`), then push
   this folder:
   ```bash
   cd ot-wire
   git init && git add . && git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/flyingmonkey789/ot-news-wire.git
   git push -u origin main
   ```
2. **Add your API key.** Get a key at console.anthropic.com. In the repo:
   *Settings → Secrets and variables → Actions → New repository secret*, name
   `ANTHROPIC_API_KEY`. Set a monthly spend limit in the Anthropic console as a
   backstop.
3. **Allow the Action to commit.** *Settings → Actions → General → Workflow
   permissions → Read and write permissions → Save.*
4. **Turn on Pages.** *Settings → Pages → Build and deployment → Deploy from a branch
   → `main` / `/docs` → Save.* The site appears at
   `https://flyingmonkey789.github.io/ot-news-wire/` within a minute or two.
5. **Set the site URL** in `config.yaml` (`site.url`) so `feed.xml` has correct links.
   If you add a custom domain later (Settings → Pages → Custom domain), update it there too.
6. **First run.** *Actions → Update feed → Run workflow.* Check the log, then open the
   site. After that it runs every hour at :17.
7. **Check the sources** (optional, recommended): locally run
   `python scripts/check_feeds.py`. A handful of research-team feed URLs in
   `sources.yaml` are marked `# verify`; fix or disable any that fail. Failing feeds
   also open an issue automatically after 24 hours.

> **Cloudflare Pages instead?** Connect the repo in Cloudflare Pages, framework
> preset "None", build command empty, output directory `docs`. Everything else is
> the same.

## Editorial controls

| File | What it does |
|---|---|
| `config.yaml` | Threshold, daily and vendor caps, model, prefilter keywords, timezone |
| `sources.yaml` | Direct RSS sources with `type` (gov/research/media/vendor), `org`, `ot_native`, `enabled`; plus a domain map that types Google News publishers |
| `queries.yaml` | Google News boolean queries |
| `blocklist.yaml` | Remove a story: by URL, domain or headline regex. Applies on the next run |
| `pinned.yaml` | Force a story in, regardless of score, caps or age |

Edit these directly on github.com; the next hourly run picks up the change, or run
the workflow manually.

**Weekly review.** Every Monday morning an issue labeled `weekly-review` lists:
stories published per source and category, yield per feed/query (reviewed vs.
published — the way to find dead queries), near misses that scored 5–6, stories
dropped by caps, the score distribution, API usage and failing feeds.

**Feed health.** If a source or query fails every run for 24 hours, an issue labeled
`feed-health` opens. It closes itself when the feed recovers.

## Cost

Only items that pass dedupe and the keyword prefilter reach the API, in batches of
20, and summaries are written only for items that clear the threshold. Expect a few
hundred items a day and roughly 1–2M input / 0.3M output tokens a month, which is a
few dollars at Haiku pricing. The weekly review reports actual token usage; check it
against current pricing in the Anthropic console. `max_items_per_run` caps spend if a
query suddenly floods. GitHub Actions and Pages are free for public repos.

## Local use

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest

pytest -q                                   # offline end-to-end tests
python scripts/check_feeds.py               # test every source and query
python -m pipeline.run --mock-editor --dry-run   # full run, no API calls, no writes
ANTHROPIC_API_KEY=sk-... python -m pipeline.run --dry-run   # real scoring, no writes
python -m pipeline.review --print           # weekly report to the terminal
```

## Repo layout

```
docs/            the site (index.html), feed.json, feed.xml — served by Pages
pipeline/        fetch.py, gnews.py (link resolver), editor.py (Haiku), run.py, review.py, publish.py
data/            state.json (seen items, feed health), stories.json (published stories
                 with scores), decisions.jsonl + runs.jsonl (21 days, for the review)
scripts/         check_feeds.py
tests/           offline tests with fixture feeds
```

## Notes

- **Google News links** are resolved with Google's own decoding endpoint (the method
  used by the open-source `googlenewsdecoder`). If Google changes it, the story keeps
  the Google link, which still opens the article, and the run log shows a warning.
- **Tuning the editor**: the rubric lives in `pipeline/editor.py` (`SYSTEM`). Adjust it
  if the weekly near-miss list shows consistent misjudgments.
- **The repo is public**, so `data/` (scores and decision logs) is visible. It contains
  only headlines, links and scores.
