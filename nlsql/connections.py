"""Register PostgreSQL sources using the DCH SDK connection lifecycle."""

from uuid import uuid4

from data_connect_hub import CredentialsRef, DCHValidationError, InlineCredentials

from .diagnostics import validation_error_hint


def resolve_postgres_type(client, *, type_name: str | None = None, type_id: str | None = None):
    """Resolve one named type in the caller's tenant, with an optional explicit ID."""
    if type_id is not None:
        if type_name is not None:
            raise ValueError("Specify a connection type name or ID, exclusively")
        if not type_id.strip():
            raise ValueError("Connection type ID must not be empty")
        connection_type = client.get_connection_type(type_id)
    else:
        name = (type_name if type_name is not None else "Postgres").strip()
        if not name:
            raise ValueError("Connection type name must not be empty")
        matches = [
            t for t in client.list_connection_types() if t.name.casefold() == name.casefold()
        ]
        if not matches:
            raise ValueError(
                f"No connection type named {name!r}. Run 'dch-nlsql connection-types' to list names"
            )
        if len(matches) > 1:
            raise ValueError(
                f"Multiple connection types named {name!r}. Use --connection-type-id to select one"
            )
        connection_type = matches[0]
    if connection_type.provider != "postgres":
        raise ValueError("Select a connection type with provider 'postgres'")
    if not connection_type.status.flight_ready:
        raise ValueError("The selected connection type is not Flight-ready")
    return connection_type


def create_postgres_connection(
    client,
    *,
    name: str,
    connection_type_name: str | None = None,
    connection_type_id: str | None = None,
    uri: str | None = None,
    existing_secret: str | None = None,
    ca_pem: str | None = None,
):
    """Create a persistent connection; readiness is checked separately by the caller."""
    if not name.strip():
        raise ValueError("Connection name must not be empty")
    if existing_secret is not None:
        if not existing_secret.strip() or uri is not None or ca_pem is not None:
            raise ValueError("Use an existing credential Secret or inline credentials, exclusively")
    elif not uri or not uri.startswith(("postgresql://", "postgres://")):
        raise ValueError("POSTGRES_URI must be a postgresql:// or postgres:// connection URI")

    connection_type = resolve_postgres_type(
        client, type_name=connection_type_name, type_id=connection_type_id
    )
    connection_type_id = connection_type.id
    matches = [c for c in client.list_connections() if c.name == name]
    if matches:
        ids = ", ".join(c.id for c in matches)
        raise ValueError(
            f"A connection named {name!r} already exists: {ids}. Use its ID or another name"
        )

    if existing_secret is not None:
        credential_args = {"credentials_ref": CredentialsRef(secret=existing_secret)}
    else:
        credentials = {"URI": uri}
        if ca_pem:
            credentials["CA_CERT"] = ca_pem
        try:
            client.test_credentials(connection_type_id, credentials)
        except DCHValidationError as exc:
            raise ValueError(f"Credential validation failed. {validation_error_hint(exc)}") from exc
        credential_args = {
            "credentials": InlineCredentials(
                secret=f"nlsql-postgres-{uuid4().hex[:12]}", properties=credentials
            )
        }
    try:
        return client.create_connection(
            name=name,
            connection_type_id=connection_type_id,
            data_format="tabular",
            **credential_args,
        )
    except DCHValidationError as exc:
        raise ValueError(f"Connection creation failed. {validation_error_hint(exc)}") from exc
