"""Output helpers: JSON or aligned text tables."""

from __future__ import annotations

import json
import sys
from typing import Any, Sequence


def print_json(obj: Any) -> None:
    json.dump(obj, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _get(row: dict, path: str) -> Any:
    """Fetch a possibly-nested value using dotted path (a.b.c)."""
    cur: Any = row
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def print_table(rows: Sequence[dict], columns: Sequence[str], *, headers: Sequence[str] | None = None) -> None:
    """Render rows as an aligned table. ``columns`` are dotted field paths."""
    if not rows:
        print("(no records)")
        return
    head = list(headers) if headers else [c.split(".")[-1] for c in columns]
    table = [head]
    for row in rows:
        table.append([_stringify(_get(row, c)) for c in columns])

    widths = [0] * len(head)
    for line in table:
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))

    def fmt(line: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)).rstrip()

    print(fmt(table[0]))
    print("  ".join("-" * w for w in widths))
    for line in table[1:]:
        print(fmt(line))


def emit(data: Any, *, as_json: bool, columns: Sequence[str] | None = None,
         headers: Sequence[str] | None = None) -> None:
    """Top-level dispatch used by commands."""
    if as_json or columns is None:
        print_json(data)
        return
    if isinstance(data, list):
        print_table(data, columns, headers=headers)
    elif isinstance(data, dict):
        # single record -> key/value listing
        print_table([{"field": k, "value": v} for k, v in _flatten(data).items()],
                    ["field", "value"])
    else:
        print(_stringify(data))


def _flatten(d: dict, prefix: str = "") -> dict:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and "data" in v and isinstance(v["data"], (list, dict)):
            out[key] = v["data"]
        else:
            out[key] = v
    return out
