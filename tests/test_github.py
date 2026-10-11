import json

from breakwise import github
from breakwise.report import COMMENT_MARKER


def test_upsert_creates_then_updates(monkeypatch, tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    comments: list[dict] = [{"id": 1, "body": "hello"}]
    calls = []

    def fake_api(method, url, token, body=None):
        calls.append((method, url))
        if method == "GET":
            return list(comments)
        if method == "POST":
            comments.append({"id": 2, "body": body["body"]})
            return {"html_url": "https://github.com/o/r/pull/7#issuecomment-2"}
        if method == "PATCH":
            comments[1]["body"] = body["body"]
            return {"html_url": "https://github.com/o/r/pull/7#issuecomment-2"}

    monkeypatch.setattr(github, "_api", fake_api)
    assert github.find_comment() is None
    github.upsert_comment("### Breakwise: first")
    assert comments[1]["body"].startswith(COMMENT_MARKER)
    github.upsert_comment("### Breakwise: second")
    assert len(comments) == 2 and "second" in comments[1]["body"]
    assert calls[-1] == ("PATCH", "https://api.github.com/repos/o/r/issues/comments/2")
    assert github.find_comment()["id"] == 2
