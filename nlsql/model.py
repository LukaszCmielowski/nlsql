"""Model-client options shared by the CLI and setup wizard."""

from openai import APIStatusError, omit


def model_auth_options(value: str | None) -> dict:
    """Use a real key, or explicitly omit authentication for an empty setting.

    The SDK requires a credential argument at construction. Its public callable
    API and public header omission sentinel let an unauthenticated endpoint use
    an empty string without a dummy credential or environment-key fallback.
    """
    key = (value or "").strip()
    if key:
        return {"api_key": key}
    return {"api_key": lambda: ""}


def model_request_options(client) -> dict:
    """Explicitly omit authentication per request when the configured key is empty."""
    if getattr(client, "api_key", None) == "":
        return {"extra_headers": {"Authorization": omit}}
    return {}


def available_models(client) -> list[str] | None:
    """Discover ready model IDs verbatim; None means model listing is unsupported."""
    try:
        models = client.models.list(**model_request_options(client))
    except APIStatusError as exc:
        if exc.status_code in {403, 404, 405, 501}:
            return None
        raise
    return sorted({model.id for model in models.data if getattr(model, "ready", True) is not False})
