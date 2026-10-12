"""Address parsing, permission checks and fan-out for messages.

Address format: <id>:<machine>@<department>:<tenancy>
Allowed patterns ('*' is the broadcast wildcard):
    john:laptop@dev:example.com   direct   -> one machine
    john:*@dev:example.com        user     -> all of john's machines ("self" if john is the sender)
    *:*@dev:example.com           department broadcast
    *:*@*:example.com             tenancy broadcast
    ...@...:*                     never allowed (no cross-tenancy broadcast)
"""

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Department, Machine, Message, MessageDelivery, Tenancy, User, utcnow
from .roles import Role, role_label
from .security import valid_name, valid_tenancy_name

ADDRESS_RE = re.compile(r"^([^:@\s]+):([^:@\s]+)@([^:@\s]+):([^:@\s]+)$")
WILD = "*"


class MessagingError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Address:
    user: str
    machine: str
    department: str
    tenancy: str

    def __str__(self) -> str:
        return f"{self.user}:{self.machine}@{self.department}:{self.tenancy}"


def parse_address(raw: str) -> Address:
    m = ADDRESS_RE.match(raw.strip().lower())
    if not m:
        raise MessagingError(400, "Invalid address. Expected <id>:<machine>@<department>:<tenancy>.")
    addr = Address(*m.groups())
    for part in (addr.user, addr.machine, addr.department):
        if part != WILD and not valid_name(part):
            raise MessagingError(400, f"Invalid address segment: {part!r}")
    if addr.tenancy != WILD and not valid_tenancy_name(addr.tenancy):
        raise MessagingError(400, f"Invalid tenancy: {addr.tenancy!r}")
    return addr


def classify(addr: Address) -> str:
    if addr.tenancy == WILD:
        raise MessagingError(403, "Broadcasting to all tenancies is not allowed.")
    if addr.department == WILD:
        if addr.user != WILD or addr.machine != WILD:
            raise MessagingError(400, "Tenancy broadcast must be *:*@*:<tenancy>.")
        return "tenancy"
    if addr.user == WILD:
        if addr.machine != WILD:
            raise MessagingError(400, "Department broadcast must be *:*@<department>:<tenancy>.")
        return "department"
    if addr.machine == WILD:
        return "user"
    return "direct"


def _check_policy(sender: User, min_role: int | None, scope: str) -> None:
    if min_role is None:
        raise MessagingError(403, f"{scope} broadcasts are disabled by tenancy policy.")
    if sender.role < min_role:
        raise MessagingError(
            403, f"{scope} broadcasts require at least {role_label(min_role)} in this tenancy."
        )


def _active_machines(db: Session, *conditions) -> list[Machine]:
    stmt = select(Machine).join(User).where(User.is_active.is_(True), *conditions)
    return list(db.scalars(stmt))


def resolve_recipients(
    db: Session, sender_machine: Machine, addr: Address
) -> tuple[str, list[Machine]]:
    sender = sender_machine.user
    if not sender.is_active or not sender.can_message:
        raise MessagingError(403, "Sender account cannot send messages.")
    kind = classify(addr)

    tenancy = db.scalar(select(Tenancy).where(Tenancy.name == addr.tenancy))
    if tenancy is None:
        raise MessagingError(404, f"Unknown tenancy: {addr.tenancy}")
    same_tenancy = tenancy.id == sender.tenancy_id

    department = None
    if addr.department != WILD:
        department = db.scalar(
            select(Department).where(
                Department.tenancy_id == tenancy.id, Department.name == addr.department
            )
        )
        if department is None:
            raise MessagingError(404, f"Unknown department: {addr.department}:{addr.tenancy}")

    if kind == "tenancy":
        if not same_tenancy and sender.role < Role.ADMIN:
            raise MessagingError(403, "You may only broadcast to your own tenancy.")
        _check_policy(sender, tenancy.min_role_tenancy_broadcast, "Tenancy")
        machines = _active_machines(db, User.tenancy_id == tenancy.id)

    elif kind == "department":
        if not same_tenancy and sender.role < Role.ADMIN:
            raise MessagingError(403, "You may only broadcast to your own tenancy.")
        if department.id != sender.department_id and sender.role < Role.TENANCY_MODERATOR:
            raise MessagingError(403, "You may only broadcast to your own department.")
        _check_policy(sender, tenancy.min_role_department_broadcast, "Department")
        machines = _active_machines(db, User.department_id == department.id)

    else:  # user / direct
        target_user = db.scalar(
            select(User).where(
                User.tenancy_id == tenancy.id,
                User.username == addr.user,
                User.department_id == department.id,
                User.is_active.is_(True),
            )
        )
        if target_user is None:
            raise MessagingError(404, f"Unknown user: {addr.user}@{addr.department}:{addr.tenancy}")
        if not same_tenancy:
            if not (tenancy.allow_cross_tenancy_dm and sender.tenancy.allow_cross_tenancy_dm):
                raise MessagingError(403, "Cross-tenancy messages are not allowed by policy.")
        if kind == "user":
            if target_user.id == sender.id:
                kind = "self"
                _check_policy(sender, tenancy.min_role_self_broadcast, "Own-device")
            machines = list(target_user.machines)
        else:
            machine = next((m for m in target_user.machines if m.name == addr.machine), None)
            if machine is None:
                raise MessagingError(404, f"Unknown machine: {addr}")
            return kind, [machine]

    # Broadcasts never echo back to the sending machine.
    return kind, [m for m in machines if m.id != sender_machine.id]


def send_message(
    db: Session, sender_machine: Machine, target: str, body: str, subject: str = ""
) -> Message:
    if not body.strip():
        raise MessagingError(400, "Message body is empty.")
    addr = parse_address(target)
    kind, recipients = resolve_recipients(db, sender_machine, addr)
    msg = Message(
        sender_machine_id=sender_machine.id,
        sender_address=sender_machine.address,
        target=str(addr),
        kind=kind,
        subject=subject[:255],
        body=body,
    )
    db.add(msg)
    now = utcnow()
    for m in recipients:
        msg.deliveries.append(
            MessageDelivery(
                machine_id=m.id,
                webhook_status="pending" if m.webhook_url else "none",
                webhook_next_at=now if m.webhook_url else None,
            )
        )
    db.commit()
    return msg


def message_dict(msg: Message, delivery: MessageDelivery | None = None) -> dict:
    data = {
        "id": msg.id,
        "from": msg.sender_address,
        "to": msg.target,
        "kind": msg.kind,
        "subject": msg.subject,
        "body": msg.body,
        "created_at": msg.created_at.isoformat(),
    }
    if delivery is not None:
        data["delivery_id"] = delivery.id
        data["read_at"] = delivery.read_at.isoformat() if delivery.read_at else None
    else:
        data["recipient_count"] = len(msg.deliveries)
    return data
