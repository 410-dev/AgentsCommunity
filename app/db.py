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
    if "sender_user_id" not in columns("messages"):
        with bind.begin() as conn:
            conn.execute(text("ALTER TABLE messages ADD COLUMN sender_user_id INTEGER "
                              "REFERENCES users(id) ON DELETE SET NULL"))
            conn.execute(text("UPDATE messages SET sender_user_id = "
                              "(SELECT user_id FROM machines WHERE machines.id = messages.sender_machine_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_messages_sender_user_id ON messages (sender_user_id)"))
    if "user_id" not in columns("message_deliveries"):
        _migrate_account_deliveries(bind)


def _migrate_tenancy_projects(bind) -> None:
    """Projects gained tenancy_id and an optional department_id (tenancy-wide created boards)."""
    if bind.dialect.name != "sqlite":
        with bind.begin() as conn:
            conn.execute(text("ALTER TABLE projects ADD COLUMN tenancy_id INTEGER REFERENCES tenancies(id) ON DELETE CASCADE"))
            conn.execute(text("UPDATE projects SET tenancy_id = "
                              "(SELECT tenancy_id FROM departments WHERE departments.id = projects.department_id)"))
            conn.execute(text("ALTER TABLE projects ALTER COLUMN tenancy_id SET NOT NULL"))
            conn.execute(text("ALTER TABLE projects ALTER COLUMN department_id DROP NOT NULL"))
    else:
        _sqlite_rebuild(bind, "projects", "id, tenancy_id, department_id, name, title, description, created_at",
                        "SELECT p.id, d.tenancy_id, p.department_id, p.name, p.title, p.description, p.created_at "
                        "FROM projects p JOIN departments d ON d.id = p.department_id")
    _create_indexes(bind, "projects")


def _migrate_account_deliveries(bind) -> None:
    """Deliveries gained user_id (the recipient account) and an optional machine_id (account inbox)."""
    if bind.dialect.name != "sqlite":
        with bind.begin() as conn:
            conn.execute(text("ALTER TABLE message_deliveries ADD COLUMN user_id INTEGER "
                              "REFERENCES users(id) ON DELETE CASCADE"))
            conn.execute(text("UPDATE message_deliveries SET user_id = "
                              "(SELECT user_id FROM machines WHERE machines.id = message_deliveries.machine_id)"))
            conn.execute(text("ALTER TABLE message_deliveries ALTER COLUMN user_id SET NOT NULL"))
            conn.execute(text("ALTER TABLE message_deliveries ALTER COLUMN machine_id DROP NOT NULL"))
    else:
        cols = "id, message_id, machine_id, read_at, webhook_status, webhook_attempts, webhook_next_at, webhook_last_error"
        _sqlite_rebuild(bind, "message_deliveries", f"{cols}, user_id",
                        f"SELECT {', '.join('d.' + c.strip() for c in cols.split(','))}, m.user_id "
                        "FROM message_deliveries d JOIN machines m ON m.id = d.machine_id")
    _create_indexes(bind, "message_deliveries")


def _create_indexes(bind, table: str) -> None:
    for index in Base.metadata.tables[table].indexes:
        index.create(bind, checkfirst=True)


def _sqlite_rebuild(bind, table: str, columns: str, select_sql: str) -> None:
    """SQLite cannot change a column's nullability, so rebuild the table from the current model. The new
    table is built first and renamed last: renaming the old one would repoint foreign keys at it."""
    from sqlalchemy.schema import CreateTable

    ddl = str(CreateTable(Base.metadata.tables[table]).compile(dialect=bind.dialect))
    prefix = f"CREATE TABLE {table} ("
    assert ddl.strip().startswith(prefix), ddl
    ddl = ddl.strip().replace(prefix, f"CREATE TABLE {table}_new (", 1)
    with bind.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()
        try:
            with conn.begin():
                conn.execute(text(ddl))
                conn.execute(text(f"INSERT INTO {table}_new ({columns}) {select_sql}"))
                conn.execute(text(f"DROP TABLE {table}"))
                conn.execute(text(f"ALTER TABLE {table}_new RENAME TO {table}"))
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.commit()
