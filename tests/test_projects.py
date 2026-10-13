import pytest
from sqlalchemy import create_engine, inspect, text

from app.db import init_db
from app.models import Department, Project
from app.roles import Role

from .conftest import csrf_of


@pytest.fixture
def setup(world, db):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    ops = world.dept(ex, "ops")
    world.user("root", role=Role.SUPER_ADMIN, machines=())
    world.user("lead", dev, role=Role.DEPARTMENT_MODERATOR)
    world.user("john", dev)
    world.user("bob", ops)
    world.ex, world.dev = ex, dev
    return world


def _login(client, name):
    client.post("/login", data={"csrf_token": csrf_of(client.get("/login").text),
                                "tenancy": "" if name == "root" else "example.com",
                                "username": name, "password": "password123"})


def test_project_boards_via_web_and_api(client, setup, db):
    # Plain users cannot create project boards.
    _login(client, "john")
    page = client.get("/boards/developers:example.com").text
    assert "New project board" not in page
    r = client.post(f"/departments/{setup.dev.id}/projects", data={"csrf_token": csrf_of(page), "name": "x"})
    assert r.status_code == 403
    client.post("/logout", data={"csrf_token": csrf_of(page)})

    _login(client, "lead")
    page = client.get("/boards/developers:example.com").text
    r = client.post(f"/departments/{setup.dev.id}/projects",
                    data={"csrf_token": csrf_of(page), "name": "Pathway", "title": "Pathway platform"})
    assert r.status_code == 200 and str(r.url).endswith("/boards/developers/pathway:example.com")
    assert "Pathway platform" in r.text

    # API: the project board is listed, writable and separate from the department board.
    boards = client.get("/api/v1/boards", headers=setup.auth("john:laptop")).json()["boards"]
    assert [(b["key"], b["kind"]) for b in boards] == [
        ("*:example.com", "tenancy"), ("developers:example.com", "department"),
        ("developers/pathway:example.com", "project")]
    r = client.post("/api/v1/boards/developers/pathway/posts", json={"title": "Kickoff", "body": "..."},
                    headers=setup.auth("john:laptop"))
    assert r.status_code == 201 and r.json()["board"] == "developers/pathway:example.com"
    pid = r.json()["id"]
    # URL-encoded slash works too (what the CLI may send).
    posts = client.get("/api/v1/boards/developers%2Fpathway:example.com/posts", headers=setup.auth("john:laptop"))
    assert [p["id"] for p in posts.json()["posts"]] == [pid]
    assert client.get("/api/v1/boards/developers/posts", headers=setup.auth("john:laptop")).json()["posts"] == []
    # Other departments cannot see it.
    assert client.get("/api/v1/boards/developers/pathway:example.com/posts",
                      headers=setup.auth("bob:laptop")).status_code == 404
    assert client.get(f"/api/v1/posts/{pid}", headers=setup.auth("bob:laptop")).status_code == 404
    assert client.get("/api/v1/boards/*/pathway/posts", headers=setup.auth("john:laptop")).status_code == 404

    # Web: post page, comment and listing on the department board.
    r = client.get(f"/posts/{pid}")
    assert str(r.url).endswith(f"/boards/developers/pathway:example.com?open={pid}")
    r = client.post(f"/posts/{pid}/comments", data={"csrf_token": csrf_of(r.text), "body": "ok"})
    assert "ok" in r.text
    page = client.get("/boards/developers:example.com").text
    assert "developers/pathway:example.com" in page and "Kickoff" not in page

    # Rename, then delete (posts go with it).
    project = db.query(Project).one()
    r = client.post(f"/projects/{project.id}/edit", data={"csrf_token": csrf_of(page), "name": "pw", "title": "PW"})
    assert str(r.url).endswith("/boards/developers/pw:example.com")
    r = client.post(f"/projects/{project.id}/delete", data={"csrf_token": csrf_of(r.text)})
    assert str(r.url).endswith("/boards/developers:example.com")
    assert client.get(f"/api/v1/posts/{pid}", headers=setup.auth("john:laptop")).status_code == 404


def test_duplicate_and_invalid_project_names(client, setup):
    _login(client, "lead")
    page = client.get("/boards/developers").text
    url = f"/departments/{setup.dev.id}/projects"
    client.post(url, data={"csrf_token": csrf_of(page), "name": "alpha"})
    r = client.post(url, data={"csrf_token": csrf_of(page), "name": "alpha"})
    assert "already exists" in r.text
    r = client.post(url, data={"csrf_token": csrf_of(page), "name": "bad name!"})
    assert "Board name must be" in r.text


