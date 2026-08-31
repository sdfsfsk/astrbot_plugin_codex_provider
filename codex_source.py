"""OpenAI Codex (ChatGPT subscription) provider adapter for AstrBot.

Talks to the ChatGPT Codex backend (``chatgpt.com/backend-api/codex``) with an
OAuth access token copied from the official Codex CLI login, instead of an
OpenAI Platform API key. Request/response handling reuses AstrBot's built-in
Responses API provider; this class only adds the Codex-specific auth headers,
payload quirks and the mandatory SSE streaming behavior.
"""

import asyncio
import base64
import binascii
import hashlib
import inspect
import json
import math
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

import httpx

from astrbot import logger
from astrbot.core.exceptions import EmptyModelOutputError
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.register import (
    provider_cls_map,
    provider_registry,
    register_provider_adapter,
)
from astrbot.core.provider.sources.openai_responses_source import (
    ProviderOpenAIResponses,
)
from astrbot.core.provider.sources.request_retry import retry_provider_request

from .codex_auth import (
    add_managed_token_hashes,
    compare_and_save_auth_store,
    load_auth_store,
    refresh_access_token,
    token_fingerprint,
    tokens_to_store,
)

CODEX_DEFAULT_API_BASE = "https://chatgpt.com/backend-api/codex"
CODEX_DEFAULT_MODEL = "gpt-5.6-sol"
CODEX_DEFAULT_INSTRUCTIONS = "You are a helpful assistant."
CODEX_JWT_CLAIM_PATH = "https://api.openai.com/auth"
CODEX_STATIC_HEADERS = {
    "OpenAI-Beta": "responses=experimental",
    "originator": "codex_cli_rs",
    "User-Agent": "codex_cli_rs/0.50.0 (Windows 10.0.22631; x86_64)",
}
CODEX_DEFAULT_PROXY = "http://127.0.0.1:10808"
CODEX_IMAGE_MODEL = "gpt-image-2"
CODEX_IMAGE_MAX_REFERENCES = 5
CODEX_IMAGE_MAX_INPUT_BYTES = 20 * 1024 * 1024
CODEX_IMAGE_MAX_TOTAL_INPUT_BYTES = 20 * 1024 * 1024
CODEX_IMAGE_MAX_OUTPUT_BYTES = 25 * 1024 * 1024
CODEX_MAX_IMAGE_PROMPT_CHARS = 32_000
CODEX_MAX_SEARCH_QUERY_CHARS = 8_000
_TOKEN_REFRESH_LOCK = asyncio.Lock()
_TERMINAL_QUOTA_PATTERN = re.compile(
    r"GoUsageLimitError|FreeUsageLimitError|Monthly usage limit reached|"
    r"usage_limit_reached|usage_not_included|insufficient_quota|out of budget|"
    r"quota exceeded|available balance|billing",
    re.IGNORECASE,
)
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


class CodexUsageLimitError(RuntimeError):
    """Raised when retrying cannot recover an exhausted subscription quota."""


def _validated_codex_api_base(value: str | None) -> str:
    """Accept only the fixed first-party endpoint used by Codex OAuth traffic."""
    raw = (value or CODEX_DEFAULT_API_BASE).strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as e:
        raise ValueError("Codex API 地址格式无效。") from e
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").rstrip(".").lower() != "chatgpt.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/") != "/backend-api/codex"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Codex OAuth 仅允许访问官方地址 "
            "https://chatgpt.com/backend-api/codex；请使用 proxy 配置代理。"
        )
    return CODEX_DEFAULT_API_BASE


def _safe_provider_detail(value: object, limit: int = 500) -> str:
    """Bound provider diagnostics and remove credential-shaped material."""
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            value = error.get("message") or error.get("code") or ""
        else:
            value = error or value.get("message") or ""
    text = " ".join(str(value).split())
    text = _JWT_PATTERN.sub("[REDACTED]", text)
    text = re.sub(r"(?i)Bearer\s+[^\s,}\"]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED]", text)
    text = re.sub(
        r"(?i)([\"']?(?:access_token|refresh_token|authorization_code|"
        r"device_auth_id)[\"']?\s*[:=]\s*[\"']?)[^\s,}\"']+",
        r"\1[REDACTED]",
        text,
    )
    return text[:limit]


def _is_terminal_quota_error(error: object) -> bool:
    """Return whether an exception or provider event reports exhausted quota."""
    parts = [str(error)]
    for attr in ("body", "message", "code"):
        value = getattr(error, attr, None)
        if value is not None:
            parts.append(str(value))
    response = getattr(error, "response", None)
    if response is not None:
        parts.append(str(getattr(response, "text", "")))
    return _TERMINAL_QUOTA_PATTERN.search(" ".join(parts)) is not None


# Static fallback mirrored from the current Codex catalog; account-specific
# additions are merged from the authenticated models endpoint when available.
CODEX_MODEL_CATALOG = [
    "gpt-5.3-codex-spark",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.5",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
]

CODEX_PROVIDER_DESC = (
    "OpenAI Codex（ChatGPT 订阅）提供商适配器。推荐将 Key 留空，"
    "由管理员私聊发送 /codex_login 完成 OAuth 登录并自动续期；"
    "也可手动粘贴 Codex CLI ~/.codex/auth.json 中的 access_token。"
    "默认代理 127.0.0.1:10808（v2rayN 混合端口），可修改或留空。"
)

CODEX_REASONING_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]
CODEX_SEARCH_MODES = ["live", "indexed", "cached"]
CODEX_SEARCH_CONTEXT_SIZES = ["low", "medium", "high"]
CODEX_IMAGE_QUALITIES = ["auto", "high", "medium", "low"]

# Runtime request settings owned by the plugin config (not the provider
# config), so they can be changed from the plugin settings page or chat
# commands and apply to every Codex provider instance immediately.
_PLUGIN_SETTINGS: dict = {
    "reasoning_effort": "medium",
    "fast_mode": False,
    "search_mode": "live",
    "search_context_size": "medium",
    "image_quality": "auto",
}


