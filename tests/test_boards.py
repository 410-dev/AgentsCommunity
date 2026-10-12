import pytest


@pytest.fixture
def setup(world):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    ops = world.dept(ex, "ops")
    world.user("john", dev)
    world.user("bob", ops)
    world.ex, world.dev = ex, dev
    return world


def test_department_board_visibility(client, setup):
    r = client.post(
        "/api/v1/boards/developers/posts",
        json={"title": "Release notes", "body": "v1 is out"},
        headers=setup.auth("john:laptop"),
    )
    assert r.status_code == 201, r.text
    post = r.json()
    assert post["board"] == "developers:example.com"
    assert post["author"] == "john:laptop@developers:example.com"
    # Other departments cannot see the department board.
    assert client.get("/api/v1/boards/developers:example.com/posts", headers=setup.auth("bob:laptop")).status_code == 404
    assert client.get(f"/api/v1/posts/{post['id']}", headers=setup.auth("bob:laptop")).status_code == 404
    boards = {b["key"] for b in client.get("/api/v1/boards", headers=setup.auth("bob:laptop")).json()["boards"]}
    assert boards == {"*:example.com", "ops:example.com"}


def test_late_joiner_reads_history_and_comments(client, setup):
    r = client.post(
        "/api/v1/boards/*/posts", json={"title": "Welcome", "body": "rules..."}, headers=setup.auth("john:laptop")
    )
    pid = r.json()["id"]
    client.post(f"/api/v1/posts/{pid}/comments", json={"body": "thanks"}, headers=setup.auth("bob:laptop"))

    setup.user("newbie", setup.dev)  # joins after the post was written
    posts = client.get("/api/v1/boards/*:example.com/posts", headers=setup.auth("newbie:laptop")).json()["posts"]
    assert [p["title"] for p in posts] == ["Welcome"]
    assert posts[0]["comment_count"] == 1
    full = client.get(f"/api/v1/posts/{pid}", headers=setup.auth("newbie:laptop")).json()
    assert full["comments"][0]["author"] == "bob:laptop@ops:example.com"


def test_other_tenancy_cannot_read(client, setup):
    other = setup.tenancy("other.org")
    sales = setup.dept(other, "sales")
    setup.user("alice", sales)
    client.post("/api/v1/boards/*/posts", json={"title": "x", "body": "y"}, headers=setup.auth("john:laptop"))
    assert client.get("/api/v1/boards/*:example.com/posts", headers=setup.auth("alice:laptop")).status_code == 404
