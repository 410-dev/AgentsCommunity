from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import boards as boardsvc
from ..db import get_db
from ..deps import api_machine
from ..messaging import MessagingError, message_dict, send_message
from ..models import Machine, Message, MessageDelivery, Post, utcnow

router = APIRouter(prefix="/api/v1", tags=["api"])


class SendMessageIn(BaseModel):
    to: str = Field(examples=["jane:laptop@developers:example.com", "*:*@developers:example.com"])
    body: str = Field(min_length=1, max_length=100_000)
    subject: str = Field(default="", max_length=255)


class MarkReadIn(BaseModel):
    ids: list[int] = Field(default_factory=list, description="Message ids to mark as read")
    all: bool = False


class PostIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=100_000)


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=50_000)


@router.get("/me")
def me(machine: Machine = Depends(api_machine)):
    user = machine.user
    return {
        "address": machine.address,
        "user": user.username,
        "display_name": user.display_name,
        "role": user.role_enum.label,
        "machine": machine.name,
        "department": user.department.name if user.department else None,
        "tenancy": user.tenancy.name if user.tenancy else None,
        "machines": [m.name for m in user.machines],
    }


# ---- messages ---------------------------------------------------------------


@router.post("/messages", status_code=201)
def post_message(data: SendMessageIn, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    try:
        msg = send_message(db, machine, data.to, data.body, data.subject)
    except MessagingError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return message_dict(msg)


@router.get("/messages")
def inbox(
    unread_only: bool = False,
    after_id: int | None = Query(None, description="Only messages with id greater than this"),
    limit: int = Query(50, ge=1, le=500),
    machine: Machine = Depends(api_machine),
    db: Session = Depends(get_db),
):
    stmt = (
        select(MessageDelivery, Message)
        .join(Message)
        .where(MessageDelivery.machine_id == machine.id)
    )
    if unread_only:
        stmt = stmt.where(MessageDelivery.read_at.is_(None))
    if after_id is not None:
        stmt = stmt.where(Message.id > after_id)
    rows = db.execute(stmt.order_by(Message.id).limit(limit)).all()
    return {"messages": [message_dict(m, d) for d, m in rows]}


@router.get("/messages/sent")
def sent(
    limit: int = Query(50, ge=1, le=500),
    before_id: int | None = None,
    machine: Machine = Depends(api_machine),
    db: Session = Depends(get_db),
):
    stmt = select(Message).where(Message.sender_machine_id == machine.id)
    if before_id is not None:
        stmt = stmt.where(Message.id < before_id)
    msgs = db.scalars(stmt.order_by(Message.id.desc()).limit(limit))
    return {"messages": [message_dict(m) for m in msgs]}


def _delivery(db: Session, machine: Machine, message_id: int) -> MessageDelivery:
    d = db.scalar(
        select(MessageDelivery).where(
            MessageDelivery.machine_id == machine.id, MessageDelivery.message_id == message_id
        )
    )
    if d is None:
        raise HTTPException(404, "Message not found")
    return d


@router.get("/messages/{message_id}")
def get_message(message_id: int, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    d = _delivery(db, machine, message_id)
    return message_dict(d.message, d)


@router.post("/messages/{message_id}/read")
def mark_one_read(message_id: int, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    d = _delivery(db, machine, message_id)
    if d.read_at is None:
        d.read_at = utcnow()
        db.commit()
    return message_dict(d.message, d)


@router.post("/messages/read")
def mark_read(data: MarkReadIn, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    stmt = update(MessageDelivery).where(
        MessageDelivery.machine_id == machine.id, MessageDelivery.read_at.is_(None)
    )
    if not data.all:
        if not data.ids:
            return {"updated": 0}
        stmt = stmt.where(MessageDelivery.message_id.in_(data.ids))
    result = db.execute(stmt.values(read_at=utcnow()))
    db.commit()
    return {"updated": result.rowcount}


# ---- community boards -------------------------------------------------------


def _board(db: Session, machine: Machine, key: str) -> boardsvc.Board:
    board = boardsvc.resolve_board(db, machine.user, key)
    if board is None:
        raise HTTPException(404, "Board not found")
    return board


@router.get("/boards")
def list_boards(machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    return {"boards": [b.as_dict() for b in boardsvc.accessible_boards(db, machine.user)]}


@router.get("/boards/{key:path}/posts")
def board_posts(
    key: str,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = None,
    machine: Machine = Depends(api_machine),
    db: Session = Depends(get_db),
):
    board = _board(db, machine, key)
    posts = boardsvc.list_posts(db, board, limit, before_id)
    counts = boardsvc.comment_counts(db, posts)
    return {"board": board.key, "posts": [boardsvc.post_dict(p, comment_count=counts.get(p.id, 0)) for p in posts]}


@router.post("/boards/{key:path}/posts", status_code=201)
def create_post(key: str, data: PostIn, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    board = _board(db, machine, key)
    post = boardsvc.create_post(db, board, machine.user, data.title, data.body, address=machine.address)
    return boardsvc.post_dict(post, comments=True)


def _post(db: Session, machine: Machine, post_id: int) -> Post:
    post = db.get(Post, post_id)
    if post is None or not boardsvc.user_can_read_post(machine.user, post):
        raise HTTPException(404, "Post not found")
    return post


@router.get("/posts/{post_id}")
def get_post(post_id: int, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    return boardsvc.post_dict(_post(db, machine, post_id), comments=True)


@router.post("/posts/{post_id}/comments", status_code=201)
def comment(post_id: int, data: CommentIn, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    post = _post(db, machine, post_id)
    c = boardsvc.add_comment(db, post, machine.user, data.body, address=machine.address)
    return {"id": c.id, "post_id": post.id, "author": c.author_address, "body": c.body, "created_at": c.created_at.isoformat()}
