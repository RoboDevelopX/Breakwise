"""Find and parse dependency manifests: requirements.txt, pyproject.toml, package.json."""

from __future__ import annotations

import json
import re
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from . import versions
from .models import Dependency, Ecosystem

IGNORED_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", ".env", "__pycache__", ".tox", ".nox",
    "site-packages", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next",
    "coverage", ".idea", ".vscode", "vendor", "third_party",
}

REQUIREMENTS_RE = re.compile(r"^(requirements|reqs)([-_.\w]*)\.(txt|in)$", re.IGNORECASE)


def discover(root: Path, max_depth: int = 3) -> list[Path]:
    """Dependency manifests under root, shallowest first."""
    found: list[Path] = []

    def walk(d: Path, depth: int):
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for p in entries:
            if p.is_dir():
                if depth < max_depth and p.name not in IGNORED_DIRS and not p.name.startswith("."):
                    walk(p, depth + 1)
            elif p.name == "package.json" or p.name == "pyproject.toml" or REQUIREMENTS_RE.match(p.name):
                found.append(p)

    walk(root, 0)
    found.sort(key=lambda p: (len(p.relative_to(root).parts), str(p)))
    return found


def parse_file(path: Path, root: Path | None = None, use_installed: bool = True) -> list[Dependency]:
    root = root or path.parent
    text = path.read_text(encoding="utf-8", errors="replace")
    rel = _rel(path, root)
    if path.name == "package.json":
        return parse_package_json(text, rel, lock=_npm_lock_versions(path.parent))
    if path.name == "pyproject.toml":
        return parse_pyproject(text, rel, lock=_python_lock_versions(path.parent), use_installed=use_installed)
    return parse_requirements(text, rel, base_dir=path.parent, use_installed=use_installed)


def load(root: Path, files: list[Path] | None = None, use_installed: bool = True) -> list[Dependency]:
    """Parse every manifest and merge duplicates, keeping the most precise version."""
    files = files or discover(root)
    merged: dict[tuple[Ecosystem, str], Dependency] = {}
    rank = {"pinned": 4, "lockfile": 3, "installed": 2, "lower-bound": 1, "": 0}
    for f in files:
        for dep in parse_file(f, root, use_installed=use_installed):
            key = (dep.ecosystem, dep.name)
            old = merged.get(key)
            if old is None or rank[dep.current_source] > rank[old.current_source]:
                merged[key] = dep
    return sorted(merged.values(), key=lambda d: (d.ecosystem.value, d.name))


# ---------------------------------------------------------------- Python


def parse_requirements(
    text: str,
    manifest: str = "requirements.txt",
    base_dir: Path | None = None,
    use_installed: bool = True,
    _seen: set[Path] | None = None,
) -> list[Dependency]:
    deps: list[Dependency] = []
    _seen = _seen if _seen is not None else set()
    for lineno, line in _logical_lines(text):
        line = re.sub(r"(^|\s)#.*$", "", line).strip()
        if not line:
            continue
        if line.startswith(("-r", "--requirement", "-c", "--constraint")):
            target = line.split(None, 1)[1] if " " in line else line[2:]
            if base_dir is not None:
                inc = (base_dir / target.strip()).resolve()
                if inc.is_file() and inc not in _seen:
                    _seen.add(inc)
                    deps += parse_requirements(
                        inc.read_text(encoding="utf-8", errors="replace"),
                        inc.name,
                        inc.parent,
                        use_installed,
                        _seen,
                    )
            continue
        if line.startswith("-"):  # -e, --index-url, --hash-only lines, etc.
            continue
        line = re.sub(r"\s--hash[=\s]\S+", "", line)
        dep = _python_dep(line, manifest, lineno, {}, use_installed)
        if dep:
            deps.append(dep)
    return deps


def parse_pyproject(
    text: str, manifest: str = "pyproject.toml", lock: dict[str, str] | None = None, use_installed: bool = True
) -> list[Dependency]:
    lock = lock or {}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    specs: list[str] = []
    project = data.get("project", {})
    specs += project.get("dependencies", []) or []
    for group in (project.get("optional-dependencies") or {}).values():
        specs += group
    for group in (data.get("dependency-groups") or {}).values():
        specs += [s for s in group if isinstance(s, str)]
    deps = [d for d in (_python_dep(s, manifest, None, lock, use_installed) for s in specs) if d]

    poetry = data.get("tool", {}).get("poetry", {})
    poetry_tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
    for g in (poetry.get("group") or {}).values():
        poetry_tables.append(g.get("dependencies", {}))
    for table in poetry_tables:
        for name, spec in table.items():
            if name.lower() == "python":
                continue
            if isinstance(spec, dict):
                spec = spec.get("version", "")
            if not isinstance(spec, str):
                continue
            deps.append(_poetry_dep(name, spec, manifest, lock, use_installed))
    return deps


