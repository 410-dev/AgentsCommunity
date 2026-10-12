"""Collect UI translation keys from templates and Python sources (used by tests/test_i18n.py)."""

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

_TEMPLATE_CALL = re.compile(r"""_\(\s*(["'])((?:(?!\1).)+)\1""")

# Python calls whose first string argument (after an optional status/request) is shown in the UI.
_PY_CALLS = {"flash", "MessagingError", "HTTPException"}


def template_keys() -> set[str]:
    keys = set()
    for path in (APP / "templates").rglob("*.html"):
        for m in _TEMPLATE_CALL.finditer(path.read_text(encoding="utf-8")):
            keys.add(m.group(2))
    return keys


def python_keys() -> set[str]:
    keys = set()
    for path in [APP / "routers" / "web.py", APP / "messaging.py", APP / "permissions.py", APP / "deps.py", APP / "webhooks.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in _PY_CALLS:
                    # The message is the 2nd positional argument (after request / status) or detail=.
                    candidates = node.args[1:2] + [k.value for k in node.keywords if k.arg == "detail"]
                    for arg in candidates:
                        for sub in ast.walk(arg):  # also covers "a" if cond else "b"
                            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                                keys.add(sub.value)
            # `error = "..."` assignments and `return "..."` in validators
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "error" for t in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    keys.add(node.value.value)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                if path.name in ("permissions.py", "webhooks.py"):
                    keys.add(node.value.value)
            # dict literals of message templates (messaging._POLICY_*)
            if isinstance(node, ast.Dict) and path.name == "messaging.py":
                for v in node.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        keys.add(v.value)
    return keys


def all_keys() -> set[str]:
    from app.roles import ROLE_LABELS

    extra = {label for label in ROLE_LABELS.values()} | {"Disabled"}
    extra |= {"direct", "user", "self", "department", "tenancy"}  # message kinds
    extra |= {"(global — Admins only)", "(tenancy only)"}
    return template_keys() | python_keys() | extra


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(APP.parent))
    for k in sorted(all_keys()):
        print(repr(k))
