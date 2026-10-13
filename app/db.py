from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs = {"connect_args": {"check_same_thread": False}}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(bind=None) -> None:
    from . import models  # noqa: F401  (register tables)

    bind = bind or engine
    Base.metadata.create_all(bind=bind)
    _migrate(bind)


def _migrate(bind) -> None:
    """Additive schema changes for databases created by older versions (create_all never alters tables)."""
    columns = {c["name"] for c in inspect(bind).get_columns("posts")}
    if "project_id" not in columns:
        with bind.begin() as conn:
            conn.execute(text(
                "ALTER TABLE posts ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_posts_project_id ON posts (project_id)"))
