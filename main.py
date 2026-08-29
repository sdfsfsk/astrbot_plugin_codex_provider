"""AstrBot plugin: OpenAI Codex (ChatGPT subscription) provider.

Importing this package registers the ``codex_chat_completion`` provider
adapter, after which the Codex provider can be added from the WebUI provider
page like any built-in provider type.
"""

import httpx
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .codex_source import ProviderCodex, format_codex_usage


@register(
    "astrbot_plugin_codex_provider",
    "Matsuko",
    "OpenAI Codex（ChatGPT 订阅）模型服务提供商：令牌登录、代理支持、订阅额度查询",
    "1.0.0",
    "https://github.com/sdfsfsk/astrbot_plugin_codex_provider",
)
class CodexProviderPlugin(Star):
    """Registers the Codex provider adapter and helper commands."""

    def __init__(self, context: Context):
        super().__init__(context)

    def _get_codex_provider(self) -> ProviderCodex | None:
        """Find the first instantiated Codex provider, if any."""
        for inst in self.context.provider_manager.provider_insts:
            if isinstance(inst, ProviderCodex):
                return inst
        return None

    @filter.command("codex_usage")
    async def codex_usage(self, event: AstrMessageEvent):
        """查询 Codex 订阅额度（走提供商配置的代理）"""
        provider = self._get_codex_provider()
        if provider is None:
            yield event.plain_result(
                "未找到已启用的 Codex 服务提供商。\n"
                "请在 WebUI「服务提供商」中新增「OpenAI Codex 订阅」，"
                "并在 Key 栏粘贴访问令牌（先在 Codex CLI 登录，"
                "再从 ~/.codex/auth.json 复制 access_token；获取过程建议挂代理）。"
            )
            return
        try:
            usage = await provider.fetch_usage()
        except (ValueError, PermissionError, httpx.HTTPError) as e:
            yield event.plain_result(f"查询 Codex 订阅用量失败：{e}")
            return
        yield event.plain_result(
            format_codex_usage(usage, provider.chosen_api_key or "")
        )
