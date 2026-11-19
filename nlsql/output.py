"""Readable terminal results without another model request or changes to the data."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from numbers import Number

import pandas as pd
import sqlglot
from pandas.api.types import is_numeric_dtype, is_scalar
from rich import box
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from sqlglot import exp
from sqlglot.errors import SqlglotError


@contextmanager
def query_progress(console: Console | None = None) -> Iterator[Callable[[str], None]]:
    """Keep waiting feedback transient and leave redirected output untouched."""
    console = console or Console()
    if not console.is_terminal:
        yield lambda message: None
        return
    console.print()
    with console.status("Preparing query...", spinner="dots") as status:
        yield status.update


def _query_details(sql: str, dialect: str):
    try:
        query = sqlglot.parse_one(sql, read=dialect)
    except SqlglotError:
        return sql, None, set()
    limit = query.args.get("limit")
    limit_value = limit.expression if limit else None
    row_limit = (
        int(limit_value.this)
        if isinstance(limit_value, exp.Literal) and limit_value.this.isdigit()
        else None
    )
    numeric = set()
    for index, expression in enumerate(query.selects):
        expression = expression.unalias()
        if isinstance(expression, exp.Window):
            expression = expression.this
        if isinstance(
            expression, (exp.Sum, exp.Avg, exp.Count, exp.Add, exp.Sub, exp.Mul, exp.Div)
        ):
            numeric.add(index)
    return query.sql(dialect=dialect, pretty=True, comments=False), row_limit, numeric


def _heading(value) -> str:
    words = str(value).replace("_", " ").split()
    label = " ".join(
        word.upper() if word.lower() in {"id", "usd", "url", "sku"} else word for word in words
    )
    return label[:1].upper() + label[1:]


def _cell(value, numeric: bool) -> Text:
    if is_scalar(value) and pd.isna(value):
        return Text("NULL", style="dim")
    if numeric and isinstance(value, str):
        # Flight can return numeric aggregates as text. Only coerce columns whose
        # SQL expression is numeric; identifiers and other text stay verbatim.
        try:
            parsed = Decimal(value)
            if parsed.is_finite():
                value = parsed
        except InvalidOperation:
            pass
    if isinstance(value, Decimal):
        return Text(format(value, ",f"))
    if isinstance(value, Number) and not isinstance(value, bool):
        return Text(format(value, ","))
    return Text(str(value))


def print_table(frame, *, console: Console | None = None, numeric_columns=()):
    console = console or Console()
    if frame.empty:
        console.print(Text("No matching rows.", style="dim"))
        return
    table = Table(box=box.ROUNDED, header_style="bold", highlight=False)
    numeric = []
    for index, column in enumerate(frame.columns):
        values = frame.iloc[:, index].dropna()
        numbers = (
            index in numeric_columns
            or is_numeric_dtype(frame.dtypes.iloc[index])
            or (not values.empty and all(isinstance(value, (Number, Decimal)) for value in values))
        )
        numeric.append(numbers)
        table.add_column(
            Text(_heading(column)), justify="right" if numbers else "left", overflow="fold"
        )
    for row in frame.itertuples(index=False, name=None):
        table.add_row(*(_cell(value, numbers) for value, numbers in zip(row, numeric, strict=True)))
    console.print(table)


def print_query_output(
    sql,
    frame,
    *,
    connection_id,
    dialect="postgres",
    max_rows=100,
    question=None,
    corrected=False,
    console: Console | None = None,
):
    console = console or Console()
    formatted_sql, limit, numeric_columns = _query_details(sql, dialect)
    if question:
        console.print(Text("Question: ", style="bold") + Text(question))
        console.print()
    if frame is not None:
        console.print(Text("Results", style="bold"))
        print_table(frame, console=console, numeric_columns=numeric_columns)
        count = len(frame)
        console.print(
            Text(
                f"{count} {'row' if count == 1 else 'rows'} returned · query limit {limit if limit is not None else max_rows}",
                style="dim",
            )
        )
        console.print()
    console.print(Text("SQL" if frame is not None else "SQL preview (not executed)", style="bold"))
    console.print(
        Syntax(formatted_sql, "sql", theme="ansi_dark", background_color="default", word_wrap=True)
    )
    if corrected:
        console.print(Text("SQL corrected once after a schema or source query error.", style="dim"))
    console.print(Text(f"Connection: {connection_id}", style="dim"))
