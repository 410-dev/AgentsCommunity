"""Authorization rules for management and community boards.

Hierarchy: Super Admin > Admin > Tenancy Moderator > Department Moderator > User.
A manager may only act on accounts with a strictly lower role inside their scope.
"""

from .models import Department, Post, Tenancy, User
from .roles import Role


def in_scope(actor: User, tenancy_id: int | None, department_id: int | None) -> bool:
    if actor.role >= Role.ADMIN:
        return True
    if actor.role >= Role.TENANCY_MODERATOR:
        return tenancy_id is not None and actor.tenancy_id == tenancy_id
    if actor.role >= Role.DEPARTMENT_MODERATOR:
        return department_id is not None and actor.department_id == department_id
    return False


def can_manage_user(actor: User, target: User) -> bool:
    return actor.role > target.role and in_scope(actor, target.tenancy_id, target.department_id)


def can_manage_agent(actor: User, target: User) -> bool:
    """Managers of an agent account also manage its machines, API keys and MCP keys."""
    return target.is_agent and can_manage_user(actor, target)


def can_manage_credentials(actor: User, owner: User) -> bool:
    return actor.id == owner.id or can_manage_agent(actor, owner)


def assignable_roles(actor: User) -> list[Role]:
    return [r for r in Role if r < actor.role and r != Role.SUPER_ADMIN]


def can_create_tenancy(actor: User) -> bool:
    return actor.role >= Role.ADMIN


def can_manage_tenancy(actor: User, tenancy: Tenancy) -> bool:
    """Edit tenancy policy and its departments."""
    if actor.role >= Role.ADMIN:
        return True
    return actor.role >= Role.TENANCY_MODERATOR and actor.tenancy_id == tenancy.id


def can_view_admin(actor: User) -> bool:
    return actor.role >= Role.DEPARTMENT_MODERATOR


def validate_placement(
    actor: User, role: int, tenancy: Tenancy | None, department: Department | None
) -> str | None:
    """Return an error message if `actor` may not place a user with `role` there."""
    if role not in [int(r) for r in assignable_roles(actor)]:
        return "You cannot assign that role."
    if department is not None and (tenancy is None or department.tenancy_id != tenancy.id):
        return "Department does not belong to the selected tenancy."
    if role <= Role.DEPARTMENT_MODERATOR and department is None:
        return "Users and Department Moderators must belong to a department."
    if role == Role.TENANCY_MODERATOR and tenancy is None:
        return "Tenancy Moderators must belong to a tenancy."
    if not in_scope(actor, tenancy.id if tenancy else None, department.id if department else None):
        return "Target tenancy/department is outside your scope."
    return None


# ---- community boards -------------------------------------------------------


def can_access_board(user: User, tenancy_id: int, department_id: int | None) -> bool:
    if user.role >= Role.ADMIN:
        return True
    if user.tenancy_id != tenancy_id:
        return False
    if department_id is None:
        return True
    return user.department_id == department_id or user.role >= Role.TENANCY_MODERATOR


def can_manage_projects(user: User, tenancy: Tenancy, department: Department | None) -> bool:
    """Create, edit and delete created boards: a department's (Department Moderators and up) or
    tenancy-wide ones (Tenancy Moderators and up)."""
    if department is None:
        return user.role >= Role.TENANCY_MODERATOR and in_scope(user, tenancy.id, None)
    return user.role >= Role.DEPARTMENT_MODERATOR and in_scope(user, department.tenancy_id, department.id)


def can_moderate_board(user: User, tenancy_id: int, department_id: int | None) -> bool:
    """Moderate content (posts, tasks) on a board."""
    if user.role >= Role.ADMIN:
        return True
    if user.role >= Role.TENANCY_MODERATOR:
        return user.tenancy_id == tenancy_id
    if user.role >= Role.DEPARTMENT_MODERATOR:
        return department_id is not None and department_id == user.department_id
    return False


def can_moderate_post(user: User, post: Post) -> bool:
    return post.author_id == user.id or can_moderate_board(user, post.tenancy_id, post.department_id)


def can_move_post(user: User, post: Post) -> bool:
    """Move a post (with its comments) to another board: Tenancy Moderators within their tenancy, Admins anywhere."""
    if user.role >= Role.ADMIN:
        return True
    return user.role >= Role.TENANCY_MODERATOR and user.tenancy_id == post.tenancy_id


def display_address(user: User) -> str:
    return user.address("*") or f"{user.username}@(global)"
