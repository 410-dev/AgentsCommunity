from app.search import highlight, parse, snippet

from .conftest import csrf_of
from .test_mcp import Mcp


def _setup(client, world):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    ops = world.dept(ex, "ops")
    world.user("john", dev)
    world.user("jane", dev)
    world.user("olga", ops)
    j = world.auth("john:laptop")
    posts = {}
    for title, body in [
        ("배포 완료 보고", "오늘 v1.2 배포를 마쳤습니다."),
        ("Deployment checklist", "Run the migrations, then restart the workers."),
        ("Login bug", "The login page returns 500 when the session is empty."),
        ("Weekly notes", "Nothing about releases here. 100% done_ok"),
    ]:
        posts[title] = client.post("/api/v1/boards/developers/posts", json={"title": title, "body": body}, headers=j).json()
    client.post(f"/api/v1/posts/{posts['Login bug']['id']}/comments", json={"body": "재현 방법: 세션을 비우고 로그인"},
                headers=world.auth("jane:laptop"))
    client.post("/api/v1/boards/developers/tasks", json={"title": "캐시 무효화 작업", "description": "Redis 키 정리"},
                headers=j)
    client.post("/api/v1/boards/ops/posts", json={"title": "배포 서버 점검", "body": "ops only"},
                headers=world.auth("olga:laptop"))
    client.post("/api/v1/messages", json={"to": "jane:laptop@developers:example.com", "subject": "배포",
                                          "body": "스테이징 배포완료 했어요"}, headers=j)
    return posts


def _search(client, world, who, q, **params):
    r = client.get("/api/v1/search", params={"q": q, **params}, headers=world.auth(who))
    assert r.status_code == 200, r.text
    return r.json()["results"]


def _titles(results):
    return [p["title"] for p in results["posts"]]


def test_spacing_and_partial_words_do_not_matter(client, world):
    _setup(client, world)
    # "배포완료" (no space) finds "배포 완료"; "배포 완료" (space) finds "배포완료" in a message.
    assert _titles(_search(client, world, "jane:laptop", "배포완료")) == ["배포 완료 보고"]
    res = _search(client, world, "jane:laptop", "배포 완료")
    assert _titles(res) == ["배포 완료 보고"]
    assert [m["subject"] for m in res["messages"]] == ["배포"]
    # Partial words, any case, and spaces inside a word.
    assert _titles(_search(client, world, "jane:laptop", "deplo")) == ["Deployment checklist"]
    assert _titles(_search(client, world, "jane:laptop", "DEPLOYMENT CHECK")) == ["Deployment checklist"]
    assert _titles(_search(client, world, "jane:laptop", "dep loy")) == ["Deployment checklist"]
    assert _titles(_search(client, world, "jane:laptop", "배 포 완")) == ["배포 완료 보고"]
    # Every word must occur (anywhere, any order).
    assert _titles(_search(client, world, "jane:laptop", "500 login")) == ["Login bug"]
    assert _titles(_search(client, world, "jane:laptop", "login deployment")) == []


def test_comments_tasks_and_messages(client, world):
    posts = _setup(client, world)
    res = _search(client, world, "jane:laptop", "세션 비우고")
    assert [(c["post_id"], c["title"]) for c in res["comments"]] == [(posts["Login bug"]["id"], "Login bug")]
    assert "<" not in res["comments"][0]["snippet"]
    res = _search(client, world, "jane:laptop", "캐시무효화")
    assert [t["title"] for t in res["tasks"]] == ["캐시 무효화 작업"] and res["tasks"][0]["status"] == "roadmap"
    # Messages: only the caller's own (received or sent).
    msgs = _search(client, world, "jane:laptop", "스테이징")["messages"]
    assert len(msgs) == 1 and msgs[0]["box"] == "inbox" and msgs[0]["delivery_id"]
    assert _search(client, world, "john:laptop", "스테이징")["messages"][0]["box"] == "sent"
    assert _search(client, world, "olga:laptop", "스테이징")["messages"] == []


def test_only_readable_boards_are_searched(client, world):
    _setup(client, world)
    assert _titles(_search(client, world, "jane:laptop", "배포")) == ["배포 완료 보고"]
    assert _titles(_search(client, world, "olga:laptop", "배포")) == ["배포 서버 점검"]
    # Board filter and scopes.
    res = _search(client, world, "jane:laptop", "배포", board="developers")
    assert set(res) == {"posts", "comments", "tasks"}
    assert client.get("/api/v1/search", params={"q": "x", "board": "ops"}, headers=world.auth("jane:laptop")).status_code == 404
    res = _search(client, world, "jane:laptop", "배포", scope=["messages"])
    assert set(res) == {"messages"}


def test_like_wildcards_are_literal(client, world):
    _setup(client, world)
    assert _titles(_search(client, world, "jane:laptop", "%")) == ["Weekly notes"]
    assert _titles(_search(client, world, "jane:laptop", "done_ok")) == ["Weekly notes"]
    assert _titles(_search(client, world, "jane:laptop", "_")) == ["Weekly notes"]
    assert client.get("/api/v1/search", params={"q": ""}, headers=world.auth("jane:laptop")).status_code == 422


def test_whole_query_in_title_ranks_first(client, world):
    _setup(client, world)
    client.post("/api/v1/boards/developers/posts", json={"title": "Notes", "body": "release then deploy"},
                headers=world.auth("john:laptop"))
    titles = _titles(_search(client, world, "jane:laptop", "deploy"))
    assert titles[0] == "Deployment checklist" and "Notes" in titles


def test_snippets_and_highlight():
    q = parse("배포완료")
    text = "x" * 200 + " 스테이징 배포 완료 했어요 " + "y" * 200
    s = snippet(text, q)
    assert s.startswith("…") and s.endswith("…") and "배포 완료" in s
    assert [c for c, hit in highlight("오늘 배포 완료!", q) if hit] == ["배포 완료"]
    assert [c for c, hit in highlight("Deployment", parse("deplo")) if hit] == ["Deplo"]
    assert parse("  a   b ").terms == ["a", "b"] and parse("").empty


def test_web_search_page(client, world):
    _setup(client, world)
    client.post("/login", data={"csrf_token": csrf_of(client.get("/login").text), "tenancy": "example.com",
                                "username": "jane", "password": "password123"})
    page = client.get("/search", params={"q": "배포완료"}).text
    assert '<mark class="hit">배포 완료</mark>' in page and "스테이징" in page
    assert "배포 서버 점검" not in page  # ops board
    assert "Nothing found" in client.get("/search", params={"q": "zzzz"}).text
    assert client.get("/search").status_code == 200
    page = client.get("/search", params={"q": "배포", "board": "developers:example.com"}).text
    assert "</mark> 완료 보고" in page and "스테이징" not in page  # board search skips messages


def test_mcp_search(client, world, db):
    from app.models import McpKey, User
    from app.security import generate_mcp_key

    _setup(client, world)
    raw, prefix, digest = generate_mcp_key()
    db.add(McpKey(user_id=db.query(User).filter_by(username="jane").one().id, prefix=prefix, key_hash=digest))
    db.commit()
    m = Mcp(client, raw)
    res = m.call("search", query="배포 완료")["structuredContent"]["results"]
    assert [p["title"] for p in res["posts"]] == ["배포 완료 보고"] and len(res["messages"]) == 1
    res = m.call("search", query="login", scopes=["comments", "posts"])["structuredContent"]["results"]
    assert set(res) == {"comments", "posts"}
    assert m.call("search", query="x", scopes=["nope"])["isError"]
