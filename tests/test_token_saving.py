"""Options that keep agent tool calls small: summaries, unique titles, quiet results, saved CLI config."""

import json
from datetime import timedelta

from app.models import McpKey, Post, User
from app.security import generate_mcp_key

from .test_cli import _Forward, _load_cli
from .test_mcp import Mcp


def _setup(world):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    world.user("john", dev)
    return world.auth("john:laptop")


def _post(client, auth, title, board="developers", **extra):
    r = client.post(f"/api/v1/boards/{board}/posts", json={"title": title, "body": "x", **extra}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def test_unique_title_appends_a_counter_per_day(client, world, db):
    auth = _setup(world)
    assert _post(client, auth, "오늘의 일기", unique_title=True)["title"] == "오늘의 일기"
    assert _post(client, auth, "오늘의 일기", unique_title=True, timezone="Asia/Seoul")["title"] == "오늘의 일기-2"
    assert _post(client, auth, "오늘의 일기", unique_title=True)["title"] == "오늘의 일기-3"
    # Without the option titles repeat as before; other boards and other titles are independent.
    assert _post(client, auth, "오늘의 일기")["title"] == "오늘의 일기"
    assert _post(client, auth, "오늘의 일기", board="*", unique_title=True)["title"] == "오늘의 일기"
    assert _post(client, auth, "오늘의 일기-2 메모", unique_title=True)["title"] == "오늘의 일기-2 메모"
    # Yesterday's posts do not count.
    for p in db.query(Post).filter(Post.title.like("오늘의 일기%")):
        p.created_at = p.created_at - timedelta(days=2)
    db.commit()
    assert _post(client, auth, "오늘의 일기", unique_title=True)["title"] == "오늘의 일기"
    r = client.post("/api/v1/boards/developers/posts", json={"title": "t", "body": "x", "unique_title": True,
                                                             "timezone": "Mars/Base"}, headers=auth)
    assert r.status_code == 400 and "time zone" in r.json()["detail"]


def test_post_list_summary(client, world):
    auth = _setup(world)
    client.post("/api/v1/boards/developers/posts", json={"title": "long", "body": "가나다 " * 200}, headers=auth)
    full = client.get("/api/v1/boards/developers/posts", headers=auth).json()["posts"][0]
    short = client.get("/api/v1/boards/developers/posts?summary=true", headers=auth).json()["posts"][0]
    assert "body" in full and "excerpt" not in full
    assert "body" not in short and len(short["excerpt"]) == 120 and short["excerpt"].endswith("…")
    assert short["comment_count"] == 0 and short["title"] == "long"


def test_mcp_options(client, world, db):
    _setup(world)
    raw, prefix, digest = generate_mcp_key()
    db.add(McpKey(user_id=db.query(User).filter_by(username="john").one().id, prefix=prefix, key_hash=digest))
    db.commit()
    m = Mcp(client, raw)
    res = m.call("create_post", board="developers", title="일기", body="본문", quiet=True, unique_title=True)
    assert set(res["structuredContent"]) == {"id", "board", "title"}
    again = m.call("create_post", board="developers", title="일기", body="본문", quiet=True, unique_title=True,
                   timezone="Asia/Seoul")["structuredContent"]
    assert again["title"] == "일기-2"
    assert m.call("create_post", board="developers", title="t", body="b", unique_title=True,
                  timezone="nope/nope")["isError"]
    c = m.call("comment", post_id=again["id"], body="덧붙임", quiet=True)["structuredContent"]
    assert set(c) == {"id", "post_id"}
    posts = m.call("list_posts", board="developers", summary=True)["structuredContent"]["posts"]
    assert all("body" not in p and p["excerpt"] == "본문" for p in posts)


def test_cli_saved_config_compact_and_quiet(client, world, monkeypatch, capsys, tmp_path):
    cli = _load_cli()
    _setup(world)
    monkeypatch.setattr(cli.urllib.request, "urlopen", _Forward(client))
    monkeypatch.delenv("AC_BASE_URL", raising=False)
    monkeypatch.delenv("AC_API_KEY", raising=False)
    monkeypatch.setenv("AC_CONFIG", str(tmp_path / "cfg.json"))

    def run(*argv):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:  # missing configuration exits with 2
            code = exc.code
        return code, capsys.readouterr().out

    code, out = run("me")
    assert code == 2 and "community.py config" in out
    code, out = run("config", "--base-url", "http://testserver/", "--key", world.keys["john:laptop"],
                    "--timezone", "Asia/Seoul")
    assert code == 0 and json.loads(out)["api_key"].endswith("…")
    assert (tmp_path / "cfg.json").stat().st_mode & 0o777 == 0o600

    code, out = run("-c", "me")
    assert code == 0 and "\n" not in out.strip() and json.loads(out)["user"] == "john"
    code, out = run("post", "developers", "--title", "일기", "--body", "본문", "--unique-title", "--quiet", "-c")
    assert json.loads(out) == {"id": json.loads(out)["id"], "board": "developers:example.com", "title": "일기"}
    code, out = run("post", "developers", "--title", "일기", "--body", "본문", "--unique-title", "--quiet")
    post = json.loads(out)
    assert post["title"] == "일기-2"
    code, out = run("comment", str(post["id"]), "덧붙임", "--quiet")
    assert set(json.loads(out)) == {"id", "post_id"}
    code, out = run("posts", "developers", "--summary", "--limit", "5")
    assert all("body" not in p for p in json.loads(out)["posts"])

    # Saving compact output makes it the default.
    run("config", "--compact")
    code, out = run("me")
    assert "\n" not in out.strip()
