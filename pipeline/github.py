"""Minimal GitHub REST client for opening/closing issues from Actions."""
from __future__ import annotations

import os

import requests

from .util import log

API = "https://api.github.com"


def _ctx():
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    return (token, repo) if token and repo else (None, None)


def enabled() -> bool:
    return all(_ctx())


def _req(method, path, **kw):
    token, repo = _ctx()
    r = requests.request(method, f"{API}/repos/{repo}{path}", timeout=20, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, **kw)
    r.raise_for_status()
    return r.json() if r.content else {}


def open_issue(title: str, body: str, labels: list[str]) -> int | None:
    if not enabled():
        log(f"  (GitHub not configured; would open issue: {title})")
        return None
    try:
        return _req("POST", "/issues", json={"title": title, "body": body, "labels": labels})["number"]
    except Exception as ex:  # noqa: BLE001
        log(f"  ! could not open issue: {ex}")
        return None


def close_issue(number: int, comment: str) -> None:
    if not enabled() or not number:
        return
    try:
        _req("POST", f"/issues/{number}/comments", json={"body": comment})
        _req("PATCH", f"/issues/{number}", json={"state": "closed"})
    except Exception as ex:  # noqa: BLE001
        log(f"  ! could not close issue #{number}: {ex}")
