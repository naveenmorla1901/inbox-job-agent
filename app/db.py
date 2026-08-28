from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event, inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from .config import ROOT, get_settings
from .models import State, utcnow

log = logging.getLogger(__name__)

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        if url.startswith("sqlite:///"):
            raw = url.replace("sqlite:///", "", 1)
            path = ROOT / raw if not raw.startswith("/") else None
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                url = f"sqlite:///{path}"
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
        if url.startswith("sqlite"):
            enforce_sqlite_foreign_keys(_engine)
    return _engine


def enforce_sqlite_foreign_keys(engine) -> None:
    """SQLite ignores foreign keys unless asked; Postgres never does.

    Without this a local run happily writes rows that Neon rejects.
    """

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection, _record):  # pragma: no cover - trivial
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def init_db() -> None:
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    add_missing_columns(engine)


def add_missing_columns(engine) -> None:
    """Poor man's migration: add columns that exist on the model but not in the table.

    Keeps databases created by an older version of the app usable without a migration tool.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            column_type = column.type.compile(engine.dialect)
            default = getattr(column.default, "arg", None)
            clause = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}'
            if isinstance(default, bool):
                clause += f" DEFAULT {int(default)}"
            elif isinstance(default, (int, float)):
                clause += f" DEFAULT {default}"
            elif isinstance(default, str):
                clause += f" DEFAULT '{default}'"
            with engine.begin() as conn:
                conn.execute(text(clause))
            log.info("added column %s.%s", table.name, column.name)


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session


def get_state(session: Session, key: str, default: str = "") -> str:
    row = session.get(State, key)
    return row.value if row else default


def set_state(session: Session, key: str, value: str) -> None:
    row = session.get(State, key)
    if row:
        row.value = value
        row.updated_at = utcnow()
    else:
        row = State(key=key, value=value)
    session.add(row)


def exists(session: Session, model, **filters) -> bool:
    stmt = select(model)
    for field, value in filters.items():
        stmt = stmt.where(getattr(model, field) == value)
    return session.exec(stmt.limit(1)).first() is not None
