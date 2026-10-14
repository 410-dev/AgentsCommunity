import hashlib
import hmac
import json
import re
from urllib.parse import quote

import pytest

from app.models import BoardHook, CommitReceipt, Department, McpKey, Task, User
from app.roles import Role
from app.security import generate_mcp_key

from .conftest import csrf_of
from .test_mcp import Mcp

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


@pytest.fixture
def setup(world, db):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    ops = world.dept(ex, "ops")
    world.user("lead", dev, role=Role.DEPARTMENT_MODERATOR)
    world.user("john", dev)
    world.user("bot", dev, machines=("worker",))
    world.user("bob", ops)
    world.ex, world.dev = ex, dev
    return world


def _login(client, name):
    page = client.get("/login").text
    client.post("/login", data={"csrf_token": csrf_of(page), "tenancy": "example.com",
                                "username": name, "password": "password123"})
    return csrf_of(client.get("/boards").text)


def _hook(client, csrf, board="developers:example.com", name="github"):
    r = client.post("/board-hooks", data={"csrf_token": csrf, "board": board, "name": name})
    url = re.search(r'<code id="hookurl">([^<]+)</code>', r.text).group(1)
    secret = re.search(r'<code id="hooksecret">([^<]+)</code>', r.text).group(1)
    return url[url.index("/hooks/"):], secret


def _push(sha=SHA, repo="acme/app", ref="refs/heads/main"):
    return {"ref": ref, "after": sha, "compare": "https://github.com/acme/app/compare/x...y",
            "repository": {"full_name": repo}, "pusher": {"name": "octocat"},
            "commits": [{"id": sha, "message": "Fix login\n\nlong text", "url": f"https://github.com/{repo}/commit/{sha}",
                         "author": {"name": "Bot"}}]}


