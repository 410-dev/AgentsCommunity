import json

import httpx

from app import webhooks
from app.config import settings
from app.models import Machine, MessageDelivery, Post, Subscription, User
from app.roles import Role
from app.webhooks import build_request, process_pending, validate_webhook

from .conftest import csrf_of
from .test_mcp import Mcp


def _login(client, name, tenancy="example.com"):
    client.post("/login", data={"csrf_token": csrf_of(client.get("/login").text), "tenancy": tenancy,
                                "username": name, "password": "password123"})


def _inbox(client, world, who, **params):
    r = client.get("/api/v1/messages", params=params, headers=world.auth(who))
    assert r.status_code == 200, r.text
    return r.json()["messages"]


def _setup(world):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    ops = world.dept(ex, "ops")
    world.user("john", dev)
    world.user("jane", dev, machines=("laptop", "server"))
    world.user("olga", ops)
    return ex, dev, ops


def test_board_subscription_notifies_new_posts(client, world):
    _setup(world)
    r = client.post("/api/v1/boards/developers/subscription", headers=world.auth("jane:laptop"))
    assert r.status_code == 200 and r.json()["subscribed"] and r.json()["created"]
    assert r.json()["board"] == "developers:example.com" and r.json()["type"] == "board"
    # Subscribing twice is harmless.
    assert client.post("/api/v1/boards/developers:example.com/subscription",
                       headers=world.auth("jane:server")).json()["created"] is False

    post = client.post("/api/v1/boards/developers/posts", json={"title": "Release 1.2", "body": "배포 완료"},
                       headers=world.auth("john:laptop")).json()

    for machine in ("laptop", "server"):  # every machine of the subscriber, like <id>:*@…
        msgs = _inbox(client, world, f"jane:{machine}")
        assert len(msgs) == 1
        m = msgs[0]
        assert m["kind"] == "post" and m["post_id"] == post["id"] and "comment_id" not in m
        assert m["from"] == "subscription:notify@developers:example.com"
        assert m["subject"] == "New post on developers:example.com: Release 1.2"
        assert "배포 완료" in m["body"] and "Author: john:laptop@developers:example.com" in m["body"]
        assert f"http://testserver/boards/developers:example.com?open={post['id']}" in m["body"]
    assert _inbox(client, world, "john:laptop") == []  # the author is not notified

    # Comments do not notify board subscribers; other boards do not either.
    client.post(f"/api/v1/posts/{post['id']}/comments", json={"body": "nice"}, headers=world.auth("john:laptop"))
    client.post("/api/v1/boards/*/posts", json={"title": "elsewhere", "body": "x"}, headers=world.auth("john:laptop"))
    assert len(_inbox(client, world, "jane:laptop")) == 1

    subs = client.get("/api/v1/subscriptions", headers=world.auth("jane:laptop")).json()["subscriptions"]
    assert [(s["type"], s["board"]) for s in subs] == [("board", "developers:example.com")]

    r = client.delete("/api/v1/boards/developers/subscription", headers=world.auth("jane:laptop"))
    assert r.json() == {"type": "board", "board": "developers:example.com", "subscribed": False, "removed": True}
    client.post("/api/v1/boards/developers/posts", json={"title": "again", "body": "x"}, headers=world.auth("john:laptop"))
    assert len(_inbox(client, world, "jane:laptop")) == 1


def test_post_subscription_notifies_new_comments(client, world):
    _setup(world)
    post = client.post("/api/v1/boards/developers/posts", json={"title": "Question", "body": "how?"},
                       headers=world.auth("john:laptop")).json()
    assert client.post(f"/api/v1/posts/{post['id']}/subscription", headers=world.auth("john:laptop")).json()["subscribed"]
    client.post(f"/api/v1/posts/{post['id']}/subscription", headers=world.auth("jane:laptop"))

    c = client.post(f"/api/v1/posts/{post['id']}/comments", json={"body": "like this"},
                    headers=world.auth("jane:laptop")).json()
    msgs = _inbox(client, world, "john:laptop")
    assert len(msgs) == 1
    m = msgs[0]
    assert m["kind"] == "comment" and m["post_id"] == post["id"] and m["comment_id"] == c["id"]
    assert m["subject"] == f"New comment on #{post['id']}: Question" and "like this" in m["body"]
    assert _inbox(client, world, "jane:laptop") == []  # own comment

    client.delete(f"/api/v1/posts/{post['id']}/subscription", headers=world.auth("john:laptop"))
    client.post(f"/api/v1/posts/{post['id']}/comments", json={"body": "more"}, headers=world.auth("jane:laptop"))
    assert len(_inbox(client, world, "john:laptop")) == 1


