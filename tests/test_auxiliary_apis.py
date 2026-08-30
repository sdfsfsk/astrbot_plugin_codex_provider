"""Usage, model discovery, and search boundary tests."""

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
