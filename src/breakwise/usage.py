"""Static analysis: where does the user's code touch each dependency?

Python files are parsed with the `ast` module; JavaScript/TypeScript files with careful
regular expressions (no Node toolchain required). The result is a UsageIndex per package
that records every place the package's API is referenced, as dotted chains such as
``urllib3.util.Retry.method_whitelist`` (a keyword argument) or ``res.send``.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from .manifests import IGNORED_DIRS
from .models import Ecosystem, UsageSite

PY_EXT = {".py", ".pyi"}
JS_EXT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte"}
MAX_FILE_BYTES = 1_000_000

# Distributions whose import name differs from the PyPI name.
KNOWN_IMPORT_NAMES = {
    "pyyaml": ["yaml"], "beautifulsoup4": ["bs4"], "pillow": ["PIL"], "scikit-learn": ["sklearn"],
    "scikit-image": ["skimage"], "python-dateutil": ["dateutil"], "opencv-python": ["cv2"],
    "opencv-python-headless": ["cv2"], "opencv-contrib-python": ["cv2"], "protobuf": ["google.protobuf"],
    "attrs": ["attr", "attrs"], "pyjwt": ["jwt"], "python-dotenv": ["dotenv"], "psycopg2-binary": ["psycopg2"],
    "psycopg-binary": ["psycopg"], "typing-extensions": ["typing_extensions"], "discord-py": ["discord"],
    "python-multipart": ["multipart", "python_multipart"], "pycryptodome": ["Crypto"], "pycryptodomex": ["Cryptodome"],
    "pyopenssl": ["OpenSSL"], "setuptools": ["setuptools", "pkg_resources"], "websocket-client": ["websocket"],
    "python-jose": ["jose"], "pyserial": ["serial"], "pyzmq": ["zmq"], "faiss-cpu": ["faiss"], "faiss-gpu": ["faiss"],
    "google-cloud-storage": ["google.cloud.storage"], "google-cloud-bigquery": ["google.cloud.bigquery"],
    "google-api-python-client": ["googleapiclient"], "google-auth": ["google.auth", "google.oauth2"],
    "msgpack-python": ["msgpack"], "python-magic": ["magic"], "ruamel-yaml": ["ruamel.yaml"], "mysqlclient": ["MySQLdb"],
    "pymupdf": ["fitz", "pymupdf"], "python-slugify": ["slugify"], "djangorestframework": ["rest_framework"],
    "django-cors-headers": ["corsheaders"], "sentry-sdk": ["sentry_sdk"], "pytest-asyncio": ["pytest_asyncio"],
    "markdown": ["markdown"], "jinja2": ["jinja2"], "markupsafe": ["markupsafe"], "werkzeug": ["werkzeug"],
    "grpcio": ["grpc"], "tensorflow-cpu": ["tensorflow"], "torch": ["torch"], "pytorch-lightning": ["pytorch_lightning"],
    "lxml-html-clean": ["lxml_html_clean"], "importlib-metadata": ["importlib_metadata"], "pywin32": ["win32api", "win32con"],
}

# Single names too common to treat as evidence on their own.
COMMON_NAMES = {
    "get", "set", "put", "post", "delete", "head", "patch", "update", "items", "keys", "values", "append", "extend",
    "json", "text", "data", "name", "id", "type", "value", "result", "error", "errors", "message", "status", "code",
    "run", "start", "stop", "close", "open", "read", "write", "send", "call", "init", "create", "load", "dump",
    "dumps", "loads", "parse", "format", "config", "options", "request", "response", "session", "headers", "body",
    "url", "path", "file", "files", "args", "kwargs", "default", "params", "query", "method", "methods", "model",
    "models", "field", "fields", "test", "app", "client", "server", "context", "content", "length", "size", "timeout",
    "user", "users", "info", "debug", "warning", "log", "logger", "copy", "index", "count", "find", "map", "filter",
    "then", "catch", "next", "use", "render", "render_template", "redirect", "route", "router", "handler", "event",
    "emit", "listen", "connect", "key", "port", "host", "time", "date", "list", "dict", "str", "int", "len",
}


@dataclass
class UsageIndex:
    package: str
    import_names: list[str]
    files: list[str] = field(default_factory=list)  # files that import the package
    resolved: dict[str, list[UsageSite]] = field(default_factory=lambda: defaultdict(list))  # pkg-qualified chains
    loose: dict[str, list[UsageSite]] = field(default_factory=lambda: defaultdict(list))  # chains/names in importing files

    @property
    def imported(self) -> bool:
        return bool(self.files)


def import_names(dist: str, ecosystem: Ecosystem) -> list[str]:
    if ecosystem == Ecosystem.NPM:
        return [dist]
    key = dist.lower().replace("_", "-").replace(".", "-")
    names = list(KNOWN_IMPORT_NAMES.get(key, []))
    try:
        for mod, dists in metadata.packages_distributions().items():
            if any(d.lower().replace("_", "-").replace(".", "-") == key for d in dists) and not mod.startswith("_"):
                names.append(mod)
    except Exception:  # pragma: no cover - metadata can be odd in some environments
        pass
    names.append(key.replace("-", "_"))
    if key.startswith("python-"):
        names.append(key[len("python-"):].replace("-", "_"))
    if key.startswith("py") and len(key) > 4:
        names.append(key[2:].replace("-", "_"))
    if key.endswith("-python"):
        names.append(key[: -len("-python")].replace("-", "_"))
    return list(dict.fromkeys(names))


def source_files(root: Path) -> tuple[list[Path], list[Path]]:
    py, js = [], []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in IGNORED_DIRS and not p.name.startswith(".") and not (p / "pyvenv.cfg").exists():
                    stack.append(p)
            elif p.suffix in PY_EXT:
                py.append(p)
            elif p.suffix in JS_EXT and not p.name.endswith((".min.js", ".d.ts")):
                js.append(p)
    return sorted(py), sorted(js)


def analyze(
    root: Path, packages: list[tuple[str, Ecosystem]], files: tuple[list[Path], list[Path]] | None = None
) -> dict[tuple[Ecosystem, str], UsageIndex]:
    """Build a UsageIndex for every requested package by scanning the project once."""
    py_files, js_files = files or source_files(root)
    indexes: dict[tuple[Ecosystem, str], UsageIndex] = {}
    py_by_module: dict[str, list[UsageIndex]] = defaultdict(list)
    js_by_module: dict[str, UsageIndex] = {}
    for name, eco in packages:
        idx = UsageIndex(package=name, import_names=import_names(name, eco))
        indexes[(eco, name)] = idx
        for mod in idx.import_names:
            if eco == Ecosystem.NPM:
                js_by_module[mod] = idx
            else:
                py_by_module[mod].append(idx)

    if py_by_module:
        for f in py_files:
            _scan_python(f, root, py_by_module)
    if js_by_module:
        for f in js_files:
            _scan_js(f, root, js_by_module)
    return indexes


# ------------------------------------------------------------------ Python


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _match_module(mod: str, by_module: dict[str, list[UsageIndex]]) -> tuple[str, list[UsageIndex]] | None:
    parts = mod.split(".")
    for i in range(len(parts), 0, -1):
        head = ".".join(parts[:i])
        if head in by_module:
            return head, by_module[head]
    return None


def _scan_python(path: Path, root: Path, by_module: dict[str, list[UsageIndex]]) -> None:
    src = _read(path)
    if src is None:
        return
    try:
        tree = ast.parse(src, filename=str(path))
    except (SyntaxError, ValueError):
        return
    lines = src.splitlines()
    rel = _rel(path, root)

    aliases: dict[str, str] = {}  # local name -> qualified dotted path
    owners: dict[str, list[UsageIndex]] = {}  # qualified root -> package indexes

    def site(node: ast.AST, symbol: str) -> UsageSite:
        ln = getattr(node, "lineno", 1)
        code = lines[ln - 1].strip() if 0 < ln <= len(lines) else ""
        return UsageSite(file=rel, line=ln, code=code[:160], symbol=symbol)

    importing: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                hit = _match_module(a.name, by_module)
                if hit:
                    local = a.asname or a.name.split(".")[0]
                    aliases[local] = a.name if a.asname else a.name.split(".")[0]
                    owners[aliases[local].split(".")[0]] = hit[1]
                    for idx in hit[1]:
                        importing.add(id(idx))
                        idx.resolved[a.name].append(site(node, a.name))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            hit = _match_module(node.module, by_module)
            if hit:
                for a in node.names:
                    qual = f"{node.module}.{a.name}"
                    if a.name != "*":
                        aliases[a.asname or a.name] = qual
                    owners[node.module.split(".")[0]] = hit[1]
                    for idx in hit[1]:
                        importing.add(id(idx))
                        idx.resolved[qual].append(site(node, qual))

    if not importing:
        return
    touched = [idx for idxs in by_module.values() for idx in idxs if id(idx) in importing]
    for idx in {id(i): i for i in touched}.values():
        idx.files.append(rel)

    def chain_of(node: ast.AST) -> list[str] | None:
        """a.b.c -> ["a","b","c"]; f().x -> ["f()", "x"] style roots are flattened."""
        parts: list[str] = []
        while True:
            if isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            elif isinstance(node, ast.Call):
                node = node.func
            elif isinstance(node, ast.Subscript):
                node = node.value
            elif isinstance(node, ast.Name):
                parts.append(node.id)
                return list(reversed(parts))
            else:
                return None

    def resolve(parts: list[str]) -> str | None:
        if parts and parts[0] in aliases:
            return ".".join([aliases[parts[0]]] + parts[1:])
        return None

    # Pass 0: classes that subclass something from the package, e.g. `class User(BaseModel)`.
    # The class name then stands for the base, and every method or attribute the class body
    # defines is recorded as an override, e.g. `pydantic.BaseModel.Config.orm_mode`.
    subclass_defs: list[tuple[ast.ClassDef, str]] = []
    for _ in range(3):
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name not in aliases:
                for base in node.bases:
                    c = chain_of(base)
                    q = resolve(c) if c else None
                    if q:
                        aliases[node.name] = q
                        subclass_defs.append((node, q))
                        break

    def record_body(body: list[ast.stmt], prefix: str) -> None:
        for stmt in body:
            names: list[str] = []
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [stmt.name]
            elif isinstance(stmt, ast.Assign):
                names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                names = [stmt.target.id]
            elif isinstance(stmt, ast.ClassDef):
                record_body(stmt.body, f"{prefix}.{stmt.name}")
                names = [stmt.name]
            for n in names:
                q = f"{prefix}.{n}"
                for idx in _owners_of(q, owners):
                    idx.resolved[q].append(site(stmt, f"{prefix.split('.')[-1]}.{n}"))

    for cls, q in subclass_defs:
        record_body(cls.body, q)

    # Pass 1: follow simple assignments, e.g. `retry = Retry(...)`, `app = Flask(__name__)`,
    # and annotated parameters, e.g. `def view(resp: Response)`.
    for _ in range(2):
        for node in ast.walk(tree):
            targets: list[ast.AST] = []
            value = None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        c = chain_of(item.context_expr)
                        q = resolve(c) if c else None
                        if q and isinstance(item.optional_vars, ast.Name):
                            aliases.setdefault(item.optional_vars.id, q)
                continue
            elif isinstance(node, ast.arg) and node.annotation is not None:
                c = chain_of(node.annotation)
                q = resolve(c) if c else None
                if q:
                    aliases.setdefault(node.arg, q)
                continue
            if value is None:
                continue
            c = chain_of(value)
            q = resolve(c) if c else None
            if not q:
                continue
            for t in targets:
                tc = chain_of(t) if isinstance(t, (ast.Name, ast.Attribute)) else None
                if tc:
                    aliases.setdefault(".".join(tc), q)

    # Pass 2: record every attribute chain and keyword argument that resolves to the package.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and not isinstance(getattr(node, "_bw_parent", None), ast.Attribute):
            c = chain_of(node)
            if not c:
                continue
            q = _resolve_longest(c, aliases)
            raw = ".".join(c)
            if q:
                for idx in _owners_of(q, owners):
                    idx.resolved[q].append(site(node, raw))
            for idx in touched:
                idx.loose[raw].append(site(node, raw))
        elif isinstance(node, ast.Call):
            c = chain_of(node.func)
            if not c:
                continue
            q = _resolve_longest(c, aliases)
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                if q:
                    for idx in _owners_of(q, owners):
                        idx.resolved[f"{q}.{kw.arg}"].append(site(kw.value, f"{'.'.join(c)}({kw.arg}=...)"))
                else:
                    for idx in touched:
                        idx.loose[f"{'.'.join(c)}.{kw.arg}"].append(site(kw.value, f"{'.'.join(c)}({kw.arg}=...)"))
        elif isinstance(node, ast.Name) and node.id in aliases and isinstance(node.ctx, ast.Load):
            q = aliases[node.id]
            for idx in _owners_of(q, owners):
                idx.resolved[q].append(site(node, node.id))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and 2 < len(node.value) < 60:
            # String settings such as Django's "django.contrib.auth.backends.ModelBackend".
            if re.fullmatch(r"[A-Za-z_][\w.]*", node.value):
                for idx in touched:
                    idx.loose[node.value].append(site(node, repr(node.value)))
        for child in ast.iter_child_nodes(node):
            child._bw_parent = node  # type: ignore[attr-defined]


def _resolve_longest(chain: list[str], aliases: dict[str, str]) -> str | None:
    for i in range(len(chain), 0, -1):
        key = ".".join(chain[:i])
        if key in aliases:
            return ".".join([aliases[key]] + chain[i:])
    return None


def _owners_of(qualified: str, owners: dict[str, list[UsageIndex]]) -> list[UsageIndex]:
    return owners.get(qualified.split(".")[0], [])


# ------------------------------------------------------------------ JavaScript / TypeScript

_JS_IMPORT = re.compile(
    r"""import\s+(?:type\s+)?(?P<clause>[\w$*{}\s,]+?)\s+from\s+['"](?P<mod>[^'"]+)['"]"""
    r"""|(?:const|let|var)\s+(?P<lhs>[\w$]+|\{[^}]*\})\s*=\s*(?:await\s+)?(?:require|import)\(\s*['"](?P<mod2>[^'"]+)['"]\s*\)(?P<member>(?:\.[\w$]+)*)"""
    r"""|import\s+['"](?P<mod3>[^'"]+)['"]""",
)
_JS_CHAIN = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*(?:\s*\??\.\s*[A-Za-z_$][\w$]*)+)")
_JS_ASSIGN = re.compile(r"(?:const|let|var)\s+([\w$]+)\s*(?::\s*[\w$.<>\[\]]+\s*)?=\s*(?:new\s+|await\s+)?([\w$.]+)\s*[(.]")
_JS_IDENT = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*[:(]")


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    return re.sub(r"(?<![:'\"\\])//[^\n]*", "", src)


def _js_module_root(mod: str) -> str:
    if mod.startswith("@"):
        return "/".join(mod.split("/")[:2])
    return mod.split("/")[0]


def _scan_js(path: Path, root: Path, by_module: dict[str, UsageIndex]) -> None:
    src = _read(path)
    if src is None:
        return
    code = _strip_js_comments(src)
    lines = src.splitlines()
    rel = _rel(path, root)

    def site_at(pos: int, symbol: str) -> UsageSite:
        ln = code.count("\n", 0, pos) + 1
        text = lines[ln - 1].strip() if 0 < ln <= len(lines) else ""
        return UsageSite(file=rel, line=ln, code=text[:160], symbol=symbol)

    aliases: dict[str, str] = {}
    touched: dict[int, UsageIndex] = {}
    for m in _JS_IMPORT.finditer(code):
        mod = m.group("mod") or m.group("mod2") or m.group("mod3")
        idx = by_module.get(_js_module_root(mod))
        if idx is None:
            continue
        touched[id(idx)] = idx
        idx.resolved[mod].append(site_at(m.start(), mod))
        clause, lhs = m.group("clause"), m.group("lhs")
        base = mod + (m.group("member") or "")
        if clause:
            named = re.search(r"\{([^}]*)\}", clause)
            default = re.sub(r"\{[^}]*\}", "", clause).strip(" ,")
            if default:
                star = re.match(r"\*\s+as\s+([\w$]+)", default)
                aliases[star.group(1) if star else default.split(",")[0].strip()] = base
            if named:
                _named_bindings(named.group(1), base, aliases, " as ")
        elif lhs:
            if lhs.startswith("{"):
                _named_bindings(lhs[1:-1], base, aliases, ":")
            else:
                aliases[lhs] = base
    if not touched:
        return
    for idx in touched.values():
        idx.files.append(rel)

    for m in _JS_ASSIGN.finditer(code):
        name, rhs = m.group(1), m.group(2)
        parts = rhs.split(".")
        if parts[0] in aliases and name not in aliases:
            aliases[name] = ".".join([aliases[parts[0]]] + parts[1:])

    for m in _JS_CHAIN.finditer(code):
        chain = re.sub(r"\s|\?", "", m.group(1))
        parts = chain.split(".")
        q = _resolve_longest(parts, aliases)
        for idx in touched.values():
            if q and _js_module_root(q.split(".")[0]) in idx.import_names:
                idx.resolved[q].append(site_at(m.start(), chain))
            idx.loose[chain].append(site_at(m.start(), chain))
    for m in _JS_IDENT.finditer(code):
        name = m.group(1)
        if name in aliases:
            for idx in touched.values():
                idx.resolved[aliases[name]].append(site_at(m.start(), name))
        else:
            for idx in touched.values():
                idx.loose[name].append(site_at(m.start(), name))


def _named_bindings(text: str, base: str, aliases: dict[str, str], sep: str) -> None:
    for part in text.split(","):
        part = part.strip()
        if not part or part.startswith("..."):
            continue
        part = re.sub(r"^type\s+", "", part)
        if sep in part:
            orig, local = (x.strip() for x in part.split(sep, 1))
        else:
            orig = local = part
        local = local.split("=")[0].strip()
        if re.fullmatch(r"[\w$]+", local):
            aliases[local] = f"{base}.{orig}"


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()  # forward slashes on Windows too
    except ValueError:
        return path.as_posix()
