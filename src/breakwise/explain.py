"""Turn matched changelog entries into plain-English explanations and fixes.

Two modes:
- Deterministic (default, no API key): template explanations plus fixes pulled out of the
  changelog text ("use X instead", "renamed A to B", ...).
- AI (`--ai`, needs ANTHROPIC_API_KEY): Claude reads the release notes next to the user's
  code, drops false positives, catches breaking changes the static matcher missed, and
  writes a concrete fix for each one.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .models import ChangeEntry, ChangeKind, Impact, PackageReport, UsageSite

DEFAULT_MODEL = "claude-opus-5-5"

# ------------------------------------------------------------------ deterministic

_REPLACEMENT_PATTERNS = [
    re.compile(r"renamed\s+(?:from\s+)?`{1,2}(?P<old>[^`]+)`{1,2}\s+to\s+`{1,2}(?P<new>[^`]+)`{1,2}", re.I),
    re.compile(r"`{1,2}(?P<old>[^`]+)`{1,2}\s+(?:has been |was |is )?renamed to\s+`{1,2}(?P<new>[^`]+)`{1,2}", re.I),
    re.compile(r"use\s+`{1,2}(?P<new>[^`]+)`{1,2}\s+instead(?:\s+of\s+`{1,2}(?P<old>[^`]+)`{1,2})?", re.I),
    re.compile(r"in favou?r of\s+`{1,2}(?P<new>[^`]+)`{1,2}", re.I),
    re.compile(r"replaced (?:by|with)\s+`{1,2}(?P<new>[^`]+)`{1,2}", re.I),
    re.compile(r"`{1,2}(?P<new>[^`]+)`{1,2}\s+should be used instead", re.I),
    re.compile(r"(?:migrate|switch|move) to\s+`{1,2}(?P<new>[^`]+)`{1,2}", re.I),
    re.compile(r"(?:^|[,;.:]\s*|\bplease\s+)(?:explicitly\s+|instead\s+)?use\s+`{1,2}(?P<new>[^`]+)`{1,2}", re.I),
]


def replacement(text: str) -> tuple[str | None, str | None]:
    for rx in _REPLACEMENT_PATTERNS:
        m = rx.search(text)
        if m:
            d = m.groupdict()
            return d.get("old"), d.get("new")
    return None, None


def _code(s: str) -> str:
    return f"`{s.strip('`')}`"


def deterministic(report: PackageReport) -> None:
    name = report.dep.name
    for imp in report.impacts:
        e = imp.entry
        where = _where(imp.sites)
        used = imp.sites[0].symbol if imp.sites else imp.matched_symbol
        if imp.matched_symbol.startswith("@"):
            runtime = "Python" if "python" in imp.matched_symbol else "Node.js"
            dropped = imp.matched_symbol.split(":")[1]
            imp.explanation = (
                f"{name} {e.version} no longer supports {runtime} {dropped}, and your project still declares "
                f"support for it ({where})."
            )
            imp.fix = (
                f"Move your minimum {runtime} version above {dropped} before upgrading, or keep "
                f"`{name}<{e.version}` until you can."
            )
            continue
        lead = {ChangeKind.SECURITY: "Security fix: ", ChangeKind.FIX: "Bug fix: "}.get(e.kind, "")
        imp.explanation = f"{lead}{_first_sentence(e.text)} Your code uses {_code(used)} at {where}."
        old, new = replacement(e.text)
        if not new:
            key = imp.matched_symbol.split(".")[-1]
            if key in report.hints:
                old, new = key, report.hints[key]
        if e.fix:
            imp.fix = e.fix
        elif new and (old or e.kind != ChangeKind.BREAKING or _says(e.text, imp.matched_symbol, "removed")
                      or _says(e.text, imp.matched_symbol, "deprecated") or "renamed" in e.text.lower()):
            imp.fix = f"Replace {_code(old or used)} with {_code(new)}."
        elif new:
            imp.fix = f"Use {_code(new)} instead, as the release notes suggest."
        elif e.kind == ChangeKind.BREAKING and _says(e.text, imp.matched_symbol, "removed"):
            imp.fix = (
                f"{_code(imp.matched_symbol)} is gone in {e.version}. Rewrite the lines above without it "
                f"(the release notes link has details), or pin `{name}<{e.version}` for now."
            )
        elif e.kind == ChangeKind.BREAKING and re.search(r"default", e.text, re.I):
            imp.fix = "Pass the old value explicitly at the call sites above to keep today's behavior."
        elif e.kind == ChangeKind.DEPRECATION and _says(e.text, imp.matched_symbol, "deprecated"):
            imp.fix = f"Still works, but expect a warning and removal in a later release. Move off {_code(imp.matched_symbol)} when convenient."
        elif e.kind == ChangeKind.DEPRECATION:
            imp.fix = "This usage now triggers a deprecation warning in some cases. Check the call sites above against the note."
        elif e.kind == ChangeKind.BREAKING:
            imp.fix = f"Check the call sites above against the new behavior, or pin `{name}<{e.version}` until you do."
        else:
            imp.fix = "Probably fine, but worth a quick look at the lines above."


def _says(text: str, symbol: str, what: str) -> bool:
    """Does the entry say `symbol` itself was removed/deprecated (not just something about it)?"""
    name = re.escape(symbol.split(".")[-1])
    sym = rf"`{{0,2}}[\w.]*{name}(?:\(\))?`{{0,2}}"
    if what == "removed":
        verbs = r"(?:remov\w*|drop\w*|delet\w*)"
    else:
        verbs = r"deprecat\w*"
    return bool(
        re.search(rf"{verbs}\s+(?:the\s+|deprecated\s+|support\s+for\s+)*(?:[\w`]+\s+){{0,2}}{sym}", text, re.I)
        or re.search(rf"{sym}\s+(?:is|are|has been|have been|was|were)\s+(?:now\s+)?{verbs}", text, re.I)
    )


def _where(sites: list[UsageSite]) -> str:
    if not sites:
        return "your code"
    first = f"{sites[0].file}:{sites[0].line}"
    return first if len(sites) == 1 else f"{first} and {len(sites) - 1} more place{'s' if len(sites) > 2 else ''}"


def _first_sentence(text: str, limit: int = 240) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text.rstrip() + ("" if text.rstrip().endswith((".", "!", "?", ":")) else ".")
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


# ------------------------------------------------------------------ AI

SYSTEM_PROMPT = """You review dependency upgrades for a developer. You get the release notes between the version they use and the version they are upgrading to, plus the places their code uses the package. Decide which changes actually affect this code, and for each one explain it in plain English and give the exact fix.

