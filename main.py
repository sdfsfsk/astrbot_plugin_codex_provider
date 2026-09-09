"""AstrBot plugin: OpenAI Codex (ChatGPT subscription) provider.

Importing this package registers the ``codex_chat_completion`` provider
adapter, after which the Codex provider can be added from the WebUI provider
page like any built-in provider type.
"""

import asyncio
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image, Reply
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)
from astrbot.core.utils.media_utils import resolve_media_ref_to_base64_data

from .codex_auth import (
    CODEX_DEVICE_VERIFICATION_URL,
    clear_auth_store,
    load_auth_store,
    poll_device_auth,
    save_auth_store,
    start_device_auth,
    token_fingerprint,
    tokens_to_store,
)
from .codex_source import (
    CODEX_DEFAULT_PROXY,
    CODEX_REASONING_EFFORTS,
    ProviderCodex,
    _register_codex_provider,
    _unregister_codex_provider,
    codex_token_expiry,
    format_codex_usage,
    get_codex_settings,
    update_codex_settings,
)
from .image_models import (
    image_model_discovery,
    normalize_image_model,
    resolve_image_model,
)


@register(
    "astrbot_plugin_codex_provider",
    "Matsuko",
    "OpenAI Codex（ChatGPT 订阅）模型服务提供商：令牌登录、代理支持、订阅额度查询",
    "1.6.0",
    "https://github.com/sdfsfsk/astrbot_plugin_codex_provider",
)
class CodexProviderPlugin(Star):
    """Registers the Codex provider adapter and helper commands."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._login_in_progress = False
        self._login_task: asyncio.Task | None = None
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
        _register_codex_provider()
        provider_manager = self.context.provider_manager
        pending_reload_ids = set(
            getattr(provider_manager, "_codex_plugin_reload_ids", set())
        )
        if hasattr(provider_manager, "_codex_plugin_reload_ids"):
            delattr(provider_manager, "_codex_plugin_reload_ids")
        try:
            store = load_auth_store()
        except (RuntimeError, ValueError, OSError) as e:
            logger.warning("[Codex] 无法读取 OAuth 凭据用于 Key 自动填充: %s", e)
            store = {}
        if store:
            await self._reload_oauth_sources(
                set(store.get("managed_token_hashes", [])),
                oauth_access_token=store.get("access_token"),
                reload_models=False,
            )
        if provider_manager.provider_insts:
            stale_instances = [
                inst
                for inst in provider_manager.provider_insts
                if inst.provider_config.get("type") == "codex_chat_completion"
                and not isinstance(inst, ProviderCodex)
            ]
            if stale_instances:
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
        if pending_reload_ids:
            for entry in provider_manager.acm.default_conf.get("provider", []):
                if (
                    isinstance(entry, dict)
                    and entry.get("id") in pending_reload_ids
                    and entry.get("enable", False)
                ):
                    logger.info(
                        "[Codex] 插件重新启用后恢复提供商实例: %s",
                        entry.get("id"),
                    )
                    await provider_manager.reload(entry)
        self._harden_tool_schemas()
        await self._apply_web_search_policy()

    def _harden_tool_schemas(self) -> None:
        """Apply required/default constraints missing from AstrBot's decorator."""
        manager = self.context.get_llm_tool_manager()
        specifications = {
            "codex_generate_image": ("prompt", {"use_reference_images": True}),
            "codex_web_search": ("query", {}),
        }
        for name, (required, defaults) in specifications.items():
            tool = manager.get_func(name)
            if tool is None or not isinstance(tool.parameters, dict):
                continue
            tool.parameters["required"] = [required]
            tool.parameters["additionalProperties"] = False
            properties = tool.parameters.get("properties")
            if not isinstance(properties, dict):
                continue
            for property_name, default in defaults.items():
                prop = properties.get(property_name)
                if isinstance(prop, dict):
                    prop["default"] = default

    def _web_search_marker_path(self) -> Path:
        """Return the policy marker path and migrate its legacy location."""
        data_dir = (
            Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_codex_provider"
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        marker = data_dir / "websearch_force.marker"
        legacy = (
            Path(get_astrbot_data_path())
            / "astrbot_plugin_codex_provider"
            / "websearch_force.marker"
        )
        if legacy.is_file() and not marker.exists():
            legacy.replace(marker)
        return marker

    async def _restore_web_search_policy(self) -> None:
        """Restore AstrBot's built-in search when this plugin disabled it."""
        marker = self._web_search_marker_path()
        if not marker.exists():
            return
        conf = self.context.get_config()
        prov_settings = conf.get("provider_settings", {})
        prov_settings["web_search"] = True
        conf.save_config()
        marker.unlink(missing_ok=True)
        logger.info("[Codex] 已恢复 AstrBot 自带联网搜索开关")

    async def _apply_web_search_policy(self) -> None:
        """Apply the search-tool toggle and the force-Codex-search switch.

        When ``force_codex_web_search`` is on, AstrBot's built-in web search
        (``provider_settings.web_search``) is disabled so the Codex search
        tool becomes the only search path; turning the toggle off restores
        the previous value, tracked by a marker file.
        """
        search_tool_enabled = bool(self.config.get("enable_search_tool", True))
        if search_tool_enabled:
            await self.context.activate_llm_tool_async("codex_web_search")
        else:
            await self.context.deactivate_llm_tool_async("codex_web_search")

        force_search = search_tool_enabled and bool(
            self.config.get("force_codex_web_search", False)
        )
        if not force_search:
            await self._restore_web_search_policy()
            return

        marker = self._web_search_marker_path()
        conf = self.context.get_config()
        prov_settings = conf.get("provider_settings", {})
        if prov_settings.get("web_search", False):
            temp_marker = marker.with_suffix(".tmp")
            temp_marker.write_text("restore=true\n", encoding="utf-8")
            temp_marker.replace(marker)
            prov_settings["web_search"] = False
            conf.save_config()
            logger.info("[Codex] 已按插件配置禁用 AstrBot 自带联网搜索")

    async def terminate(self) -> None:
        """Cancel login polling and restore global search policy on unload."""
        login_task = self._login_task
        if login_task and not login_task.done():
            login_task.cancel()
            if login_task is not asyncio.current_task():
                await asyncio.gather(login_task, return_exceptions=True)
        await self._restore_web_search_policy()
        provider_manager = self.context.provider_manager
        provider_ids = [
            inst.provider_config.get("id")
            for inst in list(provider_manager.provider_insts)
            if isinstance(inst, ProviderCodex) and inst.provider_config.get("id")
        ]
        if provider_ids:
            pending = set(getattr(provider_manager, "_codex_plugin_reload_ids", set()))
            pending.update(provider_ids)
            provider_manager._codex_plugin_reload_ids = pending
        for provider_id in provider_ids:
            await provider_manager.terminate_provider(provider_id)
        _unregister_codex_provider()

    def _get_codex_provider(self) -> ProviderCodex | None:
        """Find the first instantiated Codex provider, if any."""
        for inst in self.context.provider_manager.provider_insts:
            if isinstance(inst, ProviderCodex):
                return inst
        return None

    async def _reload_oauth_sources(
        self,
        oauth_token_hashes: set[str],
        *,
        oauth_access_token: str | None = None,
        reload_models: bool = True,
    ) -> bool:
        """Remove only OAuth token copies while preserving manual account keys.

        Args:
            oauth_token_hashes: Non-secret fingerprints of access-token copies
                previously managed by the plugin and safe to remove.
            oauth_access_token: Newly logged-in short-lived access token copied
                only into empty/OAuth-managed sources for WebUI compatibility;
                manual account keys and the refresh token remain untouched.
            reload_models: Reload affected model entries immediately. Startup
                migration disables this because core loads providers afterward.

        Returns:
            True when at least one Codex provider source exists.
        """
        provider_manager = self.context.provider_manager
        conf = provider_manager.acm.default_conf
        sources = [
            source
            for source in conf.get("provider_sources", [])
            if isinstance(source, dict)
            and source.get("type") == "codex_chat_completion"
        ]
        if not sources:
            return False
        changed = False
        reload_source_ids: set[str] = set()
        for source in sources:
            raw_keys = source.get("key", [])
            if isinstance(raw_keys, str):
                keys = [raw_keys] if raw_keys else []
            elif isinstance(raw_keys, list):
                keys = [key for key in raw_keys if isinstance(key, str) and key]
            else:
                keys = []
            filtered = [
                key for key in keys if token_fingerprint(key) not in oauth_token_hashes
            ]
            configured = (
                [oauth_access_token]
                if oauth_access_token and not filtered
                else filtered
            )
            source_id = source.get("id")
            if configured != keys:
                source["key"] = configured
                changed = True
                if isinstance(source_id, str):
                    reload_source_ids.add(source_id)
            if not filtered and isinstance(source_id, str):
                reload_source_ids.add(source_id)
        if changed:
            conf.save_config()
        if reload_models:
            for entry in conf.get("provider", []):
                if (
                    isinstance(entry, dict)
                    and entry.get("provider_source_id") in reload_source_ids
                ):
                    await provider_manager.reload(entry)
        return True

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("codex_login")
    async def codex_login(self, event: AstrMessageEvent):
        """Run the private administrator-only Codex device login flow."""
        if not event.is_private_chat():
            yield event.plain_result(
                "为防止设备码被他人抢先绑定，请管理员私聊机器人发送 /codex_login。"
            )
            return
        if self._login_in_progress:
            yield event.plain_result(
                "已有 Codex 登录流程进行中，请先完成授权或等待超时（15 分钟）。"
            )
            return
        self._login_in_progress = True
        self._login_task = asyncio.current_task()
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
                "15 分钟内有效，成功后会保存到受保护的 OAuth 凭据库～"
            )
            try:
                tokens = await poll_device_auth(device, proxy)
            except (TimeoutError, RuntimeError, httpx.HTTPError) as e:
                yield event.plain_result(f"❌ 登录失败：{e}")
                return

            try:
                previous_store = load_auth_store()
            except (RuntimeError, ValueError, OSError):
                previous_store = {}
            previous_access = previous_store.get("access_token", "")
            managed_hashes = set(previous_store.get("managed_token_hashes", []))
            if previous_access:
                managed_hashes.add(token_fingerprint(previous_access))
            try:
                store = tokens_to_store(
                    tokens,
                    managed_token_hashes=managed_hashes,
                )
                save_auth_store(store)
            except (RuntimeError, ValueError, OSError) as e:
                yield event.plain_result(f"❌ 登录凭据保存失败：{e}")
                return
            token = store["access_token"]
            exp = codex_token_expiry(token)
            expire_text = (
                datetime.fromtimestamp(exp, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M")
                if exp
                else "未知"
            )
            activated = await self._reload_oauth_sources(
                set(store["managed_token_hashes"]),
                oauth_access_token=token,
            )
            if activated:
                yield event.plain_result(
                    f"✅ Codex 登录成功！令牌有效期至 {expire_text}\n"
                    "访问令牌已自动填充到空 Key/旧 OAuth 来源；刷新令牌继续保存在"
                    "受保护凭据库，其他手工 Key 保持不变。到期会自动续期～"
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
            self._login_task = None

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("codex_logout")
    async def codex_logout(self, event: AstrMessageEvent):
        """Remove stored OAuth credentials without deleting manual account keys."""
        login_task = self._login_task
        if login_task and not login_task.done():
            login_task.cancel()
            if login_task is not asyncio.current_task():
                await asyncio.gather(login_task, return_exceptions=True)
        try:
            stored = load_auth_store()
            managed_hashes = set(stored.get("managed_token_hashes", []))
            clear_auth_store()
            updated = await self._reload_oauth_sources(
                managed_hashes,
            )
        except (RuntimeError, ValueError, OSError) as e:
            yield event.plain_result(f"❌ Codex 退出登录失败：{e}")
            return
        if updated:
            yield event.plain_result(
                "✅ Codex OAuth 凭据及其旧配置副本已删除；其他手工 Key 保持不变。"
            )
        else:
            yield event.plain_result(
                "✅ Codex OAuth 凭据已删除；当前没有 Codex 提供商。"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
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
        except (ValueError, PermissionError, RuntimeError, httpx.HTTPError) as e:
            yield event.plain_result(f"查询 Codex 订阅用量失败：{e}")
            return
        yield event.plain_result(format_codex_usage(usage, provider.get_current_key()))

    @filter.permission_type(filter.PermissionType.ADMIN)
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

    @filter.permission_type(filter.PermissionType.ADMIN)
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("codex_image_model")
    async def codex_image_model(self, event: AstrMessageEvent, model: str = ""):
        """View, refresh, or persist the image model selection (administrator only)."""
        arg = (model or "").strip().lower()
        changed = arg not in ("", "list", "refresh")
        if changed:
            try:
                selection = normalize_image_model(arg)
            except ValueError as exc:
                yield event.plain_result(f"❌ {exc}")
                return
            previous = self.config.get("image_model")
            self.config["image_model"] = selection
            try:
                self.config.save_config()
            except OSError:
                if previous is None:
                    self.config.pop("image_model", None)
                else:
                    self.config["image_model"] = previous
                yield event.plain_result(
                    "❌ 图片模型配置保存失败，未切换模型，请检查配置文件权限。"
                )
                return
            update_codex_settings({"image_model": selection})

        selection = get_codex_settings()["image_model"]
        lines = [
            ("✅ 已保存图片模型设置：" if changed else "当前图片模型设置：") + selection
        ]
        if selection == "auto" or arg in ("", "list", "refresh"):
            provider = self._get_codex_provider()
            proxy = (
                provider.provider_config.get("proxy")
                if provider is not None
                else CODEX_DEFAULT_PROXY
            )
            catalog = await image_model_discovery.get_catalog(
                proxy, force_refresh=arg == "refresh"
            )
            source = {
                "official": "OpenAI 官方在线目录（缓存 6 小时）",
                "stale": "上次成功获取的目录",
                "builtin": "内置备用目录",
            }[catalog["source"]]
            lines.extend(
                [
                    f"自动模式当前选择：{catalog['models'][0]}",
                    f"目录来源：{source}",
                    "图片模型候选：\n" + "\n".join(catalog["models"][:20]),
                ]
            )
            if catalog["warning"]:
                lines.append(f"⚠️ {catalog['warning']}，5 分钟后自动重试。")
        lines.extend(
            [
                "用法：/codex_image_model auto / <模型ID> / list / refresh",
                "手动选择会固定模型；在线候选仍需账号具备调用权限。",
            ]
        )
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _save_generated_image(data: bytes) -> Path:
        """Save generated PNG bytes under AstrBot's managed temp directory."""
        images_dir = Path(get_astrbot_temp_path()) / "astrbot_plugin_codex_provider"
        images_dir.mkdir(parents=True, exist_ok=True)
        path = images_dir / f"generated-{int(time.time())}-{secrets.token_hex(4)}.png"
        path.write_bytes(data)
        return path

    @staticmethod
    async def _collect_message_images(event: AstrMessageEvent) -> list[str]:
        """Collect valid, unique images from the current or quoted message.

        Args:
            event: Message event whose current and quoted chains may contain images.

        Returns:
            Up to five image data URLs in message order.

        Raises:
            ValueError: If the message contains image references but none can be read.
        """
        refs: list[str] = []
        seen_refs: set[str] = set()

        def add_image(comp: Image) -> None:
            ref = comp.url or comp.file or comp.path or ""
            if ref and ref not in seen_refs:
                seen_refs.add(ref)
                refs.append(ref)

        def collect_quoted(components) -> None:
            for comp in components or []:
                if isinstance(comp, Image):
                    add_image(comp)
                elif isinstance(comp, Reply) and comp.chain:
                    collect_quoted(comp.chain)

        components = event.get_messages()
        # The current message is the primary canvas; quoted images are fallback
        # references even when the platform places the Reply segment first.
        for comp in components:
            if isinstance(comp, Image):
                add_image(comp)
        for comp in components:
            if isinstance(comp, Reply) and comp.chain:
                collect_quoted(comp.chain)
        data_urls: list[str] = []
        for ref in refs:
            if len(data_urls) >= 5:
                break
            try:
                resolved = await resolve_media_ref_to_base64_data(
                    ref,
                    media_type="image",
                    strict=True,
                )
            except (httpx.HTTPError, ValueError, OSError) as e:
                logger.warning("[Codex] 读取参考图片失败: %s", e)
                continue
            if resolved:
                data_url = resolved.to_data_url()
                if data_url not in data_urls:
                    data_urls.append(data_url)
        if refs and not data_urls:
            raise ValueError(
                "检测到参考图片，但图片读取失败，已取消改图以避免误生成新图。"
            )
        return data_urls

    @filter.command("codex_image")
    async def codex_image(self, event: AstrMessageEvent, prompt: str = ""):
        """用插件配置的订阅图片模型生成图片；附加或引用图片时为改图模式"""
        prompt = (prompt or "").strip()
        if not prompt:
            yield event.plain_result(
                "用法：/codex_image <画面描述>\n"
                "消息中附加图片（或引用带图消息）即为改图模式，最多 5 张参考图。"
            )
            return
        provider = self._get_codex_provider()
        if provider is None:
            yield event.plain_result(
                "未找到已启用的 Codex 提供商，请先在 WebUI 配置或发送 /codex_login。"
            )
            return
        try:
            references = await self._collect_message_images(event)
            model = await resolve_image_model(
                get_codex_settings()["image_model"],
                provider.provider_config.get("proxy"),
            )
        except (ValueError, httpx.HTTPError) as e:
            yield event.plain_result(f"❌ {e}")
            return
        action = "编辑" if references else "生成"
        yield event.plain_result(
            f"🎨 图片{action}中（{model}），一般需要 30 秒到 2 分钟，请稍等喵～"
        )
        try:
            data = await provider.generate_image(prompt, references, model=model)
        except (ValueError, PermissionError, RuntimeError, httpx.HTTPError) as e:
            yield event.plain_result(f"❌ {e}")
            return
        path = self._save_generated_image(data)
        yield event.image_result(str(path))

    @filter.llm_tool(name="codex_generate_image")
    async def codex_generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        use_reference_images: bool = True,
    ) -> str:
        """使用插件配置的 ChatGPT Codex 订阅图片模型生成或编辑图片并直接发送给用户。当前消息或引用消息带图时，默认将图片作为编辑输入；只有用户明确要求忽略附图并从零生成时，才把 use_reference_images 设为 false。用户要求继续编辑旧图但本轮没有当前或引用图片时，先请用户引用或重发图片，不要把描述静默当成全新生成。

        Args:
            prompt(string): 完整的生成或编辑指令；编辑时必须明确只改什么、其余内容保持不变
            use_reference_images(bool): 是否使用当前消息和引用消息中的图片作为编辑输入，默认 true
        """
        provider = self._get_codex_provider()
        if provider is None:
            return "错误：未配置 Codex 提供商，无法生成图片。请提示主人先配置或发送 /codex_login。"
        try:
            references = (
                await self._collect_message_images(event)
                if use_reference_images
                else []
            )
        except (ValueError, httpx.HTTPError) as e:
            return f"图片编辑失败：{e}"
        action = "编辑" if references else "生成"
        try:
            model = await resolve_image_model(
                get_codex_settings()["image_model"],
                provider.provider_config.get("proxy"),
            )
            await event.send(
                event.plain_result(
                    f"🎨 图片{action}中（{model}），一般需要 30 秒到 2 分钟，请稍等喵～"
                )
            )
            data = await provider.generate_image(prompt, references, model=model)
        except (ValueError, PermissionError, RuntimeError, httpx.HTTPError) as e:
            return f"图片{action}失败：{e}"
        path = self._save_generated_image(data)
        await event.send(event.image_result(str(path)))
        return (
            f"图片已{action}并直接发送给用户，无需在回复中描述图片内容或声称无法发送。"
        )

    @filter.llm_tool(name="codex_web_search")
    async def codex_web_search(
        self,
        event: AstrMessageEvent,
        query: str = "",
    ) -> str:
        """使用 ChatGPT Codex 订阅进行联网搜索，获取实时网络信息。当需要查询最新资讯、新闻、天气、资料、价格、比分等实时或时效性内容时调用本工具。

        Args:
            query(string): 搜索查询词，尽量具体明确，可包含时间限定词（如"今天"、"最新"）
        """
        provider = self._get_codex_provider()
        if provider is None:
            return "错误：未配置 Codex 提供商，无法联网搜索。请提示主人配置或发送 /codex_login。"
        try:
            result = await provider.search_web(query)
        except (ValueError, PermissionError, RuntimeError, httpx.HTTPError) as e:
            return f"Codex 联网搜索失败：{e}"
        text = result["content"] or ""
        sources = result["sources"]
        source_by_ref = {
            source["ref_id"]: source
            for source in sources
            if isinstance(source.get("ref_id"), str) and source["ref_id"]
        }

        def replace_citation(match: re.Match) -> str:
            links = []
            for ref_id in re.findall(r"turn\d+search\d+", match.group(1)):
                source = source_by_ref.get(ref_id)
                if source is None:
                    continue
                title = source.get("title") or source["url"]
                links.append(f"[{title}]({source['url']})")
            return " ".join(links) if links else match.group(0)

        text = re.sub(r"cite([^]+)", replace_citation, text)
        # Keep the tool result bounded after converting provider citation ids.
        if len(text) > 6000:
            text = text[:6000].rsplit(" ", 1)[0] + "\n（内容过长已截断）"
        if sources:
            lines = ["", "来源："]
            for idx, src in enumerate(sources[:8], 1):
                title = src["title"] or src["url"]
                lines.append(f"{idx}. {title} - {src['url']}")
            text += "\n".join(lines)
        return text or "搜索完成，但没有找到相关内容。"
