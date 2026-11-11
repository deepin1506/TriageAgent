"""Parser for a subset of ServiceNow encoded queries (sysparm_query).

Supported: terms joined by ^ (AND), operators below, ORDERBY<field> / ORDERBYDESC<field>,
and ^^ as an escaped literal caret. Not supported: ^OR, ^NQ, dot-walking, relative dates.
Comparisons are text comparisons (fine for 1-5 choice values and 'YYYY-MM-DD HH:MM:SS').
"""

import re
from dataclasses import dataclass, field

from .errors import SnowAPIError
from .schema import Table

# Longest first, so 'ISNOTEMPTY' wins over 'ISEMPTY', '!=' over '=', etc.
OPERATORS = (
    "ISNOTEMPTY",
    "ISEMPTY",
    "STARTSWITH",
    "ENDSWITH",
    "NOT LIKE",
    "LIKE",
    "NOT IN",
    "IN",
    "!=",
    ">=",
    "<=",
    "=",
    ">",
    "<",
)
_TERM = re.compile(
    r"^(?P<field>[a-z_][a-z0-9_]*)(?P<op>" + "|".join(re.escape(o) for o in OPERATORS) + r")"
    r"(?P<value>.*)$",
    re.DOTALL,
)
_CARET = "\x00"


@dataclass
class ParsedQuery:
    where: list[str] = field(default_factory=list)
    params: list[str] = field(default_factory=list)
    order_by: list[str] = field(default_factory=list)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _bad(detail: str) -> SnowAPIError:
    return SnowAPIError(400, "Invalid query", detail)


def parse(query: str, table: Table) -> ParsedQuery:
    out = ParsedQuery()
    if not query.strip():
        return out
    allowed = set(table.stored)

    for raw in query.replace("^^", _CARET).split("^"):
        term = raw.replace(_CARET, "^").strip()
        if not term:
            continue
        if term.startswith("ORDERBY"):
            desc = term.startswith("ORDERBYDESC")
            name = term[len("ORDERBYDESC") :] if desc else term[len("ORDERBY") :]
            if name not in allowed:
                raise _bad(f"Unknown field '{name}' in '{term}'")
            out.order_by.append(f"{name} {'DESC' if desc else 'ASC'}")
            continue
        if term.startswith(("OR", "NQ")):
            raise _bad(f"'{term}': ^OR / ^NQ are not supported by this mock")

        m = _TERM.match(term)
        if not m:
            raise _bad(f"Cannot parse term '{term}'")
        name, op, value = m["field"], m["op"], m["value"]
        if name not in allowed:
            raise _bad(f"Unknown field '{name}' for table {table.name}")

        if op in ("=", "!=", ">", ">=", "<", "<="):
            out.where.append(f"{name} {op} ?")
            out.params.append(value)
        elif op in ("LIKE", "NOT LIKE", "STARTSWITH", "ENDSWITH"):
            esc = _like_escape(value)
            pattern = {"STARTSWITH": f"{esc}%", "ENDSWITH": f"%{esc}"}.get(op, f"%{esc}%")
            neg = "NOT " if op == "NOT LIKE" else ""
            out.where.append(f"{name} {neg}LIKE ? ESCAPE '\\'")
            out.params.append(pattern)
        elif op in ("IN", "NOT IN"):
            items = [v.strip() for v in value.split(",")]
            out.where.append(f"{name} {op} ({', '.join('?' for _ in items)})")
            out.params.extend(items)
        elif op == "ISEMPTY":
            out.where.append(f"{name} = ''")
        elif op == "ISNOTEMPTY":
            out.where.append(f"{name} != ''")
    return out
