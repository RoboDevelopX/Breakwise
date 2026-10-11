"""Match changelog entries against the user's code."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import KIND_RANK, ChangeEntry, ChangeKind, Impact, UsageSite
from .usage import COMMON_NAMES, UsageIndex

CONF_RANK = {"high": 3, "medium": 2, "low": 1}
# Receivers that changelogs use as stand-ins for "an instance of", e.g. `res.send()`.
RECEIVERS = {"res", "req", "app", "self", "client", "response", "request", "router", "ctx", "session", "instance", "obj"}
MAX_SITES = 8


@dataclass
class Runtime:
    """The oldest Python / Node version the project says it supports, and where it says so."""

    python: tuple[str, str] | None = None  # (version, file)
    node: tuple[str, str] | None = None


def distinctive(name: str) -> bool:
    if name.lower() in COMMON_NAMES or len(name) < 3:
        return False
    return (
        len(name) >= 6
        or "_" in name
        or bool(re.search(r"[a-z][A-Z]", name))  # camelCase
        or bool(re.fullmatch(r"[A-Z][a-z]+(?:[A-Z][a-z0-9]*)*", name) and len(name) >= 4)  # PascalCase
        or (name.isupper() and len(name) >= 4)
    )


def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def _ends_with(seq: list[str], tail: list[str]) -> bool:
    return bool(tail) and len(seq) >= len(tail) and seq[-len(tail):] == tail


def match_entry(entry: ChangeEntry, idx: UsageIndex, runtime: Runtime | None = None, hubs: set[str] | None = None) -> Impact | None:
    """Find where the user's code uses what `entry` changes.

    `hubs` are symbols mentioned by many entries in the same upgrade (a library's main class,
    like pydantic's BaseModel). Touching one of those is weak evidence on its own.
    """
    if entry.kind == ChangeKind.FEATURE:
        return None
    hubs = hubs or set()
    roots = {n.split(".")[0] for n in idx.import_names}
    best: tuple[tuple[int, int, int], str, str, list[UsageSite]] | None = None  # (key, confidence, symbol, sites)

    def consider(conf: str, symbol: str, sites: list[UsageSite], depth: int = 0):
        """depth: how deep the matched usage chain is; deeper (a keyword argument or a method)
        beats shallower (just importing the class) when confidence ties."""
        nonlocal best
        if not sites:
            return
        if symbol in hubs and "." not in symbol and conf != "low":
            conf = "low" if conf == "medium" else "medium"
        key = (CONF_RANK[conf], depth, len(sites))
        if best is None or key > best[0]:
            best = (key, conf, symbol, sites)

    for sym in entry.symbols:
        if sym.startswith("@"):
            runtime_hit = _match_runtime(sym, runtime)
            if runtime_hit:
                consider("high", sym, [runtime_hit])
            continue
        if sym in idx.import_names or sym in roots:
            continue  # "urllib3" alone says nothing about which part of urllib3
        sp = sym.split(".")
        qualified = sp[0] in roots
        tail = sp[1:] if qualified else sp

        # 1) package-qualified usage (followed through imports and assignments)
        for q, sites in idx.resolved.items():
            qp = q.split(".")
            d = len(qp)
            if qualified and len(sp) >= 2 and (q == sym or q.startswith(sym + ".")):
                consider("high", sym, sites, d)
            elif len(tail) >= 2 and _ends_with(qp, tail):
                consider("high", sym, sites, d)
            elif len(tail) == 1 and len(qp) >= 2 and qp[-1] == tail[0]:
                if distinctive(tail[0]):
                    consider("high", sym, sites, d)
                elif len(qp) == 2:
                    consider("low", sym, sites, d)  # common name called on the module, e.g. flask.redirect
            elif len(tail) >= 2 and (tail[0][:1].isupper() or tail[0] in RECEIVERS):
                rest = tail[1:]
                if _ends_with(qp, rest) and (len(rest) >= 2 or distinctive(rest[-1])):
                    consider("medium", sym, sites, d)

        # 2) chains in files that import the package, but that we couldn't tie to it
        if best and best[0][0] >= CONF_RANK["medium"]:
            continue
        for chain, sites in idx.loose.items():
            cp = chain.split(".")
            if len(tail) >= 2 and _ends_with(cp, tail):
                consider("medium" if tail[0] in RECEIVERS or len(tail) >= 3 else "low", sym, sites)
            elif len(tail) >= 2 and (tail[0][:1].isupper() or tail[0] in RECEIVERS) and len(tail[1:]) >= 2 and _ends_with(cp, tail[1:]):
                consider("low", sym, sites)
            elif len(tail) == 1 and cp[-1] == tail[0] and distinctive(tail[0]) and len(tail[0]) >= 8:
                consider("low", sym, sites)

    if best is None:
        return None
    _, conf, symbol, sites = best
    # Matching only the class an entry talks about ("``Retry``") while the entry is really about
    # one of its options ("``allowed_methods``") is weaker evidence.
    container = "." not in symbol and symbol[:1].isupper() and not symbol.startswith("@")
    specific = [s for s in entry.symbols if s != symbol and not s.startswith("@") and s not in roots
                and s.split(".")[-1].lower() not in COMMON_NAMES]
    if container and specific:
        conf = {"high": "medium", "medium": "low"}.get(conf, conf)
    return Impact(entry=entry, sites=_dedupe(sites), matched_symbol=symbol, confidence=conf)


def _match_runtime(sym: str, runtime: Runtime | None) -> UsageSite | None:
    if runtime is None:
        return None
    kind, _, dropped = sym[1:].partition(":")
    have = runtime.python if kind == "python" else runtime.node
    if not have:
        return None
    version, file = have
    if kind == "node":
        affected = _vtuple(version)[:1] <= _vtuple(dropped)[:1]
    else:
        affected = _vtuple(version)[:2] <= _vtuple(dropped)[:2]
    if not affected:
        return None
    label = "Python" if kind == "python" else "Node.js"
    return UsageSite(file=file, line=1, code=f"supports {label} {version}", symbol=f"{label} {version}")


def _dedupe(sites: list[UsageSite]) -> list[UsageSite]:
    seen: dict[tuple[str, int], UsageSite] = {}
    for s in sites:
        seen.setdefault((s.file, s.line), s)
    return sorted(seen.values(), key=lambda s: (s.file, s.line))[:MAX_SITES]


def hub_symbols(entries: list[ChangeEntry], threshold: int = 4) -> set[str]:
    counts: dict[str, int] = {}
    for e in entries:
        for sym in set(e.symbols):
            counts[sym] = counts.get(sym, 0) + 1
    return {s for s, n in counts.items() if n >= threshold}


def find_impacts(entries: list[ChangeEntry], idx: UsageIndex, runtime: Runtime | None = None) -> list[Impact]:
    hubs = hub_symbols(entries)
    impacts = [i for i in (match_entry(e, idx, runtime, hubs) for e in entries) if i]
    # Several releases may each drop an old runtime; keep only the newest drop per runtime.
    newest: dict[str, Impact] = {}
    for i in impacts:
        if i.is_runtime:
            kind = i.matched_symbol.split(":")[0]
            if kind not in newest or _vtuple(i.matched_symbol) > _vtuple(newest[kind].matched_symbol):
                newest[kind] = i
    impacts = [i for i in impacts if not i.is_runtime or newest.get(i.matched_symbol.split(":")[0]) is i]

    impacts.sort(key=lambda i: (-KIND_RANK[i.entry.kind], -CONF_RANK[i.confidence]))
    return impacts
