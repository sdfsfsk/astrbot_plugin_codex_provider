"""AstrBot command, tool-schema, policy, and citation integration tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_codex_provider import main as plugin_main

from astrbot.core.provider.register import provider_cls_map


class FakeConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self):
        self.saved += 1


class FakeContext:
    def __init__(self, conf=None, manager=None):
        self.conf = conf or FakeConfig(provider_settings={"web_search": True})
        self.manager = manager
        self.provider_manager = SimpleNamespace(
            provider_insts=[],
            terminate_provider=AsyncMock(),
        )
        self.activate_llm_tool_async = AsyncMock(return_value=True)
        self.deactivate_llm_tool_async = AsyncMock(return_value=True)

    def get_config(self):
        return self.conf

    def get_llm_tool_manager(self):
        return self.manager


class FakeEvent:
    def __init__(self, *, private=False):
        self.private = private
        self.sent = []

    def is_private_chat(self):
        return self.private

    @staticmethod
    def plain_result(text):
        return text

    async def send(self, result):
        self.sent.append(result)


def _plugin(context, config=None):
    plugin = plugin_main.CodexProviderPlugin.__new__(plugin_main.CodexProviderPlugin)
    plugin.context = context
    plugin.config = config or FakeConfig()
    plugin._login_in_progress = False
    plugin._login_task = None
    return plugin


def test_tool_schemas_require_primary_args_and_default_reference_mode() -> None:
    image_tool = SimpleNamespace(
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "use_reference_images": {"type": "boolean"},
            },
        }
    )
    search_tool = SimpleNamespace(
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
        }
    )
    manager = SimpleNamespace(
        get_func=lambda name: {
            "codex_generate_image": image_tool,
            "codex_web_search": search_tool,
        }.get(name)
    )
    plugin = _plugin(FakeContext(manager=manager))

    plugin._harden_tool_schemas()

    assert image_tool.parameters["required"] == ["prompt"]
    assert image_tool.parameters["additionalProperties"] is False
    assert (
        image_tool.parameters["properties"]["use_reference_images"]["default"] is True
    )
    assert search_tool.parameters["required"] == ["query"]


@pytest.mark.asyncio
async def test_oauth_source_autofill_preserves_manual_keys() -> None:
    old_oauth = "oauth-old"
    new_oauth = "oauth-new"
    manual = "manual-token"
    conf = FakeConfig(
        provider_sources=[
            {"id": "empty", "type": "codex_chat_completion", "key": []},
            {"id": "oauth", "type": "codex_chat_completion", "key": [old_oauth]},
            {"id": "manual", "type": "codex_chat_completion", "key": [manual]},
            {
                "id": "mixed",
                "type": "codex_chat_completion",
                "key": [manual, old_oauth],
            },
        ],
        provider=[
            {
                "id": f"model-{source_id}",
                "provider_source_id": source_id,
                "enable": True,
            }
            for source_id in ("empty", "oauth", "manual", "mixed")
        ],
    )
    context = FakeContext()
    context.provider_manager = SimpleNamespace(
        provider_insts=[],
        acm=SimpleNamespace(default_conf=conf),
        reload=AsyncMock(),
        terminate_provider=AsyncMock(),
    )
    plugin = _plugin(context)

    updated = await plugin._reload_oauth_sources(
        {plugin_main.token_fingerprint(old_oauth)},
        oauth_access_token=new_oauth,
    )

    assert updated is True
    sources = {source["id"]: source["key"] for source in conf["provider_sources"]}
    assert sources == {
        "empty": [new_oauth],
        "oauth": [new_oauth],
        "manual": [manual],
        "mixed": [manual],
    }
    reloaded_ids = {
        call.args[0]["id"] for call in context.provider_manager.reload.await_args_list
    }
    assert reloaded_ids == {"model-empty", "model-oauth", "model-mixed"}

    context.provider_manager.reload.reset_mock()
    await plugin._reload_oauth_sources(
        {plugin_main.token_fingerprint(new_oauth)},
    )
    sources = {source["id"]: source["key"] for source in conf["provider_sources"]}
    assert sources == {
        "empty": [],
        "oauth": [],
        "manual": [manual],
        "mixed": [manual],
    }
    reloaded_ids = {
        call.args[0]["id"] for call in context.provider_manager.reload.await_args_list
    }
    assert reloaded_ids == {"model-empty", "model-oauth"}


@pytest.mark.asyncio
async def test_disabled_search_tool_cannot_force_disable_builtin_search(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    conf = FakeConfig(provider_settings={"web_search": True})
    context = FakeContext(conf=conf)
    plugin = _plugin(
        context,
        FakeConfig(enable_search_tool=False, force_codex_web_search=True),
    )

    await plugin._apply_web_search_policy()

    context.deactivate_llm_tool_async.assert_awaited_once_with("codex_web_search")
    assert conf["provider_settings"]["web_search"] is True
    assert not plugin._web_search_marker_path().exists()


@pytest.mark.asyncio
async def test_forced_search_policy_is_restored_on_terminate(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    conf = FakeConfig(provider_settings={"web_search": True})
    context = FakeContext(conf=conf)
    plugin = _plugin(
        context,
        FakeConfig(enable_search_tool=True, force_codex_web_search=True),
    )

    await plugin._apply_web_search_policy()
    assert conf["provider_settings"]["web_search"] is False
    assert plugin._web_search_marker_path().exists()

    await plugin.terminate()

    assert conf["provider_settings"]["web_search"] is True
    assert not plugin._web_search_marker_path().exists()


@pytest.mark.asyncio
async def test_terminate_unregisters_and_stops_codex_provider(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    context = FakeContext()
    instance = plugin_main.ProviderCodex.__new__(plugin_main.ProviderCodex)
    instance.provider_config = {"id": "codex/model"}
    context.provider_manager.provider_insts = [instance]
    plugin = _plugin(context)
    plugin_main._register_codex_provider()

    try:
        await plugin.terminate()
        context.provider_manager.terminate_provider.assert_awaited_once_with(
            "codex/model"
        )
        assert context.provider_manager._codex_plugin_reload_ids == {"codex/model"}
        assert "codex_chat_completion" not in provider_cls_map
    finally:
        plugin_main._register_codex_provider()


@pytest.mark.asyncio
async def test_login_refuses_to_publish_device_code_in_group(monkeypatch) -> None:
    context = FakeContext()
    plugin = _plugin(context)
    start = AsyncMock()
    monkeypatch.setattr(plugin_main, "start_device_auth", start)

    replies = [reply async for reply in plugin.codex_login(FakeEvent(private=False))]

    assert len(replies) == 1
    assert "私聊" in replies[0]
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_citations_are_mapped_to_urls() -> None:
    provider = SimpleNamespace(
        search_web=AsyncMock(
            return_value={
                "content": "答案 citeturn0search7",
                "sources": [
                    {
                        "ref_id": "turn0search7",
                        "url": "https://example.com/source",
                        "title": "Example",
                        "snippet": "",
                    }
                ],
            }
        )
    )
    plugin = _plugin(FakeContext())
    plugin._get_codex_provider = lambda: provider

    result = await plugin.codex_web_search(FakeEvent(), "query")

    assert "[Example](https://example.com/source)" in result
    assert "turn0search7" not in result


def test_generated_images_use_managed_temp_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))

    path = plugin_main.CodexProviderPlugin._save_generated_image(b"png")

    assert path.parent == tmp_path / "data" / "temp" / "astrbot_plugin_codex_provider"
    assert path.read_bytes() == b"png"
