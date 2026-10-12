"""End-to-end tests against a running, *freshly initialized* instance (e.g. the Docker stack).

Skipped unless E2E_BASE_URL is set. The instance must not have a Super Admin yet,
because the test performs the first-run setup itself. See README "E2E tests".

    E2E_BASE_URL          base URL of the instance, e.g. http://localhost:8001
    E2E_WEBHOOK_PORT      local port for the webhook receiver (default 9999)
    E2E_WEBHOOK_HOST      host name the server uses to reach this machine
                          (default host.docker.internal); the server needs
                          WEBHOOK_ALLOW_PRIVATE_NETWORKS=true
"""

import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

httpx = pytest.importorskip("httpx")

BASE = os.getenv("E2E_BASE_URL")
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BASE, reason="E2E_BASE_URL not set"),
]

PASSWORD = "e2e-pass-123"
DEV, OPS = "developers:example.com", "ops:example.com"


def _csrf(c, page):
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get(page).text).group(1)


def _post(c, page, url, data):
    return c.post(url, data={**data, "csrf_token": _csrf(c, page)})


def _browser():
    return httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)


def _login(name):
    c = _browser()
    r = _post(c, "/login", "/login", {"tenancy": "example.com", "username": name, "password": PASSWORD})
    assert r.status_code == 200 and "/login" not in str(r.url), f"login failed for {name}"
    return c


