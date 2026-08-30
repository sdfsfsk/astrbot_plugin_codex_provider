"""Codex Responses wire-contract and streaming regression tests."""

import base64
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_codex_provider import codex_source

from astrbot.core.provider.sources.openai_responses_source import (
    ProviderOpenAIResponses,
)


def _access_token(account_id: str = "account", exp: int | None = None) -> str:
    def encode(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return (
        f"{encode({'alg': 'none'})}."
        f"{encode({'exp': exp or int(time.time()) + 3600, 'https://api.openai.com/auth': {'chatgpt_account_id': account_id}})}."
        "signature"
    )


class FakeStream:
    def __init__(self, events):
        self.events = list(events)
        self.index = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.events):
            raise StopAsyncIteration
        event = self.events[self.index]
        self.index += 1
        return event

    async def close(self):
        self.closed = True


def _terminal(text: str = "ok", event_type: str = "response.done") -> list[dict]:
    return [
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        },
        {
            "type": event_type,
            "response": {
                "id": "resp-1",
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "input_tokens_details": {"cached_tokens": 1},
                },
            },
        },
    ]


def _provider(stream_or_error, custom_extra_body=None):
    token = _access_token()
    provider = codex_source.ProviderCodex.__new__(codex_source.ProviderCodex)
    create = (
        AsyncMock(side_effect=stream_or_error)
        if isinstance(stream_or_error, Exception)
        else AsyncMock(return_value=stream_or_error)
    )
    provider.client = SimpleNamespace(
        api_key=token,
        responses=SimpleNamespace(create=create),
    )
    provider.api_keys = [token]
    provider.chosen_api_key = token
    provider.default_params = {
        "input",
        "model",
        "instructions",
        "include",
        "parallel_tool_calls",
        "prompt_cache_key",
        "store",
        "tools",
        "tool_choice",
    }
    provider.provider_config = {
        "custom_extra_body": custom_extra_body or {},
        "proxy": None,
        "api_base": codex_source.CODEX_DEFAULT_API_BASE,
    }
    provider.provider_settings = {"display_reasoning_text": False}
    provider._maybe_refresh_token = AsyncMock()
    return provider, create


def _payload() -> dict:
    return {
        "input": [{"type": "message", "role": "user", "content": "hello"}],
        "model": "gpt-5.6-sol",
        "instructions": "system",
        "include": ["reasoning.encrypted_content"],
        "parallel_tool_calls": True,
        "prompt_cache_key": "cache-key",
        "store": False,
    }


@pytest.mark.asyncio
async def test_response_done_rebuilds_terminal_output_and_closes_stream() -> None:
    stream = FakeStream(_terminal())
    provider, _ = _provider(stream)

    responses = [
        response async for response in provider._query_stream(_payload(), None)
    ]

    assert responses[-1].completion_text == "ok"
    assert responses[-1].usage.input_cached == 1
    assert stream.closed is True


@pytest.mark.asyncio
async def test_nested_stream_error_preserves_safe_code_and_message() -> None:
    stream = FakeStream(
        [
            {
                "type": "error",
                "error": {"code": "bad_request", "message": "invalid field"},
            }
        ]
    )
    provider, _ = _provider(stream)

    with pytest.raises(RuntimeError, match="bad_request: invalid field"):
        async for _ in provider._query_stream(_payload(), None):
            pass
    assert stream.closed is True


@pytest.mark.asyncio
async def test_incomplete_response_fails_instead_of_saving_partial_output() -> None:
    stream = FakeStream(
        [
            {
                "type": "response.incomplete",
                "response": {
                    "id": "resp-partial",
                    "status": "incomplete",
                    "output": [],
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            }
        ]
    )
    provider, _ = _provider(stream)

    with pytest.raises(RuntimeError, match="max_output_tokens"):
        async for _ in provider._query_stream(_payload(), None):
            pass


@pytest.mark.asyncio
async def test_required_wire_fields_override_custom_extra_body(monkeypatch) -> None:
    stream = FakeStream(_terminal())
    provider, create = _provider(
        stream,
        {
            "stream": False,
            "instructions": "overridden",
            "include": [],
            "service_tier": "priority",
            "reasoning": {"effort": "xhigh"},
        },
    )
    monkeypatch.setattr(
        codex_source,
        "get_codex_settings",
        lambda: {
            "reasoning_effort": "minimal",
            "fast_mode": False,
            "search_mode": "live",
            "search_context_size": "medium",
            "image_quality": "auto",
        },
    )

    async for _ in provider._query_stream(_payload(), None):
        pass

    kwargs = create.await_args.kwargs
    assert kwargs["stream"] is True
    assert kwargs["instructions"] == "system"
    assert kwargs["include"] == ["reasoning.encrypted_content"]
    assert kwargs["extra_body"]["reasoning"]["effort"] == "low"
    assert "service_tier" not in kwargs["extra_body"]
    assert kwargs["extra_headers"]["session-id"] == "cache-key"


@pytest.mark.asyncio
async def test_terminal_quota_is_not_retried() -> None:
    class QuotaError(Exception):
        status_code = 429
        body = {"error": {"code": "usage_limit_reached"}}

    provider, create = _provider(QuotaError("GoUsageLimitError"))

    with pytest.raises(codex_source.CodexUsageLimitError):
        async for _ in provider._query_stream(_payload(), None):
            pass
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_generator_close_closes_underlying_sse() -> None:
    stream = FakeStream([{"type": "response.output_text.delta", "delta": "chunk"}])
    provider, _ = _provider(stream)
    generator = provider._query_stream(_payload(), None)

    first = await anext(generator)
    assert first.is_chunk is True
    await generator.aclose()

    assert stream.closed is True


@pytest.mark.asyncio
async def test_session_id_becomes_opaque_prompt_cache_key(monkeypatch) -> None:
    captured = {}

    async def fake_parent_stream(self, **kwargs):
        captured.update(kwargs)
        if False:
            yield None

    monkeypatch.setattr(ProviderOpenAIResponses, "text_chat_stream", fake_parent_stream)
    provider = codex_source.ProviderCodex.__new__(codex_source.ProviderCodex)

    responses = [
        response
        async for response in provider.text_chat_stream(
            prompt="hello",
            session_id="group:user:secret",
        )
    ]

    assert responses == []
    assert (
        captured["codex_prompt_cache_key"]
        == hashlib.sha256(b"group:user:secret").hexdigest()
    )
    assert "group:user:secret" not in captured["codex_prompt_cache_key"]
