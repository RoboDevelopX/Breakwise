import http.client
import json
import re
import threading
import time

import pytest
from test_end_to_end import _http, _project

from breakwise import cli, report, scanner, web


def _embedded(html, name):
    m = re.search(rf'<script type="application/json" id="{name}">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def test_html_report_embeds_the_view_safely(tmp_path, fixture_text):
    root = _project(tmp_path)
    result = scanner.scan(root, http=_http(fixture_text), workers=1)
    imp = next(p for p in result.packages if p.dep.name == "urllib3").breaking_impacts[0]
    imp.explanation = "</script><script>alert(1)</script> & more"
    html = web.render_html(result, label="demo")
    assert html.startswith("<!doctype html>")
    assert "<script>alert(1)" not in html  # changelog text can't break out of the data block
    data = _embedded(html, "bw-data")
    assert data["headline"] == report.headline(result)
    assert data["label"] == "demo" and data["counts"]["breaking"] == 1
    u3 = next(p for p in data["packages"] if p["name"] == "urllib3")
    assert u3["tone"] == "breaking" and u3["status"] == "breaks your code"
    assert u3["impacts"][0]["explanation"].startswith("</script>")
    site = u3["impacts"][0]["sites"][0]
    assert (site["file"], site["line"]) == ("app.py", 5) and "method_whitelist" in site["code"]
    assert "allowed_methods" in u3["impacts"][0]["fix"]
    assert u3["runtime"] and "3.7" in u3["runtime"][0]
    assert _embedded(html, "bw-config")["mode"] == "report"
    assert not web.render_html(result, standalone=False).startswith("<!doctype")


def test_cli_writes_html(tmp_path, fixture_text, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr(cli, "Http", lambda **kw: _http(fixture_text))
    out = tmp_path / "report.html"
    assert cli.main([str(root), "--format", "html", "-o", str(out), "-q"]) == 0
    assert _embedded(out.read_text(encoding="utf-8"), "bw-data")["counts"]["matters"] == 1


def test_no_code_means_no_verdict_about_your_code(tmp_path, fixture_text):
    (tmp_path / "requirements.txt").write_text("urllib3==1.26.18\n")
    result = scanner.scan(tmp_path, http=_http(fixture_text), workers=1)
    assert result.code_files == 0 and not result.code_checked
    sym, status = report.verdict(result.packages[0], result.code_checked)
    assert sym == "?" and "no code to check" in status
    text = report.render_text(result, color=False)
    assert report.NO_CODE_NOTE in text and "Breaking changes in these releases:" in text
    assert report.NO_CODE_NOTE in report.render_markdown(result)


@pytest.fixture
def server(tmp_path, fixture_text):
    (tmp_path / "proj").mkdir()
    root = _project(tmp_path / "proj")
    (tmp_path / "proj" / "examples" / "demo").mkdir(parents=True)
    (tmp_path / "proj" / "examples" / "demo" / "requirements.txt").write_text("requests==2.31.0\n")
    (tmp_path / "proj" / "notes").mkdir()
    app = web.App(root, http_factory=lambda: _http(fixture_text))
    srv = web.make_server(app, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=hdrs)
    res = conn.getresponse()
    raw = res.read().decode()
    conn.close()
    return res.status, raw


def _wait(port, job):
    for _ in range(100):
        status, raw = _request(port, "GET", f"/api/jobs/{job}")
        data = json.loads(raw)
        if data["state"] != "running":
            return data
        time.sleep(0.05)
    raise AssertionError("scan never finished")


def test_serve_page_and_scan(server):
    app, port = server
    status, html = _request(port, "GET", "/")
    assert status == 200
    config = _embedded(html, "bw-config")
    assert config["mode"] == "app" and config["defaultPath"] == "." and config["demos"][0] == "examples/demo"
    assert config["aiAvailable"] is False

    status, raw = _request(port, "POST", "/api/scan", {"path": "."})
    assert status == 202
    done = _wait(port, json.loads(raw)["job"])
    assert done["state"] == "done"
    assert done["result"]["headline"] == "1 update matters, 1 has a breaking change in urllib3."

    # A pasted list is checked against the code in the chosen folder.
    status, raw = _request(port, "POST", "/api/scan", {"path": ".", "manifest": "urllib3==1.26.18\n"})
    done = _wait(port, json.loads(raw)["job"])
    pkgs = done["result"]["packages"]
    assert [p["name"] for p in pkgs] == ["urllib3"] and pkgs[0]["manifest"] == "pasted list"
    assert pkgs[0]["tone"] == "breaking"


def test_serve_refuses_bad_requests(server):
    _, port = server
    assert _request(port, "GET", "/", headers={"Host": f"attacker.example:{port}"})[0] == 403
    assert _request(port, "POST", "/api/scan", {"path": "."}, headers={"Origin": "http://attacker.example"})[0] == 403
    assert _request(port, "POST", "/api/scan", {"path": "."}, headers={"Content-Type": "text/plain"})[0] == 415
    status, raw = _request(port, "POST", "/api/scan", {"path": "missing"})
    assert status == 400 and "no folder at missing" in json.loads(raw)["error"]
    status, raw = _request(port, "POST", "/api/scan", {"path": "notes"})
    assert status == 400 and "paste your dependency list" in json.loads(raw)["error"]
    assert _request(port, "GET", "/api/jobs/unknown")[0] == 404


def test_pasted_file_type_is_detected():
    assert web._pasted_name('{"dependencies": {}}') == "package.json"
    assert web._pasted_name('[project]\ndependencies = ["flask"]') == "pyproject.toml"
    assert web._pasted_name("flask==3.0.0") == "requirements.txt"
