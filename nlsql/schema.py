"""Decode Flight SQL metadata without querying application rows."""

from dataclasses import dataclass

import pyarrow as pa
from sqlglot import exp


@dataclass(frozen=True)
class TableSchema:
    name: str
    schema: str
    columns: tuple[tuple[str, str], ...]

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
        return {
            "table": self.label,
            "columns": [{"name": name, "type": kind} for name, kind in self.columns],
        }


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
