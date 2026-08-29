"""AstrBot plugin: OpenAI Codex (ChatGPT subscription) provider.

Importing this package registers the ``codex_chat_completion`` provider
adapter, after which the Codex provider can be added from the WebUI provider
page like any built-in provider type.
"""

from datetime import datetime, timezone

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig

from .codex_auth import (
    CODEX_DEVICE_VERIFICATION_URL,
    poll_device_auth,
    save_auth_store,
    start_device_auth,
    tokens_to_store,
)
from .codex_source import (
    CODEX_DEFAULT_PROXY,
    CODEX_REASONING_EFFORTS,
    ProviderCodex,
    codex_token_expiry,
    format_codex_usage,
    get_codex_settings,
    update_codex_settings,
)


@register(
    "astrbot_plugin_codex_provider",
    "Matsuko",
    "OpenAI Codex（ChatGPT 订阅）模型服务提供商：令牌登录、代理支持、订阅额度查询",
    "1.2.0",
    "https://github.com/sdfsfsk/astrbot_plugin_codex_provider",
)
class CodexProviderPlugin(Star):
    """Registers the Codex provider adapter and helper commands."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._login_in_progress = False
        update_codex_settings(config)

    async def initialize(self):
        """Re-instantiate Codex providers left over from a hot reload.

        AstrBot re-executes plugin modules on hot reload but keeps running
        provider instances on the previously registered adapter class, so
        plugin code changes would not reach live providers until restart.
        Reload the model entries bound to Codex sources so they pick up
        this module's class immediately. At startup this is a no-op because
        providers are instantiated after plugins load.
        """
        provider_manager = self.context.provider_manager
        if not provider_manager.provider_insts:
            return
        stale_instances = [
            inst
            for inst in provider_manager.provider_insts
            if inst.provider_config.get("type") == "codex_chat_completion"
            and not isinstance(inst, ProviderCodex)
        ]
        if not stale_instances:
            return
        model_entries = {
            p.get("id"): p
            for p in provider_manager.acm.default_conf.get("provider", [])
            if isinstance(p, dict)
        }
        for inst in stale_instances:
            entry = model_entries.get(inst.provider_config.get("id"))
            if entry is None:
                continue
            logger.info(
                "[Codex] 插件热重载后重建提供商实例: %s",
                inst.provider_config.get("id"),
            )
            await provider_manager.reload(entry)

    def _get_codex_provider(self) -> ProviderCodex | None:
        """Find the first instantiated Codex provider, if any."""
        for inst in self.context.provider_manager.provider_insts:
            if isinstance(inst, ProviderCodex):
                return inst
        return None

    async def _inject_token(self, token: str) -> bool:
        """Write the access token into the Codex provider source and reload.

        Args:
            token: The fresh Codex access token.

        Returns:
            True when a Codex provider source was found and updated.
        """
        provider_manager = self.context.provider_manager
        conf = provider_manager.acm.default_conf
        source = next(
            (
                s
                for s in conf.get("provider_sources", [])
                if isinstance(s, dict) and s.get("type") == "codex_chat_completion"
            ),
            None,
        )
        if source is None:
            return False
        source["key"] = [token]
        conf.save_config()
        for entry in conf.get("provider", []):
            if isinstance(entry, dict) and entry.get(
                "provider_source_id"
            ) == source.get("id"):
                await provider_manager.reload(entry)
        return True

    @filter.command("codex_login")
    async def codex_login(self, event: AstrMessageEvent):
        """插件内置 Codex OAuth 设备码登录（无需 Codex CLI，登录成功自动注入提供商）"""
        if self._login_in_progress:
            yield event.plain_result(
                "已有 Codex 登录流程进行中，请先完成授权或等待超时（15 分钟）。"
            )
            return
        self._login_in_progress = True
        try:
            provider = self._get_codex_provider()
            proxy = (
                (provider.provider_config.get("proxy") or None)
                if provider is not None
                else CODEX_DEFAULT_PROXY
            )
            try:
                device = await start_device_auth(proxy)
            except (RuntimeError, httpx.HTTPError) as e:
                yield event.plain_result(f"❌ 登录失败：{e}")
                return
            yield event.plain_result(
                "🐾 Codex 令牌登录（设备码模式）\n"
                f"请在浏览器打开（建议挂代理）：{CODEX_DEVICE_VERIFICATION_URL}\n"
                f"然后输入设备码：【{device['user_code']}】\n\n"
                "15 分钟内有效，成功后自动注入 Codex 提供商 Key～"
            )
            try:
                tokens = await poll_device_auth(device, proxy)
            except (TimeoutError, RuntimeError, httpx.HTTPError) as e:
                yield event.plain_result(f"❌ 登录失败：{e}")
                return

            token = tokens.get("access_token", "")
            save_auth_store(tokens_to_store(tokens))
            exp = codex_token_expiry(token)
            expire_text = (
                datetime.fromtimestamp(exp, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M")
                if exp
                else "未知"
            )
            injected = await self._inject_token(token)
            if injected:
                yield event.plain_result(
                    f"✅ Codex 登录成功！令牌有效期至 {expire_text}\n"
                    "已自动注入 Codex 提供商 Key 并重建实例；"
                    "刷新令牌已保存，到期自动续期，无需再登录～"
                )
            else:
                yield event.plain_result(
                    f"✅ Codex 登录成功！令牌有效期至 {expire_text}\n"
                    "⚠️ 但未找到 Codex 提供商。请在 WebUI 新增「OpenAI Codex 订阅」"
                    "提供商（Key 可留空，将自动使用已保存的登录令牌），"
                    "或手动把令牌粘贴到 Key 栏。"
                )
        finally:
            self._login_in_progress = False

    @filter.command("codex_usage")
    async def codex_usage(self, event: AstrMessageEvent):
        """查询 Codex 订阅额度（走提供商配置的代理）"""
        provider = self._get_codex_provider()
        if provider is None:
            yield event.plain_result(
                "未找到已启用的 Codex 服务提供商。\n"
                "请在 WebUI「服务提供商」中新增「OpenAI Codex 订阅」，"
                "或直接发送 /codex_login 登录后按引导创建。"
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
