"""Inspect PostgreSQL catalogs through DCH, restricted to discovered tables."""

from collections import defaultdict
from dataclasses import replace

from data_connect_hub import DCHQueryError
from sqlglot import exp

from .schema import ForeignKey, TableSchema
from .service import read_metadata

COLUMNS_SQL = """
SELECT n.nspname AS table_schema, c.relname AS table_name,
       a.attname AS column_name,
       pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
       NOT a.attnotnull AS is_nullable,
       pg_catalog.col_description(c.oid, a.attnum) AS column_description,
       pg_catalog.obj_description(c.oid, 'pg_class') AS table_description
FROM pg_catalog.pg_attribute AS a
JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE a.attnum > 0 AND NOT a.attisdropped AND {table_filter}
ORDER BY n.nspname, c.relname, a.attnum
"""

KEYS_SQL = """
SELECT n.nspname AS table_schema, c.relname AS table_name,
       con.conname AS constraint_name, con.contype::text AS constraint_type,
       k.ordinality::integer AS position, a.attname AS column_name,
       fn.nspname AS referenced_schema, fc.relname AS referenced_table,
       fa.attname AS referenced_column
FROM pg_catalog.pg_constraint AS con
JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ordinality)
JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid AND a.attnum = k.attnum
LEFT JOIN pg_catalog.pg_class AS fc ON fc.oid = con.confrelid
LEFT JOIN pg_catalog.pg_namespace AS fn ON fn.oid = fc.relnamespace
LEFT JOIN pg_catalog.pg_attribute AS fa
  ON fa.attrelid = fc.oid AND fa.attnum = con.confkey[k.ordinality]
WHERE con.contype IN ('p', 'f') AND {table_filter}
ORDER BY n.nspname, c.relname, con.conname, k.ordinality
"""


def inspect_tables(client, connection_id: str, tables: list[TableSchema], dialect: str):
    """Augment Flight transport schemas with source types, comments, and keys.

    These two fixed metadata queries are separate from model-generated SQL.
    They use the same DCH connection and never retrieve application rows.
    """
    if dialect != "postgres" or not tables:
        return tables
    if any(not table.schema for table in tables):
        raise ValueError("PostgreSQL schema inspection requires schema-qualified DCH table names")
    table_filter = " OR ".join(
        "(n.nspname = "
        + exp.Literal.string(table.schema).sql(dialect="postgres")
        + " AND c.relname = "
        + exp.Literal.string(table.name).sql(dialect="postgres")
        + ")"
        for table in tables
    )
    table_filter = "(" + table_filter + ")"
    try:
        columns = read_metadata(
            client, COLUMNS_SQL.format(table_filter=table_filter), connection_id
        ).to_pylist()
        constraints = read_metadata(
            client, KEYS_SQL.format(table_filter=table_filter), connection_id
        ).to_pylist()
    except DCHQueryError as exc:
        raise ValueError(
            "PostgreSQL schema inspection through DCH failed. Check source catalog access "
            "and DCH service logs; no generated SQL was executed."
        ) from exc
    allowed = {table.key for table in tables}
    column_fields = {
        "table_schema",
        "table_name",
        "column_name",
        "data_type",
        "is_nullable",
        "column_description",
        "table_description",
    }
    key_fields = {
        "table_schema",
        "table_name",
        "constraint_name",
        "constraint_type",
        "position",
        "column_name",
        "referenced_schema",
        "referenced_table",
        "referenced_column",
    }
    if any(not column_fields.issubset(row) for row in columns) or any(
        not key_fields.issubset(row) for row in constraints
    ):
        raise ValueError(
            "DCH returned incomplete PostgreSQL catalog metadata; check SDK/server compatibility."
        )
    by_table = defaultdict(list)
    keys = defaultdict(list)
    for column in columns:
        key = (column["table_schema"], column["table_name"])
        if key in allowed:
            by_table[key].append(column)
    for key in constraints:
        identity = (key["table_schema"], key["table_name"], key["constraint_name"])
        if identity[:2] in allowed:
            keys[identity].append(key)
    result = []
    for table in tables:
        rows = by_table[table.key]
        if len(rows) != len(table.columns) or {row["column_name"] for row in rows} != {
            name for name, _ in table.columns
        }:
            raise ValueError(
                f"DCH and PostgreSQL column metadata disagree for {table.label}. "
                "Retry schema discovery and check source metadata permissions."
            )
        primary_key, foreign_keys = (), []
        for identity, parts in keys.items():
            if identity[:2] != table.key:
                continue
            parts = sorted(parts, key=lambda part: int(part["position"]))
            names = tuple(part["column_name"] for part in parts)
            first = parts[0]
            if not set(names).issubset(dict(table.columns)):
                raise ValueError(
                    f"PostgreSQL key metadata disagrees with columns for {table.label}"
                )
            if first["constraint_type"] == "p":
                primary_key = names
            elif (
                first["constraint_type"] == "f"
                and (first["referenced_schema"], first["referenced_table"]) in allowed
            ):
                target = next(
                    candidate
                    for candidate in tables
                    if candidate.key == (first["referenced_schema"], first["referenced_table"])
                )
                if any(part["referenced_column"] not in dict(target.columns) for part in parts):
                    raise ValueError(
                        f"PostgreSQL foreign key metadata disagrees with columns for {table.label}"
                    )
                foreign_keys.append(
                    ForeignKey(
                        names,
                        first["referenced_schema"],
                        first["referenced_table"],
                        tuple(part["referenced_column"] for part in parts),
                    )
                )
        result.append(
            replace(
                table,
                columns=tuple((row["column_name"], row["data_type"]) for row in rows),
                description=(rows[0]["table_description"] or "") if rows else "",
                column_descriptions=tuple(
                    (row["column_name"], row["column_description"])
                    for row in rows
                    if row["column_description"]
                ),
                nullable_columns=tuple(row["column_name"] for row in rows if row["is_nullable"]),
                primary_key=primary_key,
                foreign_keys=tuple(foreign_keys),
                type_source="postgresql_catalog",
            )
        )
    return result
