"""Check every source and query in the config: does it fetch, parse, and how
many items does it return? Run after editing sources.yaml or queries.yaml.

    python scripts/check_feeds.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.fetch import fetch_all  # noqa: E402
from pipeline.util import load_yaml  # noqa: E402

items, health = fetch_all(load_yaml("sources.yaml"), load_yaml("queries.yaml"))
bad = 0
for origin, h in sorted(health.items()):
    if h["ok"]:
        print(f"OK    {h['count']:>4}  {origin}")
    else:
        bad += 1
        print(f"FAIL        {origin}  ->  {h['error']}")
print(f"\n{len(health) - bad} ok, {bad} failing, {len(items)} items total")
sys.exit(1 if bad else 0)
