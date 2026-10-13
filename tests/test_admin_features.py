import pytest
from sqlalchemy import create_engine, inspect, text

from app.db import init_db
from app.models import ApiKey, Comment, Department, Machine, McpKey, Post, Project, User
from app.roles import Role

from .conftest import csrf_of


@pytest.fixture
def setup(world, db):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    ops = world.dept(ex, "ops")
    world.user("root", role=Role.SUPER_ADMIN, machines=())
    world.user("tmod", tenancy=ex, role=Role.TENANCY_MODERATOR, machines=())
    world.user("lead", dev, role=Role.DEPARTMENT_MODERATOR)
    world.user("john", dev)
    world.user("bob", ops)
    world.ex, world.dev, world.ops = ex, dev, ops
    return world


def _login(client, name):
    page = client.get("/login").text
    client.post("/login", data={"csrf_token": csrf_of(page), "tenancy": "" if name == "root" else "example.com",
                                "username": name, "password": "password123"})
    return csrf_of(client.get("/boards").text)


def _logout(client):
    client.post("/logout", data={"csrf_token": csrf_of(client.get("/boards").text)})


# ---- agent accounts ---------------------------------------------------------


def test_agent_account_credentials_are_managed_by_moderators(client, setup, db):
    csrf = _login(client, "lead")
    r = client.post("/admin/users", data={"csrf_token": csrf, "username": "bot", "password": "password123",
                                          "role": int(Role.USER), "placement": f"d:{setup.dev.id}", "is_agent": "true"})
    bot = db.query(User).filter_by(username="bot").one()
    assert bot.is_agent and str(r.url).endswith(f"/admin/users/{bot.id}")
    assert 'data-open="dlg-mcpkey"' in r.text

    # Register a machine for the agent, issue and revoke an API key.
    r = client.post(f"/admin/users/{bot.id}/machines", data={"csrf_token": csrf, "name": "worker-1"})
    m = db.query(Machine).filter_by(user_id=bot.id).one()
    assert str(r.url).endswith(f"/machines/{m.id}") and "agent account bot" in r.text
    r = client.post(f"/machines/{m.id}/keys", data={"csrf_token": csrf, "label": "ci"})
    raw = r.text.split('data-api-key="')[1].split('"')[0]
    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {raw}"}).json()
    assert me["address"] == "bot:worker-1@developers:example.com"
    key = db.query(ApiKey).filter_by(machine_id=m.id).one()
    r = client.post(f"/keys/{key.id}/revoke", data={"csrf_token": csrf})
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {raw}"}).status_code == 401

    # MCP keys: issue, then revoke from the admin page.
    r = client.post(f"/admin/users/{bot.id}/mcp-keys", data={"csrf_token": csrf, "label": "claude"})
    assert "MCP key created" in r.text
    mk = db.query(McpKey).filter_by(user_id=bot.id).one()
    r = client.post(f"/mcp-keys/{mk.id}/revoke", data={"csrf_token": csrf})
    assert str(r.url).endswith(f"/admin/users/{bot.id}#mcp")
    db.expire_all()
    assert db.get(McpKey, mk.id).revoked_at is not None

    # Deleting the machine returns to the agent's page.
    r = client.post(f"/machines/{m.id}/delete", data={"csrf_token": csrf})
    assert str(r.url).endswith(f"/admin/users/{bot.id}")

    # A human account's machines and keys stay private, even for its manager.
    john = db.query(User).filter_by(username="john").one()
    assert client.get(f"/machines/{john.machines[0].id}").status_code == 404
    assert client.post(f"/admin/users/{john.id}/mcp-keys", data={"csrf_token": csrf}).status_code == 400
    assert client.post(f"/admin/users/{john.id}/machines", data={"csrf_token": csrf, "name": "x"}).status_code == 400
    page = client.get(f"/admin/users/{john.id}").text
    assert 'data-open="dlg-mcpkey"' not in page

    # The users list can be split by account type.
    page = client.get("/admin/users?kind=agent").text
    assert ">bot<" in page and ">john<" not in page
    page = client.get("/admin/users?kind=human").text
    assert ">bot<" not in page and ">john<" in page


