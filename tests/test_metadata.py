from unittest.mock import Mock

import pyarrow as pa
import pytest
import sqlglot
from data_connect_hub import DCHAuthenticationError, DCHQueryError
from sqlglot import exp

from nlsql.metadata import inspect_tables
from nlsql.schema import TableSchema


def column(table, name, kind="integer", schema="demo", **extra):
    return {
        "table_schema": schema,
        "table_name": table,
        "column_name": name,
        "data_type": kind,
        "is_nullable": False,
        "column_description": None,
        "table_description": None,
        **extra,
    }


def key(table, constraint, name, position=1, *, reference=None):
    return {
        "table_schema": "demo",
        "table_name": table,
        "constraint_name": constraint,
        "constraint_type": "f" if reference else "p",
        "position": position,
        "column_name": name,
        "referenced_schema": reference[0] if reference else None,
        "referenced_table": reference[1] if reference else None,
        "referenced_column": reference[2] if reference else None,
    }


def client_for(columns, keys=()):
    client = Mock()
    client.read.side_effect = [pa.Table.from_pylist(columns), pa.Table.from_pylist(list(keys))]
    return client


def test_native_types_comments_primary_keys_and_relationships_are_grounded_in_dch():
    tables = [
        TableSchema("orders", "demo", (("order_id", "int32"),)),
        TableSchema("items", "demo", (("order_id", "int32"), ("price", "string"))),
    ]
    client = client_for(
        [
            column("orders", "order_id"),
            column("items", "order_id"),
            column(
                "items",
                "price",
                "numeric(10,2)",
                column_description="USD per unit",
                table_description="Order lines",
            ),
        ],
        [
            key("orders", "pk", "order_id"),
            key("items", "fk", "order_id", reference=("demo", "orders", "order_id")),
        ],
    )
    result = inspect_tables(client, "selected-connection", tables, "postgres")
    assert result[0].primary_key == ("order_id",)
    assert dict(result[1].columns)["price"] == "numeric(10,2)"
    assert result[1].foreign_keys[0].referenced_columns == ("order_id",)
    assert 'REFERENCES "demo"."orders"' in result[1].ddl()
    assert result[1].prompt_dict()["columns"][1]["description"] == "USD per unit"
    assert result[1].type_source == "postgresql_catalog"
    assert tables[1].columns[1][1] == "string"
    assert all(call.args[1] == "selected-connection" for call in client.read.call_args_list)
    client.read_pandas.assert_not_called()


def test_composite_keys_keep_column_pair_order():
    tables = [
        TableSchema(name, "demo", (("a", "int32"), ("b", "int32"))) for name in ("parent", "child")
    ]
    client = client_for(
        [column(table.name, name) for table in tables for name in ("a", "b")],
        [
            key("parent", "pk", "b", 2),
            key("parent", "pk", "a", 1),
            key("child", "fk", "b", 2, reference=("demo", "parent", "b")),
            key("child", "fk", "a", 1, reference=("demo", "parent", "a")),
        ],
    )
    parent, child = inspect_tables(client, "conn", tables, "postgres")
    assert parent.primary_key == ("a", "b")
    assert child.foreign_keys[0].columns == ("a", "b")
    assert child.foreign_keys[0].referenced_columns == ("a", "b")


def test_excluded_tables_and_relationships_do_not_enter_model_metadata():
    table = TableSchema("items", "demo", (("order_id", "int32"),))
    client = client_for(
        [column("items", "order_id"), column("private_orders", "secret")],
        [
            key("items", "fk", "order_id", reference=("private", "orders", "order_id")),
        ],
    )
    result = inspect_tables(client, "conn", [table], "postgres")
    assert result[0].foreign_keys == ()
    assert "private" not in str(result[0].prompt_dict())
    assert "private_orders" not in client.read.call_args_list[0].args[0]


def test_catalog_filter_quotes_names_as_values_without_widening_scope():
    name = "orders' OR true --"
    schema = "odd'schema"
    table = TableSchema(name, schema, (("id", "int32"),))
    client = client_for([column(name, "id", schema=schema)])
    inspect_tables(client, "conn", [table], "postgres")
    for call in client.read.call_args_list:
        statements = sqlglot.parse(call.args[0], read="postgres")
        assert len(statements) == 1 and isinstance(statements[0], exp.Select)
        literals = [value.this for value in statements[0].find_all(exp.Literal)]
        assert name in literals and schema in literals
        assert not list(statements[0].find_all(exp.Or))


def test_missing_or_stale_catalog_columns_stop_before_model_generation():
    table = TableSchema("orders", "demo", (("id", "int32"),))
    with pytest.raises(ValueError, match="metadata disagree"):
        inspect_tables(client_for([]), "conn", [table], "postgres")


def test_incomplete_metadata_response_is_actionable():
    table = TableSchema("orders", "demo", (("id", "int32"),))
    with pytest.raises(ValueError, match="incomplete PostgreSQL catalog metadata"):
        inspect_tables(client_for([{"table_name": "orders"}]), "conn", [table], "postgres")


def test_metadata_permission_failure_is_safe_and_authentication_is_not_retried():
    table = TableSchema("orders", "demo", (("id", "int32"),))
    client = Mock()
    client.read.side_effect = DCHQueryError("permission denied private-password")
    with pytest.raises(ValueError, match="catalog access") as caught:
        inspect_tables(client, "conn", [table], "postgres")
    assert "private-password" not in str(caught.value)
    client.read.assert_called_once()
    client.read.side_effect = DCHAuthenticationError("private-token", 401)
    with pytest.raises(DCHAuthenticationError):
        inspect_tables(client, "conn", [table], "postgres")


def test_other_dialects_do_not_receive_postgres_catalog_queries():
    client = Mock()
    tables = [TableSchema("orders", "main", (("id", "int32"),))]
    assert inspect_tables(client, "conn", tables, "sqlite") is tables
    client.read.assert_not_called()
