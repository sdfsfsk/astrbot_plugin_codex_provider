"""Exercise image discovery, fixed selection, persistence, and request routing."""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from astrbot_plugin_codex_provider import codex_source, image_models
from astrbot_plugin_codex_provider import main as plugin_main

CATALOG = """
<a href="/api/docs/models/gpt-image-2.5-sunburst">Sunburst</a>
<a href="/api/docs/models/gpt-image-2.5-flare">Flare</a>
"""
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


@pytest.fixture
def settings(monkeypatch):
    current = {**codex_source.get_codex_settings(), "image_model": "auto"}
    monkeypatch.setattr(codex_source, "_PLUGIN_SETTINGS", current)
    return current


@pytest.fixture
def transport(monkeypatch):
    """Keep requests on a test transport while inspecting client arguments."""
    original = httpx.AsyncClient
    calls = []

    def install(handler):
        def factory(**kwargs):
            calls.append(kwargs)
            return original(
                transport=httpx.MockTransport(handler),
                follow_redirects=kwargs.get("follow_redirects", False),
            )

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return calls

    return install


def test_catalog_sorts_numeric_versions_and_preserves_official_ties():
    html = (
        CATALOG
        + """
    <a href='/api/docs/models/gpt-image-2.10-flare/'>Future version</a>
    <a href='/api/docs/models/gpt-image-2'>Previous version</a>
    <a href='/api/docs/models/gpt-image-2.5-flare'>Duplicate</a>
    <a href='/api/docs/models/gpt-image-2.5-flare-2026-09-08'>Snapshot</a>
    <a href='/api/docs/models/gpt-6-astra'>Chat model</a>
    <a href='https://other.example/api/docs/models/gpt-image-99'>External link</a>
    Text mentioning gpt-image-100 must not become a candidate.
    """
    )
    assert image_models.parse_image_model_catalog(html) == [
        "gpt-image-2.10-flare",
        "gpt-image-2.5-sunburst",
        "gpt-image-2.5-flare",
        "gpt-image-2",
    ]


@pytest.mark.parametrize(
    "value", ["gpt-6-astra", "https://other.example", "gpt-image-2 hi", None]
)
def test_bad_model_setting_is_rejected(value):
    with pytest.raises(ValueError):
        image_models.normalize_image_model(value)


@pytest.mark.asyncio
async def test_discovery_coalesces_requests_and_refreshes_after_ttl(
    monkeypatch, transport
):
    requests = []
    clock = [100.0]
    monkeypatch.setattr(image_models.time, "monotonic", lambda: clock[0])

    async def handler(request):
        requests.append(request)
        await asyncio.sleep(0)
        return httpx.Response(200, text=CATALOG)

    clients = transport(handler)
    discovery = image_models.ImageModelDiscovery()
    results = await asyncio.gather(
        *(discovery.get_catalog("http://proxy.test") for _ in range(5))
    )
    assert len(requests) == 1
    assert all(result["models"][0] == "gpt-image-2.5-sunburst" for result in results)
    assert clients[0]["proxy"] == "http://proxy.test"
    assert clients[0]["follow_redirects"] is False
    assert str(requests[0].url) == image_models.IMAGE_MODEL_CATALOG_URL
    assert "authorization" not in requests[0].headers
    assert "chatgpt-account-id" not in requests[0].headers
    clock[0] += image_models.IMAGE_MODEL_CACHE_SECONDS + 1
    await discovery.get_catalog()
    assert len(requests) == 2
    await discovery.get_catalog(force_refresh=True)
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_failed_refresh_keeps_stale_models_then_recovers(monkeypatch, transport):
    clock = [100.0]
    monkeypatch.setattr(image_models.time, "monotonic", lambda: clock[0])
    responses = iter(
        [
            httpx.Response(200, text=CATALOG),
            httpx.Response(503, text="sensitive upstream body"),
            httpx.Response(200, text='<a href="/api/docs/models/gpt-image-3">New</a>'),
        ]
    )
    transport(lambda _: next(responses))
    discovery = image_models.ImageModelDiscovery()
    await discovery.get_catalog()
    stale = await discovery.get_catalog(force_refresh=True)
    assert stale["source"] == "stale"
    assert stale["models"][0] == "gpt-image-2.5-sunburst"
    assert "HTTP 503" in stale["warning"]
    assert "sensitive" not in stale["warning"]
    assert await discovery.get_catalog() == stale
    clock[0] += image_models.IMAGE_MODEL_RETRY_SECONDS + 1
    recovered = await discovery.get_catalog()
    assert recovered["source"] == "official"
    assert recovered["models"] == ["gpt-image-3"]
    assert recovered["warning"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="Changed HTML without model links"),
        httpx.Response(302, headers={"location": "https://other.example"}),
        httpx.Response(
            200, content=b"x" * (image_models.IMAGE_MODEL_CATALOG_MAX_BYTES + 1)
        ),
    ],
)
async def test_discovery_failure_reports_builtin_source(transport, response):
    transport(lambda _: response)
    discovery = image_models.ImageModelDiscovery()
    result = await discovery.get_catalog()
    assert result["source"] == "builtin"
    assert result["models"] == list(image_models.FALLBACK_IMAGE_MODELS)
    assert result["warning"]


