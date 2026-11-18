"""Subscriptions: follow a board (notified of new posts) or a post (notified of new comments).

Notifications are system messages (kind `post` or `comment`) delivered to every inbox of each subscriber,
exactly like a message to `<id>:*@…`: each of their machines, or the account inbox when they have none.
Those inboxes' webhooks fire as for any other message, so subscribers can be pinged on Discord, Telegram,
Slack or their own server. Authors are not notified of their own posts and comments, and subscribers who
can no longer read the board are skipped.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .boards import Board
from .config import absolute_url
from .messaging import notify
from .models import Comment, Post, Subscription, User
from .permissions import can_access_board

NOTIFY_BODY_MAX = 4000


def board_subscription(db: Session, user: User, board: Board) -> Subscription | None:
    return db.scalar(select(Subscription).where(
        Subscription.user_id == user.id, Subscription.post_id.is_(None), *board.where(Subscription)))


def post_subscription(db: Session, user: User, post: Post) -> Subscription | None:
    return db.scalar(select(Subscription).where(Subscription.user_id == user.id, Subscription.post_id == post.id))


def subscribe_board(db: Session, user: User, board: Board) -> tuple[Subscription, bool]:
    """(subscription, created). Subscribing twice is harmless."""
    existing = board_subscription(db, user, board)
    if existing is not None:
        return existing, False
    sub = Subscription(user_id=user.id, **board.ids())
    db.add(sub)
    db.commit()
    return sub, True


def subscribe_post(db: Session, user: User, post: Post) -> tuple[Subscription, bool]:
    existing = post_subscription(db, user, post)
    if existing is not None:
        return existing, False
    sub = Subscription(user_id=user.id, post_id=post.id)
    db.add(sub)
    try:
        db.commit()
    except IntegrityError:  # a concurrent request subscribed first
        db.rollback()
        return post_subscription(db, user, post), False
    return sub, True


def unsubscribe(db: Session, sub: Subscription | None) -> bool:
    if sub is None:
        return False
    db.delete(sub)
    db.commit()
    return True


def user_subscriptions(db: Session, user: User) -> list[Subscription]:
    """The user's subscriptions on boards they can still read (others stay stored but are inactive)."""
    subs = db.scalars(select(Subscription).where(Subscription.user_id == user.id).order_by(Subscription.id))
    return [s for s in subs if active(user, s)]


def active(user: User, sub: Subscription) -> bool:
    board = board_of(sub)
    return board.enabled and can_access_board(user, board.tenancy.id, board.department.id if board.department else None)


def board_of(sub: Subscription) -> Board:
    """The subscribed board, or the board the subscribed post is on now."""
    if sub.post is not None:
        return Board.of(sub.post)
    return Board(sub.tenancy, sub.department, sub.project)


def subscription_dict(sub: Subscription) -> dict:
    board = board_of(sub)
    data = {"id": sub.id, "type": sub.kind, "board": board.key, "created_at": sub.created_at.isoformat()}
    if sub.post is not None:
        data.update(post_id=sub.post.id, title=sub.post.title)
    return data


def _readers(subs, post: Post, author_id: int | None) -> list[User]:
    """Active subscribers who can still read the post's board, without the author."""
    users = {}
    for sub in subs:
        u = sub.user
        if u.id == author_id or not u.is_active or not can_access_board(u, post.tenancy_id, post.department_id):
            continue
        users[u.id] = u
    return list(users.values())


def _excerpt(text: str) -> str:
    text = text.strip()
    if len(text) <= NOTIFY_BODY_MAX:
        return text
    return text[:NOTIFY_BODY_MAX].rstrip() + "\n… (truncated; read the full text with its post_id)"


def post_link(post: Post) -> str:
    return absolute_url(f"/boards/{Board.of(post).key}?open={post.id}")


def notify_new_post(db: Session, post: Post) -> None:
    """Tell the board's subscribers about a new post. Call after flushing the post; the caller commits."""
    board = Board.of(post)
    subs = db.scalars(select(Subscription).where(Subscription.post_id.is_(None), *board.where(Subscription)))
    users = _readers(subs, post, post.author_id)
    if not users:
        return
    body = (f"New post on {board.key}\n"
            f"Title: {post.title}\n"
            f"Author: {post.author_address}\n"
            f"Link: {post_link(post)}\n\n"
            f"{_excerpt(post.body)}")
    notify(db, users, f"subscription:notify@{board.key}", board.key, "post",
           f"New post on {board.key}: {post.title}", body, post_id=post.id)


def notify_new_comment(db: Session, comment: Comment) -> None:
    """Tell the post's subscribers about a new comment. Call after flushing the comment; the caller commits."""
    post = comment.post
    subs = db.scalars(select(Subscription).where(Subscription.post_id == post.id))
    users = _readers(subs, post, comment.author_id)
    if not users:
        return
    board = Board.of(post)
    body = (f"New comment on post #{post.id} \"{post.title}\" ({board.key})\n"
            f"Author: {comment.author_address}\n"
            f"Link: {post_link(post)}#comments\n\n"
            f"{_excerpt(comment.body)}")
    notify(db, users, f"subscription:notify@{board.key}", board.key, "comment",
           f"New comment on #{post.id}: {post.title}", body, post_id=post.id, comment_id=comment.id)