def requires_python(root: Path) -> str | None:
    """The oldest Python the project supports, if it says so anywhere."""
    pp = root / "pyproject.toml"
    if pp.is_file():
        try:
            data = tomllib.loads(pp.read_text(encoding="utf-8"))
            spec = data.get("project", {}).get("requires-python") or data.get("tool", {}).get("poetry", {}).get(
                "dependencies", {}
            ).get("python")
            if isinstance(spec, str):
                m = re.search(r"(\d+\.\d+)", spec)
                if m:
                    return m.group(1)
        except (tomllib.TOMLDecodeError, OSError):
            pass
    pv = root / ".python-version"
    if pv.is_file():
        m = re.search(r"(\d+\.\d+)", pv.read_text())
        if m:
            return m.group(1)
    return None


def _python_dep(spec: str, manifest: str, line: int | None, lock: dict[str, str], use_installed: bool):
    spec = spec.strip()
    if not spec or "://" in spec.split(";")[0] and "@" not in spec:
        return None
    try:
        req = Requirement(spec)
    except InvalidRequirement:
        return None
    if req.url:
        return None
    name = canonicalize_name(req.name)
    dep = Dependency(name=name, ecosystem=Ecosystem.PYPI, spec=str(req.specifier), manifest=manifest, line=line)
    pins = [s for s in req.specifier if s.operator in ("==", "===") and "*" not in s.version]
    lower = [s for s in req.specifier if s.operator in (">=", "~=", ">") and "*" not in s.version]
    if pins:
        dep.current, dep.current_source = pins[0].version, "pinned"
    elif name in lock:
        dep.current, dep.current_source = lock[name], "lockfile"
    elif use_installed and (inst := _installed(name)):
        dep.current, dep.current_source = inst, "installed"
    elif lower:
        dep.current, dep.current_source = lower[0].version, "lower-bound"
    return dep


def _poetry_dep(name: str, spec: str, manifest: str, lock: dict[str, str], use_installed: bool) -> Dependency:
    cname = canonicalize_name(name)
    dep = Dependency(name=cname, ecosystem=Ecosystem.PYPI, spec=spec, manifest=manifest)
    if re.fullmatch(r"=?=?\s*\d[\w.]*", spec.strip()):
        dep.current, dep.current_source = spec.strip().lstrip("="), "pinned"
    elif cname in lock:
        dep.current, dep.current_source = lock[cname], "lockfile"
    elif use_installed and (inst := _installed(cname)):
        dep.current, dep.current_source = inst, "installed"
    elif floor := versions.npm_range_floor(spec):  # poetry's ^ and ~ follow npm's rules
        dep.current, dep.current_source = floor, "lower-bound"
    return dep


def _python_lock_versions(d: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in ("uv.lock", "poetry.lock", "pdm.lock"):
        p = d / name
        if p.is_file():
            try:
                data = tomllib.loads(p.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, OSError):
                continue
            for pkg in data.get("package", []):
                if "name" in pkg and "version" in pkg:
                    out.setdefault(canonicalize_name(pkg["name"]), str(pkg["version"]))
    return out


def _installed(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _logical_lines(text: str):
    """Join backslash continuations, yielding (first_line_number, text)."""
    buf, start = "", None
    for i, raw in enumerate(text.splitlines(), 1):
        if start is None:
            start = i
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        yield start, buf + raw
        buf, start = "", None
    if buf:
        yield start, buf


# ---------------------------------------------------------------- npm


def parse_package_json(text: str, manifest: str = "package.json", lock: dict[str, str] | None = None):
    lock = lock or {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    deps: list[Dependency] = []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (data.get(section) or {}).items():
            if not isinstance(spec, str):
                continue
            if spec.startswith("npm:"):  # alias: "foo": "npm:bar@^1"
                continue
            dep = Dependency(name=name, ecosystem=Ecosystem.NPM, spec=spec, manifest=manifest)
            if name in lock:
                dep.current, dep.current_source = lock[name], "lockfile"
            elif re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[\w.]+)?", spec.strip()):
                dep.current, dep.current_source = spec.strip().lstrip("v"), "pinned"
            elif floor := versions.npm_range_floor(spec):
                dep.current, dep.current_source = floor, "lower-bound"
            deps.append(dep)
    return deps


def node_engine(root: Path) -> str | None:
    pj = root / "package.json"
    if pj.is_file():
        try:
            spec = (json.loads(pj.read_text(encoding="utf-8")).get("engines") or {}).get("node")
        except (json.JSONDecodeError, OSError):
            return None
        if spec:
            m = re.search(r"(\d+)", spec)
            return m.group(1) if m else None
    return None


def _npm_lock_versions(d: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    pl = d / "package-lock.json"
    if pl.is_file():
        try:
            data = json.loads(pl.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        for key, info in (data.get("packages") or {}).items():
            if key.startswith("node_modules/") and key.count("node_modules/") == 1 and "version" in info:
                out[key[len("node_modules/"):]] = info["version"]
        for name, info in (data.get("dependencies") or {}).items():
            if isinstance(info, dict) and "version" in info:
                out.setdefault(name, info["version"])
    yl = d / "yarn.lock"
    if yl.is_file():
        text = yl.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'^"?(@?[^@\s"]+)@[^\n]*:\n(?:\s+.*\n)*?\s+version:?\s+"?([^"\s]+)"?', text, re.M):
            out.setdefault(m.group(1), m.group(2))
    return out


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()  # forward slashes on Windows too
    except ValueError:
        return path.as_posix()
