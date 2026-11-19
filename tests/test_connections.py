from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from data_connect_hub import (
    ConnectionType,
    DCHAuthenticationError,
    DCHConnectionError,
    DCHValidationError,
)

from nlsql import cli
from nlsql.connections import create_postgres_connection, resolve_postgres_type

URI = "postgresql://demo:private-password@db.example:5432/demo"


@pytest.fixture
def dch():
    client = Mock()
    client.get_connection_type.return_value = ConnectionType(
        id="pg-type",
        name="Postgres",
        provider="postgres",
        status={"flight_ready": True, "supported_formats": ["tabular"]},
    )
    client.list_connections.return_value = []
    client.list_connection_types.return_value = [client.get_connection_type.return_value]
    client.create_connection.return_value = SimpleNamespace(id="new-connection")
    client.get_connection.return_value = SimpleNamespace(status=SimpleNamespace(state="ready"))
    return client


def test_inline_registration_validates_and_keeps_connection(dch):
    created = create_postgres_connection(
        dch, name="demo", connection_type_id="postgres-type", uri=URI, ca_pem="CA CONTENT"
    )
    assert created.id == "new-connection"
    dch.test_credentials.assert_called_once_with("pg-type", {"URI": URI, "CA_CERT": "CA CONTENT"})
    call = dch.create_connection.call_args.kwargs
    assert call["connection_type_id"] == "pg-type"
    assert call["data_format"] == "tabular"
    assert call["credentials"].secret.startswith("nlsql-postgres-")
    assert call["credentials"].properties == {"URI": URI, "CA_CERT": "CA CONTENT"}
    assert "credentials_ref" not in call
    dch.delete_connection.assert_not_called()
    assert [c[0] for c in dch.method_calls] == [
        "get_connection_type",
        "list_connections",
        "test_credentials",
        "create_connection",
    ]


def test_existing_secret_registration_does_not_transmit_credentials(dch):
    create_postgres_connection(
        dch, name="demo", connection_type_id="postgres-type", existing_secret="existing-pg-secret"
    )
    dch.test_credentials.assert_not_called()
    call = dch.create_connection.call_args.kwargs
    assert call["credentials_ref"].secret == "existing-pg-secret"
    assert "credentials" not in call


def test_bad_credentials_stop_before_creating_resources(dch):
    dch.test_credentials.side_effect = DCHAuthenticationError("not authorized", 401)
    with pytest.raises(DCHAuthenticationError):
        create_postgres_connection(dch, name="demo", connection_type_id="pg", uri=URI)
    dch.create_connection.assert_not_called()


def test_duplicate_name_does_not_create_another_connection(dch):
    dch.list_connections.return_value = [SimpleNamespace(name="demo", id="existing-id")]
    with pytest.raises(ValueError, match="existing-id"):
        create_postgres_connection(dch, name="demo", connection_type_id="pg", uri=URI)
    dch.test_credentials.assert_not_called()
    dch.create_connection.assert_not_called()


@pytest.mark.parametrize("provider,flight", [("milvus", True), ("postgres", False)])
def test_incompatible_type_is_rejected(dch, provider, flight):
    dch.get_connection_type.return_value.provider = provider
    dch.get_connection_type.return_value.status.flight_ready = flight
    with pytest.raises(ValueError):
        create_postgres_connection(dch, name="demo", connection_type_id="wrong", uri=URI)
    dch.create_connection.assert_not_called()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"uri": "https://wrong:private-password@example.com"},
        {"uri": URI, "existing_secret": "existing"},
        {"existing_secret": " "},
        {},
    ],
)
def test_invalid_inputs_do_not_reach_dch(dch, kwargs):
    with pytest.raises(ValueError) as error:
        create_postgres_connection(dch, name="demo", connection_type_id="pg", **kwargs)
    assert "private-password" not in str(error.value)
    assert not dch.method_calls


