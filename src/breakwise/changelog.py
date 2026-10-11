"""Fetch changelogs and turn them into classified, per-version change entries."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import versions
from .http import Http, HttpError
from .models import KIND_RANK, ChangeEntry, ChangeKind, Ecosystem
from .registry import PackageInfo, github_repo

CHANGELOG_FILES = [
    "CHANGELOG.md", "CHANGES.rst", "HISTORY.md", "CHANGES.md", "HISTORY.rst", "History.md",
    "NEWS.md", "CHANGELOG.rst", "docs/changelog.md", "docs/changes.rst", "docs/changelog.rst",
    "NEWS.rst", "RELEASE_NOTES.md", "CHANGELOG", "CHANGES", "CHANGES.txt", "NEWS", "docs/history.rst",
    "doc/changelog.rst", "docs/source/changelog.rst", "docs/CHANGELOG.md", "Changelog.rst", "ChangeLog.md",
    "CHANGELOG.txt", "doc/en/changelog.rst", "docs/release-notes.md", "docs/en/docs/release-notes.md",
    "docs/release_notes.md", "RELEASES.md", "docs/changes.md", "docs/news.rst", "docs/history.md",
    "doc/source/changelog.rst", "docs/source/changes.rst", "docs/source/history.rst", "CHANGELOG/CHANGELOG.md",
]

# ------------------------------------------------------------------ classification

_SECTION_KIND = [
    (re.compile(r"secur|vulnerab|\bcve\b", re.I), ChangeKind.SECURITY),
    (re.compile(r"(?<!non-)break|incompatib|remov|migrat|upgrad|api change|backward", re.I), ChangeKind.BREAKING),
    (re.compile(r"deprecat", re.I), ChangeKind.DEPRECATION),
    (re.compile(r"fix|bug|patch|correct", re.I), ChangeKind.FIX),
    (re.compile(r"feature|enhance|improve|addition|added|new\b", re.I), ChangeKind.FEATURE),
]

_SECURITY = re.compile(
    r"\bCVE-\d{4}-\d+|\bGHSA-\w{4}-|vulnerab|\bsecurity (?:issue|fix|bug|problem|flaw|advisory|hardening|vulnerabilit)"
    r"|\b(?:fix(?:es|ed)?|address(?:es|ed)?|patch(?:es|ed)?) (?:a |an |the |several |two |three |multiple |some )?(?:\w+[- ])?(?:severity )?security",
    re.I,
)
_BREAKING_STRONG = re.compile(
    r"(?<!non-)(?<!non )\bbreaking\b|backwards?[- ]incompatible|\bremoved?\b(?! (?:a |an |the )?(?:bug|issue|warning|unnecessary|unused|redundant|duplicate|extra))"
    r"|\bremoves\b|\bremoving\b|\bdrop(?:ped|s)? (?:support|the|python|node)|\bno longer (?:support|accept|allow|export|work|available|provide|exist)"
    r"|\brenamed\b|\bnow raises?\b|\bnow requires?\b|\bmust now\b|\bis now required\b|\bchanged the default\b"
    r"|\bdefault(?:s)? (?:is|has been|was) changed|\bby default instead of\b|\bdefault (?:value )?(?:changed|is now)"
    r"|\bmerged with\b|\bnow (?:a )?deprecated alias\b|\bremove previously deprecated\b",
    re.I,
)
_BREAKING_WEAK = re.compile(r"\bno longer\b|\binstead of\b|\bnow returns?\b|\bchanged\b|\bnow (?:a|an|the)?\s*\w+ by default\b", re.I)
_DEPRECATION = re.compile(r"deprecat", re.I)
_FIX = re.compile(r"^(?:fix|fixed|fixes|bugfix|resolve|correct|prevent|handle)", re.I)
_FEATURE = re.compile(r"^(?:add|added|adds|new|introduce|support|allow|implement)", re.I)


_INTERNAL = re.compile(
    r"^(?:docs?|ci|tests?|build|chore|refactor|style|perf|internal|typo|deps?|dependencies|build\(deps\)|chore\(deps\))\s*[:(]"
    r"|\b(?:CI|continuous integration|documentation|docs|docstrings?|typos?|benchmarks?|test suite|unit tests?|xfail|"
    r"change ?files?|setup\.py|setup\.cfg|pre-commit|linting|linters?|github actions|release (?:process|workflow|assets?)|"
    r"contributors?|readme|changelog|code of conduct|sponsors?|funding|intoto|attestations?)\b",
    re.I,
)
_TYPING = re.compile(r"\btype (?:hints?|annotations?|stubs?|checkers?)\b|\btyping\b|\binput type\b|\bmypy\b|\bpyright\b|\.pyi\b|\bpy\.typed\b", re.I)


def classify(text: str, section: str = "") -> ChangeKind:
    if _SECURITY.search(text):
        return ChangeKind.SECURITY
    explicit = re.search(r"(?<!non-)\bbreaking\b|incompatib|drop(?:ped|s)? support|removed? support", text, re.I)
    if _INTERNAL.search(text) and not explicit:
        return ChangeKind.OTHER  # docs, CI, packaging and transitive-dependency bumps
    if _TYPING.search(text) and not explicit:
        return ChangeKind.FIX  # typing-only changes don't break running code
    if _FIX.search(text) and not explicit:
        return ChangeKind.FIX
    section_kind = ChangeKind.OTHER
    for rx, kind in _SECTION_KIND:
        if section and rx.search(section):
            section_kind = kind
            break
    # A "Deprecations and Removals" heading holds both; let the entry text decide between them.
    if section and _DEPRECATION.search(section) and re.search(r"remov", section, re.I):
        section_kind = ChangeKind.DEPRECATION

    if _SECURITY.search(text):
        text_kind = ChangeKind.SECURITY
    elif _BREAKING_STRONG.search(text):
        text_kind = ChangeKind.BREAKING
    elif _DEPRECATION.search(text):
        text_kind = ChangeKind.DEPRECATION
    elif _BREAKING_WEAK.search(text) and section_kind not in (ChangeKind.FIX, ChangeKind.FEATURE):
        text_kind = ChangeKind.BREAKING
    elif _FIX.search(text):
        text_kind = ChangeKind.FIX
    elif _FEATURE.search(text):
        text_kind = ChangeKind.FEATURE
    else:
        text_kind = ChangeKind.OTHER

    # Bug-fix sections often say "removed a crash" or "no longer fails": trust the heading there,
    # unless the entry is explicit about breaking or security.
    if section_kind == ChangeKind.FIX and text_kind == ChangeKind.BREAKING and not re.search(
        r"\bbreaking\b|incompatible|drop(?:ped)? support|renamed|removed? (?:the |support|deprecated)", text, re.I
    ):
        text_kind = ChangeKind.FIX
    return max(text_kind, section_kind, key=lambda k: KIND_RANK[k])


# ------------------------------------------------------------------ symbol extraction

_CODE_SPAN = re.compile(r":[\w:]+:`~?!?([^`]+)`|``([^`]+)``|`([^`\n]+)`")
_DOTTED_CALL = re.compile(r"(?<![\w.$/])([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)\(\)")
_DOTTED = re.compile(r"(?<![\w.$/@-])([a-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*){1,4})(?![\w$/-])")
_CODEY_WORD = re.compile(r"(?<![\w.$/-])([a-z_$][a-z0-9$]*(?:[A-Z][a-z0-9$]*)+|[a-z][a-z0-9]*(?:_[a-z0-9]+)+|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?![\w$-])")
_IDENT = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")
_NOT_SYMBOLS = {
    "none", "true", "false", "null", "undefined", "self", "cls", "this", "e.g", "i.e", "etc", "vs",
    "python", "pip", "npm", "node", "yarn", "pnpm", "github", "readme", "changelog",
    "len", "str", "int", "dict", "list", "set", "tuple", "bytes", "bool", "float", "object", "type",
    "string", "number", "boolean", "array", "promise", "function", "async", "await", "return",
}
_FILEISH = re.compile(r"\.(?:py|pyi|md|rst|txt|toml|json|js|mjs|cjs|ts|tsx|jsx|cfg|ini|yml|yaml|lock|html|css|com|org|io|dev|net)$", re.I)
_PLATFORM = re.compile(
    r"(?:drop(?:ped|s|ping)?|remove[sd]?|end(?:ed|s)?|no longer support(?:s|ed)?)\s+(?:official\s+)?(?:support\s+(?:for\s+)?)?"
    r"(?:the\s+)?(?:(?:end[- ]of[- ]life|eol|old(?:er)?|deprecated|legacy)\s+)?"
    r"(python|node(?:\.js)?|nodejs)\s*v?(\d+(?:\.\d+)?(?:\s*(?:,|and|&|or)\s*v?\d+(?:\.\d+)?)*)",
    re.I,
)


def extract_symbols(text: str) -> list[str]:
    seen: dict[str, None] = {}

    def add(raw: str):
        s = raw.strip().strip(".,;:")
        s = re.sub(r"^(?:from|import|new)\s+", "", s)
        s = re.sub(r"\(.*$", "", s)  # foo(bar=1) -> foo
        s = re.sub(r"\[.*$", "", s)
        s = s.lstrip("@~!").rstrip(".")
        s = s.split("=", 1)[0].strip()
        if not s or not _IDENT.match(s) or len(s) < 3:
            return
        if s.lower() in _NOT_SYMBOLS or _FILEISH.search(s) or re.fullmatch(r"v?\d[\d.]*", s):
            return
        seen.setdefault(s, None)

    for m in _CODE_SPAN.finditer(text):
        span = next(g for g in m.groups() if g is not None)
        # Keyword arguments inside a call, e.g. ``Retry(method_whitelist=...)``.
        for kw in re.findall(r"[(,]\s*([A-Za-z_]\w*)\s*=", span):
            add(kw)
        add(span)
    plain = _CODE_SPAN.sub(" ", text)
    plain = re.sub(r"https?://\S+", " ", plain)
    for rx in (_DOTTED_CALL, _DOTTED, _CODEY_WORD):
        for m in rx.finditer(plain):
            add(m.group(1))
    for m in _PLATFORM.finditer(text):
        runtime = "node" if m.group(1).lower().startswith("node") else "python"
        for v in re.findall(r"\d+(?:\.\d+)?", m.group(2)):
            seen.setdefault(f"@{runtime}:{v}", None)
    return list(seen)


# ------------------------------------------------------------------ text cleanup


def clean(text: str) -> str:
    t = text
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r":(?:pr|issue|ghsa|user|cve|bug|commit|gh|gh-pr|gh-issue|ticket|doc|ref):`[^`]*`", "", t)
    t = re.sub(r"`([^`<]+?)\s*<[^>]+>`__?", r"\1", t)  # rst links
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)  # md links / images
    t = re.sub(r"\s+(?:by|thanks to|from)\s+@[\w-]+(?:\s*(?:,|and)\s*@[\w-]+)*(?:\s+in\s+#?\d+(?:\s*(?:,|and)\s*#?\d+)*)?", "", t)
    t = re.sub(r"\s*\((?:#\d+(?:,\s*)?)+\)", "", t)
    t = re.sub(r"\s*\(\s*(?:[\w-]+/[\w.-]+)?#\d+\s*\)", "", t)
    t = re.sub(r"\s+-\s+by\s+\S+(?:\s+in\s+\S+)?", "", t)
    t = re.sub(r"\s+in\s+#\d+\b", "", t)
    t = re.sub(r"``([^`]+)``", r"`\1`", t)  # reST literals read better as single backticks
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


# ------------------------------------------------------------------ section parsing


@dataclass
class Section:
    version: str
    heading: str
    lines: list[str] = field(default_factory=list)


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_UNDERLINE = re.compile(r"^([=\-~^\"'*+#`:.])\1{2,}\s*$")
_BOLD_LINE = re.compile(r"^\s*(?:\*\*|__)(.+?)(?:\*\*|__):?\s*$")
_BULLET = re.compile(r"^(\s*)(?:[-*+•]|\d+[.)])\s+(.*)$")
_DATE_ONLY = re.compile(r"^\(?\s*(?:released\s*:?\s*)?(?:\d{4}-\d{2}-\d{2}|[A-Z][a-z]+ \d{1,2},? \d{4}|unreleased)\s*\)?\.?$", re.I)


def split_sections(text: str) -> list[Section]:
    """Split a changelog into one Section per released version.

    Works with Markdown (# headings), reStructuredText (underlined/overlined titles) and
    mixtures of the two. The heading style used by the first versioned heading becomes the
    "release" level; deeper headings are kept as sub-headings inside the release.
    """
    lines = text.splitlines()
    headings: list[tuple[int, int, tuple, str]] = []  # (start, end, style, title)
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _MD_HEADING.match(line)
        if m and not line.startswith("#!"):
            headings.append((i, i + 1, ("md", len(m.group(1))), m.group(2).strip()))
            i += 1
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if line.strip() and not _UNDERLINE.match(line) and _UNDERLINE.match(nxt) and len(nxt.strip()) >= max(3, len(line.strip()) * 0.6):
            over = i > 0 and _UNDERLINE.match(lines[i - 1]) and lines[i - 1].strip()[0] == nxt.strip()[0]
            start = i - 1 if over else i
            headings.append((start, i + 2, ("rst", nxt.strip()[0], bool(over)), line.strip()))
            i += 2
            continue
        i += 1

    release_style = None
    for _, _, style, title in headings:
        if len(title) < 120 and versions.find_version(title):
            release_style = style
            break
    if release_style is None:
        return _split_plain_versions(lines)

    sections: list[Section] = []
    current: Section | None = None
    cursor = 0
    for start, end, style, title in headings:
        if current is not None:
            current.lines += lines[cursor:start]
        if style == release_style:
            v = versions.find_version(title)
            if v and len(title) < 120:
                current = Section(version=v, heading=title)
                sections.append(current)
            elif re.search(r"unreleased|upcoming|in progress|\bdev\b|\bnext\b|\bmain\b|master", title, re.I):
                current = None
            elif current is not None:
                # Same level as releases but no version: a sub-heading written at the wrong level
                # (e.g. "## Security Fixes" under "## v1.19.0").
                current.lines.append(f"\x00HEADING {title}")
        elif current is not None:
            current.lines.append(f"\x00HEADING {title}")
        cursor = end
    if current is not None:
        current.lines += lines[cursor:]
    return sections


def _split_plain_versions(lines: list[str]) -> list[Section]:
    """Fallback for changelogs whose version lines aren't formatted as headings."""
    sections: list[Section] = []
    current = None
    for line in lines:
        s = line.strip()
        v = versions.find_version(s)
        if v and not _BULLET.match(line) and len(s) < 60 and re.match(r"^(?:v(?:ersion)?\s*)?\[?v?\d", s, re.I):
            current = Section(version=v, heading=s)
            sections.append(current)
        elif current is not None:
            current.lines.append(line)
    return sections


def parse_entries(section: Section) -> list[ChangeEntry]:
    entries: list[ChangeEntry] = []
    sub = ""
    buf: list[str] = []
    buf_kind = None  # "bullet" | "para"
    bullet_indent = 0
    parent_label = ""
    in_fence = False

    def flush():
        nonlocal buf, buf_kind
        if buf:
            raw = "\n".join(buf)
            text = clean(re.sub(r"\s*\n\s*", " ", raw))
            if _keep(text, buf_kind):
                label = " / ".join(x for x in (sub, parent_label if buf_kind == "bullet" else "") if x)
                entries.append(
                    ChangeEntry(
                        version=section.version,
                        text=text,
                        kind=classify(text, label),
                        section=label,
                        symbols=extract_symbols(raw),
                    )
                )
        buf, buf_kind = [], None

    for line in section.lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            if buf:
                buf.append(line)
            continue
        if in_fence:
            if buf:
                buf.append(line)
            continue
        if line.startswith("\x00HEADING "):
            flush()
            sub, parent_label = line[len("\x00HEADING "):].strip(), ""
            continue
        bold = _BOLD_LINE.match(line)
        if bold and not _BULLET.match(line):
            flush()
            sub, parent_label = bold.group(1).strip(), ""
            continue
        b = _BULLET.match(line)
        if b:
            flush()
            indent = len(b.group(1).expandtabs())
            body = b.group(2).strip()
            if indent == 0:
                parent_label = ""
            if body.endswith(":") and len(body) < 80:
                parent_label = body.rstrip(":").strip("*_ ")
                bullet_indent = indent
                continue
            if indent <= bullet_indent and indent == 0:
                parent_label = ""
            buf, buf_kind = [body], "bullet"
            continue
        if not line.strip():
            if buf_kind == "para":
                flush()
            continue
        directive = re.match(r"^\.\.\s+(caution|warning|important|note|attention|danger)::\s*(.*)$", line.strip(), re.I)
        if directive:
            flush()
            buf, buf_kind = [f"{directive.group(1).title()}: {directive.group(2)}".strip()], "para"
            continue
        if buf_kind == "bullet" and (line.startswith((" ", "\t"))):
            buf.append(line.strip())
            continue
        if buf_kind == "bullet":
            flush()
        if buf_kind is None:
            buf_kind = "para"
        buf.append(line.strip())
    flush()
    return entries


def _keep(text: str, kind: str | None) -> bool:
    if not text or len(text) < 12:
        return False
    if _DATE_ONLY.match(text) or text.startswith(("Released", "[GitHub release]", "Full Changelog", "**Full Changelog")):
        return False
    if re.fullmatch(r"(?:released\s+)?[\d\-/,\s()]+", text, re.I):
        return False
    if kind == "para" and len(text) < 30:
        return False
    if re.fullmatch(r"\[?[\w\s.-]*\]?:?\s*https?://\S+", text):
        return False
    return True


# ------------------------------------------------------------------ fetching


@dataclass
class ChangelogResult:
    entries: list[ChangeEntry]
    url: str = ""
    covered: list[str] = field(default_factory=list)  # versions we found notes for
    missing: list[str] = field(default_factory=list)  # versions in range with no notes
    hints: dict[str, str] = field(default_factory=dict)  # old name -> replacement, from the whole changelog


_IN_FAVOR = re.compile(r"(?P<old>.+?)\b(?:in favou?r of|replaced (?:by|with)|use|renamed to|superseded by)\b(?P<new>.+)", re.I | re.S)


def _hint_key(span: str) -> str | None:
    kw = re.findall(r"[(,]\s*([A-Za-z_]\w*)\s*=", span)
    if kw:
        return kw[0]
    span = re.sub(r"\(.*$", "", span).strip().lstrip("~!.")
    if not _IDENT.match(span):
        return None
    return span.split(".")[-1]


def replacement_hints(text: str) -> dict[str, str]:
    """Scan a whole changelog for "A is deprecated in favor of B" style sentences.

    Removals often only say "Removed deprecated X"; the replacement was announced back when X
    was deprecated, sometimes years earlier. This recovers it.
    """
    hints: dict[str, str] = {}
    for sentence in re.split(r"(?<=[.!?])\s+|\n\s*[-*+]\s+", text):
        if not re.search(r"deprecat|renamed|in favou?r of|replaced (?:by|with)|instead", sentence, re.I):
            continue
        m = _IN_FAVOR.search(sentence)
        if not m:
            continue
        old_spans = [next(g for g in x.groups() if g) for x in _CODE_SPAN.finditer(m.group("old"))]
        new_spans = [next(g for g in x.groups() if g) for x in _CODE_SPAN.finditer(m.group("new"))]
        # "Deprecated ``Retry`` options ``Retry.A`` and ``Retry(b=...)``": drop the container.
        old_spans = [o for o in old_spans if not any(x != o and x.startswith((o + ".", o + "(")) for x in old_spans)]
        if not old_spans or not new_spans:
            continue
        if len(old_spans) == len(new_spans):
            pairs = zip(old_spans, new_spans)
        elif len(new_spans) == 1:
            pairs = ((o, new_spans[0]) for o in old_spans)
        else:
            continue
        for o, n in pairs:
            ko, kn = _hint_key(o), _hint_key(n)
            if ko and kn and ko != kn and not kn.endswith(("Warning", "Error")):
                hints.setdefault(ko, kn)
    return hints


def raw_github_url(url: str) -> str | None:
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$", url.strip())
    if m:
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}/{m.group(4)}"
    if "raw.githubusercontent.com" in url:
        return url
    return None


