import textwrap

from breakwise import impact, usage
from breakwise.models import ChangeEntry, ChangeKind, Ecosystem


def _write(root, rel, code):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(code))


def test_python_usage_follows_imports_assignments_and_subclasses(tmp_path):
    _write(tmp_path, "app/http.py", """
        import requests as rq
        from urllib3.util.retry import Retry

        def make():
            retry = Retry(total=3, method_whitelist=["GET"])
            s = rq.Session()
            s.headers.update({"a": "b"})
            return retry.increment()
    """)
    _write(tmp_path, "app/models.py", """
        from pydantic import BaseModel

        class User(BaseModel):
            name: str
            class Config:
                orm_mode = True

        def dump(u: User):
            return u.dict()
    """)
    _write(tmp_path, ".venv/lib/site.py", "import requests\n")
    idx = usage.analyze(tmp_path, [("urllib3", Ecosystem.PYPI), ("requests", Ecosystem.PYPI), ("pydantic", Ecosystem.PYPI)])
    u3, rq, pd = idx[(Ecosystem.PYPI, "urllib3")], idx[(Ecosystem.PYPI, "requests")], idx[(Ecosystem.PYPI, "pydantic")]
    assert u3.files == ["app/http.py"] and rq.files == ["app/http.py"]
    assert "urllib3.util.retry.Retry.method_whitelist" in u3.resolved
    assert "urllib3.util.retry.Retry.increment" in u3.resolved
    assert "requests.Session.headers.update" in rq.resolved
    assert "pydantic.BaseModel.Config.orm_mode" in pd.resolved
    assert "pydantic.BaseModel.dict" in pd.resolved
    site = u3.resolved["urllib3.util.retry.Retry.method_whitelist"][0]
    assert site.file == "app/http.py" and "method_whitelist" in site.code


def test_js_usage(tmp_path):
    _write(tmp_path, "src/server.ts", """
        import express, { Router as R } from 'express';
        const { cookies } = require('next/headers');
        // app.del is commented out here: app.del('/x')
        const app = express();
        app.del('/user', (req, res) => res.sendfile('x'));
        const r = R();
        cookies().get('a');
    """)
    idx = usage.analyze(tmp_path, [("express", Ecosystem.NPM), ("next", Ecosystem.NPM)])
    ex, nx = idx[(Ecosystem.NPM, "express")], idx[(Ecosystem.NPM, "next")]
    assert ex.files == ["src/server.ts"]
    assert "express.del" in ex.resolved and "app.del" in ex.loose
    assert len(ex.loose["app.del"]) == 1  # the comment doesn't count
    assert "res.sendfile" in ex.loose
    assert "express.Router" in ex.resolved
    assert "next/headers.cookies" in nx.resolved


def _entry(text, symbols, kind=ChangeKind.BREAKING, version="2.0.0"):
    return ChangeEntry(version=version, text=text, kind=kind, symbols=symbols)


def test_matching_confidence(tmp_path):
    _write(tmp_path, "a.py", """
        from urllib3.util.retry import Retry
        r = Retry(method_whitelist=["GET"])
    """)
    idx = usage.analyze(tmp_path, [("urllib3", Ecosystem.PYPI)])[(Ecosystem.PYPI, "urllib3")]
    hit = impact.match_entry(_entry("Removed Retry option method_whitelist", ["Retry", "method_whitelist"]), idx)
    assert hit and hit.confidence == "high" and hit.matched_symbol == "method_whitelist"
    # Only the class matches, the entry is about another option: weaker.
    weak = impact.match_entry(_entry("Deprecated empty `allowed_methods`", ["Retry", "allowed_methods"], ChangeKind.DEPRECATION), idx)
    assert weak and weak.confidence in ("medium", "low")
    assert impact.match_entry(_entry("Removed `urllib3.contrib.appengine`", ["urllib3.contrib.appengine"]), idx) is None
    assert impact.match_entry(_entry("Added `foo`", ["Retry"], ChangeKind.FEATURE), idx) is None
    # The bare package name says nothing about which part of it.
    assert impact.match_entry(_entry("Install `urllib3[brotli]`", ["urllib3"]), idx) is None


def test_runtime_drop(tmp_path):
    rt = impact.Runtime(python=("3.8", "pyproject.toml"))
    idx = usage.UsageIndex(package="x", import_names=["x"])
    hit = impact.match_entry(_entry("Drop Python 3.8", ["@python:3.8"]), idx, rt)
    assert hit and hit.is_runtime and not hit.counts
    assert impact.match_entry(_entry("Drop Python 3.7", ["@python:3.7"]), idx, rt) is None
    impacts = impact.find_impacts([_entry("a", ["@python:3.8"], version="1.1"), _entry("b", ["@python:3.9"], version="1.2")], idx,
                                  impact.Runtime(python=("3.8", "pyproject.toml")))
    assert [i.entry.version for i in impacts] == ["1.2"]


def test_import_names():
    assert "yaml" in usage.import_names("pyyaml", Ecosystem.PYPI)
    assert "dotenv" in usage.import_names("python-dotenv", Ecosystem.PYPI)
    assert usage.import_names("@scope/pkg", Ecosystem.NPM) == ["@scope/pkg"]
