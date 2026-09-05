"""Usage, model discovery, and search boundary tests."""

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from astrbot_plugin_codex_provider import codex_source


def _access_token() -> str:
    def encode(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return (
        f"{encode({'alg': 'none'})}."
        f"{encode({'exp': int(time.time()) + 3600, 'https://api.openai.com/auth': {'chatgpt_account_id': 'account'}})}."
        "signature"
    )


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.mark.asyncio
async def test_usage_refreshes_before_request(monkeypatch) -> None:
    token = _access_token()
    captured = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url, *, headers):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse({"plan_type": "pro"})

    monkeypatch.setattr(codex_source.httpx, "AsyncClient", FakeAsyncClient)
    provider = SimpleNamespace(
        _maybe_refresh_token=AsyncMock(),
        _active_token=lambda: token,
        provider_config={
            "api_base": codex_source.CODEX_DEFAULT_API_BASE,
            "proxy": None,
        },
    )

    usage = await codex_source.ProviderCodex.fetch_usage(provider)

    provider._maybe_refresh_token.assert_awaited_once()
    assert usage == {"plan_type": "pro"}
    assert captured["url"] == "https://chatgpt.com/backend-api/wham/usage"


@pytest.mark.asyncio
async def test_search_rejects_malformed_success_envelope(monkeypatch) -> None:
    token = _access_token()

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, *, json, headers):
            return FakeResponse([])

    monkeypatch.setattr(codex_source.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        codex_source,
        "get_codex_settings",
        lambda: {"search_mode": "live", "search_context_size": "medium"},
    )
    provider = SimpleNamespace(
        _maybe_refresh_token=AsyncMock(),
        _active_token=lambda: token,
        get_model=lambda: "gpt-5.6-sol",
        provider_config={
            "api_base": codex_source.CODEX_DEFAULT_API_BASE,
            "proxy": None,
        },
    )

    with pytest.raises(RuntimeError, match="响应格式异常"):
        await codex_source.ProviderCodex.search_web(provider, "query")


@pytest.mark.asyncio
async def test_model_discovery_refreshes_before_fetch() -> None:
    provider = SimpleNamespace(
        _maybe_refresh_token=AsyncMock(),
        _active_token=lambda: "token",
        _fetch_remote_models=AsyncMock(return_value=["future-model"]),
    )

    models = await codex_source.ProviderCodex.get_models(provider)

    provider._maybe_refresh_token.assert_awaited_once()
    provider._fetch_remote_models.assert_awaited_once_with("token")
    assert "future-model" in models


@pytest.mark.asyncio
async def test_model_discovery_requests_current_catalog(monkeypatch) -> None:
    token = _access_token()
    captured = {}

    def handle(request):
        captured["request"] = request
        # Older clients receive a successful but empty catalog from the backend.
        models = (
            [{"slug": "gpt-6-astra"}, {"id": "future-model"}]
            if request.url.params.get("client_version") == "0.153.4"
            else []
        )
        return httpx.Response(200, json={"models": models})

    async_client = httpx.AsyncClient

    def make_client(**kwargs):
        captured["client"] = kwargs
        return async_client(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(codex_source.httpx, "AsyncClient", make_client)
    provider = SimpleNamespace(
        provider_config={"proxy": "http://127.0.0.1:10808"},
    )

    models = await codex_source.ProviderCodex._fetch_remote_models(provider, token)

    assert models == ["gpt-6-astra", "future-model"]
    request = captured["request"]
    assert str(request.url).startswith(codex_source.CODEX_DEFAULT_API_BASE + "/models?")
    version = request.url.params["client_version"]
    assert request.headers["user-agent"].startswith(f"codex_cli_rs/{version} ")
    assert request.headers["authorization"] == f"Bearer {token}"
    assert request.headers["chatgpt-account-id"] == "account"
    assert captured["client"]["proxy"] == "http://127.0.0.1:10808"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
async def test_model_discovery_http_failure_is_visible(
    monkeypatch, status_code
) -> None:
    async_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _: httpx.Response(status_code))
    monkeypatch.setattr(
        codex_source.httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=transport),
    )
    warning = Mock()
    monkeypatch.setattr(codex_source.logger, "warning", warning)
    provider = SimpleNamespace(provider_config={})

    assert (
        await codex_source.ProviderCodex._fetch_remote_models(provider, "token") == []
    )
    warning.assert_called_once()
    assert warning.call_args.args[1] == status_code


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_models", [[], ["gpt-6-astra", "future-model"]])
async def test_model_discovery_keeps_gpt6_without_duplicates(remote_models) -> None:
    provider = SimpleNamespace(
        _maybe_refresh_token=AsyncMock(),
        _active_token=lambda: "token",
        _fetch_remote_models=AsyncMock(return_value=remote_models),
    )

    models = await codex_source.ProviderCodex.get_models(provider)

    assert models.count("gpt-6-astra") == 1
    assert all(slug in models for slug in remote_models)


@pytest.mark.asyncio
async def test_model_discovery_network_failure_preserves_catalog(monkeypatch) -> None:
    token = _access_token()
    provider = SimpleNamespace(
        _maybe_refresh_token=AsyncMock(),
        _active_token=lambda: token,
        _fetch_remote_models=AsyncMock(
            side_effect=httpx.ConnectError(f"Bearer {token}")
        ),
    )
    warning = Mock()
    monkeypatch.setattr(codex_source.logger, "warning", warning)

    models = await codex_source.ProviderCodex.get_models(provider)

    assert "gpt-6-astra" in models
    warning.assert_called_once()
    assert token not in str(warning.call_args)
