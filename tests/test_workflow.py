import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pandas as pd
import pyarrow as pa
import pytest
from data_connect_hub import DCHAuthenticationError, DCHConnectionError, DCHQueryError

from nlsql import cli
from nlsql.workflow import QueryContext, QueryWorkflowError, build_workflow, run_question


def response(sql, reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=sql), finish_reason=reason)]
    )


@pytest.fixture
def context():
    dch = Mock()
    schema = pa.schema([("id", pa.int64()), ("amount", pa.float64())])
    dch.get_tables.return_value = pa.Table.from_pylist(
        [
            {
                "table_name": "orders",
                "db_schema_name": "public",
                "table_schema": schema.serialize().to_pybytes(),
            }
        ]
    )
    dch.read_pandas.return_value = pd.DataFrame({"total": [42.0]})
    llm = Mock()
    llm.chat.completions.create.return_value = response("SELECT SUM(amount) AS total FROM orders")
    return QueryContext(dch=dch, llm=llm, model="test-model", connection_id="conn-a", max_rows=10)


def test_real_graph_success_and_exact_connection(context):
    result = run_question("What is total revenue?", context)
    assert result["attempts"] == 1
    assert result["result"].iloc[0, 0] == 42.0
    context.dch.get_tables.assert_called_once_with("conn-a", include_schema=True)
    context.dch.read_pandas.assert_called_once_with(result["sql"], "conn-a")
    assert result["sql"].endswith("LIMIT 10")
    assert "dch" not in result and "llm" not in result and "connection_id" not in result
    assert "42.0" not in json.dumps(context.llm.chat.completions.create.call_args.kwargs)


def test_dry_run_visits_schema_generation_validation_only(context):
    events = list(
        build_workflow().stream({"question": "Total?"}, context=replace(context, dry_run=True))
    )
    assert [next(iter(event)) for event in events] == ["schema", "generate", "validate"]
    context.dch.read_pandas.assert_not_called()


def test_source_query_error_gets_one_sanitized_correction(context):
    context.llm.chat.completions.create.side_effect = [
        response("SELECT id FROM orders"),
        response("SELECT amount FROM orders"),
    ]
    context.dch.read_pandas.side_effect = [
        DCHQueryError('column "missing" does not exist; PRIVATE_PASSWORD_abc'),
        pd.DataFrame({"amount": [42.0]}),
    ]
    result = run_question("Revenue?", context)
    assert result["attempts"] == 2
    assert context.dch.read_pandas.call_count == 2
    assert all(call.args[1] == "conn-a" for call in context.dch.read_pandas.call_args_list)
    payload = context.llm.chat.completions.create.call_args.kwargs
    assert "PRIVATE_PASSWORD_abc" not in json.dumps(payload)
    correction = json.loads(payload["messages"][1]["content"])["correction"]
    assert "column" in correction["feedback"]
    assert correction["previous_sql"].endswith("LIMIT 10")
    context.dch.get_tables.assert_called_once()


def test_missing_column_is_repaired_before_dch_execution(context):
    context.llm.chat.completions.create.side_effect = [
        response("SELECT o.missing FROM orders o"),
        response("SELECT o.amount FROM orders o"),
    ]
    result = run_question("Revenue?", context)
    assert result["attempts"] == 2
    context.dch.read_pandas.assert_called_once()
    correction = json.loads(
        context.llm.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    )["correction"]
    assert "missing" in correction["feedback"]
    assert "o" in correction["feedback"]


def test_unrepaired_column_error_shows_sql_and_never_executes(context):
    context.llm.chat.completions.create.return_value = response("SELECT missing FROM orders")
    with pytest.raises(QueryWorkflowError, match="SQL after 2 attempt") as caught:
        run_question("Revenue?", context)
    assert "SELECT missing" in str(caught.value)
    context.dch.read_pandas.assert_not_called()
    assert context.llm.chat.completions.create.call_count == 2


def test_column_repair_respects_disabled_corrections(context):
    context.llm.chat.completions.create.return_value = response("SELECT missing FROM orders")
    with pytest.raises(QueryWorkflowError, match="Schema column check"):
        run_question("Revenue?", replace(context, max_corrections=0))
    context.dch.read_pandas.assert_not_called()
    context.llm.chat.completions.create.assert_called_once()


def test_correction_budget_is_enforced(context):
    context.dch.read_pandas.side_effect = DCHQueryError("syntax error at or near SELECT")
    with pytest.raises(QueryWorkflowError, match="syntax"):
        run_question("Revenue?", context)
    assert context.dch.read_pandas.call_count == 2
    assert context.llm.chat.completions.create.call_count == 2


