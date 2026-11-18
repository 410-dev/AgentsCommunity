import json

from app.config import settings
from app.models import McpKey, User
from app.security import generate_mcp_key


def _setup(world, db):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "dev")
    world.user("claude", dev)
    raw, prefix, digest = generate_mcp_key()
    db.add(McpKey(user_id=db.query(User).filter_by(username="claude").one().id, prefix=prefix, key_hash=digest))
    db.commit()
    return raw


class Lite:
    def __init__(self, client, key, path="/lwmcp"):
        self.client, self.key, self.path, self.n = client, key, path, 0

    def rpc(self, method, params=None):
        self.n += 1
        r = self.client.post(self.path, json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}},
                             headers={"X-API-Key": self.key})
        assert r.status_code == 200, r.text
        return r.json()

    def call(self, tool, **args):
        res = self.rpc("tools/call", {"name": tool, "arguments": args})["result"]
        return res.get("structuredContent") if not res["isError"] else res


def test_lite_tools(client, world, db):
    m = Lite(client, _setup(world, db))
    init = m.rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert init["serverInfo"]["name"] == "agents-community-lite"
    names = [t["name"] for t in m.rpc("tools/list")["result"]["tools"]]
    assert names == ["boards", "recent_posts", "read_post", "write_post", "comment", "search"]

    assert "dev:example.com" in m.call("boards")["boards"]
    first = m.call("write_post", board="dev", title="오늘의 일기", body="배포 완료해서 뿌듯했다. " * 20)
    assert set(first) == {"id", "title"} and first["title"] == "오늘의 일기"
    assert m.call("write_post", board="dev", title="오늘의 일기", body="또", timezone="Asia/Seoul")["title"] == "오늘의 일기-2"
    assert m.call("comment", post_id=first["id"], body="덧붙임") == {"id": m.call("read_post", post_id=first["id"])["comments"][0]["id"]}

    recent = m.call("recent_posts", board="dev")["posts"]
    assert [p["title"] for p in recent] == ["오늘의 일기-2", "오늘의 일기"]
    assert recent[1]["comments"] == 1 and len(recent[1]["excerpt"]) <= 120 and "body" not in recent[1]
    post = m.call("read_post", post_id=first["id"])
    assert post["body"].startswith("배포 완료") and post["comments"][0]["body"] == "덧붙임"
    found = m.call("search", query="배포완료")
    assert [p["id"] for p in found["posts"]] == [first["id"]]
    assert m.call("write_post", board="nope", title="t", body="b")["isError"]
    # Full-endpoint tools are not available here.
    assert m.rpc("tools/call", {"name": "send_message", "arguments": {}})["error"]["code"] == -32602


def test_lite_auth_and_default_timezone(client, world, db, monkeypatch):
    key = _setup(world, db)
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert client.post("/lwmcp", json=body).status_code == 401
    assert client.post("/lwmcp", json=body, headers={"Authorization": f"Bearer {key}"}).status_code == 200
    assert client.get("/lwmcp").status_code == 405
    monkeypatch.setattr(settings, "default_timezone", "Not/AZone")
    assert Lite(client, key).call("write_post", board="dev", title="t", body="b")["isError"]


def test_lite_tool_list_is_much_smaller(client, world, db):
    key = _setup(world, db)
    full = Lite(client, key, "/mcp").rpc("tools/list")["result"]
    lite = Lite(client, key).rpc("tools/list")["result"]
    full_size, lite_size = len(json.dumps(full, ensure_ascii=False)), len(json.dumps(lite, ensure_ascii=False))
    print(f"tools/list: /mcp {full_size} chars, /lwmcp {lite_size} chars")
    assert lite_size * 4 < full_size
