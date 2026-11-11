"""SQLite storage. All values are stored as TEXT, like the Table API returns them."""

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .schema import TABLES, Table


def now() -> str:
    """ServiceNow-style UTC timestamp: 'YYYY-MM-DD HH:MM:SS' (sorts correctly as text)."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def new_sys_id() -> str:
    return uuid.uuid4().hex  # 32 hex chars, same shape as a real sys_id


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # FastAPI may run a request's dependency setup, handler and teardown on different
    # threadpool threads. Each connection still serves one request at a time, so it's safe.
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    for t in TABLES.values():
        cols = ", ".join(f"{c} TEXT NOT NULL DEFAULT ''" for c in t.stored if c != "sys_id")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {t.name} (sys_id TEXT PRIMARY KEY, {cols})")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sys_number (prefix TEXT PRIMARY KEY, next INTEGER NOT NULL)"
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_incident_number ON incident(number)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_incident_corr ON incident(correlation_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_journal_elem ON sys_journal_field(element_id)")


def drop_schema(conn: sqlite3.Connection) -> None:
    for name in [*TABLES, "sys_number"]:
        conn.execute(f"DROP TABLE IF EXISTS {name}")


def is_empty(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM sys_user_group").fetchone()[0] == 0


def next_number(conn: sqlite3.Connection, prefix: str) -> str:
    row = conn.execute(
        "UPDATE sys_number SET next = next + 1 WHERE prefix = ? RETURNING next - 1", (prefix,)
    ).fetchone()
    return f"{prefix}{row[0]:07d}"


def fetch_one(conn: sqlite3.Connection, table: Table, sys_id: str) -> dict | None:
    row = conn.execute(f"SELECT * FROM {table.name} WHERE sys_id = ?", (sys_id,)).fetchone()
    return dict(row) if row else None


def fetch_many(
    conn: sqlite3.Connection,
    table: Table,
    where: list[str],
    params: list[str],
    order_by: list[str],
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM {table.name}{where_sql}", params).fetchone()[0]
    order_sql = f" ORDER BY {', '.join(order_by)}" if order_by else " ORDER BY rowid"
    rows = conn.execute(
        f"SELECT * FROM {table.name}{where_sql}{order_sql} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [dict(r) for r in rows], total


def insert_row(conn: sqlite3.Connection, table: Table, values: dict[str, str]) -> None:
    cols = list(values)
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO {table.name} ({', '.join(cols)}) VALUES ({placeholders})",
        [values[c] for c in cols],
    )


def update_row(conn: sqlite3.Connection, table: Table, sys_id: str, values: dict[str, str]) -> None:
    assignments = ", ".join(f"{c} = ?" for c in values)
    conn.execute(
        f"UPDATE {table.name} SET {assignments} WHERE sys_id = ?", [*values.values(), sys_id]
    )


def delete_row(conn: sqlite3.Connection, table: Table, sys_id: str) -> bool:
    return conn.execute(f"DELETE FROM {table.name} WHERE sys_id = ?", (sys_id,)).rowcount > 0