def test_agent_account_outside_scope_is_not_manageable(client, setup, db):
    setup.user("opsbot", setup.ops)
    bot = db.query(User).filter_by(username="opsbot").one()
    bot.is_agent = True
    db.commit()
    csrf = _login(client, "lead")  # moderator of developers, not ops
    assert client.get(f"/machines/{bot.machines[0].id}").status_code == 404
    assert client.post(f"/admin/users/{bot.id}/mcp-keys", data={"csrf_token": csrf}).status_code == 404


# ---- built-in boards on/off ------------------------------------------------


def test_builtin_boards_can_be_switched_off(client, setup, db):
    db.add(Project(department_id=setup.dev.id, name="pathway"))
    db.commit()
    john = setup.auth("john:laptop")
    pid = client.post("/api/v1/boards/developers/posts", json={"title": "Old", "body": "b"}, headers=john).json()["id"]

    csrf = _login(client, "root")
    client.post(f"/admin/tenancies/{setup.ex.id}/policy", data={"csrf_token": csrf, "board_setting": "true"})
    client.post(f"/admin/departments/{setup.dev.id}/edit",
                data={"csrf_token": csrf, "name": "developers", "board_setting": "true"})

    keys = [b["key"] for b in client.get("/api/v1/boards", headers=john).json()["boards"]]
    assert keys == ["developers/pathway:example.com"]
    assert client.post("/api/v1/boards/*/posts", json={"title": "t", "body": "b"}, headers=john).status_code == 404
    assert client.get("/api/v1/boards/developers/posts", headers=john).status_code == 404
    assert client.get(f"/api/v1/posts/{pid}", headers=john).status_code == 404
    assert client.get("/boards/developers:example.com").status_code == 404
    assert client.post("/api/v1/boards/developers/pathway/posts", json={"title": "t", "body": "b"},
                       headers=john).status_code == 201
    # ops keeps its own department board.
    keys = [b["key"] for b in client.get("/api/v1/boards", headers=setup.auth("bob:laptop")).json()["boards"]]
    assert keys == ["ops:example.com"]

    # Forms without the switch (e.g. older clients) leave it alone; turning it on brings the posts back.
    client.post(f"/admin/departments/{setup.dev.id}/edit", data={"csrf_token": csrf, "name": "developers"})
    assert client.get("/api/v1/boards/developers/posts", headers=john).status_code == 404
    client.post(f"/admin/departments/{setup.dev.id}/edit",
                data={"csrf_token": csrf, "name": "developers", "board_setting": "true", "board_enabled": "true"})
    posts = client.get("/api/v1/boards/developers/posts", headers=john).json()["posts"]
    assert [p["id"] for p in posts] == [pid]


# ---- tenancy-wide created boards -------------------------------------------


def test_tenancy_scoped_project_boards(client, setup, db):
    csrf = _login(client, "lead")
    r = client.post("/projects", data={"csrf_token": csrf, "scope": f"t:{setup.ex.id}", "name": "x"})
    assert r.status_code == 403
    _logout(client)

    csrf = _login(client, "tmod")
    assert f'value="t:{setup.ex.id}"' in client.get("/boards").text
    r = client.post("/projects", data={"csrf_token": csrf, "scope": f"t:{setup.ex.id}", "name": "townhall",
                                       "title": "Town hall"})
    assert str(r.url).endswith("/boards/*/townhall:example.com") and "Town hall" in r.text
    r = client.post("/projects", data={"csrf_token": csrf, "scope": f"t:{setup.ex.id}", "name": "townhall"})
    assert "already exists in this tenancy" in r.text
    # A department board of the same name is a different board.
    r = client.post("/projects", data={"csrf_token": csrf, "scope": f"d:{setup.dev.id}", "name": "townhall"})
    assert str(r.url).endswith("/boards/developers/townhall:example.com")
    assert "*/townhall:example.com" in client.get("/boards/*:example.com").text

    # Everyone in the tenancy can use it.
    for who in ("john:laptop", "bob:laptop"):
        boards = client.get("/api/v1/boards", headers=setup.auth(who)).json()["boards"]
        assert {"key": "*/townhall:example.com", "kind": "project"}.items() <= next(
            b for b in boards if b["key"] == "*/townhall:example.com").items()
    r = client.post("/api/v1/boards/*/townhall/posts", json={"title": "Hi", "body": "all"},
                    headers=setup.auth("bob:laptop"))
    assert r.status_code == 201 and r.json()["board"] == "*/townhall:example.com"
    assert client.get(f"/api/v1/posts/{r.json()['id']}", headers=setup.auth("john:laptop")).status_code == 200

    # It survives the tenancy board being switched off, and can be renamed and deleted.
    client.post(f"/admin/tenancies/{setup.ex.id}/policy", data={"csrf_token": csrf, "board_setting": "true"})
    assert client.get("/api/v1/boards/*/townhall/posts", headers=setup.auth("bob:laptop")).status_code == 200
    project = db.query(Project).filter_by(department_id=None).one()
    r = client.post(f"/projects/{project.id}/edit", data={"csrf_token": csrf, "name": "hall"})
    assert str(r.url).endswith("/boards/*/hall:example.com")
    r = client.post(f"/projects/{project.id}/delete", data={"csrf_token": csrf})
    assert str(r.url).endswith("/boards")  # the tenancy board is off
    assert db.query(Post).count() == 0


