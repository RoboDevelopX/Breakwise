"""Command-line interface.

    breakwise                       scan the current project: current versions -> latest
    breakwise scan path/to/project  same, for another directory
    breakwise scan -f requirements.txt --ai
    cat requirements.txt | breakwise scan -f -
    breakwise pr --base origin/main only the dependency changes on this branch
    breakwise serve                 open Breakwise in your browser
    breakwise --format html -o report.html
    breakwise comment report.md     post/update the PR comment (used by the GitHub Action)
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from . import __version__, explain, report, scanner, web
from .github import GitHubError, find_comment, upsert_comment
from .http import Http

COMMANDS = {"scan", "pr", "serve", "comment"}


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("path", nargs="?", default=".", help="project directory (default: current directory)")
    p.add_argument("-f", "--file", action="append", default=[], metavar="MANIFEST",
                   help="dependency file to read (repeatable; '-' reads requirements.txt or package.json from stdin). "
                        "Default: find requirements*.txt, pyproject.toml and package.json under PATH")
    p.add_argument("--only", action="append", default=[], metavar="PACKAGE", help="only check these packages (repeatable)")
    p.add_argument("--ai", action="store_true", help="use Claude to confirm impacts and write fixes (needs ANTHROPIC_API_KEY)")
    p.add_argument("--model", help=f"Claude model for --ai (default: {explain.DEFAULT_MODEL})")
    p.add_argument("--format", choices=["text", "markdown", "json", "html"], default="text",
                   help="html writes a single-file report you can open in a browser")
    p.add_argument("-o", "--output", help="write the report to this file instead of stdout")
    p.add_argument("--json-output", metavar="FILE", help="also write the JSON report to FILE")
    p.add_argument("--github-output", action="store_true",
                   help="write counts (checked, updated, matters, breaking) to $GITHUB_OUTPUT for later workflow steps")
    p.add_argument("--fail-on", choices=["breaking", "matters", "never"], default="never",
                   help="exit with status 1 when an update breaks your code, or when any update matters")
    p.add_argument("--no-cache", action="store_true", help="don't use the on-disk HTTP cache")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    p.add_argument("-v", "--verbose", action="store_true", help="also show up-to-date packages and unmatched breaking changes")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="breakwise",
        description="Find out which dependency updates actually affect your code, what breaks, and how to fix it.",
    )
    parser.add_argument("--version", action="version", version=f"breakwise {__version__}")
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="compare your current versions with the latest releases")
    _common(scan)

    pr = sub.add_parser("pr", help="explain the dependency changes between a base git ref and the working tree")
    _common(pr)
    pr.add_argument("--base", required=True, help="git ref to compare against, e.g. origin/main or a commit SHA")

    serve = sub.add_parser("serve", help="open Breakwise in your browser (a local web page; nothing is published)")
    serve.add_argument("path", nargs="?", default=".", help="folder to start in (default: current directory)")
    serve.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default: 8765, or the next free one)")
    serve.add_argument("--no-browser", action="store_true", help="don't open a browser window, just print the address")
    serve.add_argument("--no-cache", action="store_true", help="don't use the on-disk HTTP cache")

    comment = sub.add_parser("comment", help="post or update the Breakwise comment on the current pull request")
    comment.add_argument("body_file", nargs="?", help="Markdown file to post ('-' for stdin)")
    comment.add_argument("--exists", action="store_true", help="only check: exit 0 if the PR already has a Breakwise comment, 1 if not")
    comment.add_argument("--repo", help="owner/name (default: $GITHUB_REPOSITORY)")
    comment.add_argument("--pr", type=int, help="pull request number (default: from $GITHUB_EVENT_PATH)")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in {"-h", "--help", "--version"}):
        argv.insert(0, "scan")
    args = build_parser().parse_args(argv)
    # Windows consoles and redirected output may not be UTF-8; never crash on the ✖/✔ symbols.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(errors="replace")

    if args.command == "comment":
        if args.exists:
            try:
                return 0 if find_comment(repo=args.repo, pr=args.pr) else 1
            except GitHubError as e:
                print(f"breakwise: {e}", file=sys.stderr)
                return 2
        if not args.body_file:
            print("breakwise: comment needs a Markdown file (or '-')", file=sys.stderr)
            return 2
        body = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text(encoding="utf-8")
        try:
            url = upsert_comment(body, repo=args.repo, pr=args.pr)
        except GitHubError as e:
            print(f"breakwise: {e}", file=sys.stderr)
            return 2
        print(url or "comment posted")
        return 0

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"breakwise: {args.path} is not a directory", file=sys.stderr)
        return 2
    if args.command == "serve":
        return web.serve(root, port=args.port, open_browser=not args.no_browser, use_cache=not args.no_cache)
    if args.ai and not explain.ai_available():
        print("breakwise: --ai needs ANTHROPIC_API_KEY; continuing without it", file=sys.stderr)
        args.ai = False

    progress = None
    if not args.quiet and sys.stderr.isatty():
        def progress(msg: str) -> None:
            print(f"\r\033[K{msg[:100]}", end="", file=sys.stderr, flush=True)

    tmp = None
    try:
        files: list[Path] = []
        for f in args.file:
            if f == "-":
                tmp = tempfile.TemporaryDirectory()
                text = sys.stdin.read()
                name = "package.json" if text.lstrip().startswith("{") else "requirements.txt"
                p = Path(tmp.name) / name
                p.write_text(text, encoding="utf-8")
                files.append(p)
            else:
                p = Path(f) if Path(f).is_absolute() else (Path.cwd() / f)
                if not p.is_file():
                    print(f"breakwise: {f} not found", file=sys.stderr)
                    return 2
                files.append(p.resolve())

        http = Http(use_cache=not args.no_cache)
        kw = dict(http=http, ai=args.ai, model=args.model, progress=progress)
        if args.command == "pr":
            changes = scanner.changed_dependencies(root, args.base, files or None)
            if args.only:
                wanted = {o.lower() for o in args.only}
                changes = [c for c in changes if c[0].name.lower() in wanted]
            result = scanner.analyze(root, changes, mode="pr", **kw)
        else:
            result = scanner.scan(root, files or None, only=args.only or None, **kw)
    finally:
        if progress:
            print("\r\033[K", end="", file=sys.stderr, flush=True)
        if tmp:
            tmp.cleanup()

    if args.format == "json":
        text = report.render_json(result)
    elif args.format == "html":
        text = web.render_html(result, label=root.name if args.path == "." else args.path)
    elif args.format == "markdown":
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        repo = os.environ.get("GITHUB_REPOSITORY")
        sha = os.environ.get("BREAKWISE_HEAD_SHA") or os.environ.get("GITHUB_SHA")
        text = report.render_markdown(result, repo_url=f"{server}/{repo}" if repo else None, sha=sha)
    else:
        color = {"always": True, "never": False}.get(args.color)
        text = report.render_text(result, color=False if args.output else color, verbose=args.verbose)

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    if args.json_output:
        Path(args.json_output).write_text(report.render_json(result), encoding="utf-8")
    if args.github_output and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"checked={len(result.packages)}\nupdated={len(result.updated)}\n"
                     f"matters={len(result.mattering)}\nbreaking={len(result.breaking)}\n"
                     f"headline={report.headline(result)}\n")

    if args.fail_on == "breaking" and result.breaking:
        return 1
    if args.fail_on == "matters" and result.mattering:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
