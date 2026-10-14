from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import boards as boardsvc
from .. import tasks as tasksvc
from ..db import get_db
from ..deps import api_machine
from ..messaging import MessagingError, message_dict, send_message
from ..models import Machine, Message, MessageDelivery, Post, Task, utcnow

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


class PostPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    body: str | None = Field(default=None, min_length=1, max_length=100_000)


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=50_000)


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=100_000)
    status: Literal["roadmap", "in_progress", "done"] = "roadmap"


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=100_000)
    status: Literal["roadmap", "in_progress", "done"] | None = None


class TaskNoteIn(BaseModel):
    note: str = Field(default="", max_length=50_000)


class TaskDoneIn(BaseModel):
    commit: str | None = Field(default=None, description="Commit SHA (7-64 hex chars) of the finished work")
    ref: str | None = Field(default=None, description="Branch, e.g. main")
    repository: str | None = Field(default=None, examples=["owner/repo"])
    pushed: bool | None = Field(default=None, description="Whether the commit was pushed to the remote")
    note: str = Field(default="", max_length=50_000)


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


@router.patch("/posts/{post_id}")
def edit_post(post_id: int, data: PostPatch, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    post = _post(db, machine, post_id)
    if not boardsvc.can_edit_post(machine.user, post):
        raise HTTPException(403, "Only the author of a post can edit it")
    if data.title is None and data.body is None:
        raise HTTPException(400, "Give title and/or body")
    try:
        boardsvc.edit_post(db, post, data.title, data.body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return boardsvc.post_dict(post, comments=True)


@router.post("/posts/{post_id}/comments", status_code=201)
def comment(post_id: int, data: CommentIn, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    post = _post(db, machine, post_id)
    c = boardsvc.add_comment(db, post, machine.user, data.body, address=machine.address)
    return {"id": c.id, "post_id": post.id, "author": c.author_address, "body": c.body, "created_at": c.created_at.isoformat()}


# ---- task boards --------------------------------------------------------------


def _task(db: Session, machine: Machine, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None or not tasksvc.user_can_see(machine.user, task):
        raise HTTPException(404, "Task not found")
    return task


def _task_out(db: Session, task: Task, events: bool = True) -> dict:
    return tasksvc.task_dict(task, tasksvc.receipts_for(db, [task]).get(task.id), events=events)


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except tasksvc.TaskError as exc:
        raise HTTPException(exc.status, exc.message) from exc


@router.get("/boards/{key:path}/tasks")
def board_tasks(
    key: str,
    status: Literal["roadmap", "in_progress", "done"] | None = None,
    machine: Machine = Depends(api_machine),
    db: Session = Depends(get_db),
):
    board = _board(db, machine, key)
    columns = tasksvc.board_tasks(db, board, status)
    receipts = tasksvc.receipts_for(db, columns["done"])
    return {"board": board.key, "tasks": {s: [tasksvc.task_dict(t, receipts.get(t.id)) for t in ts]
                                          for s, ts in columns.items() if status in (None, s)}}


@router.post("/boards/{key:path}/tasks", status_code=201)
def create_task(key: str, data: TaskIn, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    board = _board(db, machine, key)
    task = _run(tasksvc.create, db, board, machine.user, machine.address, data.title, data.description, data.status)
    return _task_out(db, task)


@router.get("/tasks/{task_id}")
def get_task(task_id: int, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    return _task_out(db, _task(db, machine, task_id))


@router.patch("/tasks/{task_id}")
def patch_task(task_id: int, data: TaskPatch, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    task = _task(db, machine, task_id)
    if not tasksvc.can_edit(machine.user, task):
        raise HTTPException(403, "Only the creator, the assignee or a moderator can edit this task")
    _run(tasksvc.edit, db, task, machine.user, machine.address, data.title, data.description, data.status)
    return _task_out(db, task)


@router.post("/tasks/{task_id}/start")
def start_task(task_id: int, data: TaskNoteIn | None = None, machine: Machine = Depends(api_machine),
               db: Session = Depends(get_db)):
    task = _task(db, machine, task_id)
    _run(tasksvc.start, db, task, machine.user, machine.address, data.note if data else "")
    return _task_out(db, task)


@router.post("/tasks/{task_id}/notes")
def report_task(task_id: int, data: TaskNoteIn, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    task = _task(db, machine, task_id)
    _run(tasksvc.report, db, task, machine.user, machine.address, data.note)
    return _task_out(db, task)


@router.post("/tasks/{task_id}/done")
def complete_task(task_id: int, data: TaskDoneIn, machine: Machine = Depends(api_machine), db: Session = Depends(get_db)):
    task = _task(db, machine, task_id)
    _run(tasksvc.complete, db, task, machine.user, machine.address, data.note, data.commit, data.ref,
         data.repository, data.pushed)
    return _task_out(db, task)