# ---- batch management ------------------------------------------------------


def test_batch_delete_posts_and_comments(client, setup, db):
    john, bob = setup.auth("john:laptop"), setup.auth("bob:laptop")
    p1 = client.post("/api/v1/boards/*/posts", json={"title": "j1", "body": "b"}, headers=john).json()["id"]
    p2 = client.post("/api/v1/boards/*/posts", json={"title": "b1", "body": "b"}, headers=bob).json()["id"]
    c1 = client.post(f"/api/v1/posts/{p2}/comments", json={"body": "mine"}, headers=john).json()["id"]
    c2 = client.post(f"/api/v1/posts/{p2}/comments", json={"body": "his"}, headers=bob).json()["id"]

    # A plain user only gets their own posts and comments in the batch dialogs.
    csrf = _login(client, "john")
    page = client.get("/boards/*:example.com").text
    assert 'id="dlg-batch-posts"' in page and f'name="ids" value="{p1}"' in page and f'name="ids" value="{p2}"' not in page
    page = client.get(f"/boards/*:example.com?open={p2}").text
    assert f'name="ids" value="{c1}"' in page and f'name="ids" value="{c2}"' not in page

    r = client.post("/comments/batch-delete", data={"csrf_token": csrf, "ids": [c1, c2],
                                                    "next": f"/boards/*:example.com?open={p2}"})
    assert "Deleted 1 of 2 selected item(s)" in r.text and str(r.url).endswith(f"open={p2}")
    assert [c.id for c in db.query(Comment)] == [c2]
    r = client.post("/posts/batch-delete", data={"csrf_token": csrf, "ids": [p1, p2], "next": "//evil.example"})
    assert str(r.url).endswith("/boards")  # unsafe next is ignored
    assert [p.id for p in db.query(Post)] == [p2]
    _logout(client)

    # A Tenancy Moderator may delete everyone's.
    csrf = _login(client, "tmod")
    r = client.post("/posts/batch-delete", data={"csrf_token": csrf, "ids": [p2], "next": "/boards/*:example.com"})
    assert "Deleted 1 item(s)." in r.text
    assert db.query(Post).count() == 0 and db.query(Comment).count() == 0


def test_batch_delete_boards(client, setup, db):
    db.add_all([Project(department_id=setup.dev.id, name="a"), Project(department_id=setup.ops.id, name="b")])
    db.commit()
    ids = [p.id for p in db.query(Project).order_by(Project.name)]
    csrf = _login(client, "lead")
    page = client.get("/boards").text
    assert 'id="dlg-batch-boards"' in page and f'value="{ids[0]}"' in page
    r = client.post("/projects/batch-delete", data={"csrf_token": csrf, "ids": ids})
    assert "Deleted 1 of 2" in r.text
    assert [p.name for p in db.query(Project)] == ["b"]


def test_batch_delete_departments(client, setup, db):
    empty = setup.dept(setup.ex, "empty")
    csrf = _login(client, "tmod")
    page = client.get(f"/admin/tenancies/{setup.ex.id}").text
    assert 'id="dlg-batch-depts"' in page
    r = client.post(f"/admin/tenancies/{setup.ex.id}/departments/batch-delete",
                    data={"csrf_token": csrf, "ids": [empty.id, setup.dev.id]})
    assert "still have users" in r.text and "Deleted 1 item(s)." in r.text
    assert sorted(d.name for d in db.query(Department)) == ["developers", "ops"]

    _logout(client)
    csrf = _login(client, "lead")
    r = client.post(f"/admin/tenancies/{setup.ex.id}/departments/batch-delete",
                    data={"csrf_token": csrf, "ids": [setup.ops.id]})
    assert r.status_code == 403


