import json
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pandas as pd
import pyarrow as pa
import pytest

from nlsql import cli
from nlsql.schema import TableSchema, discover_tables
from nlsql.service import check_source_sql, draft_sql, run_sql


def metadata(name="orders", namespace="public", raw=True):
    schema = pa.schema([("id", pa.int64()), ("amount", pa.float64())])
    return {
        "table_name": name,
        "db_schema_name": namespace,
        "table_schema": schema.serialize().to_pybytes() if raw else None,
    }


def model_response(sql="SELECT SUM(amount) FROM orders", reason="stop"):
    llm = Mock()
    llm.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=sql), finish_reason=reason)]
    )
    return llm


def test_empty_model_completion_reports_a_clear_error():
    llm = Mock()
    llm.chat.completions.create.return_value = SimpleNamespace(choices=[])
    with pytest.raises(ValueError, match="Model returned no completion choices"):
        draft_sql(llm, "test-model", "How many orders?", [], "postgres", 10)


def test_question_to_dch_with_exact_connection_and_no_rows_in_prompt():
    dch = Mock()
    dch.get_tables.return_value = pa.Table.from_pylist([metadata()])
    dch.read_pandas.return_value = pd.DataFrame({"total": [42.0]})
    tables = discover_tables(dch, "selected-connection")
    llm = model_response()
    sql = draft_sql(llm, "test-model", "What is total revenue?", tables, "postgres", 10)
    executed, frame = run_sql(dch, "selected-connection", sql, tables, "postgres", 10)
    dch.get_tables.assert_called_once_with("selected-connection", include_schema=True)
    dch.read_pandas.assert_called_once_with(executed, "selected-connection")
    assert frame.iloc[0, 0] == 42.0
    call = llm.chat.completions.create.call_args.kwargs
    assert call["model"] == "test-model"
    payload = json.loads(call["messages"][1]["content"])
    assert payload["schema"][0]["table"] == "public.orders"
    assert "selected-connection" not in json.dumps(call)
    assert "42.0" not in json.dumps(call)


@pytest.mark.parametrize("sql", ["DELETE FROM orders", "SELECT * FROM secrets"])
def test_execution_revalidates_before_dch_call(sql):
    dch = Mock()
    with pytest.raises(ValueError):
        run_sql(dch, "connection", sql, [TableSchema("orders", "public", ())], "postgres", 10)
    dch.read_pandas.assert_not_called()


@pytest.mark.parametrize("sql", ["DELETE FROM orders", "SELECT pg_sleep(10)", "SELECT 1; SELECT 2"])
def test_planner_never_receives_unsafe_sql(sql):
    dch = Mock()
    with pytest.raises(ValueError):
        check_source_sql(dch, "conn", sql, [], "postgres", 10)
    dch.read.assert_not_called()


def test_planner_uses_same_connection_and_never_analyzes_or_executes_select():
    dch = Mock()
    sql = check_source_sql(dch, "conn", "SELECT 1", [], "postgres", 10)
    dch.read.assert_called_once_with("EXPLAIN " + sql, "conn")
    assert "ANALYZE" not in dch.read.call_args.args[0]
    dch.read_pandas.assert_not_called()


@pytest.mark.parametrize(
    "sql,reason",
    [
        ("CANNOT_ANSWER", "stop"),
        ("", "stop"),
        ("SELECT * FROM orders", "length"),
    ],
)
def test_model_refusal_and_truncation_are_rejected(sql, reason):
    with pytest.raises(ValueError):
        draft_sql(
            model_response(sql, reason),
            "test",
            "question",
            [TableSchema("orders", "public", ())],
            "postgres",
            10,
        )


def test_metadata_selection_and_missing_columns():
    dch = Mock()
    dch.get_tables.return_value = pa.Table.from_pylist([metadata(), metadata("secrets")])
    assert [t.label for t in discover_tables(dch, "conn", ["public.orders"])] == ["public.orders"]
    with pytest.raises(ValueError, match="absent"):
        discover_tables(dch, "conn", ["private.orders"])
    dch.get_tables.return_value = pa.Table.from_pylist([metadata(raw=False)])
    with pytest.raises(ValueError, match="column metadata"):
        discover_tables(dch, "conn")


def test_invalid_arrow_metadata_reports_table_without_a_traceback_or_raw_content():
    dch = Mock()
    row = metadata()
    row["table_schema"] = b"invalid-arrow-private-content"
    dch.get_tables.return_value = pa.Table.from_pylist([row])
    with pytest.raises(ValueError, match="invalid column metadata for public.orders") as caught:
        discover_tables(dch, "conn")
    assert "private-content" not in str(caught.value)


def test_read_only_execution_omits_known_autocommit_warning_but_keeps_other_warnings():
    dch = Mock()
    frame = pd.DataFrame({"count": [6]})

    def read(sql, connection_id):
        warnings.warn_explicit(
            "Cannot disable autocommit; conn will not be DB-API 2.0 compliant",
            Warning,
            filename="dbapi.py",
            lineno=371,
            module="adbc_driver_manager.dbapi",
        )
        warnings.warn("another driver warning", UserWarning, stacklevel=1)
        return frame

    dch.read_pandas.side_effect = read
    with pytest.warns(UserWarning, match="another driver warning") as captured:
        _, result = run_sql(
            dch,
            "conn",
            "SELECT COUNT(*) FROM orders",
            [
                TableSchema("orders", "public", (("id", "int32"),)),
            ],
            "postgres",
            10,
        )
    assert result is frame
    assert len(captured) == 1


def test_dry_run_never_executes_and_passes_tenant_to_sdk(monkeypatch, capsys):
    for name, value in {
        "DCH_HOST": "gateway:443",
        "DCH_TOKEN": "secret",
        "DCH_TENANT_ID": "tenant-a",
        "DCH_CONNECTION_ID": "conn-a",
    }.items():
        monkeypatch.setenv(name, value)
    factory = MagicMock()
    dch = factory.return_value.__enter__.return_value
    dch.get_tables.return_value = pa.Table.from_pylist([metadata()])
    monkeypatch.setattr(cli, "DataConnectClient", factory)
    monkeypatch.setattr(cli, "load_dotenv", Mock())
    assert cli.main(["query", "SELECT * FROM orders", "--dry-run"]) == 0
    assert factory.call_args.kwargs["tenant_id"] == "tenant-a"
    assert factory.call_args.kwargs["token"] == "secret"
    dch.read_pandas.assert_not_called()
    assert "secret" not in capsys.readouterr().out
