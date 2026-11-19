from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from urllib.parse import unquote, urlsplit

import pandas as pd
import pyarrow as pa
import pytest
from data_connect_hub import (
    ConnectionType,
    DCHConnectionError,
    DCHNotFoundError,
    DCHServerError,
    DCHValidationError,
)
from dotenv import dotenv_values, set_key

from nlsql import cli, setup
from nlsql.setup_ui import certificate_file


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: True)
    values = {
        "DCH_HOST": "gateway.test:443",
        "DCH_TOKEN": "private-dch-token",
        "DCH_TENANT_ID": "demo-tenant",
        "DCH_CONNECTION_ID": "existing-id",
        "POSTGRES_URI": "postgresql://user:private-db-password@source.test/demo",
        "LLM_BASE_URL": "https://model.test/v1",
        "LLM_MODEL": "test-model",
        "LLM_API_KEY": "private-model-token",
    }
    for key in (
        *values,
        "DCH_CA_CERT",
        "POSTGRES_CA_CERT",
        "DCH_CONNECTION_TYPE_NAME",
        "SSL_CERT_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    for key, value in values.items():
        set_key(env_file, key, value)
    client = Mock()
    connection = SimpleNamespace(
        id="existing-id",
        name="demo",
        format="tabular",
        data_connection_type_id="pg-type",
        status=SimpleNamespace(state="ready"),
        credentials_ref=SimpleNamespace(secret="demo-pg-creds"),
    )
    client.list_connections.return_value = [connection]
    client.get_connection.return_value = connection
    kind = ConnectionType(
        id="pg-type", name="Postgres", provider="postgres", status={"flight_ready": True}
    )
    client.get_connection_type.return_value = kind
    client.list_connection_types.return_value = [kind]
    client.create_connection.return_value = SimpleNamespace(
        **{**vars(connection), "id": "new-id", "status": SimpleNamespace(state="not_ready")}
    )
    arrow_schema = pa.schema([("id", pa.int64())]).serialize().to_pybytes()
    client.get_tables.return_value = pa.Table.from_pylist(
        [
            {"table_name": name, "db_schema_name": "nlsql_demo", "table_schema": arrow_schema}
            for name in ("customers", "orders", "order_items")
        ]
    )
    client.read_pandas.return_value = pd.DataFrame({"customers": [6]})
    factory, model_factory = MagicMock(), MagicMock()
    model_factory.return_value.__enter__.return_value.models.list.return_value = SimpleNamespace(
        data=[SimpleNamespace(id="test-model", ready=True)]
    )
    factory.return_value.__enter__.return_value = client
    monkeypatch.setattr(setup, "DataConnectClient", factory)
    monkeypatch.setattr(setup, "OpenAI", model_factory)
    monkeypatch.setattr(setup, "oc_value", Mock(return_value=""))
    monkeypatch.setattr(setup.shutil, "which", lambda _: None)
    for key in ("LLM_TIMEOUT", "DCH_FLIGHT_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    return SimpleNamespace(
        path=env_file,
        client=client,
        connection=connection,
        factory=factory,
        model_factory=model_factory,
    )


def answers(monkeypatch, inputs, secrets=("",)):
    public, hidden = iter(inputs), iter(secrets)
    monkeypatch.setattr("builtins.input", lambda _: next(public))
    monkeypatch.setattr(setup.getpass, "getpass", lambda _: next(hidden))


def assert_no_secrets(output):
    for secret in (
        "private-dch-token",
        "private-db-password",
        "private-model-token",
        "postgresql://user:",
    ):
        assert secret not in output


def test_existing_connection_resumes_without_creating_or_asking_for_uri(
    wizard, monkeypatch, capsys
):
    answers(monkeypatch, ["", "", "n"])
    assert setup.run_setup(wizard.path) == 0
    wizard.client.create_connection.assert_not_called()
    wizard.client.check_connection_readiness.assert_not_called()
    wizard.client.read_pandas.assert_called_once()
    wizard.model_factory.assert_not_called()
    assert dotenv_values(wizard.path)["DCH_CONNECTION_ID"] == "existing-id"
    output = capsys.readouterr().out
    assert "skipped" in output
    assert "End-to-end query succeeded" not in output
    assert_no_secrets(output)


def test_end_to_end_wizard_uses_selected_connection_and_saves_model_config(
    wizard, monkeypatch, capsys
):
    answers(monkeypatch, ["", "", "y", "", "", ""])
    run = Mock(
        return_value={
            "sql": "SELECT SUM(...) LIMIT 100",
            "result": pd.DataFrame({"revenue": [5510]}),
        }
    )
    monkeypatch.setattr(setup, "run_question", run)
    assert setup.run_setup(wizard.path) == 0
    question, context = run.call_args.args
    assert question == "What is total completed revenue?"
    assert context.connection_id == "existing-id"
    assert context.selected_tables == setup.DEMO_TABLES
    assert context.dch is wizard.client
    assert context.model == "test-model"
    assert context.max_corrections == 1
    output = capsys.readouterr().out
    assert "End-to-end query succeeded" in output and "5510" in output
    assert_no_secrets(output)


def test_new_connection_saves_id_before_readiness_check(wizard, monkeypatch, capsys):
    set_key(wizard.path, "DCH_CONNECTION_ID", "")
    wizard.client.list_connections.return_value = []
    answers(monkeypatch, [""] * 6 + ["n"])

    def readiness(connection_id):
        assert connection_id == "new-id"
        assert dotenv_values(wizard.path)["DCH_CONNECTION_ID"] == "new-id"

    wizard.client.check_connection_readiness.side_effect = readiness
    assert setup.run_setup(wizard.path) == 0
    wizard.client.create_connection.assert_called_once()
    assert wizard.client.create_connection.call_args.kwargs["connection_type_id"] == "pg-type"
    assert_no_secrets(capsys.readouterr().out)


def test_missing_flight_access_prints_scoped_commands_and_preserves_progress(
    wizard, monkeypatch, capsys
):
    wizard.connection.status.state = "not_ready"
    wizard.client.check_connection_readiness.side_effect = DCHValidationError(
        "private-db-password", 400, '{"code":"invalid_secret","message":"private-db-password"}'
    )
    answers(monkeypatch, ["", "", "n"])
    assert setup.run_setup(wizard.path) == 1
    output = capsys.readouterr().out
    assert "--resource-name=demo-pg-creds" in output
    assert "oc create rolebinding" in output
    assert "resume" in output
    assert dotenv_values(wizard.path)["DCH_CONNECTION_ID"] == "existing-id"
    wizard.model_factory.assert_not_called()
    assert_no_secrets(output)


def test_missing_demo_tables_guides_seeding_without_changing_database(wizard, monkeypatch, capsys):
    wizard.client.get_tables.return_value = pa.Table.from_pylist([])
    answers(monkeypatch, ["", "", "n"])
    assert setup.run_setup(wizard.path) == 1
    output = capsys.readouterr().out
    assert "examples/demo_db.sql" in output
    assert "setup_demo_db.sh" in output
    wizard.client.read_pandas.assert_not_called()
    wizard.client.create_connection.assert_not_called()


def test_tls_failure_guides_ca_configuration_without_disabling_tls(wizard, monkeypatch, capsys):
    wizard.client.list_connections.side_effect = DCHConnectionError(
        "certificate verify failed: private-dch-token"
    )
    answers(monkeypatch, [""])
    assert setup.run_setup(wizard.path) == 1
    output = capsys.readouterr().out
    assert "DCH_CA_CERT" in output and "default-ingress-cert" in output
    assert_no_secrets(output)
    assert "insecure" not in wizard.factory.call_args.kwargs


def test_interrupted_setup_preserves_existing_config(wizard, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", Mock(side_effect=KeyboardInterrupt))
    assert setup.run_setup(wizard.path) == 130
    assert dotenv_values(wizard.path)["DCH_CONNECTION_ID"] == "existing-id"
    wizard.factory.assert_not_called()
    assert "resume" in capsys.readouterr().out


def test_uri_builder_encodes_credentials_and_keeps_them_hidden(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("POSTGRES_URI", raising=False)
    answers(monkeypatch, ["source.test", "", "demo", "demo@user", ""], secrets=("", "p@ss:#/word"))
    settings = setup.Settings(tmp_path / ".env")
    uri = setup.postgres_uri(settings)
    parsed = urlsplit(uri)
    assert unquote(parsed.username) == "demo@user"
    assert unquote(parsed.password) == "p@ss:#/word"
    assert parsed.query == "sslmode=require"
    assert dotenv_values(settings.path)["POSTGRES_URI"] == uri
    assert "p@ss" not in capsys.readouterr().out


def test_secret_prompts_never_display_existing_values(wizard, monkeypatch):
    prompts = []
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: prompts.append(prompt) or "")
    value = setup.Settings(wizard.path).prompt("DCH_TOKEN", "Token", secret=True)
    assert value == "private-dch-token"
    assert "private-dch-token" not in "".join(prompts)
    assert "saved" in prompts[0]


def test_admin_commands_are_printed_and_not_executed(monkeypatch, capsys):
    oc = Mock(
        return_value="services\tdch-rest-service\trest-sa\nservices\tdch-main-flight\tflight-sa\n"
    )
    monkeypatch.setattr(setup, "oc_value", oc)
    setup.access_help("tenant", "one-credential-secret")
    output = capsys.readouterr().out
    assert "--serviceaccount=services:rest-sa" in output
    assert "--serviceaccount=services:flight-sa" in output
    assert "--resource-name=one-credential-secret" in output
    assert oc.call_args.args[0] == "get"
    assert oc.call_count == 1


def test_cli_setup_dispatches_without_requiring_configuration(monkeypatch):
    run = Mock(return_value=0)
    monkeypatch.setattr(cli, "run_setup", run)
    factory = Mock()
    monkeypatch.setattr(cli, "DataConnectClient", factory)
    monkeypatch.setattr(cli, "load_dotenv", Mock())
    assert cli.main(["setup"]) == 0
    run.assert_called_once()
    factory.assert_not_called()


def test_fresh_gateway_defaults_use_oc_login_without_printing_token(wizard, monkeypatch, capsys):
    set_key(wizard.path, "DCH_HOST", "")
    set_key(wizard.path, "DCH_TOKEN", "")
    monkeypatch.setattr(setup.shutil, "which", lambda _: "/usr/bin/oc")
    oc = Mock(side_effect=["discovered-gateway.test", "private-dch-token"])
    monkeypatch.setattr(setup, "oc_value", oc)
    answers(monkeypatch, ["y", "", "", ""])
    settings = setup.Settings(wizard.path)
    setup.configure_gateway(settings)
    assert settings.get("DCH_HOST") == "discovered-gateway.test:443"
    assert settings.get("DCH_TOKEN") == "private-dch-token"
    assert [call.args[0] for call in oc.call_args_list] == ["get", "whoami"]
    assert_no_secrets(capsys.readouterr().out)


def test_retry_after_access_fix_does_not_recreate_connection(wizard, monkeypatch):
    metadata = wizard.client.get_tables.return_value
    wizard.client.get_tables.side_effect = [DCHConnectionError("not available"), metadata]
    answers(monkeypatch, ["", "", "y", "n"])
    assert setup.run_setup(wizard.path) == 0
    assert wizard.client.get_tables.call_count == 2
    wizard.client.create_connection.assert_not_called()


def test_creation_rbac_failure_shows_help_and_does_not_claim_completion(
    wizard, monkeypatch, capsys
):
    set_key(wizard.path, "DCH_CONNECTION_ID", "")
    wizard.client.list_connections.return_value = []
    wizard.client.create_connection.side_effect = DCHValidationError(
        "private-db-password",
        400,
        '{"code":"cannot_create_secret","message":"private-db-password"}',
    )
    answers(monkeypatch, [""] * 6)
    assert setup.run_setup(wizard.path) == 1
    output = capsys.readouterr().out
    assert "nlsql-rest-secret-access" in output and "Connection creation failed" in output
    assert not dotenv_values(wizard.path).get("DCH_CONNECTION_ID")
    assert "End-to-end query succeeded" not in output
    assert_no_secrets(output)


def test_model_failure_is_safe_and_progress_is_saved(wizard, monkeypatch, capsys):
    answers(monkeypatch, ["", "", "y", "", "", ""])
    monkeypatch.setattr(
        setup,
        "run_question",
        Mock(side_effect=setup.APIError("private-model-token", request=Mock(), body=None)),
    )
    assert setup.run_setup(wizard.path) == 1
    output = capsys.readouterr().out
    assert "End-to-end query succeeded" not in output
    assert_no_secrets(output)
    assert dotenv_values(wizard.path)["LLM_MODEL"] == "test-model"


def test_noninteractive_setup_stops_before_config_or_network(wizard, monkeypatch, capsys):
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: False)
    assert setup.run_setup(wizard.path) == 2
    wizard.factory.assert_not_called()
    assert "interactive terminal" in capsys.readouterr().out


def test_readiness_server_error_reports_operation_status_and_scoped_fix(
    wizard, monkeypatch, capsys
):
    wizard.connection.status.state = "ingestion_not_ready"
    wizard.client.check_connection_readiness.side_effect = DCHServerError(
        "private-db-password",
        502,
        '{"code":"connection_check_failed","message":"private-db-password"}',
    )
    answers(monkeypatch, ["", "", "n"])
    assert setup.run_setup(wizard.path) == 1
    output = capsys.readouterr().out
    assert "Connection readiness failed" in output
    assert "HTTP 502" in output and "connection_check_failed" in output
    assert "--resource-name=demo-pg-creds" in output
    wizard.client.get_tables.assert_not_called()
    assert_no_secrets(output)


def test_wizard_accepts_and_saves_an_empty_optional_model_key(wizard, monkeypatch):
    set_key(wizard.path, "LLM_API_KEY", "")
    answers(monkeypatch, ["", "", "y", "n", "", "", "", ""], secrets=("",))
    monkeypatch.setattr(
        setup,
        "run_question",
        Mock(return_value={"sql": "SELECT 1 LIMIT 100", "result": pd.DataFrame({"value": [1]})}),
    )
    assert setup.run_setup(wizard.path) == 0
    assert dotenv_values(wizard.path)["LLM_API_KEY"] == ""
    key_provider = wizard.model_factory.call_args.kwargs["api_key"]
    assert callable(key_provider) and key_provider() == ""


def test_clearing_optional_setting_stores_empty_string(wizard, monkeypatch):
    answers(monkeypatch, [], secrets=("-",))
    settings = setup.Settings(wizard.path)
    assert settings.prompt("LLM_API_KEY", "Model key", secret=True, optional=True) == ""
    assert dotenv_values(wizard.path)["LLM_API_KEY"] == ""


def test_model_selection_saves_exact_maas_id(wizard, monkeypatch, capsys):
    model_id = "publishers/llm/models/meta-llama/Llama-3.1-8B-Instruct"
    client = Mock()
    client.models.list.return_value = SimpleNamespace(data=[SimpleNamespace(id=model_id)])
    answers(monkeypatch, ["llama-3-1-8b-instruct", "1"])
    assert setup.choose_model(client, setup.Settings(wizard.path)) == model_id
    assert dotenv_values(wizard.path)["LLM_MODEL"] == model_id
    assert "Choose a listed number or exact model ID" in capsys.readouterr().out


def test_saved_gateway_needs_one_confirmation_and_no_secret_entry(wizard, monkeypatch, capsys):
    answers(monkeypatch, [""], secrets=())
    settings = setup.Settings(wizard.path)
    setup.configure_gateway(settings)
    output = capsys.readouterr().out
    assert "[1/5]" in output and "Gateway: gateway.test:443" in output
    assert "saved (hidden)" in output
    assert_no_secrets(output)


def test_declining_saved_settings_allows_editing(wizard, monkeypatch):
    answers(monkeypatch, ["n", "new-gateway.test:443", "", ""], secrets=("new-token",))
    setup.configure_gateway(setup.Settings(wizard.path))
    values = dotenv_values(wizard.path)
    assert values["DCH_HOST"] == "new-gateway.test:443"
    assert values["DCH_TOKEN"] == "new-token"
    assert values["DCH_TENANT_ID"] == "demo-tenant"


def test_certificate_prompt_retries_without_saving_invalid_path(wizard, monkeypatch, capsys):
    path = wizard.path.parent / "ca.pem"
    path.write_text("test CA")
    settings = setup.Settings(wizard.path)
    choices = iter([str(path.parent / "missing.pem"), str(path)])

    def answer(_):
        assert not dotenv_values(wizard.path).get("DCH_CA_CERT")
        return next(choices)

    monkeypatch.setattr("builtins.input", answer)
    assert settings.prompt(
        "DCH_CA_CERT", "Gateway CA", optional=True, validate=certificate_file
    ) == str(path.resolve())
    assert "Certificate file not found" in capsys.readouterr().out


def test_missing_saved_certificate_opens_edit_prompts(wizard, monkeypatch, capsys):
    set_key(wizard.path, "DCH_CA_CERT", str(wizard.path.parent / "missing.pem"))
    answers(monkeypatch, ["", "", "-"], secrets=("",))
    setup.configure_gateway(setup.Settings(wizard.path))
    assert dotenv_values(wizard.path)["DCH_CA_CERT"] == ""
    assert "file is missing" in capsys.readouterr().out


def test_reused_certificate_path_expands_home(wizard, monkeypatch):
    path = Path(__file__).resolve()
    set_key(wizard.path, "DCH_CA_CERT", "~/" + str(path.relative_to(Path.home())))
    answers(monkeypatch, [""])
    setup.configure_gateway(setup.Settings(wizard.path))
    assert dotenv_values(wizard.path)["DCH_CA_CERT"] == str(path.resolve())


def test_uri_fields_retry_invalid_port_and_sslmode(wizard, monkeypatch, capsys):
    answers(
        monkeypatch,
        ["", "source.test", "bad-port", "65536", "5433", "demo", "user", "bad-mode", "require"],
        secrets=("-", "password with spaces "),
    )
    uri = setup.postgres_uri(setup.Settings(wizard.path))
    parsed = urlsplit(uri)
    assert parsed.port == 5433
    assert unquote(parsed.password) == "password with spaces "
    output = capsys.readouterr().out
    assert "port between 1 and 65535" in output and "Choose disable" in output
    assert "password with spaces" not in output


def test_connection_menu_reprompts_for_out_of_range_number(wizard, monkeypatch, capsys):
    answers(monkeypatch, ["999", "1"])
    setup.choose_connection(wizard.client, setup.Settings(wizard.path))
    wizard.client.get_connection.assert_called_once_with("existing-id")
    assert "Choose a listed number" in capsys.readouterr().out


def test_connection_menu_reprompts_for_unknown_id(wizard, monkeypatch, capsys):
    wizard.client.get_connection.side_effect = [
        DCHNotFoundError("private-token", 404),
        wizard.connection,
    ]
    answers(monkeypatch, ["wrong-id", "1"])
    connection = setup.choose_connection(wizard.client, setup.Settings(wizard.path))
    assert connection.id == "existing-id"
    output = capsys.readouterr().out
    assert "Connection not found" in output and "private-token" not in output


def test_connection_menu_only_lists_postgres_sources(wizard, monkeypatch, capsys):
    wizard.client.list_connection_types.return_value.append(
        ConnectionType(id="sqlite-type", name="SQLite", provider="sqlite")
    )
    wizard.client.list_connections.return_value.append(
        SimpleNamespace(
            id="sqlite-id",
            name="unrelated-sqlite",
            format="tabular",
            data_connection_type_id="sqlite-type",
            status=SimpleNamespace(state="ready"),
        )
    )
    answers(monkeypatch, ["1"])
    setup.choose_connection(wizard.client, setup.Settings(wizard.path))
    output = capsys.readouterr().out
    assert "demo [ready; saved]" in output
    assert "unrelated-sqlite" not in output


def test_dns_failure_does_not_suggest_rbac_changes(wizard, monkeypatch, capsys):
    wizard.client.get_tables.side_effect = DCHConnectionError("getaddrinfo failed")
    answers(monkeypatch, ["", "", "n"])
    assert setup.run_setup(wizard.path) == 1
    output = capsys.readouterr().out
    assert "DNS lookup failed" in output and "oc create role" not in output


def test_custom_question_does_not_print_unrelated_revenue_and_next_command_is_usable(
    wizard, monkeypatch, capsys
):
    import shlex

    answers(monkeypatch, ["", "", "y", "", "", "Which customers have never ordered?"])
    monkeypatch.setattr(
        setup,
        "run_question",
        Mock(return_value={"sql": "SELECT 1", "result": pd.DataFrame({"name": ["Sofia"]})}),
    )
    assert setup.run_setup(wizard.path) == 0
    output = capsys.readouterr().out
    assert "5510" not in output
    command = output.split("Try another question:\n", 1)[1].strip()
    args = shlex.split(command.replace("\\\n", ""))
    assert args[:4] == ["uv", "run", "dch-nlsql", "ask"]
    assert args.count("--table") == 3
    assert Path(args[-1]).is_file()
    assert "\\" not in args


def test_settings_protect_existing_env_file_and_report_shell_override(wizard, monkeypatch):
    wizard.path.chmod(0o644)
    monkeypatch.setenv("DCH_HOST", "shell-gateway.test:443")
    settings = setup.Settings(wizard.path)
    assert settings.get("DCH_HOST") == "shell-gateway.test:443"
    assert settings.overrides == ["DCH_HOST"]
    settings.save("DCH_TOKEN", "replacement-token")
    assert wizard.path.stat().st_mode & 0o777 == 0o600


def test_fresh_setup_walks_through_all_five_stages(wizard, monkeypatch, capsys):
    wizard.path.unlink()
    wizard.client.list_connections.return_value = []
    answers(
        monkeypatch,
        [
            "gateway.test:443",
            "demo-tenant",
            "",  # gateway and optional CA
            "",
            "",
            "",
            "",  # new connection, name, type, existing Secret
            "source.test",
            "",
            "demo",
            "user",
            "",
            "",  # DB fields, SSL mode, CA
            "y",
            "https://model.test/v1",
            "",
            "",
            "",  # model URL, CA, model, question
        ],
        secrets=("private-dch-token", "", "private-db-password", "private-model-token"),
    )
    monkeypatch.setattr(
        setup,
        "run_question",
        Mock(return_value={"sql": "SELECT 1", "result": pd.DataFrame({"value": [1]})}),
    )
    assert setup.run_setup(wizard.path) == 0
    values = dotenv_values(wizard.path)
    assert values["DCH_CONNECTION_ID"] == "new-id"
    assert values["LLM_MODEL"] == "test-model"
    assert values["DCH_CA_CERT"] == values["SSL_CERT_FILE"] == ""
    output = capsys.readouterr().out
    assert all(f"[{number}/5]" in output for number in range(1, 6))
    assert_no_secrets(output)
