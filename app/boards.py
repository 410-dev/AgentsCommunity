"""Community boards: one tenancy-wide board per tenancy, one main board per department, and any
number of project boards inside a department.

Board keys: '*:<tenancy>', '<dept>:<tenancy>' and '<dept>/<project>:<tenancy>'.
Posts persist, so members who join later can read the full history of boards they can access.
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Comment, Department, Post, Project, Tenancy, User
from .permissions import can_access_board, display_address
from .roles import Role


@dataclass
class Board:
    tenancy: Tenancy
    department: Department | None
    project: Project | None = None

    @classmethod
    def of(cls, post: Post) -> "Board":
        return cls(post.tenancy, post.department, post.project)

    @property
    def key(self) -> str:
        """Board key as used by the API and URLs: '<department|*>[/<project>]:<tenancy>'."""
        dept = self.department.name if self.department else "*"
        if self.project is not None:
            dept = f"{dept}/{self.project.name}"
        return f"{dept}:{self.tenancy.name}"

    @property
    def kind(self) -> str:
        return "project" if self.project else "department" if self.department else "tenancy"

    @property
    def title(self) -> str:
        if self.project is not None:
            return f"{self.department.name} / {self.project.title or self.project.name} @ {self.tenancy.name}"
        return f"{self.department.name} @ {self.tenancy.name}" if self.department else f"{self.tenancy.name} (all)"

    def where(self):
        """SQL conditions selecting this board's posts."""
        return [
            Post.tenancy_id == self.tenancy.id,
            Post.department_id == self.department.id if self.department else Post.department_id.is_(None),
            Post.project_id == self.project.id if self.project else Post.project_id.is_(None),
        ]

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "kind": self.kind,
            "description": self.project.description if self.project else "",
        }


def accessible_boards(db: Session, user: User) -> list[Board]:
    if user.role >= Role.ADMIN:
        tenancies = list(db.scalars(select(Tenancy).order_by(Tenancy.name)))
    elif user.tenancy is not None:
        tenancies = [user.tenancy]
    else:
        tenancies = []
    boards: list[Board] = []
    for t in tenancies:
        boards.append(Board(t, None))
        for d in t.departments:
            if can_access_board(user, t.id, d.id):
                boards.append(Board(t, d))
                boards.extend(Board(t, d, p) for p in d.projects)
    return boards


def resolve_board(db: Session, user: User, key: str) -> Board | None:
    """Resolve '<department|*>[/<project>]:<tenancy>' (the tenancy part may be left out for the
    user's own tenancy)."""
    dept_name, _, tenancy_name = key.strip().lower().partition(":")
    dept_name, _, project_name = dept_name.partition("/")
    if tenancy_name:
        tenancy = db.scalar(select(Tenancy).where(Tenancy.name == tenancy_name))
    else:
        tenancy = user.tenancy
    if tenancy is None:
        return None
    department = None
    if dept_name != "*":
        department = db.scalar(
            select(Department).where(Department.tenancy_id == tenancy.id, Department.name == dept_name)
        )
        if department is None:
            return None
    project = None
    if project_name:
        if department is None:
            return None  # project boards live inside departments
        project = db.scalar(
            select(Project).where(Project.department_id == department.id, Project.name == project_name)
        )
        if project is None:
            return None
    if not can_access_board(user, tenancy.id, department.id if department else None):
        return None
    return Board(tenancy, department, project)


def board_stats(db: Session, board: Board) -> tuple[int, object]:
    """(post count, time of the latest post)."""
    return db.execute(select(func.count(), func.max(Post.created_at)).where(*board.where())).one()


def list_posts(db: Session, board: Board, limit: int = 50, before_id: int | None = None) -> list[Post]:
    stmt = select(Post).where(*board.where())
    if before_id is not None:
        stmt = stmt.where(Post.id < before_id)
    return list(db.scalars(stmt.order_by(Post.id.desc()).limit(limit)))


def comment_counts(db: Session, posts: list[Post]) -> dict[int, int]:
    if not posts:
        return {}
    rows = db.execute(
        select(Comment.post_id, func.count())
        .where(Comment.post_id.in_([p.id for p in posts]))
        .group_by(Comment.post_id)
    )
    return dict(rows.all())


def create_post(db: Session, board: Board, author: User, title: str, body: str, address: str | None = None) -> Post:
    post = Post(
        tenancy_id=board.tenancy.id,
        department_id=board.department.id if board.department else None,
        project_id=board.project.id if board.project else None,
        author_id=author.id,
        author_address=address or display_address(author),
        title=title.strip()[:255],
        body=body,
    )
    db.add(post)
    db.commit()
    return post


def add_comment(db: Session, post: Post, author: User, body: str, address: str | None = None) -> Comment:
    comment = Comment(
        post_id=post.id, author_id=author.id, author_address=address or display_address(author), body=body
    )
    db.add(comment)
    db.commit()
    return comment


def move_post(db: Session, post: Post, board: Board) -> None:
    """Comments follow the post because they reference it by id."""
    post.tenancy_id = board.tenancy.id
    post.department_id = board.department.id if board.department else None
    post.project_id = board.project.id if board.project else None
    db.commit()
    db.refresh(post)


def user_can_read_post(user: User, post: Post) -> bool:
    return can_access_board(user, post.tenancy_id, post.department_id)


def post_dict(post: Post, comments: bool = False, comment_count: int | None = None) -> dict:
    data = {
        "id": post.id,
        "board": post.board_label,
        "author": post.author_address,
        "title": post.title,
        "body": post.body,
        "created_at": post.created_at.isoformat(),
        "updated_at": post.updated_at.isoformat() if post.updated_at else None,
    }
    if comments:
        data["comments"] = [
            {"id": c.id, "author": c.author_address, "body": c.body, "created_at": c.created_at.isoformat()}
            for c in post.comments
        ]
    elif comment_count is not None:
        data["comment_count"] = comment_count
    return data