class Hooks:
    def __init__(self, port):
        self.received: list[tuple[dict, bytes]] = []
        hooks = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                hooks.received.append((dict(self.headers), body))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait_for(self, needle: str, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for headers, body in self.received:
                if needle.encode() in body:
                    return headers, body
            time.sleep(0.3)
        return None


class Env:
    def __init__(self):
        self.admin = _browser()
        self.keys: dict[str, str] = {}
        self.departments: dict[str, str] = {}
        self.webhook_secret = ""
        self.api = httpx.Client(base_url=BASE, timeout=30)

    def auth(self, who):
        return {"Authorization": f"Bearer {self.keys[who]}"}

    def send(self, who, to, body="hi"):
        return self.api.post("/api/v1/messages", json={"to": to, "body": body}, headers=self.auth(who))

    def inbox(self, who, **params):
        r = self.api.get("/api/v1/messages", params=params, headers=self.auth(who))
        assert r.status_code == 200
        return r.json()["messages"]

    def create_user(self, name, role, dept):
        r = _post(self.admin, "/admin/users/new", "/admin/users",
                  {"username": name, "password": PASSWORD, "role": role, "placement": self.departments[dept]})
        assert "created" in r.text, f"could not create {name}"


@pytest.fixture(scope="module")
def hooks():
    hk = Hooks(int(os.getenv("E2E_WEBHOOK_PORT", "9999")))
    yield hk
    hk.server.shutdown()


@pytest.fixture(scope="module")
def env(hooks):
    e = Env()
    r = _post(e.admin, "/setup", "/setup", {"username": "root", "password": PASSWORD, "password2": PASSWORD})
    assert "Super Admin account created" in r.text, "instance is not fresh (Super Admin already exists?)"

    _post(e.admin, "/admin/tenancies", "/admin/tenancies", {"name": "example.com"})
    tid = re.search(r"/admin/tenancies/(\d+)", str(e.admin.get("/admin/tenancies").text)).group(1)
    for dept in ("developers", "ops"):
        _post(e.admin, f"/admin/tenancies/{tid}", f"/admin/tenancies/{tid}/departments", {"name": dept})
    page = e.admin.get("/admin/users/new").text
    e.departments = {label.strip(): val for val, label in re.findall(r'<option value="(d:\d+)"[^>]*>([^<]+)</option>', page)}

    for name, role, dept in [("john", 10, DEV), ("jane", 10, DEV), ("mod", 20, DEV), ("bob", 10, OPS)]:
        e.create_user(name, role, dept)

    hook_url = f"http://{os.getenv('E2E_WEBHOOK_HOST', 'host.docker.internal')}:{hooks.server.server_port}/hook"
    for name, machines in [("john", ["laptop", "server"]), ("jane", ["laptop"]), ("mod", ["laptop"]), ("bob", ["laptop"])]:
        c = _login(name)
        for m in machines:
            r = _post(c, "/account", "/machines", {"name": m})
            mid = re.search(r"/machines/(\d+)", str(r.url)).group(1)
            if name == "jane":
                r = _post(c, f"/machines/{mid}", f"/machines/{mid}/webhook", {"webhook_url": hook_url})
                assert "Webhook saved" in r.text, "webhook rejected (WEBHOOK_ALLOW_PRIVATE_NETWORKS=true?)"
                e.webhook_secret = re.search(r'data-secret="([^"]+)"', c.get(f"/machines/{mid}").text).group(1)
            r = _post(c, f"/machines/{mid}", f"/machines/{mid}/keys", {"label": "e2e"})
            e.keys[f"{name}:{m}"] = re.search(r'data-api-key="([^"]+)"', r.text).group(1)
    return e


def test_setup_runs_only_once(env):
    fresh = _browser()
    assert "/login" in str(fresh.get("/setup").url)


def test_api_identity(env):
    me = env.api.get("/api/v1/me", headers=env.auth("john:laptop")).json()
    assert me["address"] == "john:laptop@developers:example.com"
    assert env.api.get("/api/v1/me").status_code == 401


def test_messaging_rules(env):
    assert env.send("john:laptop", "jane:laptop@developers:example.com", "hello jane").status_code == 201
    r = env.send("john:laptop", "john:*@developers:example.com")
    assert r.status_code == 201 and r.json()["recipient_count"] == 1  # excludes the sending machine
    assert env.send("john:laptop", "*:*@developers:example.com").status_code == 403  # policy
    r = env.send("mod:laptop", "*:*@developers:example.com")
    assert r.status_code == 201 and r.json()["recipient_count"] == 3
    assert env.send("mod:laptop", "*:*@*:example.com").status_code == 403
    assert env.send("mod:laptop", "*:*@*:*").status_code == 403
    assert env.send("mod:laptop", "*:laptop@developers:example.com").status_code == 400

    assert len(env.inbox("jane:laptop", unread_only=True)) == 2
    env.api.post("/api/v1/messages/read", json={"all": True}, headers=env.auth("jane:laptop"))
    assert env.inbox("jane:laptop", unread_only=True) == []


def test_boards_and_late_joiner(env):
    r = env.api.post("/api/v1/boards/developers/posts", json={"title": "Hello", "body": "first post"},
                     headers=env.auth("john:laptop"))
    assert r.status_code == 201
    pid = r.json()["id"]
    assert env.api.get(f"/api/v1/posts/{pid}", headers=env.auth("bob:laptop")).status_code == 404
    env.api.post("/api/v1/boards/*/posts", json={"title": "Tenancy news", "body": "x"}, headers=env.auth("john:laptop"))

    env.create_user("newbie", 10, DEV)
    nb = _login("newbie")
    assert "Hello" in nb.get("/boards/developers:example.com").text
    assert "Tenancy news" in nb.get("/boards/*:example.com").text


def test_webhook_delivery(env, hooks):
    marker = f"webhook-{uuid.uuid4()}"
    assert env.send("john:laptop", "jane:laptop@developers:example.com", marker).status_code == 201
    got = hooks.wait_for(marker)
    assert got is not None, "webhook not received"
    headers, body = got
    expected = "sha256=" + hmac.new(env.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    assert headers.get("X-Community-Signature") == expected
    assert json.loads(body)["recipient"] == "jane:laptop@developers:example.com"


@pytest.mark.parametrize("page", ["/", "/messages", "/messages/compose", "/boards", "/account", "/admin",
                                  "/admin/users", "/admin/users/new", "/admin/tenancies", "/docs"])
def test_pages_render(env, page):
    assert env.admin.get(page).status_code == 200
