"""Plain data types shared across Breakwise."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Ecosystem(str, Enum):
    PYPI = "pypi"
    NPM = "npm"


class ChangeKind(str, Enum):
    BREAKING = "breaking"
    SECURITY = "security"
    DEPRECATION = "deprecation"
    FIX = "fix"
    FEATURE = "feature"
    OTHER = "other"


# Higher number = more important. Used for sorting and picking a headline.
KIND_RANK = {
    ChangeKind.BREAKING: 5,
    ChangeKind.SECURITY: 4,
    ChangeKind.DEPRECATION: 3,
    ChangeKind.FIX: 2,
    ChangeKind.FEATURE: 1,
    ChangeKind.OTHER: 0,
}


@dataclass
class Dependency:
    name: str
    ecosystem: Ecosystem
    spec: str = ""  # the raw version spec from the manifest, e.g. "==2.0.1" or "^4.18.0"
    current: str | None = None  # the version the project is on today
    current_source: str = ""  # "pinned", "lockfile", "installed", "lower-bound"
    manifest: str = ""  # which file it came from
    line: int | None = None


@dataclass
class ChangeEntry:
    """One bullet (or paragraph) from a changelog."""

    version: str
    text: str
    kind: ChangeKind = ChangeKind.OTHER
    section: str = ""  # sub-heading inside the release, e.g. "Deprecations & Removals"
    symbols: list[str] = field(default_factory=list)
    fix: str = ""  # a known fix, when the source spells one out (built-in migration rules)
    source: str = "changelog"  # "changelog" | "github-release" | "rules"


@dataclass
class UsageSite:
    file: str
    line: int
    code: str
    symbol: str  # what in the user's code matched, e.g. "flask.redirect"


@dataclass
class Impact:
    entry: ChangeEntry
    sites: list[UsageSite]
    matched_symbol: str = ""
    confidence: str = "medium"  # "high" | "medium" | "low"
    explanation: str = ""
    fix: str = ""
    ai: bool = False  # explanation/fix written by Claude rather than templates

    @property
    def is_runtime(self) -> bool:
        """A Python/Node.js version drop rather than an API change."""
        return self.matched_symbol.startswith("@")

    @property
    def counts(self) -> bool:
        """Whether this impact makes the update "matter" in the headline."""
        if self.is_runtime:
            return False
        if self.entry.kind in (ChangeKind.BREAKING, ChangeKind.DEPRECATION, ChangeKind.SECURITY):
            return self.confidence != "low"
        return self.confidence == "high" and self.entry.kind == ChangeKind.OTHER


@dataclass
class PackageReport:
    dep: Dependency
    target: str | None = None
    releases: list[str] = field(default_factory=list)
    entries: list[ChangeEntry] = field(default_factory=list)
    impacts: list[Impact] = field(default_factory=list)
    changelog_url: str = ""
    status: str = "ok"  # "ok", "up-to-date", "no-changelog", "unknown-version", "error"
    note: str = ""
    imported: bool = True  # whether the user's code imports this package at all
    hints: dict[str, str] = field(default_factory=dict)  # old API name -> replacement, from the changelog

    @property
    def has_update(self) -> bool:
        return bool(self.target and self.dep.current and self.target != self.dep.current)

    @property
    def breaking_impacts(self) -> list[Impact]:
        return [i for i in self.impacts if i.entry.kind == ChangeKind.BREAKING and i.confidence != "low" and not i.is_runtime]

    @property
    def runtime_impacts(self) -> list[Impact]:
        return [i for i in self.impacts if i.is_runtime]

    @property
    def counted_impacts(self) -> list[Impact]:
        return [i for i in self.impacts if i.counts]

    @property
    def unmatched_breaking(self) -> list[ChangeEntry]:
        hit = {id(i.entry) for i in self.impacts}
        return [e for e in self.entries if e.kind == ChangeKind.BREAKING and id(e) not in hit]

    @property
    def security_entries(self) -> list[ChangeEntry]:
        return [e for e in self.entries if e.kind == ChangeKind.SECURITY]

    @property
    def matters(self) -> bool:
        """An update matters if it touches the user's code or fixes a security issue."""
        return bool(self.counted_impacts) or bool(self.security_entries)


@dataclass
class ScanReport:
    root: str
    packages: list[PackageReport]
    mode: str = "scan"  # "scan" (current -> latest) or "pr" (base -> head)
    ai: bool = False
    code_files: int = -1  # Python/JS files scanned for usage; 0 means there was no code to check against

    @property
    def code_checked(self) -> bool:
        return self.code_files != 0

    @property
    def updated(self) -> list[PackageReport]:
        return [p for p in self.packages if p.has_update]

    @property
    def mattering(self) -> list[PackageReport]:
        return [p for p in self.updated if p.matters]

    @property
    def breaking(self) -> list[PackageReport]:
        return [p for p in self.updated if p.breaking_impacts]
