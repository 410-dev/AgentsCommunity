"""Pinned task board of every board: roadmap -> in progress (by which agent) -> done (commit/push report).

Every change is kept as a TaskEvent, so the history shows who started, reported and completed a task.
A completed task's self-reported commit is matched against CommitReceipts from incoming webhooks
(e.g. GitHub push events): the agent's report says what it did, the receipt confirms what arrived.
"""

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .boards import Board
from .models import CommitReceipt, Task, TaskEvent, User, utcnow
from .permissions import can_access_board, can_moderate_board

STATUSES = ("roadmap", "in_progress", "done")
STATUS_LABELS = {"roadmap": "Roadmap", "in_progress": "In progress", "done": "Done"}
DONE_SHOWN = 30  # done column shows the most recent ones

SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


class TaskError(Exception):
    """`message` is an English UI string."""

    def __init__(self, message: str, status: int = 400):
        self.message, self.status = message, status
        super().__init__(message)


def board_of(task: Task) -> Board:
    return Board(task.tenancy, task.department, task.project)


def user_can_see(user: User, task: Task) -> bool:
    return can_access_board(user, task.tenancy_id, task.department_id) and board_of(task).enabled


def can_edit(user: User, task: Task) -> bool:
    """Rename, move between columns: the creator, the assignee or a moderator of the board."""
    return user.id in (task.created_by_id, task.assignee_id) or can_moderate_board(user, task.tenancy_id, task.department_id)


def can_delete(user: User, task: Task) -> bool:
    return user.id == task.created_by_id or can_moderate_board(user, task.tenancy_id, task.department_id)


def board_tasks(db: Session, board: Board, status: str | None = None) -> dict[str, list[Task]]:
    out: dict[str, list[Task]] = {s: [] for s in STATUSES}
    for s in STATUSES if status is None else [status]:
        stmt = select(Task).where(*board.where(Task), Task.status == s)
        if s == "done":
            stmt = stmt.order_by(Task.completed_at.desc(), Task.id.desc()).limit(DONE_SHOWN)
        elif s == "in_progress":
            stmt = stmt.order_by(Task.started_at, Task.id)
        else:
            stmt = stmt.order_by(Task.id)
        out[s] = list(db.scalars(stmt))
    return out


def board_version(db: Session, board: Board) -> str:
    """Changes whenever a task on the board changes (used by the live-refreshing web panel)."""
    count, last = db.execute(select(func.count(), func.max(Task.updated_at)).where(*board.where(Task))).one()
    return f"{count}:{last.isoformat() if last else ''}"


def _event(task: Task, actor: User, address: str, kind: str, note: str = "") -> None:
    task.events.append(TaskEvent(actor_id=actor.id, actor_address=address, kind=kind, note=note.strip()))
    task.updated_at = utcnow()


def _title(title: str) -> str:
    title = title.strip()
    if not title:
        raise TaskError("Title is required.")
    return title[:255]


def create(db: Session, board: Board, actor: User, address: str, title: str, description: str = "",
           status: str = "roadmap") -> Task:
    if status not in STATUSES:
        raise TaskError("Unknown task status.")
    task = Task(**board.ids(), title=_title(title), description=description.strip(), status="roadmap",
                created_by_id=actor.id, created_by_address=address)
    db.add(task)
    _event(task, actor, address, "created")
    if status == "in_progress":
        _start(task, actor, address)
    elif status == "done":
        _finish(task, actor, address)
    db.commit()
    return task


def _start(task: Task, actor: User, address: str, note: str = "") -> None:
    task.status, task.started_at = "in_progress", utcnow()
    task.assignee_id, task.assignee_address = actor.id, address
    task.completed_at = task.commit_sha = task.commit_ref = task.repository = task.pushed = None
    _event(task, actor, address, "started", note)


def _finish(task: Task, actor: User, address: str, note: str = "", commit: str | None = None,
            ref: str | None = None, repository: str | None = None, pushed: bool | None = None) -> None:
    commit = (commit or "").strip().lower() or None
    if commit is not None and not SHA_RE.match(commit):
        raise TaskError("Commit must be a hexadecimal SHA of at least 7 characters.")
    if task.assignee_id is None:
        task.assignee_id, task.assignee_address = actor.id, address
    task.status, task.completed_at = "done", utcnow()
    task.commit_sha, task.pushed = commit, pushed
    task.commit_ref = (ref or "").strip().removeprefix("refs/heads/")[:255] or None
    task.repository = (repository or "").strip()[:255] or None
    details = [f"commit {commit[:12]}" if commit else "no commit",
               f"on {task.commit_ref}" if task.commit_ref else "",
               {True: "pushed", False: "not pushed", None: ""}[pushed]]
    _event(task, actor, address, "done", "\n".join(filter(None, [" ".join(filter(None, details)), note.strip()])))


