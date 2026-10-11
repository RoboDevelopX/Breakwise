"""Breakwise in the browser: a self-contained HTML report, and a small local web app (`breakwise serve`).

Both use the same page (templates/page.html). The page renders a JSON "view" of a ScanReport;
a static report embeds that view, the web app fetches it from the local server.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import __version__, explain, manifests, report, scanner
from .http import Http
from .models import Impact, ScanReport

TONES = {"✖": "breaking", "!": "security", "⚠": "warning", "?": "unknown", "✔": "ok", "·": "current"}
MAX_PASTE = 1_000_000  # bytes of pasted dependency list we accept
MAX_SITES = 8


# ------------------------------------------------------------------ view model


def view(result: ScanReport, label: str | None = None) -> dict:
    """Everything the page shows, worked out here so the page only lays it out."""
    checked = result.code_checked
    packages = []
    for p in sorted(result.packages, key=lambda p: report._order(p, checked)):
        sym, status = report.verdict(p, checked)
        sec_top, sec_total = report.security_highlights(p, limit=5)
        packages.append({
            "name": p.dep.name,
            "ecosystem": p.dep.ecosystem.value,
            "current": p.dep.current,
            "target": p.target,
            "has_update": p.has_update,
            "releases": len(p.releases),
            "tone": TONES.get(sym, "unknown"),
            "status": status,
            "manifest": p.dep.manifest,
            "version_source": p.dep.current_source,
            "imported": p.imported,
            "note": p.note,
            "changelog_url": _web_url(p.changelog_url),
            "impacts": [_impact(i) for i in p.impacts if not i.is_runtime],
            "security": {
                "total": sec_total,
                "span": report.security_span(p),
                "top": [{"version": e.version, "text": e.text} for e in sec_top],
            },
            "runtime": [report._runtime_note(i) for i in p.runtime_impacts],
            "other_breaking": [{"version": e.version, "text": e.text} for e in p.unmatched_breaking[:25]],
            "other_breaking_total": len(p.unmatched_breaking),
        })
    return {
        "headline": report.headline(result),
        "label": label or result.root,
        "mode": result.mode,
        "ai": result.ai,
        "code_checked": checked,
        "no_code_note": "" if checked else report.NO_CODE_NOTE,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "counts": {
            "checked": len(result.packages),
            "updated": len(result.updated),
            "matters": len(result.mattering),
            "breaking": len(result.breaking),
        },
        "packages": packages,
        "markdown": report.render_markdown(result),
        "version": __version__,
    }


def _impact(i: Impact) -> dict:
    e = i.entry
    return {
        "kind": e.kind.value,
        "label": report.KIND_LABEL[e.kind],
        "version": e.version,
        "symbol": i.matched_symbol,
        "confidence": i.confidence,
        "counts": i.counts,
        "explanation": i.explanation or e.text,
        "sites": [{"file": s.file, "line": s.line, "code": s.code} for s in i.sites[:MAX_SITES]],
        "more_sites": max(0, len(i.sites) - MAX_SITES),
        "fix": i.fix,
        "ai": i.ai,
    }


def _web_url(url: str) -> str:
    return url if url.startswith(("https://", "http://")) else ""


# ------------------------------------------------------------------ the page


def page(config: dict, data: dict | None, standalone: bool = True) -> str:
    """The HTML page with config and data embedded.

    standalone=False leaves out the doctype and meta tags, for hosts that wrap the page themselves.
    """
    tpl = resources.files(__package__).joinpath("templates/page.html").read_text(encoding="utf-8")
    values = {"__BREAKWISE_CONFIG__": _json_for_html(config), "__BREAKWISE_DATA__": _json_for_html(data)}
    html = re.sub(r"__BREAKWISE_(?:CONFIG|DATA)__", lambda m: values[m.group(0)], tpl)
    if standalone:
        html = (
            '<!doctype html>\n<html lang="en">\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n' + html + "</html>\n"
        )
    return html


def render_html(result: ScanReport, label: str | None = None, standalone: bool = True) -> str:
    """A self-contained HTML report: one file, no network access needed to open it."""
    return page({"mode": "report", "version": __version__}, view(result, label), standalone)


def _json_for_html(obj) -> str:
    """JSON that is safe inside a <script> element, whatever strings it carries."""
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


# ------------------------------------------------------------------ local web app


class _Job:
    def __init__(self) -> None:
        self.state = "running"  # "running" | "done" | "error"
        self.progress = "Reading your dependency files"
        self.result: dict | None = None
        self.error = ""
        self.started = time.time()


class ScanError(ValueError):
    """A problem with what the user asked for, shown to them as is."""


class App:
    """State behind `breakwise serve`: where it started, and the scans it is running."""

    def __init__(self, root: Path, http_factory: Callable[[], Http] | None = None, use_cache: bool = True):
        self.root = root.resolve()
        self.http_factory = http_factory or (lambda: Http(use_cache=use_cache))
        self.jobs: dict[str, _Job] = {}
        self.lock = threading.Lock()
        self.allowed_hosts: set[str] = set()

    def config(self) -> dict:
        return {
            "mode": "app",
            "version": __version__,
            "defaultPath": self._label(self.root),
            "startedIn": str(self.root),
            "demos": [self._label(d) for d in demo_folders(self.root)],
            "aiAvailable": explain.ai_available(),
        }

    def _label(self, path: Path) -> str:
        try:
            rel = path.resolve().relative_to(self.root)
            return rel.as_posix() if rel.parts else "."
        except ValueError:
            return str(path)

    def resolve(self, raw: str) -> Path:
        raw = (raw or "").strip() or "."
        path = Path(raw).expanduser()
        path = (path if path.is_absolute() else self.root / path).resolve()
        if not path.is_dir():
            raise ScanError(f"There's no folder at {raw}. Enter the path of your project folder.")
        return path

    def start(self, payload: dict) -> str:
        root = self.resolve(str(payload.get("path") or ""))
        pasted = str(payload.get("manifest") or "")
        if len(pasted.encode("utf-8")) > MAX_PASTE:
            raise ScanError("That dependency list is too long. Paste just requirements.txt, pyproject.toml or package.json.")
        if not pasted.strip() and not manifests.discover(root):
            raise ScanError(
                f"{self._label(root)} has no requirements.txt, pyproject.toml or package.json. "
                "Pick your project folder, or paste your dependency list instead."
            )
        ai = bool(payload.get("ai")) and explain.ai_available()
        job_id = uuid.uuid4().hex
        job = _Job()
        with self.lock:
            self.jobs[job_id] = job
            for old in list(self.jobs)[:-20]:  # keep the last 20
                del self.jobs[old]
        threading.Thread(target=self._run, args=(job, root, pasted, ai), daemon=True).start()
        return job_id

    def _run(self, job: _Job, root: Path, pasted: str, ai: bool) -> None:
        def progress(msg: str) -> None:
            job.progress = msg

        try:
            with tempfile.TemporaryDirectory() as tmp:
                files = None
                if pasted.strip():
                    f = Path(tmp) / _pasted_name(pasted)
                    f.write_text(pasted, encoding="utf-8")
                    files = [f]
                result = scanner.scan(root, files, http=self.http_factory(), ai=ai, progress=progress)
            if files:
                for p in result.packages:
                    p.dep.manifest = "pasted list"
            job.result = view(result, self._label(root))
            job.state = "done"
        except Exception as e:  # report it on the page rather than in a terminal nobody is watching
            job.error, job.state = f"The scan failed: {type(e).__name__}: {e}", "error"

    def status(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        out = {"state": job.state, "progress": job.progress, "elapsed": round(time.time() - job.started, 1)}
        if job.state == "done":
            out["result"] = job.result
        elif job.state == "error":
            out["error"] = job.error
        return out


def _pasted_name(text: str) -> str:
    if text.lstrip().startswith("{"):
        return "package.json"
    if re.search(r"^\s*\[(project|tool\.poetry|dependency-groups)", text, re.M):
        return "pyproject.toml"
    return "requirements.txt"


def demo_folders(root: Path) -> list[Path]:
    """Example projects to offer as one-click demos: ./examples/*, or the ones next to the source checkout."""
    found: list[Path] = []
    for base in (root / "examples", Path(__file__).resolve().parents[2] / "examples"):
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if d.is_dir() and manifests.discover(d, max_depth=1):
                    found.append(d.resolve())
    return list(dict.fromkeys(found))[:6]


_PAGE_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
    "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def _handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"breakwise/{__version__}"

        def log_message(self, *args) -> None:  # keep the terminal quiet
            pass

        def _send(self, code: int, body: str, ctype: str = "application/json; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", _PAGE_CSP)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj))

        def _trusted(self) -> bool:
            # Only answer requests addressed to this server by name (blocks DNS rebinding),
            # and only accept writes from its own page.
            if self.headers.get("Host", "") not in app.allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or urlsplit(origin).netloc in app.allowed_hosts

        def do_GET(self) -> None:
            if not self._trusted():
                return self._json(403, {"error": "forbidden"})
            path = urlsplit(self.path).path
            if path == "/":
                return self._send(200, page(app.config(), None), "text/html; charset=utf-8")
            if path.startswith("/api/jobs/"):
                status = app.status(path.rsplit("/", 1)[-1])
                return self._json(200, status) if status else self._json(404, {"error": "That scan isn't running any more."})
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return None
            return self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._trusted():
                return self._json(403, {"error": "forbidden"})
            if urlsplit(self.path).path != "/api/scan":
                return self._json(404, {"error": "not found"})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json(415, {"error": "expected JSON"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_PASTE * 2:
                return self._json(413, {"error": "That dependency list is too long."})
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "expected a JSON object"})
            try:
                return self._json(202, {"job": app.start(payload)})
            except ScanError as e:
                return self._json(400, {"error": str(e)})

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows it would let two servers share a port


def make_server(app: App, port: int = 8765) -> _Server:
    """Bind to localhost only, on `port` or the next free one (port 0: any free port)."""
    candidates = [port, *range(port + 1, port + 20)] if port else [0]
    server = None
    for p in candidates:
        try:
            server = _Server(("127.0.0.1", p), _handler(app))
            break
        except OSError:
            continue
    if server is None:
        server = _Server(("127.0.0.1", 0), _handler(app))
    actual = server.server_address[1]
    app.allowed_hosts = {f"127.0.0.1:{actual}", f"localhost:{actual}"}
    return server


def serve(root: Path, port: int = 8765, open_browser: bool = True, use_cache: bool = True) -> int:
    app = App(root, use_cache=use_cache)
    server = make_server(app, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Breakwise is running at {url}\nOpen that address in your browser. Press Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.6, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0
