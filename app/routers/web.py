from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from .. import boards as boardsvc
from ..db import get_db
from ..i18n import COOKIE as LANG_COOKIE, LANGUAGES, detect_language, translator
from ..deps import csrf_protect, csrf_token, current_user, optional_user, super_admin_exists
from ..messaging import MessagingError, send_message
from ..models import (
    ApiKey, Comment, Department, Machine, McpKey, Message, MessageDelivery, Post, Project, Tenancy, User, utcnow,
)
from ..permissions import (
    assignable_roles,
    can_create_tenancy,
    can_manage_agent,
    can_manage_credentials,
    can_manage_projects,
    can_manage_tenancy,
    can_access_board,
    can_manage_user,
    can_moderate_post,
    can_move_post,
    can_view_admin,
    in_scope,
    validate_placement,
)
from ..roles import POLICY_ROLES, Role, role_label
from ..security import (
    generate_api_key,
    generate_mcp_key,
    generate_webhook_secret,
    hash_password,
    valid_name,
    valid_tenancy_name,
    verify_password,
)
from ..webhooks import validate_webhook_url

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals.update(role_label=role_label, Role=Role)
templates.env.tests["manageable_by"] = lambda target, actor: can_manage_user(actor, target)
templates.env.filters["dt"] = lambda v, seconds=False: v.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M") if v else ""

router = APIRouter(dependencies=[Depends(csrf_protect)], include_in_schema=False)

MIN_PASSWORD = 8


