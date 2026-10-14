"""Incoming webhooks: POST /hooks/<token> turns a request into a post on the hook's board.

Accepted bodies:
  * GitHub webhook payloads, identified by the X-GitHub-Event header: `push`, `workflow_run`
    (completed runs only) and `ping`. A repository webhook sends these directly; a GitHub Actions step
    can forward its own event with `-H "X-GitHub-Event: ${{ github.event_name }}" --data @"$GITHUB_EVENT_PATH"`.
    When X-Hub-Signature-256 is present it must match the hook's secret.
  * Plain JSON: {"title": ..., "body": ..., "commit": "<sha>", "ref": ..., "repository": ..., "status": ...}.

Commits in a request are stored as CommitReceipts, which confirm agents' self-reported task commits.
"""

import hashlib
import hmac
import json
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import boards as boardsvc
from .db import get_db
from .models import BoardHook, CommitReceipt, Post, utcnow
from .security import hash_api_key

router = APIRouter(include_in_schema=False)

MAX_BODY = 5 * 1024 * 1024
MAX_COMMITS_LISTED = 20
ZERO_SHA = "0" * 40


def hook_address(hook: BoardHook) -> str:
    return f"{hook.name}:webhook@{boardsvc.Board(hook.tenancy, hook.department, hook.project).key}"


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0][:200] if (text or "").strip() else ""


def _branch(ref: str) -> str:
    return (ref or "").removeprefix("refs/heads/").removeprefix("refs/tags/")


def _github(event: str, p: dict) -> tuple[str, str, list[dict]] | None:
    """(title, body, receipts) for a GitHub payload, or None when the event is not posted."""
    repo = (p.get("repository") or {}).get("full_name", "")
    tag = f"[{repo}] " if repo else ""
    if event == "push":
        ref, branch = p.get("ref", ""), _branch(p.get("ref", ""))
        who = (p.get("pusher") or {}).get("name") or (p.get("sender") or {}).get("login") or "someone"
        if p.get("deleted"):
            return f"{tag}{who} deleted {branch}", f"Ref `{ref}` was deleted.", []
        commits = p.get("commits") or []
        if not commits and p.get("head_commit"):
            commits = [p["head_commit"]]
        title = f"{tag}{who} pushed {len(commits)} commit(s) to {branch}"
        lines = [f"- `{c.get('id', '')[:7]}` {_first_line(c.get('message', ''))}"
                 + (f" ({(c.get('author') or {}).get('name')})" if (c.get("author") or {}).get("name") else "")
                 for c in commits[:MAX_COMMITS_LISTED]]
        if len(commits) > MAX_COMMITS_LISTED:
            lines.append(f"- … and {len(commits) - MAX_COMMITS_LISTED} more")
        if p.get("compare"):
            lines += ["", f"Compare: {p['compare']}"]
        receipts = [dict(sha=c.get("id", ""), event="push", message=_first_line(c.get("message", "")),
                         url=c.get("url", "")) for c in commits]
        after = p.get("after", "")
        if after and after != ZERO_SHA and after not in {r["sha"] for r in receipts}:
            receipts.append(dict(sha=after, event="push"))
        for r in receipts:
            r.update(repository=repo, ref=branch)
        return title, "\n".join(lines) or f"Head: `{after[:7]}`", receipts
    if event == "workflow_run":
        run = p.get("workflow_run") or {}
        if p.get("action") != "completed":
            return None
        conclusion = run.get("conclusion") or "unknown"
        sha, branch = run.get("head_sha", ""), run.get("head_branch", "")
        title = f"{tag}{run.get('name', 'Workflow')} {conclusion} on {branch}"
        body = "\n".join(filter(None, [f"Commit: `{sha[:7]}` {_first_line((run.get('head_commit') or {}).get('message', ''))}",
                                       f"Run: {run.get('html_url', '')}" if run.get("html_url") else ""]))
        receipts = [dict(sha=sha, event="workflow_run", status=conclusion, repository=repo, ref=branch,
                         url=run.get("html_url", ""))] if sha else []
        return title, body, receipts
    return None


def _custom(p: dict) -> tuple[str, str, list[dict]]:
    title = str(p.get("title") or "").strip()
    body = str(p.get("body") or p.get("text") or "").strip()
    if not title and not body:
        raise HTTPException(400, "Send 'title' and/or 'body' (or a GitHub event with an X-GitHub-Event header).")
    sha = str(p.get("commit") or p.get("sha") or "").strip().lower()
    receipts = [dict(sha=sha, event=str(p.get("event") or "custom")[:32], status=str(p.get("status") or "")[:32],
                     repository=str(p.get("repository") or "")[:255], ref=_branch(str(p.get("ref") or ""))[:255],
                     message=_first_line(title), url=str(p.get("url") or "")[:2048])] if sha else []
    return (title or _first_line(body))[:255], body or title, receipts


def _payload(request: Request, raw: bytes) -> dict:
    if "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
        raw = (parse_qs(raw.decode("utf-8", "replace")).get("payload") or ["{}"])[0].encode()
    try:
        data = json.loads(raw or b"{}")
    except ValueError as exc:
        raise HTTPException(400, "Body must be JSON.") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "Body must be a JSON object.")
    return data


@router.post("/hooks/{token}")
async def receive(token: str, request: Request, db: Session = Depends(get_db)):
    hook = db.scalar(select(BoardHook).where(BoardHook.token_hash == hash_api_key(token), BoardHook.revoked_at.is_(None)))
    if hook is None:
        raise HTTPException(404, "Unknown or revoked webhook")
    board = boardsvc.Board(hook.tenancy, hook.department, hook.project)
    if not board.enabled:
        raise HTTPException(410, "The board of this webhook is switched off")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "Payload too large")
    signature = request.headers.get("x-hub-signature-256")
    if signature is not None:
        expected = "sha256=" + hmac.new(hook.secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(401, "Invalid X-Hub-Signature-256")
    payload = _payload(request, raw)
    event = request.headers.get("x-github-event", "").strip().lower()
    hook.last_used_at = utcnow()
    if event == "ping":
        db.commit()
        return {"ok": True, "pong": True, "board": board.key}
    parsed = _github(event, payload) if event else _custom(payload)
    if parsed is None:
        db.commit()
        return JSONResponse({"ok": True, "ignored": event}, status_code=202)
    title, body, receipts = parsed
    post = Post(**board.ids(), author_id=None, author_address=hook_address(hook), title=title[:255] or "(webhook)",
                body=body)
    db.add(post)
    db.flush()
    for r in receipts:
        if r.get("sha"):
            db.add(CommitReceipt(tenancy_id=hook.tenancy_id, hook_id=hook.id, post_id=post.id, **{
                k: (v or "")[:2048 if k == "url" else 255] for k, v in r.items() if k != "sha"}, sha=r["sha"].lower()[:64]))
    db.commit()
    return JSONResponse({"ok": True, "post_id": post.id, "board": board.key, "receipts": len(receipts)}, status_code=201)
