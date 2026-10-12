from enum import IntEnum


class Role(IntEnum):
    USER = 10
    DEPARTMENT_MODERATOR = 20
    TENANCY_MODERATOR = 30
    ADMIN = 40
    SUPER_ADMIN = 50

    @property
    def label(self) -> str:
        return ROLE_LABELS[self]


ROLE_LABELS = {
    Role.USER: "User",
    Role.DEPARTMENT_MODERATOR: "Department Moderator",
    Role.TENANCY_MODERATOR: "Tenancy Moderator",
    Role.ADMIN: "Admin",
    Role.SUPER_ADMIN: "Super Admin",
}

# Roles that may be chosen as a minimum level for a broadcast policy.
POLICY_ROLES = [Role.USER, Role.DEPARTMENT_MODERATOR, Role.TENANCY_MODERATOR, Role.ADMIN]


def role_label(value: int | None) -> str:
    if value is None:
        return "Disabled"
    return Role(value).label
