import json
import socket
import ssl
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from data_connect_hub import (
    DCHAuthenticationError,
    DCHConnectionError,
    DCHForbiddenError,
    DCHServerError,
    DCHValidationError,
)
from pyarrow import flight

from nlsql import cli
from nlsql.diagnostics import (
    connection_error_hint,
    error_hint,
    model_error_hint,
    server_error_hint,
    validation_error_hint,
)


@pytest.mark.parametrize(
    "message,expected",
    [
        ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", "DCH_CA_CERT"),
        ("certificate verify failed: Hostname mismatch", "does not match DCH_HOST"),
        ("certificate verify failed: certificate has expired", "expired"),
        ("getaddrinfo failed", "DNS lookup failed"),
        ("Connection refused", "refused"),
        ("Connection timed out", "timed out"),
        ("unknown transport failure", "Check DCH_HOST"),
        (
            "Flight unavailable: Secret not found: Failed to obtain credentials",
            "Flight service account",
        ),
    ],
)
def test_safe_actionable_hints(message, expected):
    error = DCHConnectionError(message + " postgresql://user:private-password@host/db token-secret")
    hint = connection_error_hint(error)
    assert expected in hint
    assert "private-password" not in hint and "token-secret" not in hint


@pytest.mark.parametrize(
    "cause,expected",
    [
        (ssl.SSLCertVerificationError(1, "verification failure"), "DCH_CA_CERT"),
        (socket.gaierror(-2, "lookup failed"), "DNS lookup failed"),
        (flight.FlightUnauthenticatedError("private-token"), "Refresh DCH_TOKEN"),
        (flight.FlightUnauthorizedError("private-token"), "Flight access was denied"),
        (flight.FlightUnavailableError("private-token"), "Flight is unavailable"),
        (flight.FlightTimedOutError("private-token"), "timed out"),
        (flight.FlightInternalError("private-token"), "internal error"),
        (ConnectionResetError("private-token"), "connection was interrupted"),
    ],
)
def test_nested_causes(cause, expected):
    error = DCHConnectionError("transport failed")
    error.__cause__ = cause
    hint = connection_error_hint(error)
    assert expected in hint
    assert "private-token" not in hint


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Received HTTP2 GOAWAY", "connection was interrupted"),
        ("gRPC StatusCode.UNAVAILABLE", "Flight is unavailable"),
        ("server returned no Flight endpoints for CommandGetTables", "no metadata endpoint"),
        ("Deadline exceeded", "timed out"),
        ("UNAUTHENTICATED", "Refresh DCH_TOKEN"),
    ],
)
def test_flight_status_text_is_recognized_without_exposing_raw_errors(message, expected):
    hint = connection_error_hint(DCHConnectionError(message + " private-token"))
    assert expected in hint and "private-token" not in hint


def test_unavailable_with_certificate_cause_keeps_tls_guidance():
    error = DCHConnectionError("transport failed")
    error.__cause__ = flight.FlightUnavailableError("certificate verify failed private-token")
    hint = connection_error_hint(error)
    assert "DCH_CA_CERT" in hint and "Retry" not in hint


def test_unknown_flight_error_includes_cause_type_without_its_message():
    error = DCHConnectionError("private-token")
    error.__cause__ = flight.FlightServerError("private-password")
    hint = connection_error_hint(error)
    assert "FlightServerError" in hint
    assert "private-token" not in hint and "private-password" not in hint


def test_cli_connection_types_explains_tls_failure_without_secrets(monkeypatch, capsys):
    for name, value in {
        "DCH_HOST": "gateway:443",
        "DCH_TOKEN": "token-secret",
        "DCH_TENANT_ID": "tenant",
    }.items():
        monkeypatch.setenv(name, value)
    factory = MagicMock()
    factory.return_value.__enter__.return_value.list_connection_types.side_effect = (
        DCHConnectionError("certificate verify failed: token-secret")
    )
    monkeypatch.setattr(cli, "DataConnectClient", factory)
    monkeypatch.setattr(cli, "load_dotenv", Mock())
    assert cli.main(["connection-types"]) == 1
    error = capsys.readouterr().err
    assert "DCH_CA_CERT" in error and "token-secret" not in error


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"code": "cannot_create_secret", "message": "private-password"}, "Secret permissions"),
        ({"code": "credentials_check_failed", "message": "private-password"}, "POSTGRES_URI"),
        ({"code": "missing_field", "message": "private-password"}, "required credential fields"),
        ({"code": "private-password", "message": "private-password"}, "DCH rejected the request"),
        ({"code": ["private-password"]}, "DCH rejected the request"),
        ([], "DCH rejected the request"),
    ],
)
def test_validation_hints_never_echo_server_messages(body, expected):
    error = DCHValidationError("private-password", 400, json.dumps(body))
    hint = validation_error_hint(error)
    assert expected in hint
    assert "private-password" not in hint


def test_malformed_validation_response_is_safe():
    assert "DCH rejected" in validation_error_hint(
        DCHValidationError("private-password", 400, "<html>private-password</html>")
    )


@pytest.mark.parametrize(
    "code",
    [
        "connection_check_failed",
        "flight_service_error",
        "status_update_failed",
        "private-password",
        ["private-password"],
    ],
)
def test_server_errors_show_status_and_safe_known_hints(code):
    error = DCHServerError(
        "private-password", 502, json.dumps({"code": code, "message": "private-password"})
    )
    hint = server_error_hint(error)
    assert "HTTP 502" in hint
    assert "private-password" not in hint
    if code == "connection_check_failed":
        assert "credential Secret" in hint


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "authentication was rejected"),
        (403, "does not grant access"),
        (404, "gateway prefix"),
        (429, "rate limits"),
        (500, "Model request failed"),
    ],
)
def test_model_error_hints_distinguish_authentication_and_api_path(status, expected):
    error = SimpleNamespace(status_code=status, body={"message": "private-model-token"})
    hint = model_error_hint(error)
    assert expected in hint
    assert "private-model-token" not in hint
    if status == 401:
        assert "Leave the key empty only when the endpoint allows anonymous access" in hint


@pytest.mark.parametrize(
    "error,expected",
    [
        (DCHAuthenticationError("private-token", 401), "Refresh DCH_TOKEN"),
        (DCHForbiddenError("private-token", 403), "tenant permission"),
        (FileNotFoundError("postgresql://user:private-token@host/db"), "file paths"),
    ],
)
def test_shared_diagnostics_explain_auth_and_files_without_raw_errors(error, expected):
    hint = error_hint(error)
    assert expected in hint
    assert "private-token" not in hint
