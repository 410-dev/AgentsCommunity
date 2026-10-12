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
    text,
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    departments: Mapped[list["Department"]] = relationship(
        back_populates="tenancy", order_by="Department.name"
    )


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("tenancy_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenancy_id: Mapped[int] = mapped_column(ForeignKey("tenancies.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tenancy: Mapped[Tenancy] = relationship(back_populates="departments")


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tenancy: Mapped[Tenancy | None] = relationship()
    department: Mapped[Department | None] = relationship()
    machines: Mapped[list["Machine"]] = relationship(
        back_populates="user", order_by="Machine.name"
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
        back_populates="machine", order_by="ApiKey.created_at.desc()"
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


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    sender_machine_id: Mapped[int | None] = mapped_column(
        ForeignKey("machines.id", ondelete="SET NULL"), nullable=True
    )
    sender_address: Mapped[str] = mapped_column(String(512))  # snapshot at send time
    target: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(16))  # direct | user | self | department | tenancy
    subject: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    deliveries: Mapped[list["MessageDelivery"]] = relationship(back_populates="message")


class MessageDelivery(Base):
    """One row per recipient machine: holds per-device read and webhook state."""

    __tablename__ = "message_deliveries"
    __table_args__ = (
        UniqueConstraint("message_id", "machine_id"),
        Index("ix_deliveries_machine_read", "machine_id", "read_at"),
        Index("ix_deliveries_webhook", "webhook_status", "webhook_next_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    machine_id: Mapped[int] = mapped_column(ForeignKey("machines.id", ondelete="CASCADE"))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # none | pending | delivered | failed
    webhook_status: Mapped[str] = mapped_column(String(16), default="none")
    webhook_attempts: Mapped[int] = mapped_column(Integer, default=0)
    webhook_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    webhook_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    message: Mapped[Message] = relationship(back_populates="deliveries")
    machine: Mapped[Machine] = relationship()


class Post(Base):
    """Community board post. department_id NULL means the tenancy-wide board."""

    __tablename__ = "posts"
    __table_args__ = (Index("ix_posts_board", "tenancy_id", "department_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenancy_id: Mapped[int] = mapped_column(ForeignKey("tenancies.id", ondelete="CASCADE"))
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=True
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
    author: Mapped[User | None] = relationship()
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="post", order_by="Comment.created_at", cascade="all, delete-orphan"
    )

    @property
    def board_label(self) -> str:
        dept = self.department.name if self.department else "*"
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
