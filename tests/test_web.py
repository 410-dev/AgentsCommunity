import re

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Department, Tenancy, User
from app.roles import Role
from app.security import hash_password

from .conftest import csrf_of


def form_post(client, page, url, data):
    data = {**data, "csrf_token": csrf_of(client.get(page).text)}
    return client.post(url, data=data, follow_redirects=False)


def setup_super_admin(client):
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/setup"
    r = form_post(client, "/setup", "/setup",
                  {"username": "root", "password": "rootpass1", "password2": "rootpass1"})
    assert r.status_code == 303


def test_setup_requires_csrf_and_runs_once(client, db):
    r = client.post("/setup", data={"username": "root", "password": "rootpass1", "password2": "rootpass1"})
    assert r.status_code == 400
    setup_super_admin(client)
    assert db.query(User).filter_by(role=int(Role.SUPER_ADMIN)).count() == 1
    # A second attempt is redirected to login and creates nothing.
    client.cookies.clear()
    r = client.get("/setup", follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert db.query(User).count() == 1


def test_only_one_super_admin_in_db(db):
    for name in ("a", "b"):
        db.add(User(username=name, password_hash="x", role=int(Role.SUPER_ADMIN)))
    with pytest.raises(IntegrityError):
        db.commit()


def test_full_admin_flow_and_key_issue(client, db):
    setup_super_admin(client)
    r = form_post(client, "/admin/tenancies", "/admin/tenancies", {"name": "example.com"})
    assert r.status_code == 303
    t = db.query(Tenancy).one()
    form_post(client, f"/admin/tenancies/{t.id}", f"/admin/tenancies/{t.id}/departments", {"name": "developers"})
    d = db.query(Department).one()
    r = form_post(client, "/admin/users/new", "/admin/users", {
        "username": "john", "password": "johnpass1", "role": int(Role.DEPARTMENT_MODERATOR), "placement": f"d:{d.id}",
    })
    assert r.status_code == 303, r.text

    # Log in as john (tenancy-scoped login).
    client.post("/logout", data={"csrf_token": csrf_of(client.get("/").text)})
    r = form_post(client, "/login", "/login", {"tenancy": "example.com", "username": "john", "password": "johnpass1"})
    assert r.status_code == 303
    form_post(client, "/account", "/machines", {"name": "laptop"})
    john = db.query(User).filter_by(username="john").one()
    db.refresh(john)
    mid = john.machines[0].id
    r = form_post(client, f"/machines/{mid}", f"/machines/{mid}/keys", {"label": "ci"})
    key = re.search(r'data-api-key="([^"]+)"', r.text).group(1)
    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {key}"}).json()
    assert me["address"] == "john:laptop@developers:example.com"
    assert me["role"] == "Department Moderator"

    # Department moderator cannot create a Tenancy Moderator...
    r = form_post(client, "/admin/users/new", "/admin/users", {
        "username": "x", "password": "xpass1234", "role": int(Role.TENANCY_MODERATOR), "placement": f"t:{t.id}",
    })
    assert r.status_code == 400
    # ...but can create a User in their department.
    r = form_post(client, "/admin/users/new", "/admin/users", {
        "username": "jane", "password": "janepass1", "role": int(Role.USER), "placement": f"d:{d.id}",
    })
    assert r.status_code == 303

    # Revoking the key disables it.
    db.expire_all()
    kid = db.query(User).filter_by(username="john").one().machines[0].api_keys[0].id
    form_post(client, f"/machines/{mid}", f"/keys/{kid}/revoke", {})
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_department_moderator_scope(client, db):
    t = Tenancy(name="example.com")
    db.add(t)
    db.commit()
    dev, ops = Department(tenancy_id=t.id, name="dev"), Department(tenancy_id=t.id, name="ops")
    db.add_all([dev, ops])
    db.commit()
    db.add(User(username="root", password_hash=hash_password("x" * 8), role=int(Role.SUPER_ADMIN)))
    db.add(User(username="mod", password_hash=hash_password("modpass12"), role=int(Role.DEPARTMENT_MODERATOR),
                tenancy_id=t.id, department_id=dev.id))
    db.add(User(username="bob", password_hash=hash_password("bobpass12"), role=int(Role.USER),
                tenancy_id=t.id, department_id=ops.id))
    db.commit()
    bob = db.query(User).filter_by(username="bob").one()
    form_post(client, "/login", "/login", {"tenancy": "example.com", "username": "mod", "password": "modpass12"})
    assert client.get(f"/admin/users/{bob.id}").status_code == 404  # outside scope
    r = form_post(client, "/admin/users/new", "/admin/users",
                  {"username": "eve", "password": "evepass12", "role": int(Role.USER), "placement": f"d:{ops.id}"})
    assert r.status_code == 400
    assert client.get("/admin/tenancies").status_code == 200
    r = form_post(client, "/admin", f"/admin/tenancies/{t.id}/departments", {"name": "hack"})
    assert r.status_code == 403


def test_web_compose_and_board(client, db, world):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    world.user("root", role=Role.SUPER_ADMIN, machines=())
    world.user("john", dev, machines=("laptop", "server"))
    form_post(client, "/login", "/login", {"tenancy": "example.com", "username": "john", "password": "password123"})
    john = db.query(User).filter_by(username="john").one()
    laptop = next(m for m in john.machines if m.name == "laptop")
    r = form_post(client, "/messages/compose", "/messages/compose",
                  {"machine_id": laptop.id, "to": "john:*@developers:example.com", "body": "sync"})
    assert r.status_code == 303
    assert "sync" in client.get("/messages").text
    r = form_post(client, "/boards/developers:example.com", "/boards/developers:example.com/posts",
                  {"title": "Hello board", "body": "first"})
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    assert "Hello board" in page