def test_department_rename(client, setup, db):
    client.post("/api/v1/boards/developers/posts", json={"title": "Old", "body": "b"},
                headers=setup.auth("john:laptop"))
    _login(client, "root")
    page = client.get(f"/admin/tenancies/{setup.ex.id}").text
    assert f'data-open="dlg-dept-{setup.dev.id}"' in page
    r = client.post(f"/admin/departments/{setup.dev.id}/edit",
                    data={"csrf_token": csrf_of(page), "name": "market-developers", "description": "Market"})
    assert "Department renamed from developers to market-developers" in r.text
    db.expire_all()
    assert db.get(Department, setup.dev.id).description == "Market"

    me = client.get("/api/v1/me", headers=setup.auth("john:laptop")).json()
    assert me["address"] == "john:laptop@market-developers:example.com"
    posts = client.get("/api/v1/boards/market-developers/posts", headers=setup.auth("john:laptop")).json()["posts"]
    assert [p["title"] for p in posts] == ["Old"] and posts[0]["board"] == "market-developers:example.com"
    r = client.post("/api/v1/messages", json={"to": "john:*@market-developers:example.com", "body": "x"},
                    headers=setup.auth("lead:laptop"))
    assert r.status_code == 201 and r.json()["recipient_count"] == 1

    # Name collisions and bad names are refused.
    page = client.get(f"/admin/tenancies/{setup.ex.id}").text
    r = client.post(f"/admin/departments/{setup.dev.id}/edit", data={"csrf_token": csrf_of(page), "name": "ops"})
    assert "already exists" in r.text
    r = client.post(f"/admin/departments/{setup.dev.id}/edit", data={"csrf_token": csrf_of(page), "name": "Bad Name"})
    assert "Department name must be" in r.text


def test_department_rename_needs_tenancy_manager(client, setup):
    _login(client, "lead")
    page = client.get("/boards").text
    r = client.post(f"/admin/departments/{setup.dev.id}/edit", data={"csrf_token": csrf_of(page), "name": "devs"})
    assert r.status_code == 403


def test_migration_adds_project_column(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as conn:  # posts table as created by the previous version
        conn.execute(text("CREATE TABLE posts (id INTEGER PRIMARY KEY, tenancy_id INTEGER, department_id INTEGER, "
                          "author_id INTEGER, author_address VARCHAR(512), title VARCHAR(255), body TEXT, "
                          "created_at DATETIME, updated_at DATETIME)"))
        conn.execute(text("INSERT INTO posts (id, tenancy_id, author_address, title, body) VALUES (1, 1, 'a', 't', 'b')"))
    init_db(engine)
    init_db(engine)  # idempotent
    assert "project_id" in {c["name"] for c in inspect(engine).get_columns("posts")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT project_id FROM posts WHERE id = 1")).scalar() is None


def test_tenancy_moderator_moves_posts(client, setup, db):
    setup.user("tmod", tenancy=setup.ex, role=Role.TENANCY_MODERATOR, machines=())
    other = setup.tenancy("other.org")
    sales = setup.dept(other, "sales")
    db.add(Project(department_id=setup.dev.id, name="pathway"))
    db.commit()
    r = client.post("/api/v1/boards/developers/posts", json={"title": "Misplaced", "body": "b"},
                    headers=setup.auth("john:laptop"))
    pid = r.json()["id"]
    client.post(f"/api/v1/posts/{pid}/comments", json={"body": "c1"}, headers=setup.auth("john:laptop"))

    # Plain users and department moderators cannot move posts, and see no move option.
    for name in ("john", "lead"):
        _login(client, name)
        page = client.get(f"/posts/{pid}").text
        assert 'data-open="dlg-move"' not in page
        r = client.post(f"/posts/{pid}/move", data={"csrf_token": csrf_of(page), "board": "*:example.com"})
        assert r.status_code == 403
        client.post("/logout", data={"csrf_token": csrf_of(page)})

    _login(client, "tmod")
    page = client.get(f"/posts/{pid}").text
    assert 'data-open="dlg-move"' in page and 'value="developers/pathway:example.com"' in page
    assert "sales:other.org" not in page
    r = client.post(f"/posts/{pid}/move", data={"csrf_token": csrf_of(page), "board": "developers/pathway:example.com"})
    assert str(r.url).endswith(f"/boards/developers/pathway:example.com?open={pid}")
    assert "Post moved from developers:example.com to developers/pathway:example.com." in r.text
    post = client.get(f"/api/v1/posts/{pid}", headers=setup.auth("john:laptop")).json()
    assert post["board"] == "developers/pathway:example.com" and [c["body"] for c in post["comments"]] == ["c1"]
    assert client.get("/api/v1/boards/developers/posts", headers=setup.auth("john:laptop")).json()["posts"] == []

    # To the ops board: john loses access, bob gains it.
    r = client.post(f"/posts/{pid}/move", data={"csrf_token": csrf_of(r.text), "board": "ops:example.com"})
    assert client.get(f"/api/v1/posts/{pid}", headers=setup.auth("john:laptop")).status_code == 404
    assert client.get(f"/api/v1/posts/{pid}", headers=setup.auth("bob:laptop")).status_code == 200
    # Other tenancies are out of reach for a Tenancy Moderator.
    r = client.post(f"/posts/{pid}/move", data={"csrf_token": csrf_of(r.text), "board": "sales:other.org"})
    assert r.status_code == 404
    client.post("/logout", data={"csrf_token": csrf_of(client.get("/boards").text)})

    # Admins can move across tenancies.
    _login(client, "root")
    page = client.get(f"/posts/{pid}").text
    r = client.post(f"/posts/{pid}/move", data={"csrf_token": csrf_of(page), "board": "sales:other.org"})
    assert str(r.url).endswith(f"/boards/sales:other.org?open={pid}")
    assert client.get(f"/api/v1/posts/{pid}", headers=setup.auth("bob:laptop")).status_code == 404
