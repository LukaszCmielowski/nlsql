import httpx
import pytest
from openai import DefaultHttpxClient, OpenAI

from nlsql.model import available_models, model_auth_options, model_request_options
from nlsql.schema import TableSchema
from nlsql.service import draft_sql


def test_model_discovery_preserves_qualified_ids_and_filters_unready_models():
    model_id = "publishers/llm/models/meta-llama/Llama-3.1-8B-Instruct"

    def respond(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": model_id, "ready": True},
                    {"id": "starting-model", "ready": False},
                    {"id": "standard-model"},
                ],
            },
        )

    with OpenAI(
        base_url="https://model.test/v1",
        **model_auth_options(""),
        http_client=DefaultHttpxClient(transport=httpx.MockTransport(respond)),
        max_retries=0,
    ) as client:
        assert available_models(client) == [model_id, "standard-model"]


@pytest.mark.parametrize("status", [403, 404, 405, 501])
def test_unsupported_model_listing_allows_manual_configuration(status):
    with OpenAI(
        base_url="https://model.test/v1",
        api_key="real-key",
        http_client=DefaultHttpxClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status, json={}))
        ),
        max_retries=0,
    ) as client:
        assert available_models(client) is None


def test_model_discovery_does_not_hide_invalid_credentials():
    from openai import AuthenticationError

    with (
        OpenAI(
            base_url="https://model.test/v1",
            api_key="bad-key",
            http_client=DefaultHttpxClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(401, json={}))
            ),
            max_retries=0,
        ) as client,
        pytest.raises(AuthenticationError),
    ):
        available_models(client)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_key_sends_no_authorization_header_or_environment_fallback(value, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-environment-key")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"object": "list", "data": []})

    with OpenAI(
        base_url="https://model.test/v1",
        **model_auth_options(value),
        http_client=DefaultHttpxClient(transport=httpx.MockTransport(respond)),
        max_retries=0,
    ) as client:
        client.models.list(**model_request_options(client))
    assert len(requests) == 1
    assert "authorization" not in requests[0].headers


def test_protected_endpoint_receives_real_key():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"object": "list", "data": []})

    with OpenAI(
        base_url="https://model.test/v1",
        **model_auth_options("real-model-key"),
        http_client=DefaultHttpxClient(transport=httpx.MockTransport(respond)),
        max_retries=0,
    ) as client:
        client.models.list(**model_request_options(client))
    assert requests[0].headers["authorization"] == "Bearer real-model-key"


def test_sql_generation_uses_anonymous_chat_request_when_key_is_empty():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "completion-test",
                "object": "chat.completion",
                "created": 0,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "SELECT COUNT(*) FROM demo.customers",
                        },
                    }
                ],
            },
        )

    with OpenAI(
        base_url="https://model.test/v1",
        **model_auth_options(""),
        http_client=DefaultHttpxClient(transport=httpx.MockTransport(respond)),
        max_retries=0,
    ) as client:
        sql = draft_sql(
            client,
            "test",
            "How many customers?",
            [TableSchema("customers", "demo", (("id", "int64"),))],
            "postgres",
            10,
        )
    assert sql == "SELECT COUNT(*) FROM demo.customers"
    assert requests[0].url.path == "/v1/chat/completions"
    assert "authorization" not in requests[0].headers