def start(db: Session, task: Task, actor: User, address: str, note: str = "") -> Task:
    """Claim the task: it moves to In progress with `address` (the agent/machine) as assignee."""
    _start(task, actor, address, note)
    db.commit()
    return task


def report(db: Session, task: Task, actor: User, address: str, note: str) -> Task:
    """A progress report without a status change."""
    if not note.strip():
        raise TaskError("Report text is required.")
    _event(task, actor, address, "note", note)
    db.commit()
    return task


def complete(db: Session, task: Task, actor: User, address: str, note: str = "", commit: str | None = None,
             ref: str | None = None, repository: str | None = None, pushed: bool | None = None) -> Task:
    _finish(task, actor, address, note, commit, ref, repository, pushed)
    db.commit()
    return task


def edit(db: Session, task: Task, actor: User, address: str, title: str | None = None,
         description: str | None = None, status: str | None = None) -> Task:
    if title is not None and title.strip() != task.title:
        task.title = _title(title)
        _event(task, actor, address, "edited")
    if description is not None:
        task.description = description.strip()
    if status is not None and status != task.status:
        if status not in STATUSES:
            raise TaskError("Unknown task status.")
        if status == "in_progress":
            _start(task, actor, address)
        elif status == "done":
            _finish(task, actor, address)
        else:  # back to the roadmap: nobody is working on it any more
            task.status = "roadmap"
            task.assignee_id = task.assignee_address = task.started_at = None
            task.completed_at = task.commit_sha = task.commit_ref = task.repository = task.pushed = None
            _event(task, actor, address, "moved", "roadmap")
    task.updated_at = utcnow()
    db.commit()
    return task


# ---- commit receipts ---------------------------------------------------------------


def receipts_for(db: Session, tasks: list[Task]) -> dict[int, list[CommitReceipt]]:
    """Receipts matching each done task's reported commit (prefix match, same tenancy)."""
    out: dict[int, list[CommitReceipt]] = {}
    for t in tasks:
        if not t.commit_sha:
            continue
        stmt = select(CommitReceipt).where(CommitReceipt.tenancy_id == t.tenancy_id,
                                           CommitReceipt.sha.startswith(t.commit_sha))
        rows = [r for r in db.scalars(stmt.order_by(CommitReceipt.id))
                if not t.repository or not r.repository or r.repository.lower() == t.repository.lower()]
        if rows:
            out[t.id] = rows
    return out


def verification(task: Task, receipts: list[CommitReceipt]) -> str:
    """How the self-report compares with what the webhook received.

    confirmed   reported pushed, and a receipt exists
    unexpected  receipt exists although the agent reported "not pushed" (or did not say)
    awaiting    reported pushed, no receipt (yet): the push or the webhook may have failed
    local       reported a commit that was not pushed
    none        no commit reported
    """
    if not task.commit_sha:
        return "none"
    if receipts:  # a push or a workflow run on the commit: it reached the remote
        return "confirmed" if task.pushed else "unexpected"
    return "awaiting" if task.pushed else "local"


VERIFICATION_LABELS = {
    "confirmed": "Confirmed by webhook",
    "unexpected": "Received, but reported as not pushed",
    "awaiting": "Pushed, no receipt yet",
    "local": "Local commit (not pushed)",
    "none": "No commit reported",
}


def ci_status(receipts: list[CommitReceipt]) -> str | None:
    """Latest workflow conclusion reported for the commit, if any."""
    runs = [r for r in receipts if r.event == "workflow_run" and r.status]
    return runs[-1].status if runs else None


def task_dict(task: Task, receipts: list[CommitReceipt] | None = None, events: bool = False) -> dict:
    receipts = receipts or []
    data = {
        "id": task.id,
        "board": board_of(task).key,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "created_by": task.created_by_address,
        "assignee": task.assignee_address,
        "commit": task.commit_sha,
        "ref": task.commit_ref,
        "repository": task.repository,
        "pushed": task.pushed,
        "verification": verification(task, receipts) if task.status == "done" else None,
        "ci": ci_status(receipts),
        "receipts": [{"sha": r.sha, "event": r.event, "status": r.status, "repository": r.repository,
                      "ref": r.ref, "post_id": r.post_id, "received_at": r.received_at.isoformat()} for r in receipts],
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }
    if events:
        data["events"] = [{"at": e.created_at.isoformat(), "by": e.actor_address, "kind": e.kind, "note": e.note}
                          for e in task.events]
    return data
