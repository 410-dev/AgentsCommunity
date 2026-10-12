import pytest

from app.roles import Role


@pytest.fixture
def setup(world):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    ops = world.dept(ex, "ops")
    world.user("john", dev, machines=("laptop", "server"))
    world.user("jane", dev)
    world.user("mod", dev, role=Role.DEPARTMENT_MODERATOR)
    world.user("tmod", ops, role=Role.TENANCY_MODERATOR)
    world.user("bob", ops)
    other = world.tenancy("other.org")
    sales = world.dept(other, "sales")
    world.user("alice", sales)
    return world


def send(client, world, who, to, body="hi"):
    return client.post("/api/v1/messages", json={"to": to, "body": body}, headers=world.auth(who))


def inbox(client, world, who, **params):
    r = client.get("/api/v1/messages", params=params, headers=world.auth(who))
    assert r.status_code == 200
    return r.json()["messages"]


def test_me(client, setup):
    r = client.get("/api/v1/me", headers=setup.auth("john:laptop"))
    assert r.json()["address"] == "john:laptop@developers:example.com"
    assert client.get("/api/v1/me").status_code == 401
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_direct_message_and_read_state(client, setup):
    r = send(client, setup, "john:laptop", "jane:laptop@developers:example.com", "hello jane")
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "direct" and r.json()["recipient_count"] == 1
    msgs = inbox(client, setup, "jane:laptop", unread_only=True)
    assert [m["body"] for m in msgs] == ["hello jane"]
    assert msgs[0]["from"] == "john:laptop@developers:example.com"

    mid = msgs[0]["id"]
    assert client.post(f"/api/v1/messages/{mid}/read", headers=setup.auth("jane:laptop")).status_code == 200
    assert inbox(client, setup, "jane:laptop", unread_only=True) == []
    assert len(inbox(client, setup, "jane:laptop")) == 1
    # Other machines cannot see or ack it.
    assert client.get(f"/api/v1/messages/{mid}", headers=setup.auth("john:server")).status_code == 404


def test_message_to_own_other_machine(client, setup):
    r = send(client, setup, "john:laptop", "john:server@developers:example.com")
    assert r.status_code == 201
    assert len(inbox(client, setup, "john:server")) == 1


def test_self_broadcast_excludes_sender(client, setup):
    r = send(client, setup, "john:laptop", "john:*@developers:example.com")
    assert r.status_code == 201 and r.json()["kind"] == "self"
    assert r.json()["recipient_count"] == 1
    assert len(inbox(client, setup, "john:server")) == 1
    assert inbox(client, setup, "john:laptop") == []


def test_department_broadcast_policy(client, setup):
    target = "*:*@developers:example.com"
    r = send(client, setup, "john:laptop", target)
    assert r.status_code == 403  # default policy: Department Moderator or higher
    r = send(client, setup, "mod:laptop", target)
    assert r.status_code == 201 and r.json()["kind"] == "department"
    assert r.json()["recipient_count"] == 3  # john x2 + jane, not mod's own machine
    # Department Moderators cannot broadcast to other departments.
    assert send(client, setup, "mod:laptop", "*:*@ops:example.com").status_code == 403
    # Tenancy Moderators can.
    assert send(client, setup, "tmod:laptop", target).status_code == 201


def test_tenancy_broadcast(client, setup):
    assert send(client, setup, "mod:laptop", "*:*@*:example.com").status_code == 403
    r = send(client, setup, "tmod:laptop", "*:*@*:example.com")
    assert r.status_code == 201 and r.json()["recipient_count"] == 5
    assert len(inbox(client, setup, "bob:laptop")) == 1
    assert inbox(client, setup, "alice:laptop") == []
    # Cannot broadcast into another tenancy.
    assert send(client, setup, "tmod:laptop", "*:*@*:other.org").status_code == 403


def test_all_tenancy_broadcast_always_blocked(client, setup):
    for target in ("*:*@*:*", "*:*@developers:*", "jane:laptop@developers:*"):
        assert send(client, setup, "tmod:laptop", target).status_code == 403, target


@pytest.mark.parametrize(
    "target",
    ["*:laptop@developers:example.com", "john:*@*:example.com", "nonsense", "a:b@c", "John Doe:x@d:example.com"],
)
def test_invalid_patterns(client, setup, target):
    assert send(client, setup, "john:laptop", target).status_code == 400


def test_unknown_targets(client, setup):
    assert send(client, setup, "john:laptop", "ghost:laptop@developers:example.com").status_code == 404
    assert send(client, setup, "john:laptop", "jane:phone@developers:example.com").status_code == 404
    assert send(client, setup, "john:laptop", "jane:laptop@ops:example.com").status_code == 404
    assert send(client, setup, "john:laptop", "jane:laptop@developers:nowhere.net").status_code == 404


def test_cross_tenancy_dm_requires_both_sides(client, setup, db):
    from app.models import Tenancy

    target = "alice:laptop@sales:other.org"
    assert send(client, setup, "john:laptop", target).status_code == 403
    ex = db.query(Tenancy).filter_by(name="example.com").one()
    other = db.query(Tenancy).filter_by(name="other.org").one()
    ex.allow_cross_tenancy_dm = True
    db.commit()
    assert send(client, setup, "john:laptop", target).status_code == 403
    other.allow_cross_tenancy_dm = True
    db.commit()
    assert send(client, setup, "john:laptop", target).status_code == 201
    assert len(inbox(client, setup, "alice:laptop")) == 1


def test_disabled_policy(client, setup, db):
    from app.models import Tenancy

    ex = db.query(Tenancy).filter_by(name="example.com").one()
    ex.min_role_self_broadcast = None
    ex.min_role_department_broadcast = int(Role.USER)
    db.commit()
    assert send(client, setup, "john:laptop", "john:*@developers:example.com").status_code == 403
    assert send(client, setup, "john:laptop", "*:*@developers:example.com").status_code == 201


def test_bulk_mark_read_and_sent(client, setup):
    for i in range(3):
        send(client, setup, "john:laptop", "jane:laptop@developers:example.com", f"m{i}")
    ids = [m["id"] for m in inbox(client, setup, "jane:laptop")]
    r = client.post("/api/v1/messages/read", json={"ids": ids[:2]}, headers=setup.auth("jane:laptop"))
    assert r.json()["updated"] == 2
    assert [m["body"] for m in inbox(client, setup, "jane:laptop", unread_only=True)] == ["m2"]
    r = client.post("/api/v1/messages/read", json={"all": True}, headers=setup.auth("jane:laptop"))
    assert r.json()["updated"] == 1
    sent = client.get("/api/v1/messages/sent", headers=setup.auth("john:laptop")).json()["messages"]
    assert len(sent) == 3
    assert inbox(client, setup, "jane:laptop", after_id=ids[1])[0]["body"] == "m2"


def test_deactivated_user_key_stops_working(client, setup, db):
    from app.models import User

    john = db.query(User).filter_by(username="john").one()
    john.is_active = False
    db.commit()
    assert client.get("/api/v1/me", headers=setup.auth("john:laptop")).status_code == 401
    # And they no longer receive broadcasts.
    r = send(client, setup, "mod:laptop", "*:*@developers:example.com")
    assert r.json()["recipient_count"] == 1