@pytest.mark.asyncio
async def test_manual_model_never_uses_discovery(monkeypatch):
    catalog = AsyncMock(side_effect=AssertionError("manual selection used discovery"))
    monkeypatch.setattr(image_models.image_model_discovery, "get_catalog", catalog)
    assert (
        await image_models.resolve_image_model(" GPT-IMAGE-3-CUSTOM ")
        == "gpt-image-3-custom"
    )
    catalog.assert_not_awaited()


class _Config(dict):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.save_config = Mock()


@pytest.mark.asyncio
async def test_command_persists_manual_selection_without_network(monkeypatch, settings):
    plugin = plugin_main.CodexProviderPlugin.__new__(plugin_main.CodexProviderPlugin)
    plugin.config = _Config(image_quality="high")
    catalog = AsyncMock(side_effect=AssertionError("manual selection used discovery"))
    monkeypatch.setattr(plugin_main.image_model_discovery, "get_catalog", catalog)
    event = SimpleNamespace(plain_result=lambda text: text)
    output = [
        part async for part in plugin.codex_image_model(event, "gpt-image-2.5-flare")
    ]
    assert plugin.config == {
        "image_model": "gpt-image-2.5-flare",
        "image_quality": "high",
    }
    plugin.config.save_config.assert_called_once()
    assert settings["image_model"] == "gpt-image-2.5-flare"
    assert "已保存" in output[0]
    catalog.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "selection", "source_text"),
    [
        ("official", "auto", "OpenAI 官方在线目录"),
        ("stale", "gpt-image-2", "上次成功获取的目录"),
        ("builtin", "gpt-image-2", "内置备用目录"),
    ],
)
async def test_command_refresh_reports_outcome_and_preserves_selection(
    monkeypatch, settings, source, selection, source_text
):
    settings["image_model"] = selection
    plugin = plugin_main.CodexProviderPlugin.__new__(plugin_main.CodexProviderPlugin)
    plugin.config = _Config(image_model=selection)
    plugin._get_codex_provider = lambda: SimpleNamespace(
        provider_config={"proxy": "http://proxy.test"}
    )
    catalog = AsyncMock(
        return_value={
            "models": ["gpt-image-3"],
            "source": source,
            "warning": "" if source == "official" else "在线图片目录获取失败",
        }
    )
    monkeypatch.setattr(plugin_main.image_model_discovery, "get_catalog", catalog)
    event = SimpleNamespace(plain_result=lambda text: text)
    output = [part async for part in plugin.codex_image_model(event, "refresh")]
    catalog.assert_awaited_once_with("http://proxy.test", force_refresh=True)
    plugin.config.save_config.assert_not_called()
    assert settings["image_model"] == selection
    assert source_text in output[0]
    assert "自动模式当前选择：gpt-image-3" in output[0]
    if source == "official":
        assert output[0].startswith("✅ 图片生成模型刷新成功")
        assert "在线获取 1 个模型" in output[0]
        assert "刷新失败" not in output[0]
    else:
        assert output[0].startswith("⚠️ 图片生成模型刷新失败")
        assert "刷新成功" not in output[0]


