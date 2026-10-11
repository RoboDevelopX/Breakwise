"""Tiny HTTP client with an on-disk cache. Standard library only."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__

USER_AGENT = f"breakwise/{__version__} (+https://github.com/RoboDevelopX/breakwise)"


class HttpError(Exception):
    def __init__(self, url: str, status: int | None, message: str = ""):
        super().__init__(f"{status or 'error'} fetching {url} {message}".strip())
        self.url, self.status = url, status


def cache_dir() -> Path:
    base = os.environ.get("BREAKWISE_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "breakwise"
    )
    return Path(base)


class Http:
    def __init__(self, use_cache: bool = True, ttl: int = 6 * 3600, timeout: float = 20.0, github_token: str | None = None):
        self.use_cache = use_cache
        self.ttl = ttl
        self.timeout = timeout
        self.github_token = github_token if github_token is not None else os.environ.get("GITHUB_TOKEN")

    def get_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        headers = dict(headers or {})
        key = hashlib.sha256((url + json.dumps(headers, sort_keys=True)).encode()).hexdigest()
        path = cache_dir() / key[:2] / key
        if self.use_cache and path.is_file() and time.time() - path.stat().st_mtime < self.ttl:
            cached = path.read_text(encoding="utf-8")
            if cached.startswith("\x00404"):
                raise HttpError(url, 404, "(cached)")
            return cached
        headers.setdefault("User-Agent", USER_AGENT)
        if self.github_token and url.startswith("https://api.github.com/"):
            headers.setdefault("Authorization", f"Bearer {self.github_token}")
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404 and self.use_cache:
                self._store(path, "\x00404")
            raise HttpError(url, e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise HttpError(url, None, str(e)) from None
        if self.use_cache:
            self._store(path, body)
        return body

    def get_json(self, url: str, headers: dict[str, str] | None = None):
        return json.loads(self.get_text(url, headers))

    @staticmethod
    def _store(path: Path, body: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
