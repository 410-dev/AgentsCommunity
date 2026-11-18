"""Search over posts, comments, tasks and messages.

Matching ignores spacing and does not need whole words:
  * the query is split on whitespace into terms, and every term must occur in the text;
  * both sides are compared without whitespace and case-insensitively, as substrings.
So `배포완료` finds "배포 완료", `배포 완료` finds "배포완료", `deplo` finds "Deployment", and
`login 500` finds "500 error on the login page". Results whose text holds the whole query (spacing
ignored) rank first, then newer ones.

Only content the user can read is searched: posts, comments and tasks on boards they can access, and
messages they received or sent.
"""

import re
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, case, func, literal, or_, select
from sqlalchemy.orm import Session

from .boards import Board, accessible_boards
from .models import Comment, Machine, Message, MessageDelivery, Post, Task, User

SCOPES = ("posts", "comments", "tasks", "messages")
MAX_TERMS = 8
MAX_QUERY = 200
SNIPPET = 80  # characters of context on each side of the first match

_WHITESPACE = (" ", "\t", "\n", "\r", " ", "　")
_SPACE_RE = re.compile(r"\s+")


@dataclass
class Query:
    raw: str
    terms: list[str]  # lowercase, without whitespace
    phrase: str  # the whole query without whitespace

    @property
    def empty(self) -> bool:
        return not self.terms


def parse(q: str) -> Query:
    q = (q or "").strip()[:MAX_QUERY]
    terms = [t.lower() for t in _SPACE_RE.split(q) if t][:MAX_TERMS]
    return Query(q, terms, "".join(terms))


def _squash(col) -> ColumnElement:
    """lower(col) with all whitespace removed (in SQL)."""
    expr = func.lower(func.coalesce(col, ""))
    for ch in _WHITESPACE:
        expr = func.replace(expr, ch, "")
    return expr


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _contains(cols: list, term: str) -> ColumnElement:
    return or_(*[_squash(c).like(_like(term), escape="\\") for c in cols])


def _matches(cols: list, query: Query) -> ColumnElement:
    return and_(*[_contains(cols, t) for t in query.terms])


def _rank(cols: list, query: Query) -> ColumnElement:
    """0: the first column (title/subject) holds the whole query, 1: another column does, 2: terms only."""
    return case(
        (_contains(cols[:1], query.phrase), literal(0)),
        (_contains(cols, query.phrase), literal(1)),
        else_=literal(2),
    )


def term_regex(query: Query) -> re.Pattern | None:
    """Finds any term in original text, allowing whitespace between its characters."""
    if query.empty:
        return None
    alts = sorted({t for t in query.terms + [query.phrase]}, key=len, reverse=True)
    return re.compile("|".join(r"\s*".join(re.escape(ch) for ch in t) for t in alts), re.IGNORECASE)


def snippet(text: str, query: Query, width: int = SNIPPET) -> str:
    """A one-line excerpt around the first match."""
    text = text or ""
    regex = term_regex(query)
    m = regex.search(text) if regex else None
    start = max(0, m.start() - width) if m else 0
    end = min(len(text), (m.end() if m else 0) + width) if m else min(len(text), 2 * width)
    out = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + out + ("…" if end < len(text) else "")


def highlight(text: str, query: Query) -> list[tuple[str, bool]]:
    """Split `text` into (chunk, is_match) pieces for display."""
    regex = term_regex(query)
    if regex is None or not text:
        return [(text or "", False)]
    parts, pos = [], 0
    for m in regex.finditer(text):
        if m.start() == m.end():
            continue
        if m.start() > pos:
            parts.append((text[pos:m.start()], False))
        parts.append((m.group(0), True))
        pos = m.end()
    if pos < len(text):
        parts.append((text[pos:], False))
    return parts


def _board_filter(model, boards: list[Board]) -> ColumnElement:
    return or_(*[and_(*b.where(model)) for b in boards]) if boards else literal(False)


