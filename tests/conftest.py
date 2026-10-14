import os
import re
import tempfile

_tmp = tempfile.mkdtemp()
# TEST_DATABASE_URL runs the suite against another database, e.g. a throwaway Postgres (it is wiped).
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db"
os.environ["WEBHOOK_WORKER_ENABLED"] = "false"
os.environ["SECRET_KEY"] = "test-secret"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import ApiKey, Department, Machine, Tenancy, User  # noqa: E402
from app.roles import Role  # noqa: E402
from app.security import generate_api_key, generate_webhook_secret, hash_password  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    from app import models  # noqa: F401

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    with TestClient(create_app(start_worker=False)) as c:
        yield c


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


class World:
    """Seeds a tenancy layout and issues one API key per machine."""

    def __init__(self, db):
        self.db = db
        self.keys: dict[str, str] = {}

    def tenancy(self, name, **policy):
        t = Tenancy(name=name, **policy)
        self.db.add(t)
        self.db.commit()
        return t

    def dept(self, tenancy, name):
        d = Department(tenancy_id=tenancy.id, name=name)
        self.db.add(d)
        self.db.commit()
        return d

    def user(self, username, dept=None, role=Role.USER, machines=("laptop",), tenancy=None):
        u = User(
            username=username,
            password_hash=hash_password("password123"),
            role=int(role),
            tenancy_id=(dept.tenancy_id if dept else (tenancy.id if tenancy else None)),
            department_id=dept.id if dept else None,
        )
        self.db.add(u)
        self.db.commit()
        for name in machines:
            m = Machine(user_id=u.id, name=name, webhook_secret=generate_webhook_secret())
            self.db.add(m)
            self.db.commit()
            raw, prefix, digest = generate_api_key()
            self.db.add(ApiKey(machine_id=m.id, prefix=prefix, key_hash=digest))
            self.db.commit()
            self.keys[f"{username}:{name}"] = raw
        return u

    def auth(self, who):
        return {"Authorization": f"Bearer {self.keys[who]}"}


@pytest.fixture
def world(db):
    return World(db)