def test_corrections_can_be_disabled(context):
    context.dch.read_pandas.side_effect = DCHQueryError("syntax error")
    with pytest.raises(QueryWorkflowError):
        run_question("Revenue?", replace(context, max_corrections=0))
    context.dch.read_pandas.assert_called_once()
    context.llm.chat.completions.create.assert_called_once()


@pytest.mark.parametrize(
    "message",
    [
        "permission denied",
        "unauthorized",
        "query timed out",
        "TLS certificate invalid",
        "unrecognized source failure",
    ],
)
def test_noncorrectable_query_failures_are_not_retried(context, message):
    context.dch.read_pandas.side_effect = DCHQueryError(message)
    with pytest.raises(QueryWorkflowError):
        run_question("Revenue?", context)
    context.dch.read_pandas.assert_called_once()
    context.llm.chat.completions.create.assert_called_once()


@pytest.mark.parametrize("error_type", [DCHConnectionError, DCHAuthenticationError])
def test_transport_and_authentication_errors_propagate_without_repair(context, error_type):
    context.dch.read_pandas.side_effect = (
        error_type("failure", 401)
        if error_type is DCHAuthenticationError
        else error_type("failure")
    )
    with pytest.raises(error_type):
        run_question("Revenue?", context)
    context.dch.read_pandas.assert_called_once()
    context.llm.chat.completions.create.assert_called_once()


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "SELECT * FROM secrets",
        "SELECT 1; SELECT 2",
        "Here is SQL: SELECT * FROM orders",
    ],
)
def test_invalid_initial_generation_never_executes(context, sql):
    context.llm.chat.completions.create.return_value = response(sql)
    with pytest.raises(QueryWorkflowError, match="validation"):
        run_question("Revenue?", context)
    context.dch.read_pandas.assert_not_called()
    context.llm.chat.completions.create.assert_called_once()


def test_unsafe_repair_never_executes(context):
    context.dch.read_pandas.side_effect = DCHQueryError("syntax error")
    context.llm.chat.completions.create.side_effect = [
        response("SELECT amount FROM orders"),
        response("DROP TABLE orders"),
    ]
    with pytest.raises(QueryWorkflowError, match="validation"):
        run_question("Revenue?", context)
    context.dch.read_pandas.assert_called_once()


def test_schema_failure_stops_before_model_or_execution(context):
    context.dch.get_tables.side_effect = DCHAuthenticationError("not allowed", 401)
    with pytest.raises(DCHAuthenticationError):
        run_question("Revenue?", context)
    context.llm.chat.completions.create.assert_not_called()
    context.dch.read_pandas.assert_not_called()


def test_same_compiled_graph_does_not_share_connection_state(context):
    graph = build_workflow()
    graph.invoke({"question": "Revenue?"}, context=context)
    graph.invoke({"question": "Revenue?"}, context=replace(context, connection_id="conn-b"))
    assert [call.args[1] for call in context.dch.read_pandas.call_args_list] == ["conn-a", "conn-b"]
    assert [call.args[0] for call in context.dch.get_tables.call_args_list] == ["conn-a", "conn-b"]


def test_empty_question_stops_before_network_calls(context):
    with pytest.raises(ValueError, match="empty"):
        run_question(" ", context)
    context.dch.get_tables.assert_not_called()
    context.llm.chat.completions.create.assert_not_called()


def test_cli_ask_executes_graph_result_exactly_once(context, monkeypatch, capsys):
    values = {
        "DCH_HOST": "gateway:443",
        "DCH_TOKEN": "secret",
        "DCH_TENANT_ID": "tenant",
        "DCH_CONNECTION_ID": "conn-a",
        "LLM_BASE_URL": "https://model/v1",
        "LLM_API_KEY": "llm-secret",
        "LLM_MODEL": "test-model",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    dch_factory, llm_factory = MagicMock(), MagicMock()
    dch_factory.return_value.__enter__.return_value = context.dch
    llm_factory.return_value.__enter__.return_value = context.llm
    monkeypatch.setattr(cli, "DataConnectClient", dch_factory)
    monkeypatch.setattr(cli, "OpenAI", llm_factory)
    monkeypatch.setattr(cli, "load_dotenv", Mock())
    assert cli.main(["ask", "What is total revenue?", "--max-corrections", "0"]) == 0
    context.dch.read_pandas.assert_called_once()
    output = capsys.readouterr().out
    assert "42.0" in output and "secret" not in output