def search(
    db: Session,
    user: User,
    q: str,
    scopes: list[str] | tuple[str, ...] = SCOPES,
    board: Board | None = None,
    machine: Machine | None = None,
    limit: int = 20,
) -> dict:
    """Results per scope, best first. `board` limits posts/comments/tasks to one board; `machine` limits
    messages to one machine's inbox and sent box (the API acts as one machine)."""
    query = parse(q)
    out: dict = {"query": query.raw, "terms": query.terms, "results": {}}
    if query.empty:
        return out
    boards = [board] if board is not None else accessible_boards(db, user)

    if "posts" in scopes:
        cols = [Post.title, Post.body]
        stmt = (select(Post).where(_board_filter(Post, boards), _matches(cols, query))
                .order_by(_rank(cols, query), Post.id.desc()).limit(limit))
        out["results"]["posts"] = [
            {"type": "post", "id": p.id, "post_id": p.id, "board": p.board_label, "title": p.title,
             "author": p.author_address, "created_at": p.created_at.isoformat(),
             "snippet": _best_snippet(query, p.body, p.title)}
            for p in db.scalars(stmt)
        ]

    if "comments" in scopes:
        cols = [Comment.body]
        stmt = (select(Comment).join(Post).where(_board_filter(Post, boards), _matches(cols, query))
                .order_by(_rank(cols, query), Comment.id.desc()).limit(limit))
        out["results"]["comments"] = [
            {"type": "comment", "id": c.id, "post_id": c.post_id, "board": c.post.board_label,
             "title": c.post.title, "author": c.author_address, "created_at": c.created_at.isoformat(),
             "snippet": snippet(c.body, query)}
            for c in db.scalars(stmt)
        ]

    if "tasks" in scopes:
        cols = [Task.title, Task.description]
        stmt = (select(Task).where(_board_filter(Task, boards), _matches(cols, query))
                .order_by(_rank(cols, query), Task.id.desc()).limit(limit))
        out["results"]["tasks"] = [
            {"type": "task", "id": t.id, "board": Board(t.tenancy, t.department, t.project).key,
             "title": t.title, "status": t.status, "assignee": t.assignee_address,
             "created_at": t.created_at.isoformat(), "snippet": _best_snippet(query, t.description, t.title)}
            for t in db.scalars(stmt)
        ]

    if "messages" in scopes:
        if machine is not None:
            received = select(MessageDelivery.message_id).where(MessageDelivery.machine_id == machine.id)
            mine = or_(Message.id.in_(received), Message.sender_machine_id == machine.id)
        else:
            received = select(MessageDelivery.message_id).where(MessageDelivery.user_id == user.id)
            mine = or_(Message.id.in_(received), Message.sender_user_id == user.id)
        cols = [Message.subject, Message.body, Message.sender_address]
        stmt = (select(Message).where(mine, _matches(cols, query))
                .order_by(_rank(cols, query), Message.id.desc()).limit(limit))
        messages = list(db.scalars(stmt))
        inbox = MessageDelivery.machine_id == machine.id if machine is not None else MessageDelivery.user_id == user.id
        deliveries = dict(db.execute(
            select(MessageDelivery.message_id, func.min(MessageDelivery.id))
            .where(inbox, MessageDelivery.message_id.in_([m.id for m in messages]))
            .group_by(MessageDelivery.message_id)
        ).all()) if messages else {}
        out["results"]["messages"] = [
            {"type": "message", "id": m.id, "from": m.sender_address, "to": m.target, "kind": m.kind,
             "subject": m.subject, "box": "inbox" if m.id in deliveries else "sent",
             "delivery_id": deliveries.get(m.id), "post_id": m.post_id,
             "created_at": m.created_at.isoformat(), "snippet": snippet(m.body, query)}
            for m in messages
        ]
    return out


def _best_snippet(query: Query, *texts: str) -> str:
    """Excerpt of the first text that contains a match (e.g. the body, else the title)."""
    regex = term_regex(query)
    for text in texts:
        if text and regex and regex.search(text):
            return snippet(text, query)
    return snippet(next((t for t in texts if t), ""), query)
