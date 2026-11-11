"""Record operations: validation -> business rules -> storage -> journal.

Deliberate deviations from real ServiceNow, which silently ignores bad input. This mock
fails loudly instead (400), so an agent's tool errors are visible rather than swallowed:
unknown fields, read-only fields, invalid choice values and dangling references.
"""

import sqlite3
from typing import Any

from . import db
from .errors import SnowAPIError
from .query import parse
from .rules import BEFORE_INSERT, BEFORE_UPDATE
from .schema import SYS_FIELDS, SYS_JOURNAL_FIELD, TABLES, Table


def _coerce(name: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        return str(value)
    raise SnowAPIError(400, "Invalid value", f"'{name}' must be a string, number or boolean")


def _clean(
    conn: sqlite3.Connection, table: Table, payload: Any
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (column values, journal entries) or raise 400."""
    if not isinstance(payload, dict) or not payload:
        raise SnowAPIError(400, "Invalid request", "Body must be a non-empty JSON object")

    unknown = sorted(set(payload) - set(table.output_fields))
    if unknown:
        raise SnowAPIError(400, "Invalid field(s)", ", ".join(unknown))
    if "priority" in payload and table.name == "incident":
        raise SnowAPIError(
            400,
            "Read-only field(s)",
            "priority is calculated from impact and urgency; set those instead",
        )
    read_only = sorted(set(payload) & (set(SYS_FIELDS) | set(table.read_only_columns)))
    if read_only:
        raise SnowAPIError(400, "Read-only field(s)", ", ".join(read_only))

    values = {k: _coerce(k, v) for k, v in payload.items() if k in table.columns}
    journal = {
        k: _coerce(k, v)
        for k, v in payload.items()
        if k in table.journal_fields and v not in (None, "")
    }

    for name, value in values.items():
        allowed = table.choices.get(name)
        if allowed is not None and value not in allowed:
            options = ", ".join(repr(k) for k in allowed if k)
            raise SnowAPIError(400, "Invalid choice value", f"{name}={value!r}; allowed: {options}")
        ref_table = table.references.get(name)
        if ref_table and value and db.fetch_one(conn, TABLES[ref_table], value) is None:
            raise SnowAPIError(
                400, "Invalid reference", f"{name}={value!r} is not a sys_id in {ref_table}"
            )
    return values, journal


def _require_writable(table: Table) -> None:
    if not table.writable:
        raise SnowAPIError(403, "Insufficient rights", f"Table {table.name} is read-only")


def _add_journal(
    conn: sqlite3.Connection, table: Table, sys_id: str, entries: dict[str, str], user: str
) -> None:
    for element, text in entries.items():
        ts = db.now()
        db.insert_row(
            conn,
            SYS_JOURNAL_FIELD,
            {
                "sys_id": db.new_sys_id(),
                "sys_created_on": ts,
                "sys_created_by": user,
                "sys_updated_on": ts,
                "sys_updated_by": user,
                "name": table.name,
                "element": element,
                "element_id": sys_id,
                "value": text,
            },
        )


def get_table(name: str) -> Table:
    table = TABLES.get(name)
    if table is None:
        raise SnowAPIError(400, f"Invalid table {name}")
    return table


def get(conn: sqlite3.Connection, table: Table, sys_id: str) -> dict[str, str]:
    record = db.fetch_one(conn, table, sys_id)
    if record is None:
        raise SnowAPIError(
            404, "No Record found", "Record doesn't exist or ACL restricts the record retrieval"
        )
    return record


def list_records(
    conn: sqlite3.Connection, table: Table, query: str, limit: int, offset: int
) -> tuple[list[dict], int]:
    q = parse(query, table)
    return db.fetch_many(conn, table, q.where, q.params, q.order_by, limit, offset)


def create(conn: sqlite3.Connection, table: Table, payload: Any, user: str) -> dict[str, str]:
    _require_writable(table)
    values, journal = _clean(conn, table, payload)
    if hook := BEFORE_INSERT.get(table.name):
        hook(values)
    if table.number_prefix:
        values["number"] = db.next_number(conn, table.number_prefix)
    ts = db.now()
    sys_id = db.new_sys_id()
    db.insert_row(
        conn,
        table,
        {
            "sys_id": sys_id,
            "sys_created_on": ts,
            "sys_created_by": user,
            "sys_updated_on": ts,
            "sys_updated_by": user,
            **values,
        },
    )
    _add_journal(conn, table, sys_id, journal, user)
    return get(conn, table, sys_id)


def update(
    conn: sqlite3.Connection, table: Table, sys_id: str, payload: Any, user: str
) -> dict[str, str]:
    _require_writable(table)
    current = get(conn, table, sys_id)
    values, journal = _clean(conn, table, payload)
    if hook := BEFORE_UPDATE.get(table.name):
        hook(current, values)
    values.update(sys_updated_on=db.now(), sys_updated_by=user)
    db.update_row(conn, table, sys_id, values)
    _add_journal(conn, table, sys_id, journal, user)
    return get(conn, table, sys_id)


def delete(conn: sqlite3.Connection, table: Table, sys_id: str) -> None:
    _require_writable(table)
    if not db.delete_row(conn, table, sys_id):
        get(conn, table, sys_id)  # raises the standard 404
