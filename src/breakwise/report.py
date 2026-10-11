"""Render a ScanReport as terminal text, Markdown (for PR comments) or JSON."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from enum import Enum

from .models import ChangeKind, Impact, PackageReport, ScanReport

COMMENT_MARKER = "<!-- breakwise-report -->"
NO_CODE_NOTE = "No Python or JavaScript files were found here, so this lists what changed without checking your code."

KIND_LABEL = {
    ChangeKind.BREAKING: "Breaking",
    ChangeKind.SECURITY: "Security",
    ChangeKind.DEPRECATION: "Deprecation",
    ChangeKind.FIX: "Fix",
    ChangeKind.FEATURE: "Feature",
    ChangeKind.OTHER: "Behavior change",
}


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def headline(report: ScanReport) -> str:
    updated, mattering, breaking = report.updated, report.mattering, report.breaking
    noun = "change" if report.mode == "pr" else "update"
    if not updated:
        return "Everything is up to date." if report.mode == "scan" else "No dependency versions changed."
    if not mattering:
        return f"{_plural(len(updated), noun)}, none of which touch your code."
    s = f"{_plural(len(mattering), noun)} {'matters' if len(mattering) == 1 else 'matter'}"
    if breaking:
        names = ", ".join(p.dep.name for p in breaking[:3]) + ("…" if len(breaking) > 3 else "")
        s += f", {len(breaking)} {'has a breaking change' if len(breaking) == 1 else 'have breaking changes'} in {names}"
    else:
        s += ", none break your code"
    return s + "."


def verdict(p: PackageReport, code_checked: bool = True) -> tuple[str, str]:
    """(symbol, short status) for one package. code_checked=False: the scan found no code to match against."""
    if p.status == "error":
        return "?", p.note or "error"
    if p.status == "unknown-version":
        return "?", "version unknown"
    if not p.has_update:
        return "·", "up to date"
    if p.breaking_impacts:
        return "✖", "breaks your code"
    if p.security_entries:
        return "!", "security fixes"
    if not code_checked and not p.impacts:
        n = len(p.unmatched_breaking)
        return "?", (f"{_plural(n, 'breaking change')}, no code to check" if n else "no code to check against")
    if any(i.entry.kind == ChangeKind.DEPRECATION and i.counts for i in p.impacts):
        return "⚠", "deprecations in code you use"
    if p.counted_impacts:
        return "⚠", "behavior changes in code you use"
    if p.status == "no-changelog":
        return "?", "no release notes found"
    if p.runtime_impacts:
        return "⚠", "drops a runtime version you support"
    if p.unmatched_breaking:
        return "✔", f"no impact found ({_plural(len(p.unmatched_breaking), 'breaking change')} elsewhere)"
    if not p.imported:
        return "✔", "not imported by your code"
    return "✔", "no impact found"


def _order(p: PackageReport, code_checked: bool = True) -> tuple:
    sym = verdict(p, code_checked)[0]
    return ({"✖": 0, "!": 1, "⚠": 2, "?": 3, "✔": 4, "·": 5}.get(sym, 9), p.dep.name)


# ------------------------------------------------------------------ text


class _Style:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text


def render_text(report: ScanReport, color: bool | None = None, verbose: bool = False) -> str:
    if color is None:
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    st = _Style(color)
    colors = {"✖": "31;1", "!": "35;1", "⚠": "33;1", "✔": "32", "·": "2", "?": "2"}
    out: list[str] = []
    out.append(st(headline(report), "1"))
    out.append(st(f"{_plural(len(report.packages), 'dependency', 'dependencies')} checked, {len(report.updated)} with updates"
                  + (" · explanations by Claude" if report.ai else ""), "2"))
    checked = report.code_checked
    if not checked:
        out.append(st(NO_CODE_NOTE, "33"))
    out.append("")
    for p in sorted(report.packages, key=lambda p: _order(p, checked)):
        sym, status = verdict(p, checked)
        if sym == "·" and not verbose:
            continue
        arrow = f"{p.dep.current} → {p.target}" if p.has_update else (p.dep.current or "")
        rel = f"  ({_plural(len(p.releases), 'release')})" if len(p.releases) > 1 else ""
        out.append(f"{st(sym, colors.get(sym, '0'))} {st(p.dep.name, '1')} {arrow}{rel}  {st(status, colors.get(sym, '0'))}")
        shown = [i for i in p.impacts if (i.counts or verbose) and not i.is_runtime]
        for imp in shown:
            out += _text_impact(imp, st)
        out += [f"    {st(line, '35')}" for line in _security_lines(p, verbose)]
        for imp in p.runtime_impacts:
            out.append(st(f"    Runtime: {_runtime_note(imp)}", "2"))
        if (verbose or not checked) and p.unmatched_breaking:
            out.append(st("    Breaking changes in these releases:" if not checked else "    Other breaking changes (not found in your code):", "2"))
            for e in p.unmatched_breaking[:10]:
                out.append(st(f"      - {e.version}: {_trim(e.text, 160)}", "2"))
        if p.note and (verbose or sym in "?"):
            out.append(st(f"    note: {p.note}", "2"))
        if p.changelog_url and (shown or verbose):
            out.append(st(f"    notes: {p.changelog_url}", "2"))
    return "\n".join(out).rstrip() + "\n"


def _runtime_note(imp: Impact) -> str:
    label, _, dropped = imp.matched_symbol.lstrip("@").partition(":")
    label = "Python" if label == "python" else "Node.js"
    site = imp.sites[0] if imp.sites else None
    where = f" ({site.file} says {site.code.replace('supports ', '')})" if site else ""
    return f"{imp.entry.version} drops {label} {dropped}, which your project still supports{where}"


def _severity(text: str) -> int:
    t = text.lower()
    return 3 if "critical" in t else 2 if "high" in t else 1 if ("cve-" in t or "ghsa-" in t) else 0


def security_highlights(p: PackageReport, limit: int = 3) -> tuple[list, int]:
    """The most severe security entries (and how many there are in total)."""
    entries = [e for e in p.security_entries if len(e.text) > 40]
    entries.sort(key=lambda e: -_severity(e.text))
    return entries[:limit], len(p.security_entries)


def security_span(p: PackageReport) -> str:
    """The releases that carry security fixes, e.g. "2.5.0–2.8.0"."""
    span = sorted({e.version for e in p.security_entries}, key=lambda v: [int(x) if x.isdigit() else 0 for x in v.split(".")])
    if not span:
        return ""
    return span[0] if len(span) == 1 else f"{span[0]}–{span[-1]}"


def _security_lines(p: PackageReport, verbose: bool) -> list[str]:
    top, total = security_highlights(p, limit=100 if verbose else 3)
    if not total:
        return []
    lines = [f"Security: {_plural(total, 'fix', 'fixes')} in {security_span(p)}" + ("" if p.imported else " (package isn't imported directly)")]
    lines += [f"  - {e.version}: {_trim(e.text, 160)}" for e in top]
    if total > len(top):
        lines.append(f"  …and {total - len(top)} more in the release notes")
    return lines


def _text_impact(imp: Impact, st: _Style) -> list[str]:
    e = imp.entry
    label = KIND_LABEL[e.kind] + ("" if imp.confidence != "low" else " (possible)")
    lines = [f"    {st(label, '1')} · {e.version} · {imp.matched_symbol.lstrip('@')}"]
    lines.append(f"      {_trim(imp.explanation or e.text, 400)}")
    for s in imp.sites[:3]:
        lines.append(st(f"      {s.file}:{s.line}  {s.code}", "36"))
    if len(imp.sites) > 3:
        lines.append(st(f"      …and {len(imp.sites) - 3} more", "2"))
    if imp.fix:
        fix_lines = imp.fix.splitlines()
        lines.append(f"      {st('Fix:', '32;1')} {fix_lines[0]}")
        lines += [f"           {l}" for l in fix_lines[1:]]
    return lines


def _trim(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------ markdown


def render_markdown(report: ScanReport, repo_url: str | None = None, sha: str | None = None) -> str:
    out = [COMMENT_MARKER, f"### Breakwise: {headline(report)}", ""]
    checked = report.code_checked
    if not checked:
        out += [f"> {NO_CODE_NOTE}", ""]
    rows = [p for p in sorted(report.packages, key=lambda p: _order(p, checked)) if p.has_update or p.status in ("error", "unknown-version")]
    if rows:
        out += ["| | Package | Version | What it means for your code |", "|---|---|---|---|"]
        for p in rows:
            sym, status = verdict(p, checked)
            arrow = f"`{p.dep.current}` → `{p.target}`" if p.has_update else f"`{p.dep.current or '?'}`"
            icon = {"✖": "🔴", "!": "🟣", "⚠": "🟡", "✔": "🟢", "?": "⚪", "·": "⚪"}.get(sym, "")
            name = f"[{p.dep.name}]({p.changelog_url})" if p.changelog_url else p.dep.name
            out.append(f"| {icon} | {name} | {arrow} | {status} |")
        out.append("")

    for p in sorted(report.packages, key=lambda p: _order(p, checked)):
        shown = [i for i in p.impacts if i.counts]
        sec_top, sec_total = security_highlights(p)
        if not p.has_update or not (shown or sec_total or p.runtime_impacts):
            continue
        sym, status = verdict(p, checked)
        out.append(f"<details{' open' if sym == '✖' else ''}>")
        out.append(f"<summary><b>{p.dep.name}</b> {p.dep.current} → {p.target}: {status}</summary>")
        out.append("")
        for imp in shown:
            e = imp.entry
            out.append(f"**{KIND_LABEL[e.kind]}** in {e.version}{' (possible)' if imp.confidence == 'low' else ''}: {_md_escape(_trim(imp.explanation or e.text, 600))}")
            out.append("")
            for s in imp.sites[:5]:
                loc = f"{s.file}#L{s.line}"
                link = f"[`{s.file}:{s.line}`]({repo_url}/blob/{sha}/{loc})" if repo_url and sha else f"`{s.file}:{s.line}`"
                out.append(f"- {link} `{s.code.replace('`', 'ʼ')}`")
            if len(imp.sites) > 5:
                out.append(f"- …and {len(imp.sites) - 5} more")
            if imp.fix:
                out.append("")
                out.append(f"**Fix:** {imp.fix}")
            out.append("")
        if sec_total:
            out.append(f"**Security:** {_plural(sec_total, 'fix', 'fixes')} in this upgrade. Most severe:")
            out += [f"- {e.version}: {_md_escape(_trim(e.text, 300))}" for e in sec_top]
            out.append("")
        for imp in p.runtime_impacts:
            out.append(f"**Runtime:** {_runtime_note(imp)}.")
            out.append("")
        out.append("</details>")
        out.append("")

    unmatched = sum(len(p.unmatched_breaking) for p in report.updated)
    foot = []
    if unmatched:
        foot.append(f"{_plural(unmatched, 'other breaking change')} in these releases didn't match your code")
    foot.append("explanations by Claude" if report.ai else "static analysis only; add an `anthropic-api-key` for written explanations")
    out.append(f"<sub>{' · '.join(foot)} · [Breakwise](https://github.com/RoboDevelopX/breakwise)</sub>")
    return "\n".join(out).rstrip() + "\n"


def _md_escape(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ json


def _default(o):
    if isinstance(o, Enum):
        return o.value
    raise TypeError(type(o))


def render_json(report: ScanReport) -> str:
    data = asdict(report)
    data["headline"] = headline(report)
    for p_data, p in zip(data["packages"], report.packages):
        p_data["verdict"] = verdict(p, report.code_checked)[1]
        p_data["has_update"] = p.has_update
        p_data["matters"] = p.matters
    return json.dumps(data, indent=2, default=_default)
