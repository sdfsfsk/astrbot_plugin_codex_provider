"""AstrBot plugin: OpenAI Codex (ChatGPT subscription) provider.

Importing this package registers the ``codex_chat_completion`` provider
adapter, after which the Codex provider can be added from the WebUI provider
page like any built-in provider type.
"""

import httpx
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig

from .codex_source import (
    CODEX_REASONING_EFFORTS,
    ProviderCodex,
    format_codex_usage,
    get_codex_settings,
    update_codex_settings,
)


@register(
    "astrbot_plugin_codex_provider",
    "Matsuko",
    "OpenAI Codex（ChatGPT 订阅）模型服务提供商：令牌登录、代理支持、订阅额度查询",
    "1.1.3",
    "https://github.com/sdfsfsk/astrbot_plugin_codex_provider",
)
class CodexProviderPlugin(Star):
    """Registers the Codex provider adapter and helper commands."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        update_codex_settings(config)

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

    @filter.command("codex_reasoning")
    async def codex_reasoning(self, event: AstrMessageEvent, level: str = ""):
        """查看或设置 Codex 推理深度（minimal/low/medium/high/xhigh）"""
        level = (level or "").strip().lower()
        if not level:
            current = get_codex_settings()["reasoning_effort"]
            yield event.plain_result(
                f"当前 Codex 推理深度：{current}\n"
                f"可选值：{' / '.join(CODEX_REASONING_EFFORTS)}\n"
                "用法：/codex_reasoning <级别>\n"
                "（也可在插件配置页修改，级别越高越聪明但越慢、额度消耗越大）"
            )
            return
        if level not in CODEX_REASONING_EFFORTS:
            yield event.plain_result(
                f"无效的推理深度：{level}\n可选值：{' / '.join(CODEX_REASONING_EFFORTS)}"
            )
            return
        self.config["reasoning_effort"] = level
        self.config.save_config()
        update_codex_settings(self.config)
        yield event.plain_result(f"✅ Codex 推理深度已设为：{level}")

    @filter.command("codex_fast")
    async def codex_fast(self, event: AstrMessageEvent, arg: str = ""):
        """查看或开关 Codex 1.5 倍速模式（priority 服务层级）"""
        arg = (arg or "").strip().lower()
        if not arg:
            state = "开" if get_codex_settings()["fast_mode"] else "关"
            yield event.plain_result(
                f"当前 Codex 1.5 倍速：{state}\n"
                "用法：/codex_fast on 或 /codex_fast off\n"
                "（开启后响应更快，但订阅额度消耗也更快；也可在插件配置页修改）"
            )
            return
        if arg not in ("on", "off"):
            yield event.plain_result("用法：/codex_fast on 或 /codex_fast off")
            return
        enabled = arg == "on"
        self.config["fast_mode"] = enabled
        self.config.save_config()
        update_codex_settings(self.config)
        yield event.plain_result(
            "✅ Codex 1.5 倍速已开启，响应更快但额度消耗更快"
            if enabled
            else "✅ Codex 1.5 倍速已关闭"
        )