def update_codex_settings(settings: dict) -> None:
    """Update runtime Codex request settings from the plugin config.

    Args:
        settings: Plugin config possibly carrying ``reasoning_effort``,
            ``fast_mode``, ``search_mode``, ``search_context_size`` and
            ``image_quality``; missing/invalid keys keep the current values.
    """
    effort = settings.get("reasoning_effort")
    if effort in CODEX_REASONING_EFFORTS:
        _PLUGIN_SETTINGS["reasoning_effort"] = effort
    if "fast_mode" in settings:
        _PLUGIN_SETTINGS["fast_mode"] = bool(settings["fast_mode"])
    search_mode = settings.get("search_mode")
    if search_mode in CODEX_SEARCH_MODES:
        _PLUGIN_SETTINGS["search_mode"] = search_mode
    context_size = settings.get("search_context_size")
    if context_size in CODEX_SEARCH_CONTEXT_SIZES:
        _PLUGIN_SETTINGS["search_context_size"] = context_size
    image_quality = settings.get("image_quality")
    if image_quality in CODEX_IMAGE_QUALITIES:
        _PLUGIN_SETTINGS["image_quality"] = image_quality


def get_codex_settings() -> dict:
    """Return a copy of the current runtime Codex request settings."""
    return dict(_PLUGIN_SETTINGS)


