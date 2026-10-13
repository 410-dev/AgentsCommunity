import pytest
from sqlalchemy import create_engine, inspect, text

from app.db import init_db
from app.models import McpKey, Message, MessageDelivery, User
from app.roles import Role
from app.security import generate_mcp_key

from .conftest import csrf_of
from .test_mcp import Mcp


@pytest.fixture
def setup(world, db):
    ex = world.tenancy("example.com", min_role_department_broadcast=int(Role.USER))
    dev = world.dept(ex, "developers")
    ops = world.dept(ex, "ops")
    world.user("lead", dev, role=Role.DEPARTMENT_MODERATOR)
    world.user("john", dev)
    world.user("nomad", dev, machines=())  # an account without machines
    world.user("bot", dev, machines=("worker",))
    world.user("opsbot", ops, machines=())
    for name in ("bot", "opsbot"):
        db.query(User).filter_by(username=name).one().is_agent = True
    db.commit()
    world.ex, world.dev = ex, dev
    return world


def _login(client, name):
    page = client.get("/login").text
    client.post("/login", data={"csrf_token": csrf_of(page), "tenancy": "example.com",
                                "username": name, "password": "password123"})
    return csrf_of(client.get("/messages").text)


def test_account_without_machines_receives_in_account_inbox(client, setup, db):
    john = setup.auth("john:laptop")
    r = client.post("/api/v1/messages", json={"to": "nomad:*@developers:example.com", "body": "hello"}, headers=john)
    assert r.status_code == 201 and r.json()["recipient_count"] == 1
    # Any machine name reaches the account while it has no machines.
    r = client.post("/api/v1/messages", json={"to": "nomad:laptop@developers:example.com", "body": "again"},
                    headers=john)
    assert r.status_code == 201 and r.json()["recipient_count"] == 1
    # A user with machines still needs an existing machine name.
    r = client.post("/api/v1/messages", json={"to": "bot:nope@developers:example.com", "body": "x"}, headers=john)
    assert r.status_code == 404
    # Broadcasts include account inboxes too (john's own laptop is excluded).
    r = client.post("/api/v1/messages", json={"to": "*:*@developers:example.com", "body": "all"}, headers=john)
    assert r.json()["recipient_count"] == 3  # lead:laptop, nomad (account), bot:worker
    nomad = db.query(User).filter_by(username="nomad").one()
    rows = db.query(MessageDelivery).filter_by(user_id=nomad.id).all()
    assert len(rows) == 3 and all(d.machine_id is None for d in rows)

    _login(client, "nomad")
    page = client.get("/messages").text
    assert "hello" in page and "again" in page and "(account)" in page
    assert '<span class="n">3</span>' in page  # unread badge counts the account inbox
    d = rows[0]
    page = client.get(f"/messages?open={d.id}").text
    assert "nomad:*@developers:example.com" in page and "(account inbox)" in page
    client.post(f"/messages/{d.id}/read", data={"csrf_token": csrf_of(page)})
    db.expire_all()
    assert db.get(MessageDelivery, d.id).read_at is not None
    assert "(account)" in client.get("/messages?machine=0").text


def test_account_without_machines_sends_as_account(client, setup, db):
    csrf = _login(client, "nomad")
    page = client.get("/messages/compose").text
    assert "You have no machines" in page and 'value="0"' in page
    r = client.post("/messages/compose", data={"csrf_token": csrf, "to": "john:*@developers:example.com",
                                                "subject": "hi", "body": "from my account"})
    assert "Message delivered to 1 inbox(es)." in r.text
    msg = db.query(Message).one()
    assert msg.sender_machine_id is None and msg.sender_address == "nomad:*@developers:example.com"
    assert "from my account" in client.get("/messages?box=sent").text
    inbox = client.get("/api/v1/messages", headers=setup.auth("john:laptop")).json()["messages"]
    assert [m["from"] for m in inbox] == ["nomad:*@developers:example.com"]
    # Sending as the account to the department does not echo back to the account inbox.
    r = client.post("/messages/compose", data={"csrf_token": csrf, "to": "*:*@developers:example.com", "body": "b"})
    assert "Message delivered to 3 inbox(es)." in r.text


def test_mcp_account_without_machines(client, setup, db):
    nomad = db.query(User).filter_by(username="nomad").one()
    raw, prefix, digest = generate_mcp_key()
    db.add(McpKey(user_id=nomad.id, prefix=prefix, key_hash=digest))
    db.commit()
    m = Mcp(client, raw)
    sent = m.call("send_message", to="john:*@developers:example.com", body="via mcp")["structuredContent"]
    assert sent["recipient_count"] == 1 and sent["from"] == "nomad:*@developers:example.com"
    client.post("/api/v1/messages", json={"to": "nomad:*@developers:example.com", "body": "reply"},
                headers=setup.auth("john:laptop"))
    assert m.call("whoami")["structuredContent"]["account_inbox_unread"] == 1
    inbox = m.call("inbox")["structuredContent"]["messages"]
    assert [(x["body"], x["machine"]) for x in inbox] == [("reply", "(account)")]
    assert m.call("mark_read", all=True)["structuredContent"]["updated"] == 1


