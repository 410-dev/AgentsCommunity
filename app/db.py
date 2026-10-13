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
    insp = inspect(bind)

    def columns(table: str) -> set[str]:
        return {c["name"] for c in insp.get_columns(table)}

    if "project_id" not in columns("posts"):
        with bind.begin() as conn:
            conn.execute(text(
                "ALTER TABLE posts ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE"
            ))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_posts_project_id ON posts (project_id)"))
    for table, column, default in [
        ("tenancies", "board_enabled", "true"),
        ("departments", "board_enabled", "true"),
        ("users", "is_agent", "false"),
    ]:
        if column not in columns(table):
            if bind.dialect.name == "sqlite":
                default = "1" if default == "true" else "0"
            with bind.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} BOOLEAN NOT NULL DEFAULT {default}"))
    if "tenancy_id" not in columns("projects"):
        _migrate_tenancy_projects(bind)


def _migrate_tenancy_projects(bind) -> None:
    """Projects gained tenancy_id and an optional department_id (tenancy-wide created boards)."""
    from .models import Project

    if bind.dialect.name != "sqlite":
        with bind.begin() as conn:
            conn.execute(text("ALTER TABLE projects ADD COLUMN tenancy_id INTEGER REFERENCES tenancies(id) ON DELETE CASCADE"))
            conn.execute(text("UPDATE projects SET tenancy_id = "
                              "(SELECT tenancy_id FROM departments WHERE departments.id = projects.department_id)"))
            conn.execute(text("ALTER TABLE projects ALTER COLUMN tenancy_id SET NOT NULL"))
            conn.execute(text("ALTER TABLE projects ALTER COLUMN department_id DROP NOT NULL"))
    else:
        # SQLite cannot change a column's nullability, so rebuild the table. Build the new one first and
        # rename it last: renaming the old table would repoint posts.project_id at it.
        with bind.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.commit()
            try:
                with conn.begin():
                    conn.execute(text(
                        "CREATE TABLE projects_new (id INTEGER NOT NULL PRIMARY KEY, "
                        "tenancy_id INTEGER NOT NULL REFERENCES tenancies(id) ON DELETE CASCADE, "
                        "department_id INTEGER REFERENCES departments(id) ON DELETE CASCADE, "
                        "name VARCHAR(64) NOT NULL, title VARCHAR(255) NOT NULL, description TEXT NOT NULL, "
                        "created_at DATETIME NOT NULL, UNIQUE (department_id, name))"
                    ))
                    conn.execute(text(
                        "INSERT INTO projects_new (id, tenancy_id, department_id, name, title, description, created_at) "
                        "SELECT p.id, d.tenancy_id, p.department_id, p.name, p.title, p.description, p.created_at "
                        "FROM projects p JOIN departments d ON d.id = p.department_id"
                    ))
                    conn.execute(text("DROP TABLE projects"))
                    conn.execute(text("ALTER TABLE projects_new RENAME TO projects"))
            finally:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                conn.commit()
    for index in Project.__table__.indexes:
        index.create(bind, checkfirst=True)