def test_subscriptions_respect_board_access(client, world, db):
    _setup(world)
    # Users cannot subscribe to boards or posts they cannot read.
    assert client.post("/api/v1/boards/ops/subscription", headers=world.auth("john:laptop")).status_code == 404
    post = client.post("/api/v1/boards/ops/posts", json={"title": "ops only", "body": "x"},
                       headers=world.auth("olga:laptop")).json()
    assert client.post(f"/api/v1/posts/{post['id']}/subscription", headers=world.auth("john:laptop")).status_code == 404

    # A subscriber who loses access (moved to another department) is no longer notified.
    client.post("/api/v1/boards/developers/subscription", headers=world.auth("jane:laptop"))
    jane = db.query(User).filter_by(username="jane").one()
    jane.department_id = db.query(User).filter_by(username="olga").one().department_id
    db.commit()
    client.post("/api/v1/boards/developers/posts", json={"title": "secret", "body": "x"}, headers=world.auth("john:laptop"))
    assert _inbox(client, world, "jane:laptop") == []
    assert client.get("/api/v1/subscriptions", headers=world.auth("jane:laptop")).json()["subscriptions"] == []


def test_subscriptions_go_with_their_post(client, world, db):
    _setup(world)
    post = client.post("/api/v1/boards/developers/posts", json={"title": "t", "body": "b"},
                       headers=world.auth("john:laptop")).json()
    client.post(f"/api/v1/posts/{post['id']}/subscription", headers=world.auth("jane:laptop"))
    client.post(f"/api/v1/posts/{post['id']}/comments", json={"body": "c"}, headers=world.auth("john:laptop"))
    assert db.query(Subscription).count() == 1
    db.delete(db.get(Post, post["id"]))
    db.commit()
    assert db.query(Subscription).count() == 0
    # The notification stays in the inbox; its post link is cleared.
    msg = _inbox(client, world, "jane:laptop")[0]
    assert "post_id" not in msg and msg["kind"] == "comment"


def test_incoming_webhook_posts_notify_subscribers(client, world, db):
    from app.boards import resolve_board
    from app.models import BoardHook
    from app.security import hash_api_key

    _setup(world)
    board = resolve_board(db, db.query(User).filter_by(username="jane").one(), "developers")
    db.add(BoardHook(**board.ids(), name="ci", token_prefix="ach_x", token_hash=hash_api_key("ach_test"), secret="s"))
    db.commit()
    client.post("/api/v1/boards/developers/subscription", headers=world.auth("jane:laptop"))
    r = client.post("/hooks/ach_test", json={"title": "Deployed", "body": "v2 is live"})
    assert r.status_code == 201
    m = _inbox(client, world, "jane:laptop")[0]
    assert m["kind"] == "post" and m["post_id"] == r.json()["post_id"] and "ci:webhook@" in m["body"]


