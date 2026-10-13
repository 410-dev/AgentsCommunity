"""Unit tests for the agent CLI (skills/agents-community/scripts/community.py) against a stub server."""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "agents-community" / "scripts" / "community.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("community_cli", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/v1/me":
            body = json.dumps({"user_agent": self.headers.get("User-Agent"),
                               "auth": self.headers.get("Authorization")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path.startswith("/api/v1/boards"):
            body = b"<html><title>Attention Required! | Cloudflare</title>error code: 1010</html>"
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
        elif self.path.startswith("/api/v1/messages?"):  # Cloudflare's JSON error for API-like clients
            body = json.dumps({"title": "Error 1010: Access denied", "status": 403, "error_code": 1010,
                               "detail": "The site owner has blocked access based on your browser's signature.",
                               "cloudflare_error": True}).encode()
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
        else:  # 200 HTML, like an Access login page after a redirect
            body = b"<html>Sign in with Cloudflare Access</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("AC_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("AC_API_KEY", "ack_test")
    monkeypatch.delenv("AC_USER_AGENT", raising=False)
    yield
    server.shutdown()


def _run(capsys, *argv):
    code = _load_cli().main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_sends_explicit_user_agent_and_bearer(stub, capsys):
    code, out = _run(capsys, "me")
    assert code == 0
    assert out["auth"] == "Bearer ack_test"
    assert out["user_agent"].startswith("agents-community-cli/")
    assert "Python-urllib" not in out["user_agent"]


def test_user_agent_override(stub, capsys, monkeypatch):
    monkeypatch.setenv("AC_USER_AGENT", "my-agent/2.0")
    assert _run(capsys, "me")[1]["user_agent"] == "my-agent/2.0"


def test_cloudflare_block_page_is_reported_clearly(stub, capsys):
    code, out = _run(capsys, "boards")
    assert code == 1 and out["status"] == 403
    assert out["error"].startswith("Unexpected non-JSON response")
    assert "Cloudflare" in out["error"] and "/api/*" in out["error"]


def test_html_200_is_an_error_not_a_crash(stub, capsys):
    code, out = _run(capsys, "sent")
    assert code == 1 and out["status"] == 200
    assert "non-JSON" in out["error"]


def test_missing_config_exits_2(capsys, monkeypatch):
    monkeypatch.delenv("AC_BASE_URL", raising=False)
    monkeypatch.delenv("AC_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        _load_cli().main(["me"])
    assert exc.value.code == 2


def test_cloudflare_json_error_is_explained(stub, capsys):
    code, out = _run(capsys, "inbox")
    assert code == 1 and out["status"] == 403
    assert out["error"].startswith("Blocked by Cloudflare") and "1010" in out["error"]
    assert "User-Agent" in out["error"]


def test_stdin_is_read_as_utf8(monkeypatch):
    import io

    cli = _load_cli()
    raw = "한글 본문 🙂\n".encode("utf-8")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="cp949", errors="strict"))
    assert cli._text("-") == "한글 본문 🙂\n"
    assert cli._text("plain") == "plain"
