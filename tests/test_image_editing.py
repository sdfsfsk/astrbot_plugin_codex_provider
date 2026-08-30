"""Regression tests for Codex image generation and reference-image editing."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.message.components import Image, Reply
from astrbot_plugin_codex_provider import codex_source
from astrbot_plugin_codex_provider import main as plugin_main

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def _access_token(account_id: str = "image-account") -> str:
    """Build a non-secret JWT-shaped token for request tests."""

    def encode(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return (
        f"{encode({'alg': 'none'})}."
        f"{encode({'https://api.openai.com/auth': {'chatgpt_account_id': account_id}})}."
        "signature"
    )


class _FakeImageResponse:
    status_code = 200
    text = ""

    @staticmethod
    def json() -> dict:
        return {"data": [{"b64_json": base64.b64encode(PNG_1X1).decode()}]}


class _FakeEvent:
    def __init__(self, messages=None):
        self._messages = messages or []
        self.sent = []

    def get_messages(self):
        return self._messages

    @staticmethod
    def plain_result(text: str):
        return ("text", text)

    @staticmethod
    def image_result(path: str):
        return ("image", path)

    async def send(self, result) -> None:
        self.sent.append(result)


@pytest.mark.asyncio
async def test_llm_tool_forwards_current_message_images(monkeypatch) -> None:
    """The LLM tool must edit instead of silently falling back to generation."""
    plugin = plugin_main.CodexProviderPlugin.__new__(plugin_main.CodexProviderPlugin)
    provider = SimpleNamespace(generate_image=AsyncMock(return_value=PNG_1X1))
    plugin._get_codex_provider = lambda: provider
    plugin._collect_message_images = AsyncMock(
        return_value=["data:image/png;base64,reference"]
    )
    monkeypatch.setattr(
        plugin_main.CodexProviderPlugin,
        "_save_generated_image",
        staticmethod(lambda _data: Path("generated.png")),
    )
    event = _FakeEvent()

    result = await plugin.codex_generate_image(event, "Remove the text on the right")

    provider.generate_image.assert_awaited_once_with(
        "Remove the text on the right",
        ["data:image/png;base64,reference"],
    )
    assert "已编辑" in result
    assert "图片编辑中" in event.sent[0][1]
    assert event.sent[1] == ("image", "generated.png")


@pytest.mark.asyncio
async def test_collect_images_tries_past_bad_refs_and_deduplicates(monkeypatch) -> None:
    """Bad early references must not consume the five valid-image slots."""
    calls = []

    async def resolve(ref, *, media_type, strict):
        calls.append((ref, media_type, strict))
        if ref == "bad-ref":
            raise ValueError("bad image")
        return SimpleNamespace(to_data_url=lambda: "data:image/png;base64,good")

    monkeypatch.setattr(
        plugin_main,
        "resolve_media_ref_to_base64_data",
        resolve,
    )
    event = _FakeEvent(
        [
            Image(file="bad-ref"),
            Reply(id="quoted-message", chain=[Image(file="good-ref")]),
            Image(file="good-ref"),
        ]
    )

    images = await plugin_main.CodexProviderPlugin._collect_message_images(event)

    assert images == ["data:image/png;base64,good"]
    assert calls == [
        ("bad-ref", "image", True),
        ("good-ref", "image", True),
    ]


@pytest.mark.asyncio
async def test_collect_images_fails_closed_when_all_refs_are_unreadable(
    monkeypatch,
) -> None:
    """An unreadable edit input must never be downgraded to text-to-image."""
    monkeypatch.setattr(
        plugin_main,
        "resolve_media_ref_to_base64_data",
        AsyncMock(side_effect=ValueError("download failed")),
    )
    event = _FakeEvent([Image(file="bad-ref")])

    with pytest.raises(ValueError, match="已取消改图"):
        await plugin_main.CodexProviderPlugin._collect_message_images(event)


@pytest.mark.asyncio
async def test_provider_uses_edit_endpoint_and_preservation_prompt(monkeypatch) -> None:
    """Reference images must be sent to the Codex JSON edit endpoint."""
    captured = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, *, json, headers):
            captured["url"] = url
            captured["body"] = json
            captured["headers"] = headers
            return _FakeImageResponse()

    monkeypatch.setattr(codex_source.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        codex_source,
        "get_codex_settings",
        lambda: {"image_quality": "auto"},
    )
    provider = SimpleNamespace(
        _maybe_refresh_token=AsyncMock(),
        chosen_api_key=_access_token(),
        provider_config={
            "api_base": "https://chatgpt.com/backend-api/codex",
            "proxy": None,
        },
    )
    reference = "data:image/png;base64," + base64.b64encode(PNG_1X1).decode()

    result = await codex_source.ProviderCodex.generate_image(
        provider,
        "Remove the text on the right",
        [reference],
    )

    assert result == PNG_1X1
    assert captured["url"].endswith("/images/edits")
    assert captured["body"]["images"] == [{"image_url": reference}]
    assert "exact source canvas" in captured["body"]["prompt"]
    assert captured["body"]["prompt"].endswith("Remove the text on the right")


@pytest.mark.asyncio
async def test_provider_keeps_generation_prompt_unchanged(monkeypatch) -> None:
    """Pure generation must not receive image-edit preservation instructions."""
    captured = {}

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, *, json, headers):
            captured["url"] = url
            captured["body"] = json
            return _FakeImageResponse()

    monkeypatch.setattr(codex_source.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        codex_source,
        "get_codex_settings",
        lambda: {"image_quality": "auto"},
    )
    provider = SimpleNamespace(
        _maybe_refresh_token=AsyncMock(),
        chosen_api_key=_access_token(),
        provider_config={
            "api_base": "https://chatgpt.com/backend-api/codex",
            "proxy": None,
        },
    )

    await codex_source.ProviderCodex.generate_image(
        provider,
        "Draw a blue cat",
    )

    assert captured["url"].endswith("/images/generations")
    assert captured["body"]["prompt"] == "Draw a blue cat"
    assert "images" not in captured["body"]
