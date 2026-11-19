"""Decode Flight SQL metadata without querying application rows."""

from dataclasses import dataclass

import pyarrow as pa
from sqlglot import exp


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]
    referenced_schema: str
    referenced_table: str
    referenced_columns: tuple[str, ...]


@dataclass(frozen=True)
class TableSchema:
    name: str
    schema: str
    columns: tuple[tuple[str, str], ...]
    description: str = ""
    column_descriptions: tuple[tuple[str, str], ...] = ()
    nullable_columns: tuple[str, ...] = ()
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = ()
    type_source: str = "arrow_transport"

    @property
    def key(self) -> tuple[str, ...]:
        return (self.schema, self.name) if self.schema else (self.name,)

    @property
    def label(self) -> str:
        return ".".join(self.key)

    def expression(self) -> exp.Table:
        return exp.Table(
            this=exp.to_identifier(self.name, quoted=True),
            db=exp.to_identifier(self.schema, quoted=True) if self.schema else None,
        )

    def prompt_dict(self) -> dict:
        descriptions = dict(self.column_descriptions)
        return {
            "table": self.label,
            "type_source": self.type_source,
            "description": self.description,
            "columns": [
                {"name": name, "type": kind, "description": descriptions.get(name, "")}
                for name, kind in self.columns
            ],
            "primary_key": list(self.primary_key),
            "foreign_keys": [
                {
                    "columns": list(key.columns),
                    "references": f"{key.referenced_schema}.{key.referenced_table}",
                    "referenced_columns": list(key.referenced_columns),
                }
                for key in self.foreign_keys
            ],
            "ddl": self.ddl(),
        }

    def ddl(self) -> str:
        """Reference DDL for prompting; never executed."""

        def identifier(name):
            return exp.to_identifier(name, quoted=True).sql()

        lines = [
            f"  {identifier(name)} {kind}"
            + (
                " NOT NULL"
                if self.type_source == "postgresql_catalog" and name not in self.nullable_columns
                else ""
            )
            for name, kind in self.columns
        ]
        if self.primary_key:
            lines.append("  PRIMARY KEY (" + ", ".join(map(identifier, self.primary_key)) + ")")
        for key in self.foreign_keys:
            target = ".".join(map(identifier, (key.referenced_schema, key.referenced_table)))
            lines.append(
                "  FOREIGN KEY ("
                + ", ".join(map(identifier, key.columns))
                + f") REFERENCES {target} ("
                + ", ".join(map(identifier, key.referenced_columns))
                + ")"
            )
        return f"CREATE TABLE {self.expression().sql()} (\n" + ",\n".join(lines) + "\n);"


def discover_tables(client, connection_id: str, selected: list[str] | None = None):
    metadata = client.get_tables(connection_id, include_schema=True)
    tables = []
    for row in metadata.to_pylist():
        name = row["table_name"]
        namespace = row.get("db_schema_name") or ""
        label = f"{namespace}.{name}" if namespace else name
        if selected and label not in selected:
            continue
        raw = row.get("table_schema")
        if not raw:
            raise ValueError(f"DCH did not return column metadata for {label}")
        try:
            schema = pa.ipc.read_schema(pa.BufferReader(raw))
        except (pa.ArrowException, TypeError):
            raise ValueError(f"DCH returned invalid column metadata for {label}") from None
        tables.append(TableSchema(name, namespace, tuple((f.name, str(f.type)) for f in schema)))
    if selected:
        missing = set(selected) - {t.label for t in tables}
        if missing:
            raise ValueError(f"Tables absent from DCH metadata: {', '.join(sorted(missing))}")
    if not tables:
        raise ValueError("DCH returned no tables for this connection and selection")
    if len({t.key for t in tables}) != len(tables):
        raise ValueError(
            "Ambiguous table metadata across catalogs; select a narrower DCH connection"
        )
    return tables
