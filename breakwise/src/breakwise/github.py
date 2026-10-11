"""Post or update the Breakwise comment on a GitHub pull request."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .http import USER_AGENT
from .report import COMMENT_MARKER


class GitHubError(Exception):
    pass


def _api(method: str, url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
        **({"Content-Type": "application/json"} if data else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise GitHubError(f"GitHub API {method} {url} failed with {e.code}: {detail}") from None


def pr_number_from_event() -> int | None:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        event = json.load(f)
    pr = event.get("pull_request") or {}
    return pr.get("number") or (event.get("issue") or {}).get("number")


def _context(repo, pr, token, api):
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    pr = pr or pr_number_from_event()
    token = token or os.environ.get("GITHUB_TOKEN")
    api = (api or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    if not (repo and pr and token):
        raise GitHubError("Need GITHUB_REPOSITORY, a pull request number and GITHUB_TOKEN to comment")
    return repo, pr, token, api


def find_comment(*, repo: str | None = None, pr: int | None = None, token: str | None = None, api: str | None = None):
    """The existing Breakwise comment on the PR, if any."""
    repo, pr, token, api = _context(repo, pr, token, api)
    existing = None
    page = 1
    while page <= 10:
        comments = _api("GET", f"{api}/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}", token) or []
        for c in comments:
            if COMMENT_MARKER in (c.get("body") or ""):
                existing = c
        if len(comments) < 100:
            break
        page += 1
    return existing


def upsert_comment(body: str, *, repo: str | None = None, pr: int | None = None, token: str | None = None,
                   api: str | None = None) -> str:
    """Create the Breakwise comment, or edit the existing one so a PR only ever has one."""
    repo, pr, token, api = _context(repo, pr, token, api)
    if COMMENT_MARKER not in body:
        body = COMMENT_MARKER + "\n" + body
    existing = find_comment(repo=repo, pr=pr, token=token, api=api)
    if existing:
        res = _api("PATCH", f"{api}/repos/{repo}/issues/comments/{existing['id']}", token, {"body": body})
    else:
        res = _api("POST", f"{api}/repos/{repo}/issues/{pr}/comments", token, {"body": body})
    return (res or {}).get("html_url", "")
