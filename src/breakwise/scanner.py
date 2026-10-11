"""Glue: manifests -> registry -> changelogs -> code usage -> impacts -> explanations."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import changelog, explain, impact, manifests, registry, rules, usage, versions
from .http import Http, HttpError
from .models import Dependency, Ecosystem, PackageReport, ScanReport, UsageSite

LOCKFILES = ["package-lock.json", "yarn.lock", "uv.lock", "poetry.lock", "pdm.lock"]


def runtime_for(root: Path) -> impact.Runtime:
    rt = impact.Runtime()
    py = manifests.requires_python(root)
    if py:
        src = "pyproject.toml" if (root / "pyproject.toml").is_file() else ".python-version"
        rt.python = (py, src)
    node = manifests.node_engine(root)
    if node:
        rt.node = (node, "package.json")
    return rt


def analyze(
    root: Path,
    deps: list[tuple[Dependency, str | None]],
    *,
    http: Http | None = None,
    ai: bool = False,
    model: str | None = None,
    workers: int = 8,
    mode: str = "scan",
    progress: Callable[[str], None] | None = None,
) -> ScanReport:
    """Analyze each (dependency, target_version). target None means "latest stable"."""
    http = http or Http()
    progress = progress or (lambda _msg: None)
    code = usage.source_files(root)
    indexes = usage.analyze(root, [(d.name, d.ecosystem) for d, _ in deps], files=code)
    rt = runtime_for(root)

    def one(item: tuple[Dependency, str | None]) -> PackageReport:
        try:
            return analyze_one(item)
        except Exception as e:  # one odd package must never sink the whole report
            return PackageReport(dep=item[0], status="error", note=f"internal error: {type(e).__name__}: {e}")

    def analyze_one(item: tuple[Dependency, str | None]) -> PackageReport:
        dep, target = item
        rep = PackageReport(dep=dep)
        idx = indexes[(dep.ecosystem, dep.name)]
        rep.imported = idx.imported
        try:
            progress(f"{dep.name}: fetching metadata")
            pi = registry.fetch(dep.name, dep.ecosystem, http)
        except (HttpError, ValueError) as e:
            rep.status, rep.note = "error", f"couldn't fetch package metadata ({e})"
            return rep
        rep.target = target or pi.latest
        if not dep.current:
            rep.status, rep.note = "unknown-version", "not pinned, so Breakwise can't tell which version you use (pin it with == or add a lockfile)"
            return rep
        if not rep.target or versions.same(dep.current, rep.target, dep.ecosystem):
            rep.status = "up-to-date"
            return rep
        cur, tgt = versions.parse(dep.current, dep.ecosystem), versions.parse(rep.target, dep.ecosystem)
        if cur is not None and tgt is not None and cur > tgt:
            rep.status, rep.note = "up-to-date", f"you're ahead of the latest stable release ({rep.target})"
            rep.target = dep.current
            return rep
        rep.releases = versions.releases_between(pi.versions, dep.current, rep.target, dep.ecosystem)
        if not rep.releases:
            rep.releases = [rep.target]
        progress(f"{dep.name}: reading release notes for {len(rep.releases)} release(s)")
        cl = changelog.fetch_entries(pi, rep.releases, http)
        rep.entries, rep.changelog_url, rep.hints = cl.entries, cl.url, cl.hints
        rule_entries = rules.entries_for(dep.name, dep.ecosystem, dep.current, rep.target)
        rep.entries = rep.entries + rule_entries
        if not cl.covered and not rule_entries:
            rep.status = "no-changelog"
            rep.note = "couldn't find release notes" + (f" (looked in github.com/{'/'.join(pi.github)})" if pi.github else "")
        elif cl.missing:
            rep.note = f"no notes found for {', '.join(cl.missing[:5])}{'…' if len(cl.missing) > 5 else ''}"
        rep.impacts = impact.find_impacts(rep.entries, idx, rt)
        if ai and rep.entries and (rep.imported or rep.impacts):
            progress(f"{dep.name}: asking Claude")
            try:
                explain.ai_explain(rep, _usage_summary(idx), root, model=model)
            except Exception as e:  # keep the deterministic result if the API call fails
                explain.deterministic(rep)
                rep.note = (rep.note + "; " if rep.note else "") + f"AI explanation failed: {e}"
        else:
            explain.deterministic(rep)
        return rep

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        reports = list(pool.map(one, deps))
    return ScanReport(root=str(root), packages=reports, mode=mode, ai=ai, code_files=len(code[0]) + len(code[1]))


def _usage_summary(idx: usage.UsageIndex, limit: int = 15) -> list[UsageSite]:
    """A spread of the places the code touches the package, for the AI prompt."""
    out: list[UsageSite] = []
    for q, sites in sorted(idx.resolved.items(), key=lambda kv: -len(kv[1])):
        if "." in q and sites:
            out.append(sites[0])
        if len(out) >= limit:
            break
    return out


def environment_belongs_to(root: Path) -> bool:
    """Is the Python environment running Breakwise the project's own (e.g. root/.venv)?

    Only then do installed package versions say anything about the project. Otherwise they
    describe Breakwise's own environment and would be misleading.
    """
    try:
        sys.prefix and Path(sys.prefix).resolve().relative_to(root.resolve())
        return sys.prefix != sys.base_prefix
    except ValueError:
        return False


def scan(root: Path, files: list[Path] | None = None, only: list[str] | None = None, **kw) -> ScanReport:
    deps = manifests.load(root, files, use_installed=environment_belongs_to(root))
    if only:
        wanted = {o.lower() for o in only}
        deps = [d for d in deps if d.name.lower() in wanted]
    return analyze(root, [(d, None) for d in deps], **kw)


# ------------------------------------------------------------------ PR mode


def changed_dependencies(root: Path, base: str, files: list[Path] | None = None) -> list[tuple[Dependency, str]]:
    """Dependencies whose version differs between git ref `base` and the working tree."""
    head_files = files or manifests.discover(root)
    head = manifests.load(root, head_files, use_installed=False)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        base_files: list[Path] = []
        for f in head_files:
            rel = f.resolve().relative_to(root.resolve())
            content = _git_show(root, base, rel.as_posix())
            if content is None:
                continue
            dest = tmp_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            base_files.append(dest)
            for lock in LOCKFILES:
                lock_rel = (rel.parent / lock).as_posix()
                lock_content = _git_show(root, base, lock_rel)
                if lock_content is not None:
                    (tmp_root / lock_rel).write_text(lock_content, encoding="utf-8")
        before = {(d.ecosystem, d.name): d for d in manifests.load(tmp_root, base_files, use_installed=False)} if base_files else {}
    changes: list[tuple[Dependency, str]] = []
    for d in head:
        old = before.get((d.ecosystem, d.name))
        if old is None or not old.current or not d.current:
            continue
        if not versions.same(old.current, d.current, d.ecosystem):
            changed = Dependency(
                name=d.name, ecosystem=d.ecosystem, spec=d.spec, current=old.current,
                current_source=old.current_source, manifest=d.manifest, line=d.line,
            )
            changes.append((changed, d.current))
    return changes


def _git_show(root: Path, ref: str, path: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "show", f"{ref}:{path}"], cwd=root, capture_output=True, text=True, check=False
        )
    except OSError:
        print("breakwise: git is not available", file=sys.stderr)
        return None
    return out.stdout if out.returncode == 0 else None


__all__ = ["scan", "analyze", "changed_dependencies", "Ecosystem"]