def test_web_ui_subscribe_toggle_and_account_list(client, world, db):
    _setup(world)
    _login(client, "jane")
    page = client.get("/boards/developers").text
    assert "Subscribe to board" in page
    client.post("/subscriptions/board", data={"csrf_token": csrf_of(page), "board": "developers:example.com",
                                              "action": "subscribe", "next": "/boards/developers:example.com"})
    page = client.get("/boards/developers").text
    assert "Subscribed to board" in page and 'name="action" value="unsubscribe"' in page

    post = client.post("/api/v1/boards/developers/posts", json={"title": "Hello", "body": "x"},
                       headers=world.auth("john:laptop")).json()
    page = client.get(f"/boards/developers:example.com?open={post['id']}").text
    assert "Subscribe to comments" in page
    client.post(f"/subscriptions/post/{post['id']}", data={"csrf_token": csrf_of(page), "action": "subscribe"})
    assert "Subscribed to comments" in client.get(f"/boards/developers:example.com?open={post['id']}").text

    account = client.get("/account").text
    assert "developers:example.com" in account and f"#{post['id']} Hello" in account
    assert "Subscribed" in client.get("/boards").text

    # The notification opens in the inbox with a link to the post and no reply action.
    d = db.query(MessageDelivery).filter_by(user_id=db.query(User).filter_by(username="jane").one().id).first()
    page = client.get(f"/messages?open={d.id}").text
    assert f'href="/posts/{post["id"]}"' in page and "Reply…" not in page

    sub = db.query(Subscription).filter_by(post_id=post["id"]).one()
    client.post(f"/subscriptions/{sub.id}/delete", data={"csrf_token": csrf_of(account)})
    assert db.query(Subscription).filter_by(post_id=post["id"]).count() == 0
    # Someone else's subscription cannot be removed.
    other = db.query(Subscription).first()
    _login(client, "john")
    r = client.post(f"/subscriptions/{other.id}/delete", data={"csrf_token": csrf_of(client.get("/account").text)})
    assert r.status_code == 404


def test_account_inbox_notification_and_discord_webhook(client, world, db, monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow_private_networks", True)
    _, dev, _ = _setup(world)
    world.user("hana", dev, machines=())  # a person without machines: account inbox
    _login(client, "hana")
    page = client.get("/account").text
    r = client.post("/account/webhook", data={"csrf_token": csrf_of(page), "webhook_format": "discord",
                                              "webhook_url": "http://discord.local/api/webhooks/1/abc"})
    assert r.status_code == 200
    hana = db.query(User).filter_by(username="hana").one()
    db.refresh(hana)
    assert hana.webhook_format == "discord" and hana.webhook_secret
    client.post("/subscriptions/board", data={"csrf_token": csrf_of(page), "board": "developers:example.com"})

    client.post("/api/v1/boards/developers/posts", json={"title": "@everyone outage", "body": "db is down"},
                headers=world.auth("john:laptop"))
    d = db.query(MessageDelivery).filter_by(user_id=hana.id).one()
    assert d.machine_id is None and d.webhook_status == "pending"

    calls = []
    http = httpx.Client(transport=httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(204)))
    assert process_pending(db, http) == 1
    db.refresh(d)
    assert d.webhook_status == "delivered"
    payload = json.loads(calls[0].content)
    assert set(payload) == {"content", "username", "allowed_mentions"}
    assert payload["allowed_mentions"] == {"parse": []}  # never pings the channel
    assert "@everyone outage" in payload["content"] and "db is down" in payload["content"]
    assert "X-Community-Signature" not in calls[0].headers

    # The account page shows the webhook, and a test message can be sent.
    sent = []
    monkeypatch.setattr(webhooks, "http_client", lambda: httpx.Client(
        transport=httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(204))))
    r = client.post("/account/webhook/test", data={"csrf_token": csrf_of(page)})
    assert "Test message delivered to the webhook." in r.text
    assert "Webhook test" in json.loads(sent[0].content)["content"]


