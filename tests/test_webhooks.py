import json

import httpx

from app.config import settings
from app.models import Machine, MessageDelivery
from app.security import sign_webhook
from app.webhooks import process_pending, validate_webhook_url


def test_webhook_delivery_signed_and_retried(client, world, db, monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow_private_networks", True)
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    world.user("john", dev)
    jane = world.user("jane", dev)
    m = db.query(Machine).filter_by(user_id=jane.id).one()
    m.webhook_url = "http://agent.local/hook"
    db.commit()

    calls = []
    status = {"code": 500}

    def handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(status["code"])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    r = client.post("/api/v1/messages", json={"to": "jane:laptop@developers:example.com", "body": "ping"},
                    headers=world.auth("john:laptop"))
    assert r.status_code == 201

    assert process_pending(db, http) == 1
    d = db.query(MessageDelivery).one()
    assert d.webhook_status == "pending" and d.webhook_attempts == 1 and d.webhook_last_error == "HTTP 500"

    # Make it due again and succeed.
    d.webhook_next_at = d.webhook_next_at.replace(year=2000)
    db.commit()
    status["code"] = 204
    assert process_pending(db, http) == 1
    db.refresh(d)
    assert d.webhook_status == "delivered"

    req = calls[-1]
    assert req.headers["X-Community-Signature"] == sign_webhook(m.webhook_secret, req.content)
    payload = json.loads(req.content)
    assert payload["recipient"] == "jane:laptop@developers:example.com"
    assert payload["message"]["body"] == "ping"


def test_private_webhook_urls_rejected_by_default(monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow_private_networks", False)
    assert validate_webhook_url("http://127.0.0.1:8080/hook") is not None
    assert validate_webhook_url("ftp://example.com") is not None
