import json
import subprocess
import textwrap

from conftest import FakeHttp, npm_meta, pypi_meta

from breakwise import cli, report, scanner
from breakwise.explain import apply_ai_result, build_prompt


def _project(tmp_path):
    (tmp_path / "requirements.txt").write_text("urllib3==1.26.18\nrequests==2.31.0\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="demo"\nrequires-python=">=3.7"\n')
    (tmp_path / "app.py").write_text(textwrap.dedent("""
        import requests
        from urllib3.util.retry import Retry

        RETRY = Retry(total=3, method_whitelist=["GET"])
        print(requests.get("https://example.com").status_code)
    """))
    return tmp_path


def _http(fixture_text):
    return FakeHttp({
        "https://pypi.org/pypi/urllib3/json": pypi_meta("urllib3", ["1.26.18", "2.0.0", "2.0.1", "2.1.0"], "urllib3/urllib3", yanked={"2.0.0"}),
        "https://raw.githubusercontent.com/urllib3/urllib3/HEAD/CHANGES.rst": fixture_text("urllib3_CHANGES.rst"),
        "https://pypi.org/pypi/requests/json": pypi_meta("requests", ["2.31.0"], "psf/requests"),
    })


def test_scan_finds_the_breaking_change(tmp_path, fixture_text):
    root = _project(tmp_path)
    result = scanner.scan(root, http=_http(fixture_text), workers=1)
    pkgs = {p.dep.name: p for p in result.packages}
    u3 = pkgs["urllib3"]
    assert u3.target == "2.1.0"
    assert u3.releases == ["2.0.0", "2.0.1", "2.1.0"]  # the yanked 2.0.0 still contributes notes
    assert [i.matched_symbol for i in u3.breaking_impacts] == ["method_whitelist"]
    imp = u3.breaking_impacts[0]
    assert imp.sites[0].file == "app.py" and imp.sites[0].line == 5
    assert "allowed_methods" in imp.fix  # recovered from the 1.26.0 deprecation note
    assert u3.runtime_impacts and "3.7" in u3.runtime_impacts[0].matched_symbol
    assert pkgs["requests"].status == "up-to-date"
    assert report.headline(result) == "1 update matters, 1 has a breaking change in urllib3."

    md = report.render_markdown(result, repo_url="https://github.com/o/r", sha="abc")
    assert md.startswith(report.COMMENT_MARKER)
    assert "https://github.com/o/r/blob/abc/app.py#L5" in md
    data = json.loads(report.render_json(result))
    assert data["packages"][1]["verdict"] in ("breaks your code", "up to date")
    text = report.render_text(result, color=False)
    assert "Replace `method_whitelist` with `allowed_methods`" in text


def test_npm_scan_with_rules(tmp_path, fixture_text):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"express": "4.21.0"}}))
    (tmp_path / "index.js").write_text("const express = require('express');\nconst app = express();\napp.del('/x', (req, res) => res.redirect('back'));\n")
    http = FakeHttp({
        "https://registry.npmjs.org/express": npm_meta("express", ["4.21.0", "5.0.0"], "expressjs/express"),
        "https://raw.githubusercontent.com/expressjs/express/HEAD/History.md": fixture_text("express_History.md"),
    })
    result = scanner.scan(tmp_path, http=http, workers=1)
    ex = result.packages[0]
    symbols = {i.matched_symbol for i in ex.breaking_impacts}
    assert {"app.del", "res.redirect"} <= symbols
    app_del = next(i for i in ex.impacts if i.matched_symbol == "app.del")
    assert app_del.entry.source == "rules" and "app.delete()" in app_del.fix


def test_pr_mode_compares_against_git_base(tmp_path, fixture_text):
    root = _project(tmp_path)
    git = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    (root / "requirements.txt").write_text("urllib3==2.1.0\nrequests==2.31.0\n")
    changes = scanner.changed_dependencies(root, "HEAD")
    assert [(d.name, d.current, t) for d, t in changes] == [("urllib3", "1.26.18", "2.1.0")]
    result = scanner.analyze(root, changes, http=_http(fixture_text), mode="pr", workers=1)
    assert result.breaking and result.breaking[0].dep.name == "urllib3"
    assert report.headline(result).startswith("1 change matters")


def test_cli_exit_codes_and_stdin(tmp_path, fixture_text, monkeypatch, capsys):
    root = _project(tmp_path)
    monkeypatch.setattr(scanner, "Http", lambda **kw: _http(fixture_text))
    monkeypatch.setattr(cli, "Http", lambda **kw: _http(fixture_text))
    assert cli.main([str(root), "--fail-on", "breaking", "-q"]) == 1
    out = capsys.readouterr().out
    assert "method_whitelist" in out
    assert cli.main(["scan", str(root), "--only", "requests", "--fail-on", "breaking", "-q"]) == 0
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("urllib3==1.26.18\n"))
    assert cli.main(["scan", str(root), "-f", "-", "--format", "json", "-q"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [p["dep"]["name"] for p in data["packages"]] == ["urllib3"]


class _FakeMessages:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def create(self, **kw):
        self.calls.append(kw)
        block = type("B", (), {"type": "text", "text": json.dumps(self.payload)})()
        return type("R", (), {"content": [block], "stop_reason": "end_turn"})()


def test_ai_mode_filters_and_rewrites(tmp_path, fixture_text):
    from breakwise import explain

    root = _project(tmp_path)
    result = scanner.scan(root, http=_http(fixture_text), workers=1, only=["urllib3"])
    u3 = result.packages[0]
    prompt, candidates = build_prompt(u3, [], root)
    assert "method_whitelist" in prompt and "app.py:5" in prompt
    cid = next(i for i, e in enumerate(candidates) if "method_whitelist" in e.text)
    other = next(i for i, e in enumerate(candidates) if "from_httplib" in e.text)
    payload = {"impacts": [
        {"change_id": cid, "affects_code": True, "severity": "breaking", "confidence": "high", "locations": ["app.py:5"],
         "explanation": "app.py:5 passes method_whitelist, which urllib3 2 removed.", "fix": "Use allowed_methods=[\"GET\"]."},
        {"change_id": other, "affects_code": False, "severity": "none", "confidence": "high", "locations": [],
         "explanation": "", "fix": ""},
    ]}
    msgs = _FakeMessages(payload)
    client = type("C", (), {"beta": type("Beta", (), {"messages": msgs})()})()
    explain.ai_explain(u3, [], root, client=client)
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default"
    assert [i.fix for i in u3.impacts] == ['Use allowed_methods=["GET"].']
    assert u3.impacts[0].ai


def test_apply_ai_result_adds_missed_impacts(tmp_path, fixture_text):
    root = _project(tmp_path)
    result = scanner.scan(root, http=_http(fixture_text), workers=1, only=["urllib3"])
    u3 = result.packages[0]
    _, candidates = build_prompt(u3, [], root)
    missed = next(i for i, e in enumerate(candidates) if "from_httplib" in e.text)
    apply_ai_result(u3, {"impacts": [{"change_id": missed, "affects_code": True, "severity": "breaking", "confidence": "medium",
                                      "locations": ["app.py:4"], "explanation": "x", "fix": "y"}]}, candidates)
    assert len(u3.impacts) == 1 and u3.impacts[0].sites[0].line == 4