def entries_for_range(text: str, wanted: list[str], ecosystem: Ecosystem) -> tuple[list[ChangeEntry], list[str]]:
    sections = split_sections(text)
    out: list[ChangeEntry] = []
    covered: list[str] = []
    for v in wanted:
        for s in sections:
            if versions.same(s.version, v, ecosystem):
                out += parse_entries(s)
                covered.append(v)
                break
    return out, covered


def fetch_entries(pi: PackageInfo, wanted: list[str], http: Http) -> ChangelogResult:
    """Release notes for each version in `wanted`, from changelog files first, then GitHub releases."""
    if not wanted:
        return ChangelogResult(entries=[])
    eco = pi.ecosystem
    candidates: list[str] = []
    for u in pi.changelog_urls:
        if raw := raw_github_url(u):
            candidates.append(raw)
    if pi.github:
        owner, repo = pi.github
        prefixes = [f"{pi.repo_subdir}/"] if pi.repo_subdir else []
        prefixes.append("")
        for prefix in prefixes:
            for name in CHANGELOG_FILES:
                candidates.append(f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{prefix}{name}")

    best = ChangelogResult(entries=[], missing=list(wanted))
    for url in dict.fromkeys(candidates):
        try:
            text = http.get_text(url)
        except HttpError:
            continue
        if text.lstrip().startswith("<!DOCTYPE") or text.lstrip().startswith("<html"):
            continue
        entries, covered = entries_for_range(text, wanted, eco)
        if len(covered) > len(best.covered):
            best = ChangelogResult(entries=entries, url=_browse_url(url), covered=covered,
                                   missing=[v for v in wanted if v not in covered], hints=replacement_hints(text))
        if not best.missing:
            break

    if best.missing and pi.github:
        rel = _from_github_releases(pi, best.missing, http)
        if rel.covered:
            best.entries += rel.entries
            best.covered += rel.covered
            best.missing = [v for v in best.missing if v not in rel.covered]
            best.url = best.url or rel.url
            for k, v in rel.hints.items():
                best.hints.setdefault(k, v)

    order = {v: i for i, v in enumerate(wanted)}
    best.entries.sort(key=lambda e: order.get(e.version, len(order)))
    return best


def _browse_url(raw: str) -> str:
    m = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$", raw)
    return f"https://github.com/{m.group(1)}/{m.group(2)}/blob/{m.group(3)}/{m.group(4)}" if m else raw


def _from_github_releases(pi: PackageInfo, wanted: list[str], http: Http) -> ChangelogResult:
    owner, repo = pi.github  # type: ignore[misc]
    out = ChangelogResult(entries=[], url=f"https://github.com/{owner}/{repo}/releases")
    remaining = set(wanted)
    for page in range(1, 4):
        try:
            data = http.get_json(
                f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=100&page={page}",
                headers={"Accept": "application/vnd.github+json"},
            )
        except (HttpError, ValueError):
            break
        if not isinstance(data, list) or not data:
            break
        for rel in data:
            tag = rel.get("tag_name") or ""
            # Monorepos tag like "pkg@1.2.3"; only take tags for this package (or untagged ones).
            if "@" in tag and not tag.startswith(("v", "@")) and not tag.split("@")[0].endswith(pi.name.split("/")[-1]):
                continue
            v = versions.find_version(tag) or versions.find_version(rel.get("name") or "")
            if not v:
                continue
            match = next((w for w in remaining if versions.same(v, w, pi.ecosystem)), None)
            if not match:
                continue
            section = Section(version=match, heading=tag, lines=(rel.get("body") or "").splitlines())
            for e in parse_entries(section):
                e.source = "github-release"
                out.entries.append(e)
            out.hints.update({k: v for k, v in replacement_hints(rel.get("body") or "").items() if k not in out.hints})
            out.covered.append(match)
            remaining.discard(match)
        if not remaining:
            break
    out.missing = sorted(remaining)
    return out


__all__ = [
    "classify", "extract_symbols", "split_sections", "parse_entries", "fetch_entries",
    "entries_for_range", "raw_github_url", "github_repo", "ChangelogResult",
]
