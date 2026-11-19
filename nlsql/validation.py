"""Conservative SQL checks. Database permissions remain the security boundary."""

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.schema import MappingSchema

from .schema import TableSchema

SAFE_FUNCTIONS = {
    "ABS",
    "AVG",
    "CAST",
    "CEIL",
    "COALESCE",
    "CONCAT",
    "COUNT",
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",
    "DATE",
    "DATE_TRUNC",
    "EXTRACT",
    "EXISTS",
    "FLOOR",
    "GREATEST",
    "LEAST",
    "LENGTH",
    "LOWER",
    "MAX",
    "MIN",
    "NULLIF",
    "ROUND",
    "ROW_NUMBER",
    "RANK",
    "DENSE_RANK",
    "LAG",
    "LEAD",
    "SUBSTRING",
    "SUM",
    "TRIM",
    "UPPER",
    "CASE",
    "IF",
    "STRFTIME",
    "TIME_TO_STR",
    "TIMESTAMP_TRUNC",
    "TS_OR_DS_TO_DATE",
}


class CorrectableSQLValidationError(ValueError):
    """An invalid draft that can be rewritten without executing it."""


class ColumnValidationError(CorrectableSQLValidationError):
    """A read-only draft references missing or ambiguous schema columns."""


def validate_sql(sql: str, tables: list[TableSchema], dialect: str, max_rows: int) -> str:
    if dialect not in {"postgres", "sqlite", "duckdb"}:
        raise ValueError("Supported dialects: postgres, sqlite, duckdb")
    if max_rows < 1 or max_rows > 10000:
        raise ValueError("max_rows must be between 1 and 10000")
    statements = sqlglot.parse(sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.SetOperation)):
        raise ValueError("Exactly one SELECT query is required")
    query = normalize_identifiers(statements[0], dialect=dialect)
    compatibility_errors = []
    forbidden = (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock, exp.Copy)
    for node in query.walk():
        if isinstance(node, forbidden):
            raise ValueError("SQL contains a prohibited operation")  # noqa: TRY004 -- invalid SQL value
        if isinstance(node, exp.Func):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            if isinstance(node.parent, exp.Dot):
                raise ValueError(f"SQL function is not allowed: {name}")  # noqa: TRY004 -- invalid SQL value
            if name not in SAFE_FUNCTIONS:
                if name in {"REPLACE", "SPLIT_PART", "SPLIT", "REGEXP_REPLACE"}:
                    compatibility_errors.append(
                        f"Function {name} is not supported by this query layer."
                    )
                else:
                    raise ValueError(f"SQL function is not allowed: {name}")
        if isinstance(node, exp.Cast) and node.to.this == exp.DataType.Type.USERDEFINED:
            compatibility_errors.append(
                "Unrecognized cast type. Use native SQL types from the schema, preserving numeric precision."
            )

    allowed = {t.key: t for t in tables}
    physical = set()
    # Scope resolution distinguishes CTE references from physical source tables.
    for scope in traverse_scope(query):
        for source in scope.sources.values():
            if not isinstance(source, exp.Table):
                continue
            if not isinstance(source.this, exp.Identifier) or source.catalog:
                raise ValueError("Table functions and cross-catalog references are not allowed")
            physical.add(id(source))
            key = (source.db, source.name) if source.db else (source.name,)
            matches = [
                t for k, t in allowed.items() if k == key or (len(key) == 1 and k[-1] == key[0])
            ]
            if len(matches) != 1:
                raise ValueError(f"Unknown or ambiguous table: {source.sql(dialect=dialect)}")
            resolved = matches[0].expression()
            source.set("this", resolved.this)
            source.set("db", resolved.args.get("db"))

    cte_tables = {
        id(t)
        for scope in traverse_scope(query)
        for t in scope.tables
        if t.name in scope.cte_sources and not t.db
    }
    if any(id(t) not in physical | cte_tables for t in query.find_all(exp.Table)):
        raise ValueError("Unresolved table reference")
    limit = query.args.get("limit")
    if limit:
        value = limit.expression
        if (
            not isinstance(value, exp.Literal)
            or value.is_string
            or not value.this.isdigit()
            or limit.args.get("limit_options")
        ):
            raise ValueError("LIMIT must be a non-negative integer")
        row_limit = min(int(value.this), max_rows)
    else:
        row_limit = max_rows
    if tables:
        schema = MappingSchema(dialect=dialect, normalize=False)
        for table in tables:
            schema.add_table(
                table.expression(),
                column_mapping=dict.fromkeys((name for name, _ in table.columns), "UNKNOWN"),
                normalize=False,
            )
        try:
            # Check a copy: qualification must not rewrite the submitted SQL.
            qualify(query.copy(), dialect=dialect, schema=schema, infer_schema=False)
        except OptimizeError as exc:
            if compatibility_errors:
                compatibility_errors.append(f"Schema column check failed: {exc}")
            else:
                raise ColumnValidationError(f"Schema column check failed: {exc}") from None
    if compatibility_errors:
        raise CorrectableSQLValidationError(
            "SQL validation failed: "
            + " ".join(dict.fromkeys(compatibility_errors))
            + " Simplify the query using only supplied columns and supported functions: "
            + ", ".join(sorted(SAFE_FUNCTIONS))
        )
    return query.limit(row_limit).sql(dialect=dialect, comments=False)
