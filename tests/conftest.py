import json
from pathlib import Path

import pytest

from breakwise.http import HttpError

FIXTURES = Path(__file__).parent / "fixtures"


class FakeHttp:
    """Serves registry metadata and changelogs from memory. Unknown URLs are 404s."""

    def __init__(self, routes: dict[str, str | dict]):
        self.routes = routes
        self.requested: list[str] = []

    def get_text(self, url, headers=None):
        self.requested.append(url)
        if url not in self.routes:
            raise HttpError(url, 404)
        body = self.routes[url]
        return body if isinstance(body, str) else json.dumps(body)

    def get_json(self, url, headers=None):
        return json.loads(self.get_text(url, headers))


def pypi_meta(name, versions, github=None, latest=None, yanked=()):
    return {
        "info": {
            "version": latest or versions[-1],
            "project_urls": {"Source": f"https://github.com/{github}"} if github else {},
            "home_page": "",
        },
        "releases": {v: [{"upload_time": "2024-01-01T00:00:00", "yanked": v in yanked}] for v in versions},
    }


def npm_meta(name, versions, github=None, latest=None):
    return {
        "name": name,
        "dist-tags": {"latest": latest or versions[-1]},
        "versions": {v: {} for v in versions},
        "time": {v: "2024-01-01T00:00:00Z" for v in versions},
        "repository": {"type": "git", "url": f"git+https://github.com/{github}.git"} if github else None,
    }


@pytest.fixture
def fixture_text():
    return lambda name: (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("BREAKWISE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
