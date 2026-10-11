"""Version parsing and comparison for PyPI (PEP 440) and npm (semver)."""

from __future__ import annotations

import re
from functools import total_ordering

from packaging.version import InvalidVersion, Version

from .models import Ecosystem

# A version number as it appears in changelog headings and release tags.
VERSION_RE = re.compile(
    r"(?<![\w.])v?(\d+\.\d+(?:\.\d+){0,2}(?:[-.]?(?:a|b|rc|alpha|beta|dev|post|pre|next|canary)[\w.-]*)?)(?![\w])",
    re.IGNORECASE,
)


@total_ordering
class SemVer:
    """Small semver implementation, good enough for ordering npm versions."""

    _re = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")

    def __init__(self, text: str):
        m = self._re.match(text.strip())
        if not m:
            raise ValueError(f"not a semver: {text!r}")
        self.major, self.minor, self.patch = (int(g or 0) for g in m.groups()[:3])
        self.pre = tuple(m.group(4).split(".")) if m.group(4) else ()

    @property
    def is_prerelease(self) -> bool:
        return bool(self.pre)

    def _key(self):
        # Releases sort after their pre-releases; numeric identifiers sort numerically.
        pre = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in self.pre)
        return (self.major, self.minor, self.patch, 0 if self.pre else 1, pre)

    def __eq__(self, other):
        return isinstance(other, SemVer) and self._key() == other._key()

    def __lt__(self, other):
        return self._key() < other._key()

    def __hash__(self):
        return hash(self._key())


def parse(version: str, ecosystem: Ecosystem):
    """Parse a version string. Returns None when it can't be parsed."""
    if not version:
        return None
    try:
        if ecosystem == Ecosystem.NPM:
            return SemVer(version)
        return Version(version.lstrip("vV"))
    except (InvalidVersion, ValueError):
        return None


def is_prerelease(version: str, ecosystem: Ecosystem) -> bool:
    v = parse(version, ecosystem)
    return bool(v and v.is_prerelease)


def latest_stable(versions: list[str], ecosystem: Ecosystem) -> str | None:
    parsed = [(parse(v, ecosystem), v) for v in versions]
    stable = [(p, v) for p, v in parsed if p is not None and not p.is_prerelease]
    if not stable:
        return None
    return max(stable, key=lambda pv: pv[0])[1]


def in_range(version: str, lower: str, upper: str, ecosystem: Ecosystem) -> bool:
    """True when lower < version <= upper."""
    v, lo, hi = parse(version, ecosystem), parse(lower, ecosystem), parse(upper, ecosystem)
    if v is None or lo is None or hi is None:
        return False
    return lo < v <= hi


def same(a: str, b: str, ecosystem: Ecosystem) -> bool:
    pa, pb = parse(a, ecosystem), parse(b, ecosystem)
    return pa is not None and pa == pb


def releases_between(versions: list[str], lower: str, upper: str, ecosystem: Ecosystem) -> list[str]:
    """Stable releases in (lower, upper], oldest first."""
    hits = [v for v in versions if in_range(v, lower, upper, ecosystem) and not is_prerelease(v, ecosystem)]
    return sorted(hits, key=lambda v: parse(v, ecosystem))


def find_version(text: str) -> str | None:
    m = VERSION_RE.search(text)
    return m.group(1) if m else None


def npm_range_floor(spec: str) -> str | None:
    """The lowest version an npm range allows: "^4.18.2" -> "4.18.2", ">=1.0 <2" -> "1.0"."""
    spec = spec.strip()
    if not spec or spec in {"*", "latest", "x"}:
        return None
    if spec.startswith(("file:", "link:", "git", "http", "workspace:", "npm:")) or "/" in spec:
        return None
    first = re.split(r"\s*\|\|\s*", spec)[0]
    m = re.search(r"(\d+(?:\.(?:\d+|x|\*))?(?:\.(?:\d+|x|\*))?(?:-[0-9A-Za-z.-]+)?)", first)
    if not m:
        return None
    parts = m.group(1).split("-")[0].split(".")
    parts = ["0" if p in {"x", "*"} else p for p in parts]
    while len(parts) < 3:
        parts.append("0")
    rest = m.group(1)[len(m.group(1).split("-")[0]):]
    return ".".join(parts) + rest