def test_machine_webhook_formats(client, world, db, monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow_private_networks", True)
    _setup(world)
    _login(client, "jane")
    m = db.query(Machine).filter_by(name="laptop", user_id=db.query(User).filter_by(username="jane").one().id).one()
    page = client.get(f"/machines/{m.id}").text
    assert "Discord" in page and "Telegram" in page

    # Telegram needs the chat id in the URL.
    r = client.post(f"/machines/{m.id}/webhook", data={"csrf_token": csrf_of(page), "webhook_format": "telegram",
                                                       "webhook_url": "http://tg.local/bot123:abc/sendMessage"})
    assert "add ?chat_id=" in r.text
    url = "http://tg.local/bot123:abc/sendMessage?chat_id=-1001"
    client.post(f"/machines/{m.id}/webhook", data={"csrf_token": csrf_of(page), "webhook_format": "telegram",
                                                   "webhook_url": url})
    db.refresh(m)
    assert m.webhook_format == "telegram" and m.webhook_url == url

    client.post("/api/v1/messages", json={"to": "jane:laptop@developers:example.com", "subject": "ci", "body": "green"},
                headers=world.auth("john:laptop"))
    calls = []
    http = httpx.Client(transport=httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(200, json={"ok": True})))
    assert process_pending(db, http) == 1
    assert str(calls[0].url) == url
    payload = json.loads(calls[0].content)
    assert payload["chat_id"] == "-1001" and payload["text"].startswith("ci\nFrom: john:laptop@developers:example.com")

    # Failure text from the service is kept for the delivery.
    client.post("/api/v1/messages", json={"to": "jane:laptop@developers:example.com", "body": "x"},
                headers=world.auth("john:laptop"))
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(400, text='{"description":"chat not found"}')))
    process_pending(db, http)
    d = db.query(MessageDelivery).order_by(MessageDelivery.id.desc()).first()
    assert d.webhook_status == "pending" and "chat not found" in d.webhook_last_error

    assert validate_webhook("http://x.local/hook", "nope") == "Unknown webhook format."


def test_chat_payloads_are_clipped():
    msg = {"from": "a:b@c:d", "to": "x", "subject": "s", "body": "y" * 10_000}
    body, headers = build_request("discord", "https://discord.com/api/webhooks/1/x", None, "message.created", 1, "r", msg)
    assert len(json.loads(body)["content"]) == 2000 and "X-Community-Signature" not in headers
    body, _ = build_request("slack", "https://hooks.slack.com/x", None, "message.created", 1, "r", msg)
    assert set(json.loads(body)) == {"text"}
    body, _ = build_request("telegram", "https://api.telegram.org/bot1:x/sendMessage?chat_id=5", None,
                            "message.created", 1, "r", msg)
    assert len(json.loads(body)["text"]) == 4096
    body, headers = build_request("community", "https://example.com/h", "sec", "message.created", 7, "r", msg)
    assert json.loads(body)["delivery_id"] == 7 and headers["X-Community-Signature"].startswith("sha256=")


def test_mcp_subscription_tools(client, world, db):
    from app.models import McpKey
    from app.security import generate_mcp_key

    _setup(world)
    jane = db.query(User).filter_by(username="jane").one()
    raw, prefix, digest = generate_mcp_key()
    db.add(McpKey(user_id=jane.id, prefix=prefix, key_hash=digest))
    db.commit()
    m = Mcp(client, raw)

    assert m.call("subscribe")["isError"]  # needs board or post_id
    res = m.call("subscribe", board="developers")
    assert not res["isError"] and res["structuredContent"]["board"] == "developers:example.com"
    post = client.post("/api/v1/boards/developers/posts", json={"title": "via mcp", "body": "x"},
                       headers=world.auth("john:laptop")).json()
    assert m.call("subscribe", post_id=post["id"])["structuredContent"]["type"] == "post"
    subs = m.call("list_subscriptions")["structuredContent"]["subscriptions"]
    assert {s["type"] for s in subs} == {"board", "post"}
    inbox = m.call("inbox")["structuredContent"]["messages"]
    assert {i["kind"] for i in inbox} == {"post"} and len(inbox) == 2  # one per machine
    assert m.call("unsubscribe", board="developers")["structuredContent"]["removed"] is True
    assert len(m.call("list_subscriptions")["structuredContent"]["subscriptions"]) == 1


def test_admin_subscribes_without_department(client, world, db):
    """A global admin has no machines and no department; notifications land in the account inbox."""
    _setup(world)
    world.user("root", role=Role.ADMIN, machines=())
    _login(client, "root", tenancy="")
    page = client.get("/boards/developers:example.com").text
    client.post("/subscriptions/board", data={"csrf_token": csrf_of(page), "board": "developers:example.com"})
    client.post("/api/v1/boards/developers/posts", json={"title": "for admin", "body": "x"},
                headers=world.auth("john:laptop"))
    root = db.query(User).filter_by(username="root").one()
    d = db.query(MessageDelivery).filter_by(user_id=root.id).one()
    assert d.machine_id is None and d.message.kind == "post"
    assert "for admin" in client.get("/messages").text
