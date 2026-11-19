"""Actionable connection diagnostics without exposing transport exception bodies."""

import json
import socket
import ssl

from data_connect_hub import (
    DCHAuthenticationError,
    DCHConnectionError,
    DCHForbiddenError,
    DCHServerError,
    DCHValidationError,
)
from openai import APIError
from pyarrow import flight
from sqlglot.errors import SqlglotError


def dch_error_code(error: Exception) -> str:
    """Read a structured code without exposing backend messages."""
    try:
        payload = json.loads(getattr(error, "body", "") or "{}")
    except (ValueError, TypeError):
        return ""
    code = payload.get("code") if isinstance(payload, dict) else None
    return code if isinstance(code, str) else ""


def error_hint(error: Exception) -> str:
    """Use the same credential-safe diagnostics in setup and ordinary commands."""
    if isinstance(error, DCHConnectionError):
        return connection_error_hint(error)
    if isinstance(error, DCHValidationError):
        return validation_error_hint(error)
    if isinstance(error, DCHServerError):
        return server_error_hint(error)
    if isinstance(error, DCHAuthenticationError):
        return "DCH authentication failed. Refresh DCH_TOKEN using your OpenShift login."
    if isinstance(error, DCHForbiddenError):
        return "Your token lacks the required DCH tenant permission. Ask the tenant administrator to grant access."
    if isinstance(error, APIError):
        return model_error_hint(error)
    if isinstance(error, ValueError):
        return str(error)
    if isinstance(error, SqlglotError):
        return "SQL could not be parsed. Check its syntax for the selected dialect."
    if isinstance(error, (FileNotFoundError, PermissionError)):
        return "Could not read a local file. Check configured file paths and permissions."
    return f"Request failed ({type(error).__name__}). Check endpoint access and service logs."


def model_error_hint(error: Exception) -> str:
    """Explain model errors without printing credentials or provider response bodies."""
    status = getattr(error, "status_code", None)
    hints = {
        401: "The model endpoint returned HTTP 401: authentication was rejected. Provide the serving token or API key accepted by this endpoint. Leave the key empty only when the endpoint allows anonymous access. A gateway can reject authentication before checking the URL path.",
        403: "The model endpoint returned HTTP 403: the token does not grant access to this model. Check model-serving permissions.",
        404: "The model endpoint returned HTTP 404. Check LLM_BASE_URL (including any gateway prefix and /v1 where required) and the deployed LLM_MODEL ID.",
        429: "The model endpoint returned HTTP 429. Check model rate limits, quota, and available capacity.",
    }
    if isinstance(status, int) and status in hints:
        return hints[status]
    return (
        f"Model request failed ({type(error).__name__}). Check LLM_BASE_URL, LLM_MODEL, "
        "the model token, and SSL_CERT_FILE if the endpoint uses a private CA."
    )


def server_error_hint(error: Exception) -> str:
    """Explain server failures using fixed messages, never backend response bodies."""
    hints = {
        "connection_check_failed": "DCH could not verify the saved source connection. Check Flight's read access to its credential Secret in the tenant, then source connectivity.",
        "flight_service_error": "The REST-to-Flight request failed. Check Flight service health, authentication, TLS, and credential Secret access.",
        "status_update_failed": "DCH could not persist connection status. Ask the DCH administrator to check REST and metadata-store logs.",
        "auth_error": "DCH's server-side authorization check failed. Ask the DCH administrator to check Kubernetes API access.",
        "connection": "A DCH backend connection failed. Check the service logs and source connectivity.",
        "config": "A DCH service configuration error occurred. Check the DCH service logs.",
    }
    code = dch_error_code(error)
    status = getattr(error, "status_code", None)
    prefix = (
        f"DCH returned HTTP {status}. "
        if isinstance(status, int)
        else "DCH server request failed. "
    )
    if code in hints:
        return prefix + f"Error {code}: {hints[code]}"
    return prefix + "Check the DCH REST and Flight service logs for the failing operation."


