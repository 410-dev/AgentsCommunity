import pytest

from app.models import McpKey, User
from app.roles import Role
from app.security import generate_mcp_key

from .conftest import csrf_of


@pytest.fixture
def setup(world, db):
    ex = world.tenancy("example.com", min_role_self_broadcast=int(Role.USER))
    dev = world.dept(ex, "developers")
    world.user("john", dev, machines=("laptop", "server"))
    world.user("jane", dev)
    world.mcp = {}
    for name in ("john", "jane"):
        u = db.query(User).filter_by(username=name).one()
        raw, prefix, digest = generate_mcp_key()
        db.add(McpKey(user_id=u.id, prefix=prefix, key_hash=digest))
        db.commit()
        world.mcp[name] = raw
    return world


class Mcp:
    def __init__(self, client, key):
        self.client, self.key, self.n = client, key, 0

    def rpc(self, method, params=None, status=200):
        self.n += 1
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}},
                             headers={"Authorization": f"Bearer {self.key}",
                                      "Accept": "application/json, text/event-stream"})
        assert r.status_code == status, r.text
        return r.json()

    def call(self, tool, **args):
        res = self.rpc("tools/call", {"name": tool, "arguments": args})["result"]
        return res


def test_handshake_and_tool_list(client, setup):
    m = Mcp(client, setup.mcp["john"])
    init = m.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "1"}})["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["serverInfo"]["name"] == "agents-community" and "tools" in init["capabilities"]
    assert m.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-06-18"
    # Notifications get 202 without a body.
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={"Authorization": f"Bearer {setup.mcp['john']}"})
    assert r.status_code == 202 and r.content == b""
    names = {t["name"] for t in m.rpc("tools/list")["result"]["tools"]}
    assert names == {"whoami", "send_message", "inbox", "mark_read", "list_boards", "list_posts", "read_post",
                     "create_post", "edit_post", "comment", "list_tasks", "create_task", "start_task", "report_task",
                     "complete_task"}
    assert m.rpc("nope")["error"]["code"] == -32601
    assert m.rpc("tools/call", {"name": "nope"})["error"]["code"] == -32602
    assert m.rpc("ping")["result"] == {}


def test_auth(client, setup):
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    r = client.post("/mcp", json=body)
    assert r.status_code == 401 and "Bearer" in r.headers["www-authenticate"]
    # Machine API keys are not MCP keys.
    assert client.post("/mcp", json=body, headers=setup.auth("john:laptop")).status_code == 401
    assert client.get("/mcp").status_code == 405
    r = client.post("/mcp", content=b"{", headers={"Authorization": f"Bearer {setup.mcp['john']}"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700


def test_messaging_through_mcp(client, setup):
    john, jane = Mcp(client, setup.mcp["john"]), Mcp(client, setup.mcp["jane"])
    me = john.call("whoami")["structuredContent"]
    assert [x["address"] for x in me["machines"]] == ["john:laptop@developers:example.com",
                                                     "john:server@developers:example.com"]

    # Several machines: the tool must be told which one to act as.
    res = john.call("send_message", to="jane:*@developers:example.com", body="hi")
    assert res["isError"] and "laptop" in res["content"][0]["text"]
    res = john.call("send_message", to="jane:*@developers:example.com", body="hi", subject="task", machine="server")
    assert not res["isError"] and res["structuredContent"]["from"] == "john:server@developers:example.com"
    assert res["structuredContent"]["recipient_count"] == 1

    # One machine: it is used implicitly.
    inbox = jane.call("inbox")["structuredContent"]["messages"]
    assert [(x["body"], x["machine"]) for x in inbox] == [("hi", "laptop")]
    assert jane.call("mark_read", ids=[inbox[0]["id"]])["structuredContent"]["updated"] == 1
    assert jane.call("inbox")["structuredContent"]["messages"] == []
    assert len(jane.call("inbox", unread_only=False)["structuredContent"]["messages"]) == 1

    # Policy errors come back as tool errors, not protocol errors.
    res = jane.call("send_message", to="*:*@developers:example.com", body="x")
    assert res["isError"] and "Department Moderator" in res["content"][0]["text"]

    # Own-device message reaches the other machine; inbox without `machine` covers all machines.
    john.call("send_message", to="john:*@developers:example.com", body="sync", machine="laptop")
    got = john.call("inbox")["structuredContent"]["messages"]
    assert [(x["body"], x["machine"]) for x in got] == [("sync", "server")]
    assert john.call("inbox", machine="laptop")["structuredContent"]["messages"] == []


def test_boards_through_mcp(client, setup):
    john = Mcp(client, setup.mcp["john"])
    boards = john.call("list_boards")["structuredContent"]["boards"]
    assert [b["key"] for b in boards] == ["*:example.com", "developers:example.com"]
    post = john.call("create_post", board="developers", title="Hello", body="안녕하세요 🙂")["structuredContent"]
    # No machine given and several machines: the account address is shown.
    assert post["author"] == "john:*@developers:example.com"
    c = john.call("comment", post_id=post["id"], body="first", machine="laptop")["structuredContent"]
    assert c["author"] == "john:laptop@developers:example.com"
    full = john.call("read_post", post_id=post["id"])["structuredContent"]
    assert full["body"] == "안녕하세요 🙂" and [x["body"] for x in full["comments"]] == ["first"]
    listed = john.call("list_posts", board="developers:example.com")["structuredContent"]["posts"]
    assert listed[0]["comment_count"] == 1
    assert john.call("list_posts", board="ops")["isError"]
    assert john.call("read_post", post_id=9999)["isError"]


def test_issue_and_revoke_mcp_key_in_ui(client, world, db):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    world.user("root", role=Role.SUPER_ADMIN, machines=())
    world.user("john", dev)
    client.post("/login", data={"csrf_token": csrf_of(client.get("/login").text), "tenancy": "example.com",
                                "username": "john", "password": "password123"})
    page = client.get("/account").text
    assert "MCP keys" in page
    r = client.post("/account/mcp-keys", data={"csrf_token": csrf_of(page), "label": "claude"})
    assert r.status_code == 200
    raw = r.text.split('data-api-key="')[1].split('"')[0]
    assert raw.startswith("acm_") and "claude mcp add --transport http --scope user" in r.text

    m = Mcp(client, raw)
    assert m.call("whoami")["structuredContent"]["user"] == "john"
    key = db.query(McpKey).one()
    assert key.label == "claude"
    client.post(f"/mcp-keys/{key.id}/revoke", data={"csrf_token": csrf_of(client.get("/account").text)})
    m.rpc("ping", status=401)
