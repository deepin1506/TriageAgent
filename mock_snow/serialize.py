"""Render stored rows the way the Table API does, honouring sysparm_display_value,
sysparm_exclude_reference_link and sysparm_fields."""

import sqlite3
from typing import Any, Literal

from . import db
from .errors import SnowAPIError
from .schema import TABLES, Table

DisplayValue = Literal["true", "false", "all"]


def parse_fields(table: Table, sysparm_fields: str) -> list[str]:
    if not sysparm_fields.strip():
        return list(table.output_fields)
    fields = [f.strip() for f in sysparm_fields.split(",") if f.strip()]
    unknown = [f for f in fields if f not in table.output_fields]
    if unknown:
        raise SnowAPIError(400, "Invalid field(s)", ", ".join(unknown))
    return fields


class Renderer:
    def __init__(
        self,
        conn: sqlite3.Connection,
        base_url: str,
        display_value: DisplayValue,
        exclude_reference_link: bool,
    ) -> None:
        self.conn = conn
        self.base_url = base_url.rstrip("/")
        self.display_value = display_value
        self.exclude_link = exclude_reference_link
        self._display_cache: dict[tuple[str, str], str] = {}

    def _display_of(self, table_name: str, sys_id: str) -> str:
        key = (table_name, sys_id)
        if key not in self._display_cache:
            ref_table = TABLES[table_name]
            row = db.fetch_one(self.conn, ref_table, sys_id)
            self._display_cache[key] = row[ref_table.display_field] if row else ""
        return self._display_cache[key]

    def _field(self, table: Table, name: str, raw: str) -> Any:
        ref_table = table.references.get(name)
        if ref_table and raw:
            if self.display_value == "true":
                return self._display_of(ref_table, raw)
            link = f"{self.base_url}/api/now/table/{ref_table}/{raw}"
            if self.display_value == "all":
                out = {"display_value": self._display_of(ref_table, raw), "value": raw}
                if not self.exclude_link:
                    out["link"] = link
                return out
            return raw if self.exclude_link else {"link": link, "value": raw}

        label = table.choices.get(name, {}).get(raw, raw)
        if self.display_value == "true":
            return label
        if self.display_value == "all":
            return {"display_value": label, "value": raw}
        return raw

    def record(self, table: Table, row: dict[str, str], fields: list[str]) -> dict[str, Any]:
        return {f: self._field(table, f, row.get(f, "")) for f in fields}