def validation_error_hint(error: Exception) -> str:
    """Explain known DCH response codes without echoing server-provided messages."""
    hints = {
        "cannot_create_secret": "DCH could not create the credential Secret. Check the REST service's Secret permissions in DCH_TENANT_ID and whether the Secret name already exists.",
        "credentials_check_failed": "DCH could not validate the PostgreSQL credentials. Check POSTGRES_URI, source TLS settings, and database reachability from DCH.",
        "invalid_secret": "DCH rejected the credential Secret reference. Check the Secret name and tenant namespace.",
        "missing_field": "DCH reports a missing request field. Check the connection type's required credential fields and SDK/server compatibility.",
        "invalid_json": "DCH rejected the request format. Check SDK/server API compatibility.",
        "invalid_data_connection_type": "DCH rejected the connection type. Refresh the type lookup in the selected tenant.",
        "invalid_tenant_id": "DCH rejected DCH_TENANT_ID. Use your tenant's Kubernetes namespace.",
    }
    code = dch_error_code(error)
    if code in hints:
        return f"DCH error {code}: {hints[code]}"
    return "DCH rejected the request. Check required credential fields, tenant configuration, and DCH service logs."


def _exception_chain(error: Exception) -> list[Exception]:
    chain, seen, current = [], set(), error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def connection_error_hint(error: Exception) -> str:
    chain = _exception_chain(error)
    message = " ".join(str(item) for item in chain).lower()
    if "failed to obtain credentials" in message:
        return (
            "DCH Flight could not read the connection's credential Secret. Verify that it exists "
            "in DCH_TENANT_ID and grant the Flight service account get access to that specific Secret."
        )
    if "hostname mismatch" in message or "not valid for" in message:
        return "The TLS certificate does not match DCH_HOST. Check the gateway hostname and certificate."
    if "certificate has expired" in message or "certificate is not yet valid" in message:
        return "The gateway TLS certificate is expired or not yet valid. Check its validity and your clock."
    if any(isinstance(item, ssl.SSLCertVerificationError) for item in chain) or any(
        text in message for text in ("certificate_verify_failed", "certificate verify failed")
    ):
        return (
            "TLS certificate verification failed. Set DCH_CA_CERT to a trusted CA bundle "
            "for the DCH gateway (not the PostgreSQL server)."
        )
    if any(isinstance(item, flight.FlightUnauthenticatedError) for item in chain) or any(
        text in message for text in ("unauthenticated", "token has expired", "token is expired")
    ):
        return (
            "DCH Flight authentication failed. Refresh DCH_TOKEN and confirm that the "
            "gateway accepts it for Flight as well as REST."
        )
    if any(isinstance(item, flight.FlightUnauthorizedError) for item in chain) or any(
        text in message for text in ("permission denied", "permission_denied", "forbidden")
    ):
        return (
            "DCH Flight access was denied. Check tenant permissions, service-account access "
            "to the credential Secret, and source database permissions."
        )
    if any(isinstance(item, socket.gaierror) for item in chain) or any(
        text in message
        for text in ("getaddrinfo", "nodename nor servname", "name or service not known")
    ):
        return "DNS lookup failed. Check DCH_HOST and your VPN/DNS connection."
    if "connection refused" in message:
        return "The gateway refused the connection. Check DCH_HOST, its port, and gateway availability."
    if any(isinstance(item, flight.FlightTimedOutError) for item in chain) or any(
        text in message for text in ("timed out", "timeout", "deadline exceeded")
    ):
        return "The gateway connection timed out. Check routing, firewall, proxy, and VPN access."
    if any(
        isinstance(item, (ConnectionResetError, ConnectionAbortedError)) for item in chain
    ) or any(
        text in message
        for text in ("connection reset", "unexpected eof", "stream removed", "goaway")
    ):
        return (
            "The DCH connection was interrupted. Retry this check; if it repeats, check "
            "gateway/proxy connection handling, VPN access, and Flight service health."
        )
    if any(isinstance(item, flight.FlightUnavailableError) for item in chain) or any(
        text in message for text in ("no healthy upstream", "statuscode.unavailable")
    ):
        return (
            "DCH Flight is unavailable. Retry this check; if it persists, check gateway "
            "routing and Flight service health. REST connection status may still be ready."
        )
    if any(isinstance(item, flight.FlightInternalError) for item in chain):
        return (
            "DCH Flight returned an internal error. Check the Flight service logs for this "
            "operation; REST connection status may still be ready."
        )
    if "server returned no flight endpoints" in message:
        return (
            "DCH Flight returned no metadata endpoint. Check Flight service health and "
            "SDK/server compatibility."
        )
    cause = type(chain[-1]).__name__
    return (
        f"DCH connection failed ({cause}). Check DCH_HOST, gateway routing, proxy/VPN access, "
        "and TLS configuration. If it persists, check DCH Flight logs for this operation."
    )