@pytest.fixture
def registration_cli(dch, monkeypatch):
    for name, value in {
        "DCH_HOST": "gateway:443",
        "DCH_TOKEN": "dch-secret",
        "DCH_TENANT_ID": "tenant-a",
        "DCH_CONNECTION_TYPE_NAME": "Postgres",
        "POSTGRES_URI": URI,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("POSTGRES_CA_CERT", raising=False)
    monkeypatch.delenv("DCH_CONNECTION_ID", raising=False)
    factory = MagicMock()
    factory.return_value.__enter__.return_value = dch
    monkeypatch.setattr(cli, "DataConnectClient", factory)
    monkeypatch.setattr(cli, "load_dotenv", Mock())
    return dch


def test_cli_prints_persistent_id_and_checks_readiness(registration_cli, capsys):
    assert cli.main(["create-connection", "--name", "demo"]) == 0
    registration_cli.check_connection_readiness.assert_called_once_with("new-connection")
    registration_cli.get_connection.assert_called_once_with("new-connection")
    output = capsys.readouterr().out
    assert "DCH_CONNECTION_ID=new-connection" in output
    assert "Readiness: ready" in output
    assert "private-password" not in output and "dch-secret" not in output
    registration_cli.delete_connection.assert_not_called()


def test_readiness_failure_still_prints_created_id(registration_cli, capsys):
    registration_cli.check_connection_readiness.side_effect = DCHConnectionError(URI)
    assert cli.main(["create-connection"]) == 1
    captured = capsys.readouterr()
    assert "DCH_CONNECTION_ID=new-connection" in captured.out
    assert "private-password" not in captured.out + captured.err
    registration_cli.delete_connection.assert_not_called()


def test_not_ready_is_reported_as_failure(registration_cli, capsys):
    registration_cli.get_connection.return_value.status.state = "not_ready"
    assert cli.main(["create-connection"]) == 1
    captured = capsys.readouterr()
    assert "DCH_CONNECTION_ID=new-connection" in captured.out
    assert "not ready" in captured.err


def test_explicit_existing_secret_ignores_inline_uri(registration_cli):
    assert cli.main(["create-connection", "--credentials-secret", "saved-secret"]) == 0
    registration_cli.test_credentials.assert_not_called()
    assert (
        registration_cli.create_connection.call_args.kwargs["credentials_ref"].secret
        == "saved-secret"
    )


def test_list_types_uses_installed_sdk_status_model(registration_cli, capsys):
    registration_cli.list_connection_types.return_value = [
        registration_cli.get_connection_type.return_value
    ]
    assert cli.main(["connection-types"]) == 0
    assert '"flight_ready": true' in capsys.readouterr().out


@pytest.mark.parametrize("type_name", [None, "Postgres", "postgres", "  POSTGRES  "])
def test_type_name_lookup_uses_resolved_id(dch, type_name):
    create_postgres_connection(dch, name="demo", connection_type_name=type_name, uri=URI)
    dch.list_connection_types.assert_called_once()
    dch.get_connection_type.assert_not_called()
    assert dch.create_connection.call_args.kwargs["connection_type_id"] == "pg-type"
    dch.test_credentials.assert_called_once_with("pg-type", {"URI": URI})


def test_type_name_does_not_match_provider_or_substring(dch):
    dch.list_connection_types.return_value = [
        ConnectionType(
            id="vector", name="PGVector", provider="postgres", status={"flight_ready": True}
        )
    ]
    with pytest.raises(ValueError, match="No connection type named"):
        create_postgres_connection(dch, name="demo", connection_type_name="Postgres", uri=URI)
    dch.test_credentials.assert_not_called()
    dch.create_connection.assert_not_called()


def test_duplicate_type_names_require_explicit_selection(dch):
    dch.list_connection_types.return_value *= 2
    with pytest.raises(ValueError, match="Multiple connection types"):
        create_postgres_connection(dch, name="demo", uri=URI)
    dch.create_connection.assert_not_called()


def test_empty_type_name_and_conflicting_selectors_fail_before_network(dch):
    with pytest.raises(ValueError, match="empty"):
        resolve_postgres_type(dch, type_name=" ")
    with pytest.raises(ValueError, match="exclusively"):
        resolve_postgres_type(dch, type_name="Postgres", type_id="pg-type")
    assert not dch.method_calls


def test_cli_type_name_overrides_environment(registration_cli, monkeypatch):
    monkeypatch.setenv("DCH_CONNECTION_TYPE_NAME", "Different")
    assert cli.main(["create-connection", "--connection-type", "Postgres"]) == 0
    registration_cli.list_connection_types.assert_called_once()
    registration_cli.get_connection_type.assert_not_called()
    assert registration_cli.create_connection.call_args.kwargs["connection_type_id"] == "pg-type"


def test_cli_explicit_id_overrides_name_environment(registration_cli):
    assert cli.main(["create-connection", "--connection-type-id", "explicit-id"]) == 0
    registration_cli.get_connection_type.assert_called_once_with("explicit-id")
    registration_cli.list_connection_types.assert_not_called()


def test_creation_failure_reports_stage_and_secret_permission_hint(dch):
    dch.create_connection.side_effect = DCHValidationError(
        URI, 400, '{"code":"cannot_create_secret","message":"private-password"}'
    )
    with pytest.raises(ValueError, match="Connection creation failed") as error:
        create_postgres_connection(dch, name="demo", uri=URI)
    assert "Secret permissions" in str(error.value)
    assert "private-password" not in str(error.value)
    dch.test_credentials.assert_called_once()


def test_credential_failure_reports_stage_before_creation(dch):
    dch.test_credentials.side_effect = DCHValidationError(
        URI, 400, '{"code":"credentials_check_failed","message":"private-password"}'
    )
    with pytest.raises(ValueError, match="Credential validation failed") as error:
        create_postgres_connection(dch, name="demo", uri=URI)
    assert "private-password" not in str(error.value)
    dch.create_connection.assert_not_called()