def test_github_webhooks_post_to_the_board(client, setup, db):
    csrf = _login(client, "lead")
    path, secret = _hook(client, csrf)
    john = setup.auth("john:laptop")

    assert client.post(path, json={"zen": "hi"}, headers={"X-GitHub-Event": "ping"}).json()["pong"] is True

    # A repository webhook signs the body with the secret.
    body = json.dumps(_push()).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r = client.post(path, content=body, headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sig,
                                                 "Content-Type": "application/json"})
    assert r.status_code == 201 and r.json()["receipts"] == 1
    post = client.get(f"/api/v1/posts/{r.json()['post_id']}", headers=john).json()
    assert post["title"] == "[acme/app] octocat pushed 1 commit(s) to main"
    assert post["author"] == "github:webhook@developers:example.com" and "`a1b2c3d` Fix login (Bot)" in post["body"]
    bad = client.post(path, content=body, headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=00"})
    assert bad.status_code == 401

    # A GitHub Actions step forwarding $GITHUB_EVENT_PATH (no signature), and form-encoded payloads.
    r = client.post(path, json=_push(sha="b" * 40), headers={"X-GitHub-Event": "push"})
    assert r.status_code == 201
    r = client.post(path, data={"payload": json.dumps(_push(sha="c" * 40))}, headers={"X-GitHub-Event": "push"})
    assert r.status_code == 201
    # Completed workflow runs are posted; other actions and events are acknowledged and ignored.
    run = {"action": "completed", "repository": {"full_name": "acme/app"},
           "workflow_run": {"name": "CI", "conclusion": "success", "head_sha": SHA, "head_branch": "main",
                            "html_url": "https://github.com/acme/app/actions/runs/1"}}
    r = client.post(path, json=run, headers={"X-GitHub-Event": "workflow_run"})
    assert r.status_code == 201
    assert client.post(path, json={**run, "action": "requested"}, headers={"X-GitHub-Event": "workflow_run"}).status_code == 202
    assert client.post(path, json={}, headers={"X-GitHub-Event": "issues"}).json()["ignored"] == "issues"
    # Plain JSON from anything else.
    r = client.post(path, json={"title": "Deployed", "body": "v1.2", "commit": "D1D2D3D", "repository": "acme/app"})
    assert r.status_code == 201
    assert client.post(path, json={}).status_code == 400
    assert db.query(CommitReceipt).count() == 5  # 3 pushes, 1 workflow run, 1 custom
    assert db.query(CommitReceipt).filter_by(sha="d1d2d3d").one().event == "custom"
    titles = [p["title"] for p in client.get("/api/v1/boards/developers/posts", headers=john).json()["posts"]]
    assert titles[:2] == ["Deployed", "[acme/app] CI success on main"]

    # Revoked hooks and switched-off boards refuse requests.
    hook = db.query(BoardHook).one()
    setup.dev.board_enabled = False
    db.commit()
    assert client.post(path, json={"title": "x"}).status_code == 410
    setup.dev.board_enabled = True
    db.commit()
    page = client.get("/boards/developers:example.com").text
    client.post(f"/board-hooks/{hook.id}/revoke", data={"csrf_token": csrf_of(page)})
    assert client.post(path, json={"title": "x"}).status_code == 404


def test_only_moderators_manage_webhooks(client, setup):
    csrf = _login(client, "john")
    assert 'data-open="dlg-hooks"' not in client.get("/boards/developers:example.com").text
    r = client.post("/board-hooks", data={"csrf_token": csrf, "board": "developers:example.com", "name": "x"})
    assert r.status_code == 403
    assert client.post("/hooks/ach_nope", json={"title": "x"}).status_code == 404


def test_task_lifecycle_and_receipts(client, setup, db):
    bot = setup.auth("bot:worker")
    r = client.post("/api/v1/boards/developers/tasks", json={"title": "Fix login", "description": "Users get 500"},
                    headers=bot)
    assert r.status_code == 201 and r.json()["status"] == "roadmap"
    tid = r.json()["id"]
    r = client.post(f"/api/v1/tasks/{tid}/start", json={"note": "on it"}, headers=bot)
    assert r.json()["status"] == "in_progress" and r.json()["assignee"] == "bot:worker@developers:example.com"
    client.post(f"/api/v1/tasks/{tid}/notes", json={"note": "found the bug"}, headers=bot)
    assert client.post(f"/api/v1/tasks/{tid}/notes", json={"note": " "}, headers=bot).status_code == 400
    assert client.post(f"/api/v1/tasks/{tid}/done", json={"commit": "zzz"}, headers=bot).status_code == 400
    r = client.post(f"/api/v1/tasks/{tid}/done", json={"commit": SHA[:10], "ref": "main", "repository": "acme/app",
                                                       "pushed": True, "note": "fixed"}, headers=bot)
    task = r.json()
    assert task["status"] == "done" and task["verification"] == "awaiting" and task["ci"] is None
    assert [e["kind"] for e in task["events"]] == ["created", "started", "note", "done"]
    assert "commit a1b2c3d4e5 on main pushed" in task["events"][-1]["note"]

    # GitHub's push and CI run confirm the self-report.
    csrf = _login(client, "lead")
    path, _ = _hook(client, csrf)
    client.post(path, json=_push(), headers={"X-GitHub-Event": "push"})
    client.post(path, json={"action": "completed", "repository": {"full_name": "acme/app"},
                            "workflow_run": {"name": "CI", "conclusion": "failure", "head_sha": SHA, "head_branch": "main"}},
                headers={"X-GitHub-Event": "workflow_run"})
    task = client.get(f"/api/v1/tasks/{tid}", headers=bot).json()
    assert task["verification"] == "confirmed" and task["ci"] == "failure" and len(task["receipts"]) == 2

    # A local-only report, and a contradiction (receipt exists, reported as not pushed).
    t2 = client.post("/api/v1/boards/developers/tasks", json={"title": "Local", "status": "in_progress"}, headers=bot).json()
    assert t2["assignee"] == "bot:worker@developers:example.com"
    t2 = client.post(f"/api/v1/tasks/{t2['id']}/done", json={"commit": "abcdef1", "pushed": False}, headers=bot).json()
    assert t2["verification"] == "local"
    t3 = client.post("/api/v1/boards/developers/tasks", json={"title": "Odd"}, headers=bot).json()
    t3 = client.post(f"/api/v1/tasks/{t3['id']}/done", json={"commit": SHA[:7], "pushed": False}, headers=bot).json()
    assert t3["verification"] == "unexpected"
    # Receipts from another repository do not confirm.
    t4 = client.post("/api/v1/boards/developers/tasks", json={"title": "Other repo"}, headers=bot).json()
    t4 = client.post(f"/api/v1/tasks/{t4['id']}/done", json={"commit": SHA[:7], "pushed": True,
                                                             "repository": "acme/other"}, headers=bot).json()
    assert t4["verification"] == "awaiting"

    columns = client.get("/api/v1/boards/developers/tasks", headers=bot).json()["tasks"]
    assert [len(columns[s]) for s in ("roadmap", "in_progress", "done")] == [0, 0, 4]
    # Other departments cannot see the board's tasks.
    assert client.get(f"/api/v1/tasks/{tid}", headers=setup.auth("bob:laptop")).status_code == 404
    # Only the creator, the assignee or a moderator may edit.
    assert client.patch(f"/api/v1/tasks/{tid}", json={"title": "x"}, headers=setup.auth("john:laptop")).status_code == 403
    r = client.patch(f"/api/v1/tasks/{tid}", json={"status": "roadmap"}, headers=bot).json()
    assert r["status"] == "roadmap" and r["assignee"] is None and r["commit"] is None


def test_task_board_web_and_live_panel(client, setup, db):
    csrf = _login(client, "john")
    page = client.get("/boards/developers:example.com").text
    assert 'data-task-panel="developers:example.com"' in page and 'id="dlg-task-new"' in page
    version = re.search(r'data-version="([^"]*)"', page).group(1)
    assert client.get("/task-panel", params={"board": "developers:example.com", "v": version}).status_code == 204

    client.post("/tasks", data={"csrf_token": csrf, "board": "developers:example.com", "title": "Roadmap item"})
    task = db.query(Task).one()
    r = client.get("/task-panel", params={"board": "developers:example.com", "v": version})
    assert r.status_code == 200 and "Roadmap item" in r.text and "<html" not in r.text
    r = client.post(f"/tasks/{task.id}/start", data={"csrf_token": csrf})
    assert "/boards/developers:example.com" in str(r.url) and r.status_code == 200
    client.post(f"/tasks/{task.id}/done", data={"csrf_token": csrf, "commit": "1234567", "ref": "main", "pushed": "no"})
    db.expire_all()
    task = db.get(Task, task.id)
    assert task.status == "done" and task.assignee_address == "john:*@developers:example.com" and task.pushed is False
    page = client.get("/boards/developers:example.com").text
    assert "Local commit (not pushed)" in page and "<code>1234567</code>" in page

    # Someone else's task: no edit or delete for a plain user, but a moderator may.
    client.post("/api/v1/boards/developers/tasks", json={"title": "Bot task"}, headers=setup.auth("bot:worker"))
    other = db.query(Task).filter_by(title="Bot task").one()
    assert client.post(f"/tasks/{other.id}/edit", data={"csrf_token": csrf, "title": "x", "status": "done"}).status_code == 403
    assert client.post(f"/tasks/{other.id}/delete", data={"csrf_token": csrf}).status_code == 403
    client.post("/logout", data={"csrf_token": csrf})
    csrf = _login(client, "lead")
    r = client.post(f"/tasks/{other.id}/delete", data={"csrf_token": csrf})
    assert "Task deleted." in r.text and db.query(Task).count() == 1
    # The tenancy board and project boards each have their own task board.
    assert 'data-task-panel="*:example.com"' in client.get("/boards/*:example.com").text


def test_mcp_task_tools(client, setup, db):
    bot = db.query(User).filter_by(username="bot").one()
    raw, prefix, digest = generate_mcp_key()
    db.add(McpKey(user_id=bot.id, prefix=prefix, key_hash=digest))
    db.commit()
    m = Mcp(client, raw)
    t = m.call("create_task", board="developers", title="From MCP")["structuredContent"]
    assert t["status"] == "roadmap" and t["created_by"] == "bot:worker@developers:example.com"
    assert m.call("start_task", task_id=t["id"])["structuredContent"]["status"] == "in_progress"
    m.call("report_task", task_id=t["id"], note="halfway")
    done = m.call("complete_task", task_id=t["id"], commit="abc1234", pushed=True, ref="main")["structuredContent"]
    assert done["verification"] == "awaiting" and [e["kind"] for e in done["events"]][-2:] == ["note", "done"]
    listed = m.call("list_tasks", board="developers", status="done")["structuredContent"]["tasks"]
    assert list(listed) == ["done"] and listed["done"][0]["id"] == t["id"]
    assert m.call("complete_task", task_id=t["id"], commit="xyz")["isError"] is True