def test_managers_read_agent_messages(client, setup, db):
    bot = db.query(User).filter_by(username="bot").one()
    john = db.query(User).filter_by(username="john").one()
    opsbot = db.query(User).filter_by(username="opsbot").one()
    client.post("/api/v1/messages", json={"to": "bot:worker@developers:example.com", "body": "task for bot"},
                headers=setup.auth("john:laptop"))
    client.post("/api/v1/messages", json={"to": "john:*@developers:example.com", "body": "bot result"},
                headers=setup.auth("bot:worker"))

    _login(client, "lead")
    page = client.get(f"/admin/users/{bot.id}").text
    assert f'href="/admin/users/{bot.id}/messages"' in page
    page = client.get(f"/admin/users/{bot.id}/messages").text
    assert "task for bot" in page and "Read-only view" in page and "/read" not in page
    assert "bot result" in client.get(f"/admin/users/{bot.id}/messages?box=sent").text
    delivery = db.query(MessageDelivery).filter_by(user_id=bot.id).one()
    page = client.get(f"/admin/users/{bot.id}/messages?open={delivery.id}").text
    assert "task for bot" in page and "Mark as read" not in page
    # Opening does not change the agent's read state, and the manager cannot mark it read.
    r = client.post(f"/messages/{delivery.id}/read", data={"csrf_token": csrf_of(page)})
    assert r.status_code == 404
    db.expire_all()
    assert db.get(MessageDelivery, delivery.id).read_at is None

    # Human accounts stay private; agents outside the manager's scope are not visible.
    assert client.get(f"/admin/users/{john.id}/messages").status_code == 400
    assert client.get(f"/admin/users/{opsbot.id}/messages").status_code == 404
    # The agent's own mailbox does not leak other accounts' messages.
    other = db.query(MessageDelivery).filter_by(user_id=john.id).one()
    page = client.get(f"/admin/users/{bot.id}/messages?open={other.id}").text
    assert 'class="reading"' not in page


def test_migration_adds_account_deliveries(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as conn:  # tables as created by the previous version
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64), display_name VARCHAR(128), "
                          "password_hash VARCHAR(255), role INTEGER, tenancy_id INTEGER, department_id INTEGER, "
                          "is_active BOOLEAN, is_agent BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME)"))
        conn.execute(text("CREATE TABLE machines (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), "
                          "name VARCHAR(64), description TEXT, webhook_url VARCHAR(2048), webhook_secret VARCHAR(128), "
                          "created_at DATETIME)"))
        conn.execute(text("CREATE TABLE messages (id INTEGER PRIMARY KEY, sender_machine_id INTEGER "
                          "REFERENCES machines(id) ON DELETE SET NULL, sender_address VARCHAR(512), target VARCHAR(512), "
                          "kind VARCHAR(16), subject VARCHAR(255), body TEXT, created_at DATETIME)"))
        conn.execute(text("CREATE TABLE message_deliveries (id INTEGER NOT NULL PRIMARY KEY, "
                          "message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE, "
                          "machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE, read_at DATETIME, "
                          "webhook_status VARCHAR(16) NOT NULL, webhook_attempts INTEGER NOT NULL, "
                          "webhook_next_at DATETIME, webhook_last_error TEXT, UNIQUE (message_id, machine_id))"))
        conn.execute(text("INSERT INTO users (id, username) VALUES (4, 'john')"))
        conn.execute(text("INSERT INTO machines (id, user_id, name) VALUES (9, 4, 'laptop')"))
        conn.execute(text("INSERT INTO messages (id, sender_machine_id, sender_address, target, kind, body) "
                          "VALUES (1, 9, 'a', 'b', 'direct', 'x')"))
        conn.execute(text("INSERT INTO message_deliveries (id, message_id, machine_id, webhook_status, webhook_attempts) "
                          "VALUES (2, 1, 9, 'none', 0)"))
    init_db(engine)
    init_db(engine)  # idempotent
    cols = {c["name"]: c for c in inspect(engine).get_columns("message_deliveries")}
    assert cols["machine_id"]["nullable"] and not cols["user_id"]["nullable"]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT user_id FROM message_deliveries WHERE id = 2")).scalar() == 4
        assert conn.execute(text("SELECT sender_user_id FROM messages WHERE id = 1")).scalar() == 4
