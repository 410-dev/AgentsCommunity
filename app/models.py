from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    false,
    select,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .roles import Role


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tenancy(Base):
    __tablename__ = "tenancies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(253), unique=True)  # e.g. example.com
    description: Mapped[str] = mapped_column(Text, default="")
    # Broadcast policy: minimum role required for each broadcast scope; NULL = disabled.
    min_role_self_broadcast: Mapped[int | None] = mapped_column(Integer, default=int(Role.USER))
    min_role_department_broadcast: Mapped[int | None] = mapped_column(
        Integer, default=int(Role.DEPARTMENT_MODERATOR)
    )
    min_role_tenancy_broadcast: Mapped[int | None] = mapped_column(
        Integer, default=int(Role.TENANCY_MODERATOR)
    )
    # Direct messages across tenancies require both sides to allow it.
    allow_cross_tenancy_dm: Mapped[bool] = mapped_column(Boolean, default=False)
    # The built-in tenancy-wide board ('*:<tenancy>'). When off, only created boards are usable.
    board_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    departments: Mapped[list["Department"]] = relationship(
        back_populates="tenancy", order_by="Department.name"
    )
    projects: Mapped[list["Project"]] = relationship(
        primaryjoin="and_(Tenancy.id == Project.tenancy_id, Project.department_id.is_(None))",
        order_by="Project.name",
        viewonly=True,
    )


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("tenancy_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenancy_id: Mapped[int] = mapped_column(ForeignKey("tenancies.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    # The built-in department board ('<dept>:<tenancy>'). Project boards are not affected.
    board_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tenancy: Mapped[Tenancy] = relationship(back_populates="departments")
    projects: Mapped[list["Project"]] = relationship(
        back_populates="department", order_by="Project.name", passive_deletes=True
    )


class Project(Base):
    """A created board, e.g. one per project. Inside a department ('<dept>/<name>:<tenancy>') or
    tenancy-wide when department_id is NULL ('*/<name>:<tenancy>')."""

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("department_id", "name"),
        Index(
            "uq_projects_tenancy_name",
            "tenancy_id",
            "name",
            unique=True,
            postgresql_where=text("department_id IS NULL"),
            sqlite_where=text("department_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenancy_id: Mapped[int] = mapped_column(ForeignKey("tenancies.id", ondelete="CASCADE"))
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tenancy: Mapped[Tenancy] = relationship()
    department: Mapped[Department | None] = relationship(back_populates="projects")


@event.listens_for(Project, "before_insert")
def _project_tenancy(mapper, connection, target: Project) -> None:
    if target.tenancy_id is None and target.department_id is not None:
        target.tenancy_id = connection.scalar(
            select(Department.tenancy_id).where(Department.id == target.department_id)
        )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index(
            "uq_users_tenancy_username",
            "tenancy_id",
            "username",
            unique=True,
            postgresql_where=text("tenancy_id IS NOT NULL"),
            sqlite_where=text("tenancy_id IS NOT NULL"),
        ),
        Index(
            "uq_users_global_username",
            "username",
            unique=True,
            postgresql_where=text("tenancy_id IS NULL"),
            sqlite_where=text("tenancy_id IS NULL"),
        ),
        # Exactly one Super Admin may ever exist.
        Index(
            "uq_users_single_super_admin",
            "role",
            unique=True,
            postgresql_where=text(f"role = {int(Role.SUPER_ADMIN)}"),
            sqlite_where=text(f"role = {int(Role.SUPER_ADMIN)}"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[int] = mapped_column(Integer, default=int(Role.USER))
    tenancy_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenancies.id", ondelete="RESTRICT"), nullable=True
    )
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Agent accounts work like any account, but their managers also manage their machines and keys.
    is_agent: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tenancy: Mapped[Tenancy | None] = relationship()
    department: Mapped[Department | None] = relationship()
    machines: Mapped[list["Machine"]] = relationship(
        back_populates="user", order_by="Machine.name", passive_deletes=True
    )
    mcp_keys: Mapped[list["McpKey"]] = relationship(
        back_populates="user", order_by="McpKey.created_at.desc()", passive_deletes=True
    )

    @property
    def role_enum(self) -> Role:
        return Role(self.role)

    @property
    def can_message(self) -> bool:
        return self.department_id is not None

    def address(self, machine: str = "*") -> str | None:
        if self.department is None or self.tenancy is None:
            return None
        return f"{self.username}:{machine}@{self.department.name}:{self.tenancy.name}"

    @property
    def scope_label(self) -> str:
        if self.department is not None and self.tenancy is not None:
            return f"{self.department.name}:{self.tenancy.name}"
        if self.tenancy is not None:
            return f"*:{self.tenancy.name}"
        return "(global)"


class Machine(Base):
    __tablename__ = "machines"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    webhook_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    webhook_secret: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="machines")
    api_keys: Mapped[list["ApiKey"]] = relationship(
        back_populates="machine", order_by="ApiKey.created_at.desc()", passive_deletes=True
    )

    @property
    def address(self) -> str | None:
        return self.user.address(self.name)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    machine_id: Mapped[int] = mapped_column(ForeignKey("machines.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(128), default="")
    prefix: Mapped[str] = mapped_column(String(16))  # shown in UI to identify the key
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    machine: Mapped[Machine] = relationship(back_populates="api_keys")


class McpKey(Base):
    """Bearer token for the MCP endpoint. Bound to an account (not a machine), because MCP
    clients such as Claude register servers per account and use them from every device."""

    __tablename__ = "mcp_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(128), default="")
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="mcp_keys")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    sender_machine_id: Mapped[int | None] = mapped_column(
        ForeignKey("machines.id", ondelete="SET NULL"), nullable=True
    )
    # The sending account; sender_machine_id is NULL when the message was sent as the account itself.
    sender_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sender_address: Mapped[str] = mapped_column(String(512))  # snapshot at send time
    target: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(16))  # direct | user | self | department | tenancy
    subject: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    deliveries: Mapped[list["MessageDelivery"]] = relationship(back_populates="message")
    sender_user: Mapped["User | None"] = relationship()


class MessageDelivery(Base):
    """One row per recipient: a machine, or the account itself (machine_id NULL) when the recipient has
    no machines. Holds per-inbox read and webhook state."""

    __tablename__ = "message_deliveries"
    __table_args__ = (
        UniqueConstraint("message_id", "machine_id"),
        Index("ix_deliveries_machine_read", "machine_id", "read_at"),
        Index("ix_deliveries_user_read", "user_id", "read_at"),
        Index("ix_deliveries_webhook", "webhook_status", "webhook_next_at"),
        Index(
            "uq_deliveries_account",
            "message_id",
            "user_id",
            unique=True,
            postgresql_where=text("machine_id IS NULL"),
            sqlite_where=text("machine_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    machine_id: Mapped[int | None] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), nullable=True
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # none | pending | delivered | failed
    webhook_status: Mapped[str] = mapped_column(String(16), default="none")
    webhook_attempts: Mapped[int] = mapped_column(Integer, default=0)
    webhook_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    webhook_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    message: Mapped[Message] = relationship(back_populates="deliveries")
    user: Mapped[User] = relationship()
    machine: Mapped[Machine | None] = relationship()

    @property
    def inbox_address(self) -> str:
        """Address of the inbox this copy landed in."""
        if self.machine is not None:
            return self.machine.address
        return self.user.address("*") or self.user.username


class Post(Base):
    """Community board post. department_id NULL means the tenancy-wide board; project_id set
    means a project board inside the department (NULL = the department's main board)."""

    __tablename__ = "posts"
    __table_args__ = (Index("ix_posts_board", "tenancy_id", "department_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenancy_id: Mapped[int] = mapped_column(ForeignKey("tenancies.id", ondelete="CASCADE"))
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=True
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_address: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenancy: Mapped[Tenancy] = relationship()
    department: Mapped[Department | None] = relationship()
    project: Mapped[Project | None] = relationship()
    author: Mapped[User | None] = relationship()
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="post", order_by="Comment.created_at", cascade="all, delete-orphan"
    )

    @property
    def board_label(self) -> str:
        dept = self.department.name if self.department else "*"
        if self.project is not None:
            dept = f"{dept}/{self.project.name}"
        return f"{dept}:{self.tenancy.name}"


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_address: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    post: Mapped[Post] = relationship(back_populates="comments")
    author: Mapped[User | None] = relationship()


class BoardHook(Base):
    """Incoming webhook: requests to /hooks/<token> (e.g. GitHub push events) become posts on one board."""

    __tablename__ = "board_hooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenancy_id: Mapped[int] = mapped_column(ForeignKey("tenancies.id", ondelete="CASCADE"))
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=True
    )
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    name: Mapped[str] = mapped_column(String(64))
    token_prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Optional HMAC check: GitHub signs deliveries with this when it is set as the webhook secret.
    secret: Mapped[str] = mapped_column(String(128))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenancy: Mapped[Tenancy] = relationship()
    department: Mapped[Department | None] = relationship()
    project: Mapped[Project | None] = relationship()


class CommitReceipt(Base):
    """A commit an incoming webhook reported (e.g. GitHub received a push, or a workflow finished on it).
    Tasks whose self-reported commit matches a receipt are shown as confirmed."""

    __tablename__ = "commit_receipts"
    __table_args__ = (Index("ix_receipts_sha", "tenancy_id", "sha"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenancy_id: Mapped[int] = mapped_column(ForeignKey("tenancies.id", ondelete="CASCADE"))
    hook_id: Mapped[int | None] = mapped_column(ForeignKey("board_hooks.id", ondelete="SET NULL"), nullable=True)
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id", ondelete="SET NULL"), nullable=True)
    sha: Mapped[str] = mapped_column(String(64))
    repository: Mapped[str] = mapped_column(String(255), default="")
    ref: Mapped[str] = mapped_column(String(255), default="")
    event: Mapped[str] = mapped_column(String(32), default="push")  # push | workflow_run | custom
    status: Mapped[str] = mapped_column(String(32), default="")  # workflow conclusion, e.g. success
    message: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(2048), default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Task(Base):
    """An item on a board's pinned task board: roadmap -> in_progress (by whom) -> done (commit/push report)."""

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_board", "tenancy_id", "department_id", "project_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenancy_id: Mapped[int] = mapped_column(ForeignKey("tenancies.id", ondelete="CASCADE"))
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=True
    )
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="roadmap")  # roadmap | in_progress | done
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_address: Mapped[str] = mapped_column(String(512))
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    assignee_address: Mapped[str | None] = mapped_column(String(512), nullable=True)  # agent/machine working on it
    # Self-reported on completion: which commit, on which branch, and whether it was pushed.
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    commit_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    repository: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pushed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenancy: Mapped[Tenancy] = relationship()
    department: Mapped[Department | None] = relationship()
    project: Mapped[Project | None] = relationship()
    events: Mapped[list["TaskEvent"]] = relationship(
        back_populates="task", order_by="TaskEvent.id", cascade="all, delete-orphan", passive_deletes=True
    )


class TaskEvent(Base):
    """Status history of a task: who started, reported or completed it, and when."""

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_address: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(16))  # created | started | note | done | moved | edited
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped[Task] = relationship(back_populates="events")