def decode_codex_token_payload(token: str) -> dict | None:
    """Decode the payload of a Codex OAuth access token (JWT).

    Args:
        token: The raw access token string.

    Returns:
        The decoded JWT payload dict, or None when the token is not a JWT.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error):
        return None
    return decoded if isinstance(decoded, dict) else None


def extract_codex_account_id(token: str) -> str | None:
    """Extract the ``chatgpt_account_id`` claim from a Codex access token.

    Args:
        token: The raw access token string.

    Returns:
        The ChatGPT account id, or None when absent/undecodable.
    """
    payload = decode_codex_token_payload(token)
    if not payload:
        return None
    auth_claim = payload.get(CODEX_JWT_CLAIM_PATH)
    if isinstance(auth_claim, dict):
        account_id = auth_claim.get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id:
            return account_id
    return None


def codex_token_expiry(token: str) -> int | None:
    """Return the expiry unix timestamp (``exp``) of a Codex access token.

    Args:
        token: The raw access token string.

    Returns:
        The expiry timestamp in seconds, or None when unavailable.
    """
    payload = decode_codex_token_payload(token)
    if not payload:
        return None
    exp = payload.get("exp")
    return exp if isinstance(exp, int | float) else None


def format_codex_usage(usage: dict, token: str | None = None) -> str:
    """Render the wham/usage response (and token state) as plain text.

    Args:
        usage: Parsed JSON from the ChatGPT subscription usage endpoint.
        token: Optional access token, used to display its expiry state.

    Returns:
        A human-readable multi-line summary.
    """
    lines = ["🐾 Codex 订阅用量"]

    if token:
        exp = codex_token_expiry(token)
        if exp is not None:
            expire_at = (
                datetime.fromtimestamp(exp, tz=timezone.utc)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M")
            )
            state = "已过期" if exp < time.time() else "有效"
            lines.append(f"令牌状态: {state}（到期时间 {expire_at}）")

    plan = usage.get("plan_type")
    if isinstance(plan, str) and plan:
        lines.append(f"订阅计划: {plan}")

    def window_line(title: str, window: dict) -> str | None:
        used = window.get("used_percent")
        if (
            isinstance(used, bool)
            or not isinstance(used, int | float)
            or not math.isfinite(used)
            or not 0 <= used <= 100
        ):
            return None
        seconds = window.get("limit_window_seconds")
        if (
            not isinstance(seconds, bool)
            and isinstance(seconds, int | float)
            and math.isfinite(seconds)
            and seconds >= 86400
        ):
            span = f"{seconds // 86400}天"
        elif (
            not isinstance(seconds, bool)
            and isinstance(seconds, int | float)
            and math.isfinite(seconds)
            and seconds > 0
        ):
            span = f"{seconds // 3600}小时"
        else:
            span = "未知周期"
        text = f"{title}（{span}）: 已用 {used:.0f}% · 剩余 {100 - used:.0f}%"
        reset = window.get("reset_at")
        if (
            not isinstance(reset, bool)
            and isinstance(reset, int | float)
            and math.isfinite(reset)
            and reset > 0
        ):
            reset_at = (
                datetime.fromtimestamp(reset, tz=timezone.utc)
                .astimezone()
                .strftime("%m-%d %H:%M")
            )
            text += f" · 重置于 {reset_at}"
        return text

    def limit_lines(rate_limit: dict) -> list[str]:
        result = []
        primary = window_line("主要窗口", rate_limit.get("primary_window") or {})
        if primary:
            result.append(primary)
        secondary = window_line("次要窗口", rate_limit.get("secondary_window") or {})
        if secondary:
            result.append(secondary)
        return result

    rate_limit = usage.get("rate_limit")
    if isinstance(rate_limit, dict):
        lines.extend(limit_lines(rate_limit))

    additional = usage.get("additional_rate_limits")
    if isinstance(additional, list):
        for item in additional:
            if not isinstance(item, dict):
                continue
            name = item.get("limit_name") or item.get("metered_feature") or "附加限额"
            sub = item.get("rate_limit")
            if isinstance(sub, dict):
                for line in limit_lines(sub):
                    lines.append(f"[{name}] {line}")

    credits = usage.get("credits")
    if isinstance(credits, dict) and credits.get("has_credits"):
        if credits.get("unlimited"):
            lines.append("额度: 无限制")
        elif isinstance(credits.get("balance"), str):
            lines.append(f"额度余额: {credits['balance']}")

    return "\n".join(lines)


CODEX_CONFIG_TMPL = {
    "id": "codex",
    "provider": "openai-codex",
    "type": "codex_chat_completion",
    "provider_type": "chat_completion",
    "enable": True,
    "key": [],
    "api_base": CODEX_DEFAULT_API_BASE,
    "timeout": 120,
    "proxy": CODEX_DEFAULT_PROXY,
    "model": CODEX_DEFAULT_MODEL,
    "custom_headers": dict(CODEX_STATIC_HEADERS),
    "custom_extra_body": {},
}


class ProviderCodex(ProviderOpenAIResponses):
    """ChatGPT Codex backend provider driven by a pasted OAuth access token."""

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        """Initialize the Codex provider with Codex-specific defaults.

        Args:
            provider_config: Provider source and model configuration.
            provider_settings: Global provider settings.
        """
        merged_config = dict(provider_config)
        merged_config["api_base"] = _validated_codex_api_base(
            merged_config.get("api_base")
        )
        merged_config.setdefault("model", CODEX_DEFAULT_MODEL)
        merged_config["custom_headers"] = {
            **CODEX_STATIC_HEADERS,
            **(merged_config.get("custom_headers") or {}),
        }
        super().__init__(merged_config, provider_settings)
        # AstrBot owns transport retries; disable the SDK's hidden retry layer.
        self.client.max_retries = 0

        store = load_auth_store()
        stored_token = store.get("access_token")
        configured_token = self.api_keys[0] if len(self.api_keys) == 1 else None
        if stored_token and not any(self.api_keys):
            logger.info("[Codex] 提供商未配置 Key，改用 /codex_login 保存的登录令牌。")
            self._set_runtime_token(stored_token)
        elif stored_token and configured_token:
            stored_account = extract_codex_account_id(stored_token)
            configured_account = extract_codex_account_id(configured_token)
            stored_exp = (
                codex_token_expiry(stored_token) or store.get("expires_at") or 0
            )
            configured_exp = codex_token_expiry(configured_token) or 0
            if (
                stored_account
                and stored_account == configured_account
                and stored_exp > configured_exp
            ):
                logger.info(
                    "[Codex] 使用登录凭据库中更新的访问令牌，忽略配置里的旧副本。"
                )
                add_managed_token_hashes(
                    stored_token,
                    {token_fingerprint(configured_token)},
                )
                self._set_runtime_token(stored_token, previous_token=configured_token)
        self._warn_token_state()

    def _active_token(self) -> str:
        """Return the token selected for the current request."""
        return (
            self.client.api_key
            or self.chosen_api_key
            or (self.api_keys[0] if self.api_keys else "")
        )

    def _set_runtime_token(self, token: str, previous_token: str | None = None) -> None:
        """Adopt one refreshed token without discarding unrelated account keys."""
        account_id = extract_codex_account_id(token)
        updated: list[str] = []
        replaced = False
        for key in self.api_keys:
            if not key:
                continue
            same_account = bool(
                account_id and extract_codex_account_id(key) == account_id
            )
            if key == previous_token or same_account:
                if not replaced:
                    updated.append(token)
                    replaced = True
                continue
            updated.append(key)
        if not replaced:
            updated.insert(0, token)
        self.api_keys = updated
        self.chosen_api_key = token
        self.client.api_key = token

    async def _maybe_refresh_token(self) -> None:
        """Refresh only the stored credential matching the active account."""
        active_token = self._active_token()
        store = load_auth_store()
        active_exp = codex_token_expiry(active_token)
        if store.get("access_token") == active_token and store.get("expires_at"):
            active_exp = min(active_exp or store["expires_at"], store["expires_at"])
        if active_exp is None or active_exp - time.time() > 300:
            return

        async with _TOKEN_REFRESH_LOCK:
            active_token = self._active_token()
            active_account = extract_codex_account_id(active_token)
            store = load_auth_store()
            stored_token = store.get("access_token", "")
            stored_account = extract_codex_account_id(stored_token)
            stored_exp = (
                codex_token_expiry(stored_token) or store.get("expires_at") or 0
            )
            if not active_account or active_account != stored_account:
                logger.warning(
                    "[Codex] 当前访问令牌与登录凭据库不属于同一账号，拒绝自动刷新。"
                )
                return
            if stored_token != active_token:
                if stored_exp - time.time() > 300:
                    self._set_runtime_token(stored_token, previous_token=active_token)
                    return
                logger.warning(
                    "[Codex] 登录凭据已被其他流程替换，拒绝使用不匹配的刷新令牌。"
                )
                return

            active_exp = codex_token_expiry(active_token) or store.get("expires_at")
            if active_exp is None or active_exp - time.time() > 300:
                return
            refresh_token = store.get("refresh_token")
            if not refresh_token:
                logger.warning(
                    "[Codex] 访问令牌即将过期且没有刷新令牌，请发送 /codex_login。"
                )
                return
            try:
                tokens = await refresh_access_token(
                    refresh_token,
                    self.provider_config.get("proxy") or None,
                )
                new_store = tokens_to_store(
                    tokens,
                    previous_refresh_token=refresh_token,
                    managed_token_hashes=set(store.get("managed_token_hashes", []))
                    | {token_fingerprint(active_token)},
                )
                new_token = new_store["access_token"]
                if extract_codex_account_id(new_token) != active_account:
                    raise RuntimeError("刷新响应切换了 Codex 账号，已拒绝采用。")
                committed = compare_and_save_auth_store(
                    active_token,
                    refresh_token,
                    new_store,
                )
            except (httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
                logger.warning("[Codex] 访问令牌自动刷新失败: %s", e)
                return

            if not committed:
                latest = load_auth_store()
                latest_token = latest.get("access_token", "")
                if extract_codex_account_id(latest_token) == active_account:
                    self._set_runtime_token(
                        latest_token,
                        previous_token=active_token,
                    )
                logger.info("[Codex] 登录状态已变化，丢弃过期刷新请求的响应。")
                return
            self._set_runtime_token(new_token, previous_token=active_token)
            new_exp = codex_token_expiry(new_token) or new_store.get("expires_at")
            expire_at = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(new_exp))
                if new_exp
                else "未知"
            )
            logger.info("[Codex] 访问令牌已自动刷新，新令牌有效期至 %s", expire_at)

    def _warn_token_state(self) -> None:
        """Log a warning for malformed/expired tokens at startup."""
        for key in self.api_keys:
            if not key:
                continue
            payload = decode_codex_token_payload(key)
            if payload is None:
                logger.warning(
                    "[Codex] 配置的 Key 不是有效的 JWT 访问令牌，请粘贴 Codex CLI "
                    "登录后 ~/.codex/auth.json 中的 access_token。"
                )
                continue
            exp = payload.get("exp")
            if isinstance(exp, int | float):
                expire_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp))
                if exp < time.time():
                    logger.warning(
                        "[Codex] 访问令牌已于 %s 过期，请重新登录 Codex CLI 获取新令牌"
                        "（获取过程建议挂代理）。",
                        expire_at,
                    )
                else:
                    logger.info("[Codex] 访问令牌有效期至 %s", expire_at)
            if not extract_codex_account_id(key):
                logger.warning(
                    "[Codex] 令牌中未找到 chatgpt_account_id，请求可能失败。"
                )

    def _codex_request_headers(
        self,
        prompt_cache_key: str | None = None,
    ) -> dict[str, str]:
        """Build account and optional cache-routing headers for one request."""
        account_id = extract_codex_account_id(self._active_token())
        headers = {"chatgpt-account-id": account_id} if account_id else {}
        if prompt_cache_key:
            headers["session-id"] = prompt_cache_key
            headers["x-client-request-id"] = prompt_cache_key
        return headers

    async def _fetch_remote_models(self, token: str) -> list[str]:
        """Fetch server-advertised model slugs from the Codex backend.

        The ``/codex/models`` endpoint (``client_version`` query required)
        lets OpenAI roll out account-specific model additions on top of the
        built-in catalog. Any failure simply yields an empty list.

        Args:
            token: The current Codex access token.

        Returns:
            Extra model slugs advertised by the backend.
        """
        api_base = _validated_codex_api_base(self.provider_config.get("api_base"))
        proxy = self.provider_config.get("proxy") or None
        headers = {
            "authorization": f"Bearer {token}",
            "originator": "codex_cli_rs",
            "accept": "application/json",
            "User-Agent": CODEX_STATIC_HEADERS["User-Agent"],
        }
        account_id = extract_codex_account_id(token)
        if account_id:
            headers["chatgpt-account-id"] = account_id
        async with httpx.AsyncClient(proxy=proxy, timeout=15) as client:
            resp = await client.get(
                f"{api_base}/models",
                params={"client_version": "0.50.0"},
                headers=headers,
            )
        if resp.status_code != 200:
            if resp.status_code in (401, 403):
                logger.warning(
                    "[Codex] 在线模型目录拒绝当前登录凭据（HTTP %s）。",
                    resp.status_code,
                )
            return []
        try:
            payload = resp.json()
        except ValueError as e:
            raise RuntimeError("Codex 在线模型目录返回了无效 JSON。") from e
        if not isinstance(payload, dict):
            raise RuntimeError("Codex 在线模型目录响应格式异常。")
        raw_models = payload.get("models") or []
        if not isinstance(raw_models, list):
            raise RuntimeError("Codex 在线模型目录缺少模型数组。")
        slugs: list[str] = []
        for model in raw_models:
            if isinstance(model, dict):
                slug = model.get("slug") or model.get("id")
            else:
                slug = str(model)
            if slug:
                slugs.append(str(slug))
        return slugs

    async def get_models(self) -> list[str]:
        """Return the static catalog merged with server-advertised models.

        The backend currently returns an empty additions list for most
        accounts, so the static catalog mirrored from the Codex CLI remains
        the primary source; new official models appear automatically once
        the endpoint advertises them. Fetch failures fall back to the
        static catalog.

        Returns:
            The merged, deduplicated model id list.
        """
        models = list(CODEX_MODEL_CATALOG)
        await self._maybe_refresh_token()
        token = self._active_token()
        if not token:
            return models
        try:
            remote_slugs = await self._fetch_remote_models(token)
        except (httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
            logger.debug("[Codex] 拉取在线模型列表失败，使用内置目录: %s", e)
            return models
        for slug in remote_slugs:
            if slug not in models:
                models.append(slug)
        return models

    def _convert_chat_messages_to_response_input(
        self,
        messages: list[dict],
    ) -> list[dict]:
        """Convert chat history, then normalize it for the Codex backend.

        The Codex backend is stricter than the platform Responses API: message
        content must use typed parts (``input_text``/``output_text``) and the
        ``system`` role must be rewritten as ``developer``.

        Args:
            messages: AstrBot context in OpenAI Chat Completions format.

        Returns:
            A list of Responses API input items accepted by the Codex backend.
        """
        items = super()._convert_chat_messages_to_response_input(messages)
        for item in items:
            if item.get("type") != "message":
                continue
            if item.get("role") == "system":
                item["role"] = "developer"
            content = item.get("content")
            if isinstance(content, str):
                part_type = (
                    "output_text" if item.get("role") == "assistant" else "input_text"
                )
                item["content"] = [{"type": part_type, "text": content}]
        return items

    async def _prepare_chat_payload(self, *args, **kwargs) -> tuple[dict, list[dict]]:
        """Build the Responses payload with Codex-required fields.

        The Codex backend requires ``instructions`` to be present and benefits
        from encrypted reasoning replay (the backend runs with ``store:
        false``).
        """
        prompt_cache_key = kwargs.pop("codex_prompt_cache_key", None)
        payloads, context_query = await super()._prepare_chat_payload(*args, **kwargs)
        if not self.api_keys or any(
            not extract_codex_account_id(key) for key in self.api_keys
        ):
            raise ValueError(
                "Codex 提供商的 Key 不是有效的访问令牌：请粘贴 Codex CLI 登录后 "
                "~/.codex/auth.json 中 eyJ 开头的 access_token 本体，"
                "而不是 account_id 等其他字段。"
            )
        payloads.setdefault("instructions", CODEX_DEFAULT_INSTRUCTIONS)
        payloads["include"] = ["reasoning.encrypted_content"]
        payloads["parallel_tool_calls"] = True
        if isinstance(prompt_cache_key, str) and prompt_cache_key:
            payloads["prompt_cache_key"] = prompt_cache_key
        return payloads, context_query

    async def _query_stream(
        self,
        payloads: dict,
        tools,
        *,
        request_max_retries: int | None = None,
    ):
        """Stream a request and rebuild the final response from item events.

        Unlike the platform Responses API, the Codex backend returns an empty
        ``output`` array in the terminal ``response.completed`` event; every
        output item arrives through ``response.output_item.done`` events.
        Those items are collected here and injected back into the terminal
        response so the shared response parser can handle them. The
        ``chatgpt-account-id`` header is recomputed on every call so key
        rotation picks up the account id matching the newly selected token.

        Args:
            payloads: Prepared Responses API payload.
            tools: Functions available to the model.
            request_max_retries: Maximum transport-level request attempts.

        Yields:
            Text/reasoning deltas followed by one complete normalized response.

        Raises:
            RuntimeError: If the backend reports a stream error.
            EmptyModelOutputError: If the stream ends without a terminal event.
        """
        if tools:
            response_tools = []
            for tool in tools.openai_schema():
                function = tool.get("function", {})
                response_tools.append({"type": "function", **function})
            if response_tools:
                payloads["tools"] = response_tools
                payloads["tool_choice"] = payloads.get("tool_choice", "auto")

        extra_body: dict = {}
        custom_extra_body = self.provider_config.get("custom_extra_body", {})
        protected_fields = {
            "stream",
            "input",
            "model",
            "instructions",
            "include",
            "parallel_tool_calls",
            "tool_choice",
            "tools",
            "store",
            "previous_response_id",
            "conversation",
            "prompt_cache_key",
            "reasoning",
            "reasoning_effort",
            "service_tier",
        }
        if isinstance(custom_extra_body, dict):
            extra_body.update(
                {
                    key: value
                    for key, value in custom_extra_body.items()
                    if key not in protected_fields
                }
            )

        for key in list(payloads):
            if key not in self.default_params:
                extra_body[key] = payloads.pop(key)

        max_tokens = extra_body.pop("max_tokens", None)
        if max_tokens is not None and "max_output_tokens" not in extra_body:
            extra_body["max_output_tokens"] = max_tokens
        # Reasoning depth and fast mode are owned by the plugin-level
        # settings; any reasoning keys in custom_extra_body are overridden.
        extra_body.pop("reasoning_effort", None)
        extra_body.pop("reasoning", None)
        settings = get_codex_settings()
        reasoning_effort = settings["reasoning_effort"]
        if reasoning_effort == "minimal":
            reasoning_effort = "low"
        extra_body["reasoning"] = {
            "effort": reasoning_effort,
            # Ask for reasoning summaries explicitly; the Codex backend
            # returns no visible thinking content otherwise. With "auto" the
            # backend frequently omits summaries on simple turns, so use
            # "detailed" when AstrBot's show-reasoning option is enabled.
            "summary": (
                "detailed"
                if self.provider_settings.get("display_reasoning_text")
                else "auto"
            ),
        }
        extra_body.pop("service_tier", None)
        if settings["fast_mode"]:
            extra_body["service_tier"] = "priority"
        extra_body.pop("previous_response_id", None)
        extra_body.pop("conversation", None)
        extra_body.pop("store", None)
        payloads.pop("previous_response_id", None)
        payloads.pop("conversation", None)
        payloads["store"] = False

        prompt_cache_key = payloads.get("prompt_cache_key") or extra_body.get(
            "prompt_cache_key"
        )
        await self._maybe_refresh_token()

        async def create_stream():
            try:
                return await self.client.responses.create(
                    **payloads,
                    stream=True,
                    extra_body=extra_body,
                    extra_headers=self._codex_request_headers(prompt_cache_key),
                )
            except Exception as e:
                if _is_terminal_quota_error(e):
                    raise CodexUsageLimitError(
                        "Codex 订阅额度已耗尽，请等待额度窗口重置。"
                    ) from e
                raise

        stream = await retry_provider_request(
            "OpenAI Codex",
            create_stream,
            max_attempts=request_max_retries,
        )

        response_id: str | None = None
        output_items: list = []
        try:
            async for event in stream:
                event_type = self._field(event, "type", "")
                event_response = self._field(event, "response")
                if event_response is not None:
                    response_id = self._field(event_response, "id", response_id)

                if event_type == "error":
                    event_error = self._field(event, "error")
                    code = self._field(event, "code") or self._field(
                        event_error,
                        "code",
                        "stream_error",
                    )
                    message = self._field(event, "message") or self._field(
                        event_error,
                        "message",
                        "Codex stream failed",
                    )
                    if _TERMINAL_QUOTA_PATTERN.search(f"{code} {message}"):
                        raise CodexUsageLimitError(
                            "Codex 订阅额度已耗尽，请等待额度窗口重置。"
                        )
                    raise RuntimeError(
                        "Codex stream failed: "
                        f"{_safe_provider_detail(code)}: "
                        f"{_safe_provider_detail(message)}. response_id={response_id}"
                    )

                if event_type == "response.output_item.done":
                    item = self._field(event, "item")
                    if item is not None:
                        output_items.append(item)
                    continue

                if event_type in {
                    "response.output_text.delta",
                    "response.refusal.delta",
                }:
                    delta = self._field(event, "delta", "")
                    if delta:
                        yield LLMResponse(
                            "assistant",
                            result_chain=MessageChain(chain=[Plain(str(delta))]),
                            is_chunk=True,
                            id=response_id,
                        )
                    continue

                if event_type in {
                    "response.reasoning_text.delta",
                    "response.reasoning_summary_text.delta",
                }:
                    delta = self._field(event, "delta", "")
                    if delta:
                        yield LLMResponse(
                            "assistant",
                            reasoning_content=str(delta),
                            is_chunk=True,
                            id=response_id,
                        )
                    continue

                if event_type in {
                    "response.completed",
                    "response.done",
                    "response.incomplete",
                    "response.failed",
                }:
                    if event_response is None:
                        raise EmptyModelOutputError(
                            f"Codex stream terminal event has no response: {event_type}"
                        )
                    status = self._field(event_response, "status")
                    if event_type == "response.incomplete" or status == "incomplete":
                        details = self._field(event_response, "incomplete_details")
                        reason = self._field(details, "reason", "unknown")
                        raise RuntimeError(
                            "Codex response was incomplete: "
                            f"{_safe_provider_detail(reason)}. response_id={response_id}"
                        )
                    if status == "cancelled":
                        raise RuntimeError(
                            f"Codex response was cancelled. response_id={response_id}"
                        )
                    if (
                        not (self._field(event_response, "output", None) or [])
                        and output_items
                    ):
                        if hasattr(event_response, "model_copy"):
                            event_response = event_response.model_copy(
                                update={"output": output_items}
                            )
                        elif isinstance(event_response, dict):
                            event_response = {**event_response, "output": output_items}
                    yield await self._parse_response(event_response, tools)
                    return
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close_result = close()
                    if inspect.isawaitable(close_result):
                        await close_result
                except Exception as e:
                    logger.debug("[Codex] Failed to close SSE stream: %s", e)

        raise EmptyModelOutputError(
            f"Codex stream ended without a terminal event. response_id={response_id}"
        )

    async def text_chat_stream(
        self,
        prompt=None,
        session_id=None,
        image_urls=None,
        audio_urls=None,
        func_tool=None,
        contexts=None,
        system_prompt=None,
        tool_calls_result=None,
        model=None,
        extra_user_content_parts=None,
        tool_choice: Literal["auto", "required"] = "auto",
        request_max_retries: int | None = None,
        **kwargs,
    ):
        """Stream one turn with an opaque stable prompt-cache identity."""
        prompt_cache_key = None
        if session_id is not None:
            prompt_cache_key = hashlib.sha256(str(session_id).encode()).hexdigest()
        async for response in super().text_chat_stream(
            prompt=prompt,
            session_id=session_id,
            image_urls=image_urls,
            audio_urls=audio_urls,
            func_tool=func_tool,
            contexts=contexts,
            system_prompt=system_prompt,
            tool_calls_result=tool_calls_result,
            model=model,
            tool_choice=tool_choice,
            request_max_retries=request_max_retries,
            extra_user_content_parts=extra_user_content_parts,
            codex_prompt_cache_key=prompt_cache_key,
            **kwargs,
        ):
            yield response

    async def text_chat(
        self,
        prompt=None,
        session_id=None,
        image_urls=None,
        audio_urls=None,
        func_tool=None,
        contexts=None,
        system_prompt=None,
        tool_calls_result=None,
        model=None,
        extra_user_content_parts=None,
        tool_choice: Literal["auto", "required"] = "auto",
        request_max_retries: int | None = None,
        **kwargs,
    ) -> LLMResponse:
        """Aggregate a streaming turn into one response.

        The Codex backend only supports SSE streaming (``stream: true``), so
        the non-streaming entry point delegates to the streaming one.

        Raises:
            EmptyModelOutputError: If the stream yields no complete response.
        """
        final_response = None
        async for response in self.text_chat_stream(
            prompt=prompt,
            session_id=session_id,
            image_urls=image_urls,
            audio_urls=audio_urls,
            func_tool=func_tool,
            contexts=contexts,
            system_prompt=system_prompt,
            tool_calls_result=tool_calls_result,
            model=model,
            tool_choice=tool_choice,
            request_max_retries=request_max_retries,
            extra_user_content_parts=extra_user_content_parts,
            **kwargs,
        ):
            if not response.is_chunk:
                final_response = response
        if final_response is None:
            raise EmptyModelOutputError("Codex backend returned no complete response.")
        return final_response

    async def fetch_usage(self) -> dict:
        """Query the ChatGPT subscription usage endpoint (``wham/usage``).

        Uses the provider's token and proxy configuration so the request
        behaves exactly like normal model traffic.

        Returns:
            The parsed usage JSON.

        Raises:
            ValueError: If no token is configured or it carries no account id.
            PermissionError: If the token is rejected (expired/invalid).
            httpx.HTTPStatusError: For other non-2xx responses.
        """
        await self._maybe_refresh_token()
        token = self._active_token()
        if not token:
            raise ValueError("未配置 Codex 访问令牌，请先在提供商 Key 栏粘贴令牌。")
        account_id = extract_codex_account_id(token)
        if not account_id:
            raise ValueError(
                "无法从令牌解析 chatgpt-account-id，请确认粘贴的是 access_token 本体。"
            )

        api_base = _validated_codex_api_base(self.provider_config.get("api_base"))
        backend_base = api_base.removesuffix("/codex")
        proxy = self.provider_config.get("proxy") or None

        async with httpx.AsyncClient(proxy=proxy, timeout=30) as client:
            resp = await client.get(
                f"{backend_base}/wham/usage",
                headers={
                    "authorization": f"Bearer {token}",
                    "chatgpt-account-id": account_id,
                    "originator": "codex_cli_rs",
                    "accept": "application/json",
                },
            )
        if resp.status_code in (401, 403):
            raise PermissionError(
                f"令牌无效或已过期（HTTP {resp.status_code}），请重新获取（建议挂代理）。"
            )
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError as e:
            raise RuntimeError("Codex 用量接口返回了无效 JSON。") from e
        if not isinstance(payload, dict):
            raise RuntimeError("Codex 用量接口响应格式异常。")
        return payload

    async def generate_image(
        self,
        prompt: str,
        reference_images: list[str] | None = None,
    ) -> bytes:
        """Generate or edit an image with the subscription's gpt-image-2.

        Uses the standalone Codex image endpoints: ``images/generations``
        for pure generation and ``images/edits`` when reference images are
        supplied (mirroring the official Codex image extension).

        Args:
            prompt: The generation/edit instruction.
            reference_images: Optional reference images as data URLs
                (``data:image/...;base64,...``); at most 5 are used.

        Returns:
            The generated PNG bytes.

        Raises:
            ValueError: If no token is configured or the prompt is empty.
            PermissionError: If the token is rejected (expired/invalid).
            RuntimeError: For other backend or payload failures.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("图片生成提示词不能为空。")
        if len(prompt) > CODEX_MAX_IMAGE_PROMPT_CHARS:
            raise ValueError(
                f"图片提示词不能超过 {CODEX_MAX_IMAGE_PROMPT_CHARS} 个字符。"
            )
        await self._maybe_refresh_token()
        token = self._active_token()
        if not token:
            raise ValueError("未配置 Codex 访问令牌，请先 /codex_login 或配置 Key。")
        account_id = extract_codex_account_id(token)
        if not account_id:
            raise ValueError("无法从令牌解析 chatgpt-account-id。")

        refs = [ref for ref in (reference_images or []) if ref][
            :CODEX_IMAGE_MAX_REFERENCES
        ]
        total_input_bytes = 0
        for ref in refs:
            if not ref.startswith("data:image/") or "," not in ref:
                raise ValueError("参考图片必须是有效的 image data URL。")
            encoded = ref.split(",", 1)[1]
            estimated_bytes = len(encoded) * 3 // 4
            if estimated_bytes > CODEX_IMAGE_MAX_INPUT_BYTES:
                raise ValueError("单张参考图片不能超过 20 MiB。")
            total_input_bytes += estimated_bytes
        if total_input_bytes > CODEX_IMAGE_MAX_TOTAL_INPUT_BYTES:
            raise ValueError("参考图片总大小不能超过 20 MiB。")
        settings = get_codex_settings()
        quality = settings["image_quality"]
        if refs:
            # Treat the first image as a source canvas instead of a loose visual
            # reference so short edit instructions do not trigger a redesign.
            prompt = (
                "Treat Image 1 as the exact source canvas, not merely as visual "
                "inspiration. Apply only the requested edit. Preserve the "
                "original crop, framing, composition, viewpoint, geometry, "
                "subject identity, pose, expression, proportions, background, "
                "lighting, colors, texture, linework, and every unmentioned "
                "detail. Do not redesign, restyle, recompose, replace, or add "
                "anything outside the requested change. "
                + (
                    "Images 2 and later are supporting references unless the "
                    "request explicitly assigns them another role. "
                    if len(refs) > 1
                    else ""
                )
                + "Requested edit: "
                + prompt
            )
        api_base = _validated_codex_api_base(self.provider_config.get("api_base"))
        proxy = self.provider_config.get("proxy") or None
        body: dict = {
            "prompt": prompt,
            "background": "auto",
            "model": CODEX_IMAGE_MODEL,
            "quality": quality,
            "size": "auto",
        }
        if refs:
            body["images"] = [{"image_url": ref} for ref in refs]
        endpoint = (
            f"{api_base}/images/edits" if refs else f"{api_base}/images/generations"
        )
        logger.info(
            "[Codex] Image request mode=%s references=%d quality=%s",
            "edit" if refs else "generate",
            len(refs),
            quality,
        )

        async with httpx.AsyncClient(proxy=proxy, timeout=300) as client:
            resp = await client.post(
                endpoint,
                json=body,
                headers={
                    "authorization": f"Bearer {token}",
                    "chatgpt-account-id": account_id,
                    "originator": "codex_cli_rs",
                    "content-type": "application/json",
                    "accept": "application/json",
                },
            )
        if resp.status_code in (401, 403):
            raise PermissionError(
                f"令牌无效或已过期（HTTP {resp.status_code}），请重新 /codex_login。"
            )
        if resp.status_code != 200:
            detail = ""
            if len(resp.content) <= 64 * 1024:
                try:
                    error_payload = resp.json()
                except ValueError:
                    error_payload = None
                if isinstance(error_payload, dict):
                    provider_error = error_payload.get("error")
                    if isinstance(provider_error, dict):
                        raw_message = provider_error.get("message")
                        raw_code = provider_error.get("code") or provider_error.get(
                            "type"
                        )
                        message = (
                            _safe_provider_detail(raw_message)
                            if raw_message is not None
                            else ""
                        )
                        code = (
                            _safe_provider_detail(raw_code)
                            if raw_code is not None
                            else ""
                        )
                    else:
                        message = (
                            _safe_provider_detail(provider_error)
                            if provider_error is not None
                            else ""
                        )
                        code = ""
                    if "rejected by the safety system" in message.lower():
                        request_id = re.search(
                            r"\brequest ID ([A-Za-z0-9-]{8,})",
                            message,
                            re.IGNORECASE,
                        )
                        detail = "请求被 OpenAI 安全系统拒绝，请调整提示词或参考图片"
                        if request_id:
                            detail += f"（request_id={request_id.group(1)}）"
                    elif message:
                        detail = message
                        if code and code.lower() not in message.lower():
                            detail += f"（code={code}）"
                    elif code:
                        detail = f"上游错误代码：{code}"
            if detail:
                logger.warning(
                    "[Codex] Image request failed HTTP %d: %s",
                    resp.status_code,
                    detail,
                )
                raise RuntimeError(
                    f"上游图片接口返回 HTTP {resp.status_code}：{detail}。"
                )
            logger.warning(
                "[Codex] Image request failed HTTP %d without safe JSON detail",
                resp.status_code,
            )
            raise RuntimeError(
                f"上游图片接口返回 HTTP {resp.status_code}，未提供可解析原因。"
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise RuntimeError("图片生成接口返回了无效 JSON。") from e
        if not isinstance(payload, dict):
            raise RuntimeError("图片生成接口响应格式异常。")
        data = payload.get("data") or []
        first = (
            data[0]
            if isinstance(data, list) and data and isinstance(data[0], dict)
            else {}
        )
        b64 = first.get("b64_json")
        if not isinstance(b64, str) or not b64.strip():
            raise RuntimeError("图片生成响应中没有图像数据。")
        if len(b64) * 3 // 4 > CODEX_IMAGE_MAX_OUTPUT_BYTES:
            raise RuntimeError("图片生成响应超过 25 MiB 安全上限。")
        try:
            image_data = base64.b64decode(b64.strip(), validate=True)
        except (ValueError, binascii.Error) as e:
            raise RuntimeError("图片生成响应包含无效的 Base64 数据。") from e
        if len(image_data) > CODEX_IMAGE_MAX_OUTPUT_BYTES:
            raise RuntimeError("图片生成响应超过 25 MiB 安全上限。")
        if not image_data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("图片生成响应不是有效的 PNG 文件。")
        return image_data

    async def search_web(self, query: str) -> dict:
        """Run a standalone Codex web search via the ``alpha/search`` endpoint.

        This is the Codex client's built-in search protocol (not the regular
        Responses API), used by the plugin's ``codex_web_search`` LLM tool.
        Search mode and context size come from the plugin-level settings.

        Args:
            query: The search query text.

        Returns:
            Dict with ``content`` (answer text) and ``sources`` (list of
            {url, title, snippet}).

        Raises:
            ValueError: If the query is empty or no token is configured.
            PermissionError: If the token is rejected (expired/invalid).
            RuntimeError: For other backend or payload failures.
        """
        query = (query or "").strip()
        if not query:
            raise ValueError("搜索关键词不能为空。")
        if len(query) > CODEX_MAX_SEARCH_QUERY_CHARS:
            raise ValueError(
                f"搜索关键词不能超过 {CODEX_MAX_SEARCH_QUERY_CHARS} 个字符。"
            )
        await self._maybe_refresh_token()
        token = self._active_token()
        if not token:
            raise ValueError("未配置 Codex 访问令牌，请先 /codex_login 或配置 Key。")
        account_id = extract_codex_account_id(token)
        if not account_id:
            raise ValueError("无法从令牌解析 chatgpt-account-id。")

        settings = get_codex_settings()
        # cached -> no external fetch, indexed -> index only, live -> real web
        external_web_access: bool | str = {
            "cached": False,
            "indexed": "indexed",
            "live": True,
        }[settings["search_mode"]]
        api_base = _validated_codex_api_base(self.provider_config.get("api_base"))
        proxy = self.provider_config.get("proxy") or None
        body = {
            "id": f"astrbot-{int(time.time() * 1000)}-{secrets.token_hex(4)}",
            "model": self.get_model() or CODEX_DEFAULT_MODEL,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": query}],
                }
            ],
            "commands": {"search_query": [{"q": query}]},
            "settings": {
                "search_context_size": settings["search_context_size"],
                "allowed_callers": ["direct"],
                "external_web_access": external_web_access,
            },
            "max_output_tokens": 10000,
        }

        async with httpx.AsyncClient(proxy=proxy, timeout=120) as client:
            resp = await client.post(
                f"{api_base}/alpha/search",
                json=body,
                headers={
                    "authorization": f"Bearer {token}",
                    "chatgpt-account-id": account_id,
                    "originator": "codex_cli_rs",
                    "content-type": "application/json",
                    "accept": "application/json",
                },
            )
        if resp.status_code in (401, 403):
            raise PermissionError(
                f"令牌无效或已过期（HTTP {resp.status_code}），请重新 /codex_login。"
            )
        if resp.status_code != 200:
            raise RuntimeError(f"搜索失败（HTTP {resp.status_code}）。")

        try:
            payload = resp.json()
        except ValueError as e:
            raise RuntimeError("搜索接口返回了无效 JSON。") from e
        if not isinstance(payload, dict):
            raise RuntimeError("搜索接口响应格式异常。")
        output = payload.get("output")
        if not isinstance(output, str):
            raise RuntimeError("搜索响应中没有文本输出。")
        raw_results = payload.get("results") or []
        if not isinstance(raw_results, list):
            raise RuntimeError("搜索响应中的 results 不是数组。")
        sources: list[dict] = []
        seen: set[str] = set()
        for item in raw_results:
            if not isinstance(item, dict) or item.get("type") != "text_result":
                continue
            url = item.get("url")
            if not isinstance(url, str):
                continue
            try:
                parsed_url = urlsplit(url)
            except ValueError:
                continue
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                continue
            if url in seen:
                continue
            seen.add(url)
            sources.append(
                {
                    "url": url,
                    "title": item.get("title") or "",
                    "snippet": item.get("snippet") or "",
                    "ref_id": (
                        item.get("ref_id")
                        if isinstance(item.get("ref_id"), str)
                        else ""
                    ),
                }
            )
        return {"content": output, "sources": sources}


def _register_codex_provider() -> None:
    """Register the provider adapter, replacing any stale registration.

    AstrBot re-executes plugin modules on hot reload while provider
    registrations live in process-global registries, so a plain decorator
    registration would raise a duplicate-type error on the second load.
    Replacing the stale entry also keeps the registered class pointing at
    this (newest) module instance.
    """
    current = provider_cls_map.get("codex_chat_completion")
    if current is not None and current.cls_type is ProviderCodex:
        return
    stale = provider_cls_map.pop("codex_chat_completion", None)
    if stale is not None and stale in provider_registry:
        provider_registry.remove(stale)
    register_provider_adapter(
        "codex_chat_completion",
        CODEX_PROVIDER_DESC,
        default_config_tmpl=dict(CODEX_CONFIG_TMPL),
        provider_display_name="OpenAI Codex 订阅",
    )(ProviderCodex)


def _unregister_codex_provider() -> None:
    """Remove this module's provider registration without touching replacements."""
    current = provider_cls_map.get("codex_chat_completion")
    if current is None or current.cls_type is not ProviderCodex:
        return
    provider_cls_map.pop("codex_chat_completion", None)
    if current in provider_registry:
        provider_registry.remove(current)


_register_codex_provider()
