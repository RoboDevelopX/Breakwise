from breakwise import changelog
from breakwise.models import ChangeKind, Ecosystem


def test_rst_changelog_sections_and_classification(fixture_text):
    text = fixture_text("urllib3_CHANGES.rst")
    sections = changelog.split_sections(text)
    assert [s.version for s in sections] == ["2.1.0", "2.0.0", "1.26.0"]
    entries, covered = changelog.entries_for_range(text, ["2.0.0", "2.1.0"], Ecosystem.PYPI)
    assert covered == ["2.0.0", "2.1.0"]
    retry = next(e for e in entries if "method_whitelist" in e.text)
    assert retry.kind == ChangeKind.BREAKING
    assert "method_whitelist" in retry.symbols and "Retry" in retry.symbols
    assert "#2086" not in retry.text  # issue links are cleaned out
    py37 = next(e for e in entries if "Python 3.7" in e.text)
    assert "@python:3.7" in py37.symbols
    dep = next(e for e in entries if "getheaders" in e.text)
    assert dep.kind == ChangeKind.DEPRECATION
    fix = next(e for e in entries if "amt" in e.text)
    assert fix.kind == ChangeKind.FIX


def test_replacement_hints_reach_back_in_time(fixture_text):
    hints = changelog.replacement_hints(fixture_text("urllib3_CHANGES.rst"))
    assert hints["method_whitelist"] == "allowed_methods"
    assert hints["DEFAULT_METHOD_WHITELIST"] == "DEFAULT_ALLOWED_METHODS"


def test_markdown_with_overlined_versions_and_nested_bullets(fixture_text):
    text = fixture_text("express_History.md")
    entries, covered = changelog.entries_for_range(text, ["5.0.0"], Ecosystem.NPM)
    assert covered == ["5.0.0"]
    back = next(e for e in entries if "magic string" in e.text)
    assert back.kind == ChangeKind.BREAKING
    assert back.section == "Breaking changes"
    assert {"res.redirect", "res.location"} <= set(back.symbols)
    deps = [e for e in entries if e.text.startswith("deps:")]
    assert deps and all(e.kind == ChangeKind.OTHER for e in deps)


def test_subheadings_written_at_release_level(fixture_text):
    """axios writes "## 🔒 Security Fixes" at the same level as "## v1.19.0"."""
    entries, covered = changelog.entries_for_range(fixture_text("axios_CHANGELOG.md"), ["1.19.0"], Ecosystem.NPM)
    assert covered == ["1.19.0"]
    sec = [e for e in entries if e.kind == ChangeKind.SECURITY]
    assert any("GHSA-hmw2" in e.text for e in sec)
    assert any(e.kind == ChangeKind.FIX and "NO_PROXY" in e.symbols for e in entries)


def test_classify_edge_cases():
    assert changelog.classify("Fix crash when removing items", "Bug fixes") == ChangeKind.FIX
    assert changelog.classify("Several minor changes (considered non-breaking changes)") != ChangeKind.BREAKING
    assert changelog.classify("Drop support for Python 3.8.") == ChangeKind.BREAKING
    assert changelog.classify("Fixed CVE-2024-1234 in header parsing") == ChangeKind.SECURITY
    assert changelog.classify("Improve CI by using PDM instead of pip") == ChangeKind.OTHER
    assert changelog.classify("Relax type hint for `send_file`") == ChangeKind.FIX
    assert changelog.classify("`redirect` returns a 303 status code by default instead of 302.") == ChangeKind.BREAKING


def test_extract_symbols():
    syms = changelog.extract_symbols("Removed ``Retry(method_whitelist=...)``; use `Session.get()` or AxiosHeaders.parseParameters().")
    assert "method_whitelist" in syms and "Retry" in syms
    assert "Session.get" in syms
    assert "AxiosHeaders.parseParameters" in syms
    assert "@node:16" in changelog.extract_symbols("Dropped support for Node.js 16")
    assert not any(s.endswith(".py") for s in changelog.extract_symbols("Moved config to `setup.py`"))


def test_raw_github_url():
    assert changelog.raw_github_url("https://github.com/urllib3/urllib3/blob/main/CHANGES.rst") == \
        "https://raw.githubusercontent.com/urllib3/urllib3/main/CHANGES.rst"
    assert changelog.raw_github_url("https://docs.example.com/changes") is None
