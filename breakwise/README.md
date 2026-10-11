# Breakwise

**Which of your dependency updates actually affect your code, what breaks, and the exact fix.**

Point Breakwise at a project with a `requirements.txt`, `pyproject.toml` or `package.json`. It reads the release notes between the version you're on and the latest one, finds where your code uses each changed API, and tells you in plain English what that means for you.

```
$ breakwise
3 updates matter, 1 has a breaking change in urllib3.
5 dependencies checked, 5 with updates

✖ urllib3 1.26.18 → 2.8.0  (24 releases)  breaks your code
    Breaking · 2.0.0 · method_whitelist
      Removed deprecated `Retry` options `method_whitelist`, `DEFAULT_REDIRECT_HEADERS_BLACKLIST`.
      Your code uses `Retry(method_whitelist=...)` at app/http.py:7.
      app/http.py:7  retry = Retry(total=3, backoff_factor=0.5, method_whitelist=["GET", "POST"])
      Fix: Replace `method_whitelist` with `allowed_methods`.
    Security: 15 fixes in 2.5.0–2.8.0
      ...
! requests 2.31.0 → 2.34.2  (11 releases)  security fixes
    ...
⚠ pydantic 1.10.13 → 2.14.0  (76 releases)  deprecations in code you use
    Deprecation · 2.0 · BaseModel.dict
      `BaseModel.dict()` is deprecated in Pydantic V2 and renamed to `model_dump()`.
      Your code uses `user.dict` at app/models.py:17.
      app/models.py:17  return user.dict(exclude_none=True)
      Fix: Replace `.dict(...)` with `.model_dump(...)`; the keyword arguments (...) are the same.
    ...4 more: parse_obj, @validator, class Config, orm_mode
✔ flask 3.0.0 → 3.1.3  (7 releases)  no impact found (1 breaking change elsewhere)
✔ python-dotenv 1.0.0 → 1.2.4  (8 releases)  no impact found (5 breaking changes elsewhere)
```

That's the real output on [`examples/python-demo`](examples/python-demo), trimmed where marked `...`.

Notice the fix for urllib3: the 2.0 release notes only say the option was *removed*. Breakwise found the replacement in the 1.26 notes, where it was deprecated two and a half years earlier.

## Install

```bash
pipx install breakwise            # or: pip install breakwise
pip install "breakwise[ai]"       # adds Claude-written explanations (optional)
```

Python 3.11+. Breakwise scans Python and JavaScript/TypeScript projects; it doesn't need Node installed.

## Use it

```bash
breakwise                                  # this directory: current versions -> latest
breakwise path/to/project -v               # also list up-to-date packages and unmatched breaking changes
breakwise -f requirements-prod.txt         # one manifest
cat requirements.txt | breakwise -f -      # paste a dependency list
breakwise pr --base origin/main            # only what this branch changes (what the GitHub Action runs)
breakwise --format markdown -o report.md   # or --format json, or --format html
breakwise --fail-on breaking               # exit 1 if an update breaks your code (for CI)
breakwise --ai                             # let Claude confirm each impact and write the fix
```

### In your browser

```bash
breakwise serve                            # opens http://127.0.0.1:8765 in your browser
breakwise --format html -o report.html     # or save one report as a single HTML file
```

`breakwise serve` runs a small web page on your own computer. Type a project folder (or click one of the examples), or paste a dependency list, and press **Check dependencies**. It only listens on 127.0.0.1, so nothing is reachable from other machines, and it stops when you press Ctrl+C in the terminal.

### With Claude (`--ai`)

Set `ANTHROPIC_API_KEY` and add `--ai`. For each package with updates, Claude reads the release notes next to the lines of your code that use the package, drops matches that don't really matter, catches breaking changes the static matcher missed, and writes a concrete before/after fix. Without a key, Breakwise is fully deterministic and makes no calls except to package registries and GitHub.

## GitHub Action

Breakwise can comment on every pull request that changes a dependency, including Dependabot's and Renovate's:

```yaml
# .github/workflows/breakwise.yml
name: Breakwise
on:
  pull_request:
    paths: ["**/requirements*.txt", "**/pyproject.toml", "**/package.json", "**/*.lock", "**/package-lock.json"]
permissions:
  contents: read
  pull-requests: write
jobs:
  breakwise:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: RoboDevelopX/breakwise@v0
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}  # optional
          fail-on: never                                       # or "breaking"
```

All inputs and recipes are in [docs/github-action.md](docs/github-action.md).

## How it works

1. **Read your manifests.** `requirements*.txt` (with `-r` includes), `pyproject.toml` (PEP 621, Poetry, `uv.lock`/`poetry.lock`/`pdm.lock`) and `package.json` (with `package-lock.json` or `yarn.lock`). Exact pins and lockfiles give the version you're on; otherwise the installed version or the range's lower bound.
2. **Find the release notes.** Package metadata from PyPI or npm points at the repository; Breakwise reads its `CHANGELOG`/`CHANGES`/`HISTORY`/`NEWS` file (Markdown or reStructuredText, about 40 common locations) and falls back to GitHub Releases. Yanked releases still count: urllib3 2.0.0 was yanked, and it's where most of the 2.x breaking changes are.
3. **Split them into entries.** One entry per bullet, classified as breaking, security, deprecation, fix, feature or other from the wording and the section it sits in, with the API names it mentions (`Retry(method_whitelist=...)`, `res.redirect()`, `np.float_`).
4. **Find where your code uses each package.** Python is parsed with `ast`, following imports, aliases, assignments, annotations and subclasses (so `class User(BaseModel)` with an inner `Config.orm_mode` is understood as using pydantic's `Config.orm_mode`). JavaScript and TypeScript are scanned for `import`/`require` bindings and member chains.
5. **Match.** An entry affects you when the API it names is used in your code. Matches are graded high, medium or low; low ones are only shown with `-v`.
6. **Explain.** A fix comes from the entry itself ("use X instead"), from an older deprecation note elsewhere in the changelog, from built-in migration rules for big upgrades whose notes just say "see the migration guide" (pydantic 2, SQLAlchemy 2, NumPy 2, pandas 2, Flask, Werkzeug 3, Django, Express 5, React 18/19, Next.js 15), or from Claude with `--ai`.

Breakwise also tells you when an upgrade drops a Python or Node.js version your project still declares support for.

## What it won't catch

Static matching finds changes to APIs you call by name. It misses behavior changes that don't name an API, uses that go through heavy indirection, and changelogs that live only on a docs site. `--ai` closes some of that gap. If you hit a miss or a false alarm, please open an issue with the package and versions.

## Privacy

Without `--ai`, nothing about your code leaves your machine; Breakwise only downloads public package metadata and release notes. With `--ai`, the release notes and short snippets around the lines that use each changed package are sent to the Claude API.

## Contributing

`pip install -e ".[ai,dev]" && pytest`. Migration rules live in `src/breakwise/rules/*.json`; each one is a version, the API names it affects, what changed and the fix. Adding one for a library you know well is the easiest way to help.

## License

MIT