def test_batch_delete_users(client, setup, db):
    client.post("/api/v1/boards/developers/posts", json={"title": "kept", "body": "b"},
                headers=setup.auth("john:laptop"))
    ids = {u.username: u.id for u in db.query(User)}
    csrf = _login(client, "lead")
    page = client.get("/admin/users").text
    assert 'id="dlg-batch-users"' in page
    assert f'name="ids" value="{ids["john"]}"' in page and f'name="ids" value="{ids["lead"]}"' not in page
    r = client.post("/admin/users/batch-delete",
                    data={"csrf_token": csrf, "ids": [ids["john"], ids["bob"], ids["lead"], ids["root"]]})
    assert "Deleted 1 of 4" in r.text
    db.expire_all()
    assert db.get(User, ids["john"]) is None and db.get(User, ids["bob"]) is not None
    assert db.query(Machine).filter_by(user_id=ids["john"]).count() == 0
    post = db.query(Post).one()
    assert post.author_id is None and post.author_address == "john:laptop@developers:example.com"
    assert client.get("/api/v1/me", headers=setup.auth("john:laptop")).status_code == 401


# ---- migration -------------------------------------------------------------


def test_migration_makes_projects_tenancy_scoped(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as conn:  # tables as created by the previous version
        conn.execute(text("CREATE TABLE tenancies (id INTEGER PRIMARY KEY, name VARCHAR(253), description TEXT, "
                          "min_role_self_broadcast INTEGER, min_role_department_broadcast INTEGER, "
                          "min_role_tenancy_broadcast INTEGER, allow_cross_tenancy_dm BOOLEAN, created_at DATETIME)"))
        conn.execute(text("CREATE TABLE departments (id INTEGER PRIMARY KEY, tenancy_id INTEGER REFERENCES tenancies(id), "
                          "name VARCHAR(64), description TEXT, created_at DATETIME)"))
        conn.execute(text("CREATE TABLE projects (id INTEGER NOT NULL PRIMARY KEY, "
                          "department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE, "
                          "name VARCHAR(64) NOT NULL, title VARCHAR(255) NOT NULL, description TEXT NOT NULL, "
                          "created_at DATETIME NOT NULL, UNIQUE (department_id, name))"))
        conn.execute(text("CREATE TABLE posts (id INTEGER PRIMARY KEY, tenancy_id INTEGER, department_id INTEGER, "
                          "project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE, author_id INTEGER, "
                          "author_address VARCHAR(512), title VARCHAR(255), body TEXT, created_at DATETIME, "
                          "updated_at DATETIME)"))
        conn.execute(text("INSERT INTO tenancies (id, name) VALUES (7, 'example.com')"))
        conn.execute(text("INSERT INTO departments (id, tenancy_id, name) VALUES (3, 7, 'dev')"))
        conn.execute(text("INSERT INTO projects VALUES (5, 3, 'pathway', '', '', '2026-01-01')"))
        conn.execute(text("INSERT INTO posts (id, tenancy_id, department_id, project_id, author_address, title, body) "
                          "VALUES (1, 7, 3, 5, 'a', 't', 'b')"))
    init_db(engine)
    init_db(engine)  # idempotent
    cols = {c["name"]: c for c in inspect(engine).get_columns("projects")}
    assert cols["department_id"]["nullable"] and "tenancy_id" in cols
    assert "board_enabled" in {c["name"] for c in inspect(engine).get_columns("departments")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT tenancy_id FROM projects WHERE id = 5")).scalar() == 7
        assert conn.execute(text("SELECT board_enabled FROM tenancies WHERE id = 7")).scalar() == 1
        posts_sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'posts'")).scalar()
        assert "REFERENCES projects(id)" in posts_sql  # not repointed at a temporary table
        conn.execute(text("INSERT INTO projects (tenancy_id, name, title, description, created_at) "
                          "VALUES (7, 'hall', '', '', '2026-01-01')"))
        with pytest.raises(Exception):
            conn.execute(text("INSERT INTO projects (tenancy_id, name, title, description, created_at) "
                              "VALUES (7, 'hall', '', '', '2026-01-01')"))
