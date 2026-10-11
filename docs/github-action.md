# Breakwise GitHub Action

Breakwise comments on pull requests that change dependency versions (yours, Dependabot's or Renovate's) and says which of those changes actually touch your code, what breaks, and how to fix it. The comment is edited in place on every push, so a PR only ever has one.

## Quick start

Add `.github/workflows/breakwise.yml`:

```yaml
name: Breakwise

on:
  pull_request:
    paths:
      - "**/requirements*.txt"
      - "**/pyproject.toml"
      - "**/package.json"
      - "**/package-lock.json"
      - "**/yarn.lock"
      - "**/uv.lock"
      - "**/poetry.lock"

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
          # Optional: lets Claude drop false positives and write exact fixes.
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

That's it. Open a PR that bumps a dependency and Breakwise replies with something like:

> ### Breakwise: 2 changes matter, 1 has a breaking change in urllib3.
>
> | | Package | Version | What it means for your code |
> |---|---|---|---|
> | 🔴 | urllib3 | `1.26.18` → `2.8.0` | breaks your code |
> | 🟡 | pydantic | `1.10.13` → `2.14.0` | deprecations in code you use |
>
> **Breaking** in 2.0.0: Removed deprecated `Retry` options `method_whitelist` ... Your code uses `Retry(method_whitelist=...)` at app/http.py:7.
>
> **Fix:** Replace `method_whitelist` with `allowed_methods`.

## Inputs

| Input | Default | What it does |
|---|---|---|
| `anthropic-api-key` | (none) | Turns on Claude explanations. Without it, Breakwise uses static analysis, changelog parsing and built-in migration rules only. |
| `model` | `claude-opus-5-5` | Claude model for explanations. |
| `path` | `.` | Project directory, for monorepos. Run the action once per project. |
| `mode` | `auto` | `auto` explains the versions a PR changes, and on other events (push, schedule) compares everything with the latest releases. Force `pr` or `scan` if you like. |
| `fail-on` | `never` | `breaking` fails the check when an update breaks your code; `matters` fails when any update affects it. |
| `comment` | `true` | Set to `false` to only write the job summary. |
| `github-token` | `${{ github.token }}` | Token used to comment. |

Outputs: `checked`, `updated`, `matters`, `breaking`, `report` (path to the Markdown report).

## Recipes

**Block merging Dependabot PRs that break your code**

```yaml
      - uses: RoboDevelopX/breakwise@v0
        with:
          fail-on: breaking
```

Dependabot runs workflows with its own secret store, so add `ANTHROPIC_API_KEY` under *Settings → Secrets → Dependabot* as well if you want AI explanations on its PRs.

**A weekly "what's outdated and does it matter" report**

```yaml
on:
  schedule:
    - cron: "0 8 * * 1"
jobs:
  breakwise:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: RoboDevelopX/breakwise@v0
```

On a schedule there's no PR to comment on, so the report lands in the run's job summary.

## Notes

- Pull requests from forks get a read-only token, so Breakwise can't comment there. The report is still in the job summary.
- Breakwise reads release notes from GitHub. The default `github.token` gives it a much higher API rate limit than anonymous requests.
- Your code never leaves the runner unless you set `anthropic-api-key`; then the release notes and short snippets around the lines that use each changed package are sent to the Claude API.