@pytest.mark.asyncio
async def test_command_returns_to_auto_and_preserves_other_settings(
    monkeypatch, settings
):
    settings["image_model"] = "gpt-image-2"
    plugin = plugin_main.CodexProviderPlugin.__new__(plugin_main.CodexProviderPlugin)
    plugin.config = _Config(image_model="gpt-image-2", image_quality="high")
    plugin._get_codex_provider = lambda: None
    monkeypatch.setattr(
        plugin_main.image_model_discovery,
        "get_catalog",
        AsyncMock(
            return_value={
                "models": ["gpt-image-2.5-sunburst"],
                "source": "official",
                "warning": "",
            }
        ),
    )
    output = [
        part
        async for part in plugin.codex_image_model(
            SimpleNamespace(plain_result=str), "auto"
        )
    ]
    assert plugin.config == {"image_model": "auto", "image_quality": "high"}
    assert settings["image_model"] == "auto"
    assert "gpt-image-2.5-sunburst" in output[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, "gpt-image-2"])
async def test_command_save_failure_rolls_back(previous, settings):
    plugin = plugin_main.CodexProviderPlugin.__new__(plugin_main.CodexProviderPlugin)
    plugin.config = _Config(**({"image_model": previous} if previous else {}))
    plugin.config.save_config.side_effect = OSError("not writable")
    original = dict(settings)
    output = [
        part
        async for part in plugin.codex_image_model(
            SimpleNamespace(plain_result=str), "gpt-image-2.5-flare"
        )
    ]
    assert plugin.config.get("image_model") == previous
    assert settings == original
    assert "保存失败" in output[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "references", [[], ["data:image/png;base64," + base64.b64encode(PNG_1X1).decode()]]
)
async def test_auto_model_reaches_generation_and_edit_requests(
    monkeypatch, settings, transport, references
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(PNG_1X1).decode()}]}
        )

    transport(handler)
    catalog = AsyncMock(
        return_value={
            "models": ["gpt-image-3-future"],
            "source": "official",
            "warning": "",
        }
    )
    monkeypatch.setattr(image_models.image_model_discovery, "get_catalog", catalog)
    token = (
        "header."
        + base64.urlsafe_b64encode(
            json.dumps(
                {"https://api.openai.com/auth": {"chatgpt_account_id": "test-account"}}
            ).encode()
        )
        .decode()
        .rstrip("=")
        + ".signature"
    )
    provider = SimpleNamespace(
        _maybe_refresh_token=AsyncMock(),
        _active_token=lambda: token,
        provider_config={},
    )
    result = await codex_source.ProviderCodex.generate_image(
        provider, "Draw a blue circle", references
    )
    assert result == PNG_1X1
    assert len(requests) == 1
    assert json.loads(requests[0].content)["model"] == "gpt-image-3-future"
    assert requests[0].url.path.endswith(
        "/images/edits" if references else "/images/generations"
    )


@pytest.mark.asyncio
async def test_generation_command_shows_and_sends_same_resolved_model(
    monkeypatch, settings
):
    plugin = plugin_main.CodexProviderPlugin.__new__(plugin_main.CodexProviderPlugin)
    provider = SimpleNamespace(
        provider_config={}, generate_image=AsyncMock(return_value=PNG_1X1)
    )
    plugin._get_codex_provider = lambda: provider
    plugin._collect_message_images = AsyncMock(return_value=[])
    plugin._save_generated_image = lambda _: "generated.png"
    monkeypatch.setattr(
        plugin_main, "resolve_image_model", AsyncMock(return_value="gpt-image-3-future")
    )
    event = SimpleNamespace(
        plain_result=lambda text: text, image_result=lambda path: path
    )
    output = [part async for part in plugin.codex_image(event, "Draw a blue circle")]
    assert "gpt-image-3-future" in output[0]
    provider.generate_image.assert_awaited_once_with(
        "Draw a blue circle", [], model="gpt-image-3-future"
    )
