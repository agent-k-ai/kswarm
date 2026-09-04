from __future__ import annotations

import json
from typing import Any

from rich.json import JSON
from rich.table import Table

from kswarm_cli.context import CliContext


def emit(ctx: CliContext, payload: dict[str, Any] | list[Any]) -> None:
    if ctx.json_output:
        ctx.console.print_json(json.dumps(payload, default=str))
    else:
        ctx.console.print(JSON.from_data(payload))


def emit_table(ctx: CliContext, title: str, rows: list[dict[str, Any]], columns: list[str]) -> None:
    if ctx.json_output:
        emit(ctx, rows)
        return
    table = Table(title=title)
    for column in columns:
        table.add_column(column.replace("_", " ").title())
    for row in rows:
        table.add_row(*(str(row.get(column, "")) for column in columns))
    ctx.console.print(table)


def emit_signature(ctx: CliContext, signature: str, extra: dict[str, Any] | None = None) -> None:
    payload = {"signature": signature}
    if extra:
        payload.update(extra)
    emit(ctx, payload)
