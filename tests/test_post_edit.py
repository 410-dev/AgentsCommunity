import pytest

from app.models import McpKey, User
from app.roles import Role
from app.security import generate_mcp_key

from .test_mcp import Mcp


@pytest.fixture
def setup(world, db):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    world.user("john", dev)
    world.user("lead", dev, role=Role.DEPARTMENT_MODERATOR)
    world.mcp = {}
    for name in ("john", "lead"):
        u = db.query(User).filter_by(username=name).one()
        raw, prefix, digest = generate_mcp_key()
        db.add(McpKey(user_id=u.id, prefix=prefix, key_hash=digest))
        db.commit()
        world.mcp[name] = raw
    return world


def test_author_edits_post_via_mcp(client, setup):
    john = Mcp(client, setup.mcp["john"])
    post = john.call("create_post", board="developers", title="Draft", body="v1")["structuredContent"]
    assert post["updated_at"] is None

    edited = john.call("edit_post", post_id=post["id"], body="v2")["structuredContent"]
    assert edited["title"] == "Draft" and edited["body"] == "v2" and edited["updated_at"] is not None
    edited = john.call("edit_post", post_id=post["id"], title="Final")["structuredContent"]
    assert edited["title"] == "Final" and edited["body"] == "v2"

    assert john.call("edit_post", post_id=post["id"])["isError"] is True
    assert john.call("edit_post", post_id=post["id"], title="  ")["isError"] is True
    # Not even a moderator may rewrite someone else's post.
    res = Mcp(client, setup.mcp["lead"]).call("edit_post", post_id=post["id"], body="hijack")
    assert res["isError"] is True and "author" in res["content"][0]["text"]
    assert john.call("read_post", post_id=post["id"])["structuredContent"]["body"] == "v2"


def test_author_edits_post_via_api(client, setup):
    john, lead = setup.auth("john:laptop"), setup.auth("lead:laptop")
    pid = client.post("/api/v1/boards/developers/posts", json={"title": "t", "body": "b"}, headers=john).json()["id"]
    r = client.patch(f"/api/v1/posts/{pid}", json={"title": "t2"}, headers=john)
    assert r.status_code == 200 and r.json()["title"] == "t2" and r.json()["body"] == "b"
    assert client.patch(f"/api/v1/posts/{pid}", json={}, headers=john).status_code == 400
    assert client.patch(f"/api/v1/posts/{pid}", json={"body": "x"}, headers=lead).status_code == 403
    assert client.patch("/api/v1/posts/999", json={"body": "x"}, headers=john).status_code == 404