Rules:
- Only say a change affects the code when the code shown actually uses the changed API or relies on the changed behavior. When unsure, say so in the explanation and set confidence to "low".
- Explanations are one or two sentences a busy developer can act on. Name the file and line.
- Fixes are concrete: show the replacement code when there is one (a short before/after is ideal), otherwise the smallest safe change. Never invent APIs; if the notes don't name a replacement, say what to check.
- Ignore changes to features this code doesn't use."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "impacts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "change_id": {"type": "integer"},
                    "affects_code": {"type": "boolean"},
                    "severity": {"type": "string", "enum": ["breaking", "deprecation", "security", "behavior", "none"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "locations": {"type": "array", "items": {"type": "string"}},
                    "explanation": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["change_id", "affects_code", "severity", "confidence", "locations", "explanation", "fix"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["impacts"],
    "additionalProperties": False,
}

_SEVERITY_TO_KIND = {
    "breaking": ChangeKind.BREAKING,
    "deprecation": ChangeKind.DEPRECATION,
    "security": ChangeKind.SECURITY,
    "behavior": ChangeKind.OTHER,
}


class AIUnavailable(Exception):
    pass


def ai_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def build_prompt(report: PackageReport, usage_summary: list[UsageSite], root: Path) -> tuple[str, list[ChangeEntry]]:
    """The user message for one package, and the change list indexed by change_id."""
    candidates: list[ChangeEntry] = []
    for imp in report.impacts:
        candidates.append(imp.entry)
    for e in report.entries:
        if e.kind in (ChangeKind.BREAKING, ChangeKind.DEPRECATION, ChangeKind.SECURITY, ChangeKind.OTHER) and e not in candidates:
            candidates.append(e)
    candidates = candidates[:60]

    lines = [
        f"Package: {report.dep.name} ({report.dep.ecosystem.value})",
        f"Upgrade: {report.dep.current} -> {report.target}",
        "",
        "## Changes from the release notes",
    ]
    for i, e in enumerate(candidates):
        lines.append(f"[{i}] ({e.version}, {e.kind.value}{', ' + e.section if e.section else ''}) {e.text}")

    lines += ["", "## Where the code uses this package"]
    snippets = _snippets(root, [s for imp in report.impacts for s in imp.sites] + usage_summary)
    lines += snippets or ["(no direct usage found)"]
    lines += [
        "",
        "For each change that affects this code, return an impact with its change_id. "
        "Leave out changes that don't affect it. Locations are file:line strings from the code above.",
    ]
    return "\n".join(lines), candidates


def _snippets(root: Path, sites: list[UsageSite], context: int = 2, budget: int = 120) -> list[str]:
    out: list[str] = []
    seen: set[tuple[str, int]] = set()
    files: dict[str, list[str]] = {}
    for s in sites:
        if len(seen) >= 25 or budget <= 0:
            break
        if (s.file, s.line) in seen:
            continue
        seen.add((s.file, s.line))
        if s.file not in files:
            try:
                files[s.file] = (root / s.file).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                files[s.file] = []
        src = files[s.file]
        if not src:
            out.append(f"{s.file}:{s.line}: {s.code}")
            continue
        lo, hi = max(1, s.line - context), min(len(src), s.line + context)
        out.append(f"--- {s.file}:{s.line}")
        for n in range(lo, hi + 1):
            out.append(f"{n:>5}{'>' if n == s.line else ' '} {src[n - 1][:200]}")
            budget -= 1
    return out


def ai_explain(report: PackageReport, usage_summary: list[UsageSite], root: Path, model: str | None = None, client=None) -> None:
    """Ask Claude to confirm, extend, and explain the impacts for one package (mutates report)."""
    if not report.entries:
        return
    deterministic(report)  # a baseline the model's answer replaces; kept if the call fails
    if client is None:
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise AIUnavailable("Install the AI extra: pip install 'breakwise[ai]'") from e
        client = anthropic.Anthropic()

    prompt, candidates = build_prompt(report, usage_summary, root)
    response = client.beta.messages.create(
        model=model or os.environ.get("BREAKWISE_MODEL") or DEFAULT_MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise AIUnavailable("Claude declined to review this package")
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(text)
    apply_ai_result(report, data, candidates)


def apply_ai_result(report: PackageReport, data: dict, candidates: list[ChangeEntry]) -> None:
    by_entry = {id(i.entry): i for i in report.impacts}
    all_sites = {f"{s.file}:{s.line}": s for i in report.impacts for s in i.sites}
    kept: list[Impact] = []
    for item in data.get("impacts", []):
        cid = item.get("change_id")
        if not isinstance(cid, int) or not 0 <= cid < len(candidates) or not item.get("affects_code"):
            continue
        entry = candidates[cid]
        imp = by_entry.get(id(entry))
        if imp is None:
            sites = []
            for loc in item.get("locations", []):
                s = all_sites.get(loc)
                if s is None and re.match(r".+:\d+$", loc):
                    f, ln = loc.rsplit(":", 1)
                    s = UsageSite(file=f, line=int(ln), code="", symbol="")
                if s:
                    sites.append(s)
            imp = Impact(entry=entry, sites=sites, matched_symbol=(entry.symbols or [""])[0])
        kind = _SEVERITY_TO_KIND.get(item.get("severity", ""))
        if kind and kind != entry.kind:
            entry.kind = kind
        imp.confidence = item.get("confidence", imp.confidence)
        imp.explanation = item.get("explanation", "").strip()
        imp.fix = item.get("fix", "").strip()
        imp.ai = True
        kept.append(imp)
    from .impact import CONF_RANK
    from .models import KIND_RANK

    kept.sort(key=lambda i: (-KIND_RANK[i.entry.kind], -CONF_RANK.get(i.confidence, 0)))
    report.impacts = kept