def render(request: Request, name: str, user: User | None = None, status_code: int = 200, **ctx):
    flashes = request.session.pop("flash", [])
    unread_total = 0
    db = object_session(user) if user is not None else None
    if db is not None:
        unread_total = db.scalar(
            select(func.count()).where(MessageDelivery.user_id == user.id, MessageDelivery.read_at.is_(None))
        )
    lang = detect_language(request)
    _ = translator(lang)
    ctx.update(
        request=request,
        user=user,
        csrf=csrf_token(request),
        flashes=[[f[0], _(f[1], **{k: _(v) if isinstance(v, str) else v for k, v in (f[2] if len(f) > 2 else {}).items()})]
                 for f in flashes],
        _=_,
        lang=lang,
        languages=LANGUAGES,
        role_label=lambda value: _(role_label(value)),
        can_view_admin=bool(user and can_view_admin(user)),
        unread_total=unread_total,
        path=request.url.path,
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def flash(request: Request, message: str, category: str = "info", **params) -> None:
    """Queue a notice. `message` is an English source string with optional {placeholders}."""
    request.session.setdefault("flash", []).append([category, message, params])


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def forbid() -> HTTPException:
    return HTTPException(403, "You do not have permission to do that.")


def safe_next(url: str, default: str) -> str:
    """A local path to return to after a form post (never an absolute or protocol-relative URL)."""
    return url if url.startswith("/") and not url.startswith("//") else default


# ---- setup & auth -----------------------------------------------------------


@router.get("/setup")
def setup_form(request: Request, db: Session = Depends(get_db)):
    if super_admin_exists(db):
        return redirect("/login")
    return render(request, "setup.html")


@router.post("/setup")
def setup(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    display_name: str = Form(""),
    db: Session = Depends(get_db),
):
    if super_admin_exists(db):
        return redirect("/login")
    username = username.strip().lower()
    error = None
    if not valid_name(username):
        error = "Username must be lowercase letters, digits, '.', '_' or '-'."
    elif len(password) < MIN_PASSWORD:
        error = "Password must be at least {n} characters."
    elif password != password2:
        error = "Passwords do not match."
    if error:
        flash(request, error, "error", n=MIN_PASSWORD)
        return render(request, "setup.html", status_code=400)
    user = User(
        username=username,
        display_name=display_name.strip(),
        password_hash=hash_password(password),
        role=int(Role.SUPER_ADMIN),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return redirect("/login")
    request.session.clear()
    request.session["uid"] = user.id
    flash(request, "Super Admin account created. Next, create a tenancy.", "success")
    return redirect("/admin/tenancies")


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    if not super_admin_exists(db):
        return redirect("/setup")
    if user:
        return redirect("/")
    return render(request, "login.html")


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    tenancy: str = Form(""),
    db: Session = Depends(get_db),
):
    username = username.strip().lower()
    tenancy = tenancy.strip().lower()
    stmt = select(User).where(User.username == username)
    if tenancy:
        stmt = stmt.join(Tenancy, User.tenancy_id == Tenancy.id).where(Tenancy.name == tenancy)
    else:
        stmt = stmt.where(User.tenancy_id.is_(None))
    user = db.scalar(stmt)
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        flash(request, "Invalid credentials.", "error")
        return render(request, "login.html", status_code=401, tenancy=tenancy, username=username)
    request.session.clear()
    request.session["uid"] = user.id
    return redirect("/")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@router.get("/lang/{code}")
def set_language(code: str, next: str = "/"):
    if code not in LANGUAGES:
        raise HTTPException(404, "Unknown language")
    if not next.startswith("/") or next.startswith("//"):
        next = "/"
    resp = redirect(next)
    resp.set_cookie(LANG_COOKIE, code, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


# ---- dashboard --------------------------------------------------------------


def _unread_by_machine(db: Session, user: User) -> dict[int | None, int]:
    """Unread count per machine id; the key None is the account inbox."""
    rows = db.execute(
        select(MessageDelivery.machine_id, func.count())
        .where(MessageDelivery.user_id == user.id, MessageDelivery.read_at.is_(None))
        .group_by(MessageDelivery.machine_id)
    )
    return dict(rows.all())


@router.get("/")
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    boards = boardsvc.accessible_boards(db, user)
    recent = []
    for b in boards[:12]:
        recent.extend(boardsvc.list_posts(db, b, limit=5))
    recent.sort(key=lambda p: p.id, reverse=True)
    tenancies = _visible_tenancies(db, user)
    machine_ids = [m.id for m in user.machines]
    quick = {
        "tenancies": len(tenancies),
        "departments": sum(len(t.departments) for t in tenancies),
        "users": len(_managed_users(db, user)) if can_view_admin(user) else 0,
        "keys": db.scalar(
            select(func.count()).where(ApiKey.machine_id.in_(machine_ids), ApiKey.revoked_at.is_(None))
        ) if machine_ids else 0,
        "sent": db.scalar(select(func.count()).where(Message.sender_user_id == user.id)),
    }
    failed = dict(
        db.execute(
            select(MessageDelivery.machine_id, func.count())
            .where(MessageDelivery.machine_id.in_(machine_ids), MessageDelivery.webhook_status == "failed")
            .group_by(MessageDelivery.machine_id)
        ).all()
    ) if machine_ids else {}
    return render(
        request,
        "dashboard.html",
        user,
        unread=_unread_by_machine(db, user),
        failed=failed,
        boards=boards,
        recent_posts=recent[:15],
        quick=quick,
    )


# ---- messages ---------------------------------------------------------------


def _own_machine(db: Session, user: User, machine_id: int | None) -> Machine | None:
    if machine_id is None:
        return None
    m = db.get(Machine, machine_id)
    if m is None or m.user_id != user.id:
        raise HTTPException(404, "Machine not found")
    return m


def _managed_machine(db: Session, user: User, machine_id: int) -> Machine:
    """A machine of the user's own, or of an agent account the user manages."""
    m = db.get(Machine, machine_id)
    if m is None or not can_manage_credentials(user, m.user):
        raise HTTPException(404, "Machine not found")
    return m


def _owner_url(user: User, owner: User, own: str) -> str:
    """Where to return after changing `owner`'s machines or keys."""
    return own if owner.id == user.id else f"/admin/users/{owner.id}"


ACCOUNT_INBOX = 0  # `machine` filter value for messages sent to / as the account itself


def _mailbox(
    request: Request, db: Session, user: User, owner: User, base: str, box: str, machine: int | None,
    unread: bool, open: int | None,
):
    """Inbox or sent box of `owner`: all machines, one machine, or the account inbox (machine=0)."""
    selected = None
    if machine and machine != ACCOUNT_INBOX:
        selected = db.get(Machine, machine)
        if selected is None or selected.user_id != owner.id:
            raise HTTPException(404, "Machine not found")
    if box == "sent":
        stmt = select(Message).where(Message.sender_user_id == owner.id)
        if selected:
            stmt = stmt.where(Message.sender_machine_id == selected.id)
        elif machine == ACCOUNT_INBOX:
            stmt = stmt.where(Message.sender_machine_id.is_(None))
        rows = db.scalars(stmt.order_by(Message.id.desc()).limit(200)).all()
        opened = db.get(Message, open) if open is not None else None
        if opened is not None and opened.sender_user_id != owner.id:
            opened = None
    else:
        box = "inbox"
        stmt = select(MessageDelivery).join(Message).where(MessageDelivery.user_id == owner.id)
        if selected:
            stmt = stmt.where(MessageDelivery.machine_id == selected.id)
        elif machine == ACCOUNT_INBOX:
            stmt = stmt.where(MessageDelivery.machine_id.is_(None))
        if unread:
            stmt = stmt.where(MessageDelivery.read_at.is_(None))
        rows = db.scalars(stmt.order_by(Message.id.desc()).limit(200)).all()
        opened = db.get(MessageDelivery, open) if open is not None else None
        if opened is not None and opened.user_id != owner.id:
            opened = None
    return render(
        request, "messages.html", user, owner=owner, base=base, readonly=owner.id != user.id,
        box=box, rows=rows, selected=selected, account_only=machine == ACCOUNT_INBOX, unread=unread,
        unread_counts=_unread_by_machine(db, owner), opened=opened,
    )


@router.get("/messages")
def messages(
    request: Request,
    box: str = "inbox",
    machine: int | None = None,
    unread: bool = False,
    open: int | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return _mailbox(request, db, user, user, "/messages", box, machine, unread, open)


@router.get("/admin/users/{user_id}/messages")
def admin_agent_messages(
    request: Request,
    user_id: int,
    box: str = "inbox",
    machine: int | None = None,
    unread: bool = False,
    open: int | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Managers of an agent account can read everything it sent and received (read-only)."""
    target = _managed_target(db, user, user_id)
    if not can_manage_agent(user, target):
        raise HTTPException(400, "Only agent accounts' messages can be viewed by administrators.")
    return _mailbox(request, db, user, target, f"/admin/users/{target.id}/messages", box, machine, unread, open)


@router.get("/messages/compose")
def compose_form(request: Request, to: str = "", user: User = Depends(current_user)):
    return render(request, "compose.html", user, to=to)


@router.post("/messages/compose")
def compose(
    request: Request,
    machine_id: int = Form(ACCOUNT_INBOX),
    to: str = Form(...),
    subject: str = Form(""),
    body: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    # Without a machine, the message is sent as the account itself.
    sender = _own_machine(db, user, machine_id) if machine_id != ACCOUNT_INBOX else user
    try:
        msg = send_message(db, sender, to, body, subject)
    except MessagingError as exc:
        flash(request, exc.template, "error", **exc.params)
        return render(request, "compose.html", user, status_code=exc.status, to=to, subject=subject, body=body,
                      machine_id=machine_id)
    flash(request, "Message delivered to {n} inbox(es).", "success", n=len(msg.deliveries))
    return redirect("/messages?box=sent")


@router.get("/messages/{delivery_id}")
def message_detail(delivery_id: int, user: User = Depends(current_user)):
    return redirect(f"/messages?open={delivery_id}")


@router.post("/messages/{delivery_id}/read")
def message_mark_read(request: Request, delivery_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = db.get(MessageDelivery, delivery_id)
    if d is None or d.user_id != user.id:
        raise HTTPException(404, "Message not found")
    d.read_at = None if d.read_at else utcnow()
    db.commit()
    back = request.headers.get("referer", "")
    return redirect(back if "/messages" in back else f"/messages?open={delivery_id}")


# ---- boards -----------------------------------------------------------------


def _project_scopes(db: Session, user: User) -> list[tuple[str, str]]:
    """Where `user` may create boards: ('t:<id>', '*/…:<tenancy>') and ('d:<id>', '<dept>/…:<tenancy>')."""
    opts = []
    for t in _visible_tenancies(db, user):
        if can_manage_projects(user, t, None):
            opts.append((f"t:{t.id}", f"*/…:{t.name}"))
        opts.extend((f"d:{d.id}", f"{d.name}/…:{t.name}") for d in t.departments if can_manage_projects(user, t, d))
    return opts


@router.get("/boards")
def boards_index(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    boards = boardsvc.accessible_boards(db, user)
    stats = {b.key: boardsvc.board_stats(db, b) for b in boards}
    deletable = [b for b in boards if b.project and can_manage_projects(user, b.tenancy, b.department)]
    return render(request, "boards.html", user, boards=boards, stats=stats, scopes=_project_scopes(db, user),
                  deletable=deletable)


def _board_or_404(db: Session, user: User, key: str) -> boardsvc.Board:
    board = boardsvc.resolve_board(db, user, key)
    if board is None:
        raise HTTPException(404, "Board not found")
    return board


@router.get("/boards/{key:path}")
def board_view(
    request: Request,
    key: str,
    before: int | None = None,
    open: int | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    board = _board_or_404(db, user, key)
    posts = boardsvc.list_posts(db, board, limit=100, before_id=before)
    opened = db.get(Post, open) if open is not None else None
    if opened is not None and boardsvc.Board.of(opened).key != board.key:
        opened = None
    projects = []
    if board.project is None:
        scope_projects = board.department.projects if board.department else board.tenancy.projects
        projects = [(p, *boardsvc.board_stats(db, boardsvc.Board(board.tenancy, board.department, p)))
                    for p in scope_projects]
    can_moderate = bool(opened and can_moderate_post(user, opened))
    return render(
        request, "board.html", user, board=board, posts=posts, counts=boardsvc.comment_counts(db, posts),
        opened=opened, can_moderate=can_moderate, projects=projects,
        move_targets=[b for b in boardsvc.accessible_boards(db, user) if b.key != board.key]
        if opened and can_move_post(user, opened) else [],
        can_manage_projects=can_manage_projects(user, board.tenancy, board.department),
        scope_value=f"d:{board.department.id}" if board.department else f"t:{board.tenancy.id}",
        main_board=_main_board_url(board.tenancy, board.department),
        batch_posts=[p for p in posts if can_moderate_post(user, p)],
        batch_comments=[c for c in opened.comments if c.author_id == user.id or can_moderate] if opened else [],
    )


def _main_board_url(tenancy: Tenancy, department: Department | None) -> str | None:
    """URL of a scope's built-in board, or None while it is switched off."""
    if not boardsvc.is_enabled(tenancy, department, None):
        return None
    return f"/boards/{boardsvc.Board(tenancy, department).key}"


def _post_url(post: Post) -> str:
    return f"/boards/{boardsvc.Board.of(post).key}?open={post.id}"


@router.post("/boards/{key:path}/posts")
def board_post(
    request: Request,
    key: str,
    title: str = Form(...),
    body: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    board = _board_or_404(db, user, key)
    if not title.strip() or not body.strip():
        flash(request, "Title and body are required.", "error")
        return redirect(f"/boards/{board.key}")
    post = boardsvc.create_post(db, board, user, title, body)
    return redirect(_post_url(post))


def _post_or_404(db: Session, user: User, post_id: int) -> Post:
    post = db.get(Post, post_id)
    if post is None or not boardsvc.user_can_read_post(user, post):
        raise HTTPException(404, "Post not found")
    return post


@router.get("/posts/{post_id}")
def post_view(request: Request, post_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return redirect(_post_url(_post_or_404(db, user, post_id)))


@router.post("/posts/{post_id}/comments")
def post_comment(
    request: Request, post_id: int, body: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)
):
    post = _post_or_404(db, user, post_id)
    if body.strip():
        boardsvc.add_comment(db, post, user, body)
    return redirect(_post_url(post) + "#comments")


@router.post("/posts/{post_id}/delete")
def post_delete(request: Request, post_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    post = _post_or_404(db, user, post_id)
    if not can_moderate_post(user, post):
        raise forbid()
    key = boardsvc.Board.of(post).key
    db.delete(post)
    db.commit()
    flash(request, "Post deleted.", "success")
    return redirect(f"/boards/{key}")


@router.post("/posts/{post_id}/move")
def post_move(
    request: Request, post_id: int, board: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)
):
    post = _post_or_404(db, user, post_id)
    if not can_move_post(user, post):
        raise forbid()
    target = boardsvc.resolve_board(db, user, board)
    if target is None or (user.role < Role.ADMIN and target.tenancy.id != user.tenancy_id):
        raise HTTPException(404, "Board not found")
    source = boardsvc.Board.of(post).key
    if target.key != source:
        boardsvc.move_post(db, post, target)
        flash(request, "Post moved from {old} to {new}.", "success", old=source, new=target.key)
    return redirect(_post_url(post))


@router.post("/comments/{comment_id}/delete")
def comment_delete(request: Request, comment_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = db.get(Comment, comment_id)
    if c is None or not boardsvc.user_can_read_post(user, c.post):
        raise HTTPException(404, "Comment not found")
    if c.author_id != user.id and not can_moderate_post(user, c.post):
        raise forbid()
    url = _post_url(c.post) + "#comments"
    db.delete(c)
    db.commit()
    return redirect(url)


@router.post("/posts/batch-delete")
def posts_batch_delete(
    request: Request,
    ids: list[int] = Form([]),
    next: str = Form("/boards"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deleted = 0
    for post in db.scalars(select(Post).where(Post.id.in_(ids))):
        if boardsvc.user_can_read_post(user, post) and can_moderate_post(user, post):
            db.delete(post)
            deleted += 1
    db.commit()
    _flash_batch(request, deleted, len(ids))
    return redirect(safe_next(next, "/boards"))


@router.post("/comments/batch-delete")
def comments_batch_delete(
    request: Request,
    ids: list[int] = Form([]),
    next: str = Form("/boards"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deleted = 0
    for c in db.scalars(select(Comment).where(Comment.id.in_(ids))):
        if boardsvc.user_can_read_post(user, c.post) and (c.author_id == user.id or can_moderate_post(user, c.post)):
            db.delete(c)
            deleted += 1
    db.commit()
    _flash_batch(request, deleted, len(ids))
    return redirect(safe_next(next, "/boards"))


def _flash_batch(request: Request, deleted: int, requested: int) -> None:
    if deleted < requested:
        flash(request, "Deleted {n} of {total} selected item(s); you cannot delete the rest.", "warn",
              n=deleted, total=requested)
    else:
        flash(request, "Deleted {n} item(s).", "success", n=deleted)


# ---- created (project) boards: inside a department or tenancy-wide ---------


def _project_form(request: Request, name: str, title: str, description: str) -> tuple[str, str, str] | None:
    name = name.strip().lower()
    if not valid_name(name):
        flash(request, "Board name must be lowercase letters, digits, '.', '_' or '-'.", "error")
        return None
    return name, title.strip()[:255], description.strip()


def _create_project(
    request: Request, db: Session, user: User, tenancy: Tenancy, department: Department | None,
    name: str, title: str, description: str, back: str | None,
):
    if not can_access_board(user, tenancy.id, department.id if department else None):
        raise HTTPException(404, "Department not found" if department else "Tenancy not found")
    if not can_manage_projects(user, tenancy, department):
        raise forbid()
    back = back or _main_board_url(tenancy, department) or "/boards"
    form = _project_form(request, name, title, description)
    if form is None:
        return redirect(back)
    p = Project(tenancy_id=tenancy.id, department_id=department.id if department else None,
                name=form[0], title=form[1], description=form[2])
    db.add(p)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "That board already exists in this department." if department
              else "That board already exists in this tenancy.", "error")
        return redirect(back)
    key = boardsvc.Board(tenancy, department, p).key
    flash(request, "Project board {name} created.", "success", name=key)
    return redirect(f"/boards/{key}")


@router.post("/projects")
def project_create(
    request: Request,
    scope: str = Form(...),
    name: str = Form(...),
    title: str = Form(""),
    description: str = Form(""),
    next: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    tenancy, department = _parse_placement(db, scope)
    if tenancy is None:
        raise HTTPException(400, "Choose a tenancy or department.")
    return _create_project(request, db, user, tenancy, department, name, title, description,
                           safe_next(next, "") or None)


@router.post("/departments/{department_id}/projects")
def department_project_create(
    request: Request,
    department_id: int,
    name: str = Form(...),
    title: str = Form(""),
    description: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    d = db.get(Department, department_id)
    if d is None:
        raise HTTPException(404, "Department not found")
    return _create_project(request, db, user, d.tenancy, d, name, title, description, None)


def _managed_project(db: Session, user: User, project_id: int) -> Project:
    p = db.get(Project, project_id)
    if p is None or not can_access_board(user, p.tenancy_id, p.department_id):
        raise HTTPException(404, "Board not found")
    if not can_manage_projects(user, p.tenancy, p.department):
        raise forbid()
    return p


@router.post("/projects/{project_id}/edit")
def project_edit(
    request: Request,
    project_id: int,
    name: str = Form(...),
    title: str = Form(""),
    description: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    p = _managed_project(db, user, project_id)
    old_key = boardsvc.Board(p.tenancy, p.department, p).key
    form = _project_form(request, name, title, description)
    if form is None:
        return redirect(f"/boards/{old_key}")
    p.name, p.title, p.description = form
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "That board already exists in this department." if p.department
              else "That board already exists in this tenancy.", "error")
        return redirect(f"/boards/{old_key}")
    flash(request, "Board saved.", "success")
    return redirect(f"/boards/{boardsvc.Board(p.tenancy, p.department, p).key}")


@router.post("/projects/{project_id}/delete")
def project_delete(request: Request, project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _managed_project(db, user, project_id)
    back = _main_board_url(p.tenancy, p.department) or "/boards"
    db.delete(p)
    db.commit()
    flash(request, "Project board deleted (including its posts).", "success")
    return redirect(back)


@router.post("/projects/batch-delete")
def projects_batch_delete(
    request: Request, ids: list[int] = Form([]), user: User = Depends(current_user), db: Session = Depends(get_db)
):
    deleted = 0
    for p in db.scalars(select(Project).where(Project.id.in_(ids))):
        if can_access_board(user, p.tenancy_id, p.department_id) and can_manage_projects(user, p.tenancy, p.department):
            db.delete(p)
            deleted += 1
    db.commit()
    _flash_batch(request, deleted, len(ids))
    return redirect("/boards")


# ---- account, machines & API keys -------------------------------------------


@router.get("/account")
def account(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "account.html", user, unread=_unread_by_machine(db, user), new_mcp_key=None)


@router.post("/account/mcp-keys")
def mcp_key_create(request: Request, label: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    raw, prefix, digest = generate_mcp_key()
    db.add(McpKey(user_id=user.id, label=label.strip()[:128], prefix=prefix, key_hash=digest))
    db.commit()
    db.refresh(user)
    # The plaintext key is rendered once and never stored.
    return render(request, "account.html", user, unread=_unread_by_machine(db, user), new_mcp_key=raw)


@router.post("/mcp-keys/{key_id}/revoke")
def mcp_key_revoke(request: Request, key_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    k = db.get(McpKey, key_id)
    if k is None or not can_manage_credentials(user, k.user):
        raise HTTPException(404, "Key not found")
    if k.revoked_at is None:
        k.revoked_at = utcnow()
        db.commit()
    flash(request, "Key {prefix}… revoked.", "success", prefix=k.prefix)
    return redirect(_owner_url(user, k.user, "/account") + "#mcp")


@router.post("/account/password")
def change_password(
    request: Request,
    current: str = Form(...),
    new: str = Form(...),
    new2: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(current, user.password_hash):
        flash(request, "Current password is incorrect.", "error")
    elif len(new) < MIN_PASSWORD:
        flash(request, "Password must be at least {n} characters.", "error", n=MIN_PASSWORD)
    elif new != new2:
        flash(request, "Passwords do not match.", "error")
    else:
        user.password_hash = hash_password(new)
        db.commit()
        flash(request, "Password changed.", "success")
    return redirect("/account")


def _register_machine(request: Request, db: Session, owner: User, name: str, description: str, back: str):
    name = name.strip().lower()
    if not owner.can_message:
        flash(request, "Your account must belong to a department before registering machines.", "error")
    elif not valid_name(name):
        flash(request, "Machine name must be lowercase letters, digits, '.', '_' or '-'.", "error")
    elif any(m.name == name for m in owner.machines):
        flash(request, "You already have a machine with that name.", "error")
    else:
        m = Machine(user_id=owner.id, name=name, description=description.strip(), webhook_secret=generate_webhook_secret())
        db.add(m)
        db.commit()
        flash(request, "Machine registered: {address}", "success", address=m.address)
        return redirect(f"/machines/{m.id}")
    return redirect(back)


@router.post("/machines")
def machine_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return _register_machine(request, db, user, name, description, "/account")


@router.get("/machines/{machine_id}")
def machine_view(request: Request, machine_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = _managed_machine(db, user, machine_id)
    failed = db.scalar(
        select(func.count()).where(MessageDelivery.machine_id == m.id, MessageDelivery.webhook_status == "failed")
    )
    return render(request, "machine.html", user, m=m, new_key=None, failed_webhooks=failed)


@router.post("/machines/{machine_id}/keys")
def key_create(
    request: Request, machine_id: int, label: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)
):
    m = _managed_machine(db, user, machine_id)
    raw, prefix, digest = generate_api_key()
    db.add(ApiKey(machine_id=m.id, label=label.strip()[:128], prefix=prefix, key_hash=digest))
    db.commit()
    db.refresh(m)
    # The plaintext key is rendered once and never stored.
    return render(request, "machine.html", user, m=m, new_key=raw, failed_webhooks=0)


@router.post("/keys/{key_id}/revoke")
def key_revoke(request: Request, key_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    k = db.get(ApiKey, key_id)
    if k is None or not can_manage_credentials(user, k.machine.user):
        raise HTTPException(404, "Key not found")
    if k.revoked_at is None:
        k.revoked_at = utcnow()
        db.commit()
    flash(request, "Key {prefix}… revoked.", "success", prefix=k.prefix)
    return redirect(f"/machines/{k.machine_id}")


@router.post("/machines/{machine_id}/webhook")
def machine_webhook(
    request: Request,
    machine_id: int,
    webhook_url: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    m = _managed_machine(db, user, machine_id)
    url = webhook_url.strip()
    if url:
        error = validate_webhook_url(url)
        if error:
            flash(request, error, "error")
            return redirect(f"/machines/{m.id}")
    m.webhook_url = url or None
    db.commit()
    flash(request, "Webhook saved." if url else "Webhook removed.", "success")
    return redirect(f"/machines/{m.id}")


@router.post("/machines/{machine_id}/webhook/rotate")
def machine_webhook_rotate(request: Request, machine_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = _managed_machine(db, user, machine_id)
    m.webhook_secret = generate_webhook_secret()
    db.commit()
    flash(request, "Webhook secret rotated.", "success")
    return redirect(f"/machines/{m.id}")


@router.post("/machines/{machine_id}/webhook/retry")
def machine_webhook_retry(request: Request, machine_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = _managed_machine(db, user, machine_id)
    for d in db.scalars(select(MessageDelivery).where(MessageDelivery.machine_id == m.id, MessageDelivery.webhook_status == "failed")):
        d.webhook_status, d.webhook_attempts, d.webhook_next_at = "pending", 0, utcnow()
    db.commit()
    flash(request, "Failed webhook deliveries re-queued.", "success")
    return redirect(f"/machines/{m.id}")


@router.post("/machines/{machine_id}/delete")
def machine_delete(request: Request, machine_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = _managed_machine(db, user, machine_id)
    back = _owner_url(user, m.user, "/account")
    db.delete(m)
    db.commit()
    flash(request, "Machine deleted.", "success")
    return redirect(back)


# ---- administration ---------------------------------------------------------


def _require_admin_view(user: User) -> None:
    if not can_view_admin(user):
        raise forbid()


def _visible_tenancies(db: Session, user: User) -> list[Tenancy]:
    if user.role >= Role.ADMIN:
        return list(db.scalars(select(Tenancy).order_by(Tenancy.name)))
    return [user.tenancy] if user.tenancy else []


def _placements(db: Session, actor: User) -> list[tuple[str, str, str]]:
    """Options for the placement select: ('', global) / ('t:<id>', tenancy) / ('d:<id>', department)."""
    opts: list[tuple[str, str]] = []
    if actor.role >= Role.ADMIN:
        opts.append(("", "", "(global — Admins only)"))
    for t in _visible_tenancies(db, actor):
        if actor.role >= Role.TENANCY_MODERATOR:
            opts.append((f"t:{t.id}", f"*:{t.name}", "(tenancy only)"))
        for d in t.departments:
            if in_scope(actor, t.id, d.id):
                opts.append((f"d:{d.id}", f"{d.name}:{t.name}", ""))
    return opts


def _parse_placement(db: Session, value: str) -> tuple[Tenancy | None, Department | None]:
    if value.startswith("d:"):
        d = db.get(Department, int(value[2:]))
        if d is None:
            raise HTTPException(400, "Unknown department")
        return d.tenancy, d
    if value.startswith("t:"):
        t = db.get(Tenancy, int(value[2:]))
        if t is None:
            raise HTTPException(400, "Unknown tenancy")
        return t, None
    return None, None


def _managed_users(db: Session, actor: User, tenancy_id: int | None = None) -> list[User]:
    stmt = select(User).order_by(User.tenancy_id, User.department_id, User.username)
    if actor.role < Role.ADMIN:
        if actor.role >= Role.TENANCY_MODERATOR:
            stmt = stmt.where(User.tenancy_id == actor.tenancy_id)
        else:
            stmt = stmt.where(User.department_id == actor.department_id)
    if tenancy_id is not None:
        stmt = stmt.where(User.tenancy_id == tenancy_id)
    return list(db.scalars(stmt))


@router.get("/admin")
def admin_index(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require_admin_view(user)
    return render(
        request, "admin/index.html", user,
        tenancies=_visible_tenancies(db, user),
        user_count=len(_managed_users(db, user)),
        can_create_tenancy=can_create_tenancy(user),
    )


@router.get("/admin/tenancies")
def admin_tenancies(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require_admin_view(user)
    return render(request, "admin/tenancies.html", user, tenancies=_visible_tenancies(db, user),
                  can_create_tenancy=can_create_tenancy(user))


@router.post("/admin/tenancies")
def admin_tenancy_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not can_create_tenancy(user):
        raise forbid()
    name = name.strip().lower()
    if not valid_tenancy_name(name):
        flash(request, "Tenancy name must look like a domain, e.g. example.com.", "error")
        return redirect("/admin/tenancies")
    t = Tenancy(name=name, description=description.strip())
    db.add(t)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "A tenancy with that name already exists.", "error")
        return redirect("/admin/tenancies")
    flash(request, "Tenancy {name} created. Add departments next.", "success", name=name)
    return redirect(f"/admin/tenancies/{t.id}")


def _tenancy_or_404(db: Session, user: User, tenancy_id: int) -> Tenancy:
    t = db.get(Tenancy, tenancy_id)
    if t is None or (user.role < Role.ADMIN and user.tenancy_id != t.id):
        raise HTTPException(404, "Tenancy not found")
    return t


@router.get("/admin/tenancies/{tenancy_id}")
def admin_tenancy(request: Request, tenancy_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require_admin_view(user)
    t = _tenancy_or_404(db, user, tenancy_id)
    counts = dict(
        db.execute(
            select(User.department_id, func.count()).where(User.tenancy_id == t.id).group_by(User.department_id)
        ).all()
    )
    return render(
        request, "admin/tenancy.html", user, t=t, counts=counts,
        users=_managed_users(db, user, tenancy_id=t.id),
        can_manage=can_manage_tenancy(user, t), policy_roles=POLICY_ROLES,
    )


def _policy_value(raw: str) -> int | None:
    if raw == "":
        return None
    value = int(raw)
    if value not in [int(r) for r in POLICY_ROLES]:
        raise HTTPException(400, "Invalid policy role")
    return value


@router.post("/admin/tenancies/{tenancy_id}/policy")
def admin_tenancy_policy(
    request: Request,
    tenancy_id: int,
    description: str = Form(""),
    min_role_self_broadcast: str = Form(""),
    min_role_department_broadcast: str = Form(""),
    min_role_tenancy_broadcast: str = Form(""),
    allow_cross_tenancy_dm: bool = Form(False),
    board_setting: bool = Form(False),
    board_enabled: bool = Form(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    t = _tenancy_or_404(db, user, tenancy_id)
    if not can_manage_tenancy(user, t):
        raise forbid()
    if board_setting:  # forms without the switch leave it unchanged
        t.board_enabled = board_enabled
    t.description = description.strip()
    t.min_role_self_broadcast = _policy_value(min_role_self_broadcast)
    t.min_role_department_broadcast = _policy_value(min_role_department_broadcast)
    t.min_role_tenancy_broadcast = _policy_value(min_role_tenancy_broadcast)
    t.allow_cross_tenancy_dm = allow_cross_tenancy_dm
    db.commit()
    flash(request, "Tenancy policy saved.", "success")
    return redirect(f"/admin/tenancies/{t.id}")


@router.post("/admin/tenancies/{tenancy_id}/departments")
def admin_department_create(
    request: Request,
    tenancy_id: int,
    name: str = Form(...),
    description: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    t = _tenancy_or_404(db, user, tenancy_id)
    if not can_manage_tenancy(user, t):
        raise forbid()
    name = name.strip().lower()
    if not valid_name(name):
        flash(request, "Department name must be lowercase letters, digits, '.', '_' or '-'.", "error")
        return redirect(f"/admin/tenancies/{t.id}")
    db.add(Department(tenancy_id=t.id, name=name, description=description.strip()))
    try:
        db.commit()
        flash(request, "Department {name} created.", "success", name=f"{name}:{t.name}")
    except IntegrityError:
        db.rollback()
        flash(request, "That department already exists.", "error")
    return redirect(f"/admin/tenancies/{t.id}")


@router.post("/admin/departments/{department_id}/edit")
def admin_department_edit(
    request: Request,
    department_id: int,
    name: str = Form(...),
    description: str = Form(""),
    board_setting: bool = Form(False),
    board_enabled: bool = Form(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    d = db.get(Department, department_id)
    if d is None:
        raise HTTPException(404, "Department not found")
    if not can_manage_tenancy(user, d.tenancy):
        raise forbid()
    name = name.strip().lower()
    if not valid_name(name):
        flash(request, "Department name must be lowercase letters, digits, '.', '_' or '-'.", "error")
        return redirect(f"/admin/tenancies/{d.tenancy_id}")
    old = d.name
    d.name, d.description = name, description.strip()
    if board_setting:
        d.board_enabled = board_enabled
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "That department already exists.", "error")
        return redirect(f"/admin/tenancies/{d.tenancy_id}")
    if old != name:
        # Addresses and board keys are derived from the name, so they change at once; API keys keep working.
        flash(request, "Department renamed from {old} to {new}. Member addresses changed accordingly.", "success",
              old=old, new=name)
    else:
        flash(request, "Department saved.", "success")
    return redirect(f"/admin/tenancies/{d.tenancy_id}")


@router.post("/admin/departments/{department_id}/delete")
def admin_department_delete(request: Request, department_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = db.get(Department, department_id)
    if d is None:
        raise HTTPException(404, "Department not found")
    if not can_manage_tenancy(user, d.tenancy):
        raise forbid()
    if db.scalar(select(func.count()).where(User.department_id == d.id)):
        flash(request, "Move or remove the department's users first.", "error")
    else:
        db.delete(d)
        db.commit()
        flash(request, "Department deleted (including its board).", "success")
    return redirect(f"/admin/tenancies/{d.tenancy_id}")


@router.post("/admin/tenancies/{tenancy_id}/departments/batch-delete")
def admin_departments_batch_delete(
    request: Request,
    tenancy_id: int,
    ids: list[int] = Form([]),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    t = _tenancy_or_404(db, user, tenancy_id)
    if not can_manage_tenancy(user, t):
        raise forbid()
    populated = set(db.scalars(select(User.department_id).where(User.department_id.in_(ids))))
    deleted = skipped = 0
    for d in db.scalars(select(Department).where(Department.id.in_(ids), Department.tenancy_id == t.id)):
        if d.id in populated:
            skipped += 1
        else:
            db.delete(d)
            deleted += 1
    db.commit()
    if skipped:
        flash(request, "{n} department(s) still have users and were kept. Move or remove their users first.",
              "warn", n=skipped)
    flash(request, "Deleted {n} item(s).", "success", n=deleted)
    return redirect(f"/admin/tenancies/{t.id}")


@router.get("/admin/users")
def admin_users(request: Request, kind: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require_admin_view(user)
    users = _managed_users(db, user)
    counts = {"": len(users), "human": sum(not u.is_agent for u in users), "agent": sum(u.is_agent for u in users)}
    if kind in ("human", "agent"):
        users = [u for u in users if u.is_agent == (kind == "agent")]
    else:
        kind = ""
    return render(request, "admin/users.html", user, users=users, kind=kind, kind_counts=counts,
                  can_manage_user=can_manage_user)


@router.post("/admin/users/batch-delete")
def admin_users_batch_delete(
    request: Request,
    ids: list[int] = Form([]),
    next: str = Form("/admin/users"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_admin_view(user)
    targets = [u.id for u in db.scalars(select(User).where(User.id.in_(ids))) if can_manage_user(user, u)]
    if targets:
        # Machines, keys and inboxes go with the account (ON DELETE CASCADE); posts and comments stay
        # under their recorded author address.
        db.execute(delete(User).where(User.id.in_(targets)))
        db.commit()
        db.expire_all()
    _flash_batch(request, len(targets), len(ids))
    return redirect(safe_next(next, "/admin/users"))


@router.get("/admin/users/new")
def admin_user_new(
    request: Request, placement: str = "", kind: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)
):
    _require_admin_view(user)
    return render(request, "admin/user_form.html", user, target=None, roles=assignable_roles(user),
                  placements=_placements(db, user),
                  form={"placement": placement, "role": int(Role.USER), "is_agent": kind == "agent"})


@router.post("/admin/users")
def admin_user_create(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    role: int = Form(...),
    placement: str = Form(""),
    is_agent: bool = Form(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_admin_view(user)
    username = username.strip().lower()
    tenancy, department = _parse_placement(db, placement)
    error = validate_placement(user, role, tenancy, department)
    if error is None and not valid_name(username):
        error = "Username must be lowercase letters, digits, '.', '_' or '-'."
    if error is None and len(password) < MIN_PASSWORD:
        error = "Password must be at least {n} characters."
    if error is None:
        new = User(
            username=username, display_name=display_name.strip(), password_hash=hash_password(password), role=role,
            tenancy_id=tenancy.id if tenancy else None, department_id=department.id if department else None,
            is_agent=is_agent,
        )
        db.add(new)
        try:
            db.commit()
            flash(request, "User {name} created.", "success", name=new.address() or username)
            return redirect(f"/admin/users/{new.id}")
        except IntegrityError:
            db.rollback()
            error = "That username is already taken in this tenancy."
    flash(request, error, "error", n=MIN_PASSWORD)
    return render(request, "admin/user_form.html", user, status_code=400, target=None, roles=assignable_roles(user),
                  placements=_placements(db, user),
                  form={"username": username, "display_name": display_name, "role": role, "placement": placement,
                        "is_agent": is_agent})


def _managed_target(db: Session, actor: User, user_id: int) -> User:
    target = db.get(User, user_id)
    if target is None or not can_manage_user(actor, target):
        raise HTTPException(404, "User not found or not manageable by you")
    return target


def _placement_value(u: User) -> str:
    if u.department_id:
        return f"d:{u.department_id}"
    if u.tenancy_id:
        return f"t:{u.tenancy_id}"
    return ""


def _user_page(request: Request, db: Session, user: User, target: User, new_mcp_key: str | None = None):
    return render(request, "admin/user_form.html", user, target=target, roles=assignable_roles(user),
                  placements=_placements(db, user), new_mcp_key=new_mcp_key,
                  manage_agent=can_manage_agent(user, target),
                  form={"username": target.username, "display_name": target.display_name, "role": target.role,
                        "placement": _placement_value(target), "is_active": target.is_active,
                        "is_agent": target.is_agent})


@router.get("/admin/users/{user_id}")
def admin_user(request: Request, user_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _user_page(request, db, user, _managed_target(db, user, user_id))


def _managed_agent(db: Session, actor: User, user_id: int) -> User:
    target = _managed_target(db, actor, user_id)
    if not target.is_agent:
        raise HTTPException(400, "Only agent accounts' machines and keys can be managed by administrators.")
    return target


@router.post("/admin/users/{user_id}/machines")
def admin_agent_machine_create(
    request: Request,
    user_id: int,
    name: str = Form(...),
    description: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    target = _managed_agent(db, user, user_id)
    return _register_machine(request, db, target, name, description, f"/admin/users/{target.id}")


@router.post("/admin/users/{user_id}/mcp-keys")
def admin_agent_mcp_key_create(
    request: Request, user_id: int, label: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)
):
    target = _managed_agent(db, user, user_id)
    raw, prefix, digest = generate_mcp_key()
    db.add(McpKey(user_id=target.id, label=label.strip()[:128], prefix=prefix, key_hash=digest))
    db.commit()
    db.refresh(target)
    # The plaintext key is rendered once and never stored.
    return _user_page(request, db, user, target, new_mcp_key=raw)


@router.post("/admin/users/{user_id}")
def admin_user_update(
    request: Request,
    user_id: int,
    display_name: str = Form(""),
    role: int = Form(...),
    placement: str = Form(""),
    is_active: bool = Form(False),
    is_agent: bool = Form(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    target = _managed_target(db, user, user_id)
    tenancy, department = _parse_placement(db, placement)
    error = validate_placement(user, role, tenancy, department)
    if error is None and department is None and target.machines:
        error = "Users with registered machines must stay in a department (delete machines first)."
    if error:
        flash(request, error, "error")
        return redirect(f"/admin/users/{target.id}")
    target.display_name = display_name.strip()
    target.role = role
    target.tenancy_id = tenancy.id if tenancy else None
    target.department_id = department.id if department else None
    target.is_active = is_active
    target.is_agent = is_agent
    try:
        db.commit()
        flash(request, "User updated.", "success")
    except IntegrityError:
        db.rollback()
        flash(request, "That username already exists in the target tenancy.", "error")
    return redirect(f"/admin/users/{target.id}")


@router.post("/admin/users/{user_id}/password")
def admin_user_password(
    request: Request, user_id: int, password: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)
):
    target = _managed_target(db, user, user_id)
    if len(password) < MIN_PASSWORD:
        flash(request, "Password must be at least {n} characters.", "error", n=MIN_PASSWORD)
    else:
        target.password_hash = hash_password(password)
        db.commit()
        flash(request, "Password reset.", "success")
    return redirect(f"/admin/users/{target.id}")
