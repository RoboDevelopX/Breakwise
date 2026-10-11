"""Package metadata from PyPI and the npm registry."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

from . import versions
from .http import Http, HttpError
from .models import Ecosystem

GITHUB_RE = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?].*)?$", re.IGNORECASE)


@dataclass
class PackageInfo:
    name: str
    ecosystem: Ecosystem
    versions: list[str] = field(default_factory=list)
    latest: str | None = None
    github: tuple[str, str] | None = None  # (owner, repo)
    repo_subdir: str = ""  # for monorepos, e.g. "packages/foo"
    changelog_urls: list[str] = field(default_factory=list)
    release_dates: dict[str, str] = field(default_factory=dict)


def github_repo(url: str | None) -> tuple[str, str] | None:
    if not url:
        return None
    m = GITHUB_RE.search(url.strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if owner.lower() in {"sponsors", "orgs", "apps", "marketplace"}:
        return None
    return owner, repo


def fetch(name: str, ecosystem: Ecosystem, http: Http) -> PackageInfo:
    if ecosystem == Ecosystem.NPM:
        return _fetch_npm(name, http)
    return _fetch_pypi(name, http)


def _fetch_pypi(name: str, http: Http) -> PackageInfo:
    data = http.get_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    info = data.get("info", {})
    releases = data.get("releases", {}) or {}
    # Yanked releases still carry release notes that apply to anyone crossing them (urllib3 2.0.0
    # was yanked, and it's where most of the 2.x breaking changes live), so keep them in `versions`
    # but never offer one as the upgrade target.
    vers = list(releases.keys())
    installable = [v for v, files in releases.items() if files and not all(f.get("yanked") for f in files)]
    pi = PackageInfo(name=name, ecosystem=Ecosystem.PYPI, versions=vers)
    pi.latest = versions.latest_stable(installable, Ecosystem.PYPI) or info.get("version")
    for v, files in releases.items():
        if files:
            pi.release_dates[v] = (files[0].get("upload_time") or "")[:10]

    urls = info.get("project_urls") or {}
    ordered = sorted(
        urls.items(),
        key=lambda kv: (0 if re.search(r"source|repo|code|github", kv[0], re.I) else 1 if "home" in kv[0].lower() else 2),
    )
    candidates = [u for _, u in ordered] + [info.get("home_page") or ""]
    for u in candidates:
        if gh := github_repo(u):
            pi.github = gh
            break
    for label, u in urls.items():
        if re.search(r"change|history|release|news|what.?s.?new", label, re.I):
            pi.changelog_urls.append(u)
    return pi


def _fetch_npm(name: str, http: Http) -> PackageInfo:
    quoted = urllib.parse.quote(name, safe="@")
    # The abbreviated "install" document is a fraction of the size of the full one (which runs
    # to tens of megabytes for packages like typescript) and has everything but the repo URL.
    data = http.get_json(f"https://registry.npmjs.org/{quoted}", headers={"Accept": "application/vnd.npm.install-v1+json"})
    vers = list((data.get("versions") or {}).keys())
    pi = PackageInfo(name=name, ecosystem=Ecosystem.NPM, versions=vers)
    pi.latest = (data.get("dist-tags") or {}).get("latest") or versions.latest_stable(vers, Ecosystem.NPM)
    try:
        latest_doc = http.get_json(f"https://registry.npmjs.org/{quoted}/{pi.latest}") if pi.latest else {}
    except (HttpError, ValueError):
        latest_doc = {}
    repo = latest_doc.get("repository") or data.get("repository")
    if isinstance(repo, dict):
        pi.github = github_repo(repo.get("url"))
        pi.repo_subdir = (repo.get("directory") or "").strip("/")
    elif isinstance(repo, str):
        pi.github = github_repo(repo if ":" in repo else f"github.com/{repo}")
    if not pi.github:
        bugs = latest_doc.get("bugs")
        bugs_url = bugs.get("url") if isinstance(bugs, dict) else bugs if isinstance(bugs, str) else None
        pi.github = github_repo(latest_doc.get("homepage")) or github_repo(bugs_url)
    return pi


__all__ = ["PackageInfo", "fetch", "github_repo", "HttpError"]
