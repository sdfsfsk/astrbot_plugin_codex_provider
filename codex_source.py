"""OpenAI Codex (ChatGPT subscription) provider adapter for AstrBot.

Talks to the ChatGPT Codex backend (``chatgpt.com/backend-api/codex``) with an
OAuth access token copied from the official Codex CLI login, instead of an
OpenAI Platform API key. Request/response handling reuses AstrBot's built-in
Responses API provider; this class only adds the Codex-specific auth headers,
payload quirks and the mandatory SSE streaming behavior.
"""

import base64
import binascii
import json
import time
from datetime import datetime, timezone
from typing import Literal

import httpx
from astrbot import logger
from astrbot.core.exceptions import EmptyModelOutputError
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.register import register_provider_adapter
from astrbot.core.provider.sources.openai_responses_source import (
    ProviderOpenAIResponses,
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

# Static catalog mirrored from the Codex CLI; the ChatGPT backend exposes no
# model listing endpoint, so discovery has to be hardcoded.
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
    "OpenAI Codex（ChatGPT 订阅）提供商适配器。Key 栏粘贴 Codex 访问令牌："
    "先在官方 Codex CLI 登录（登录/获取令牌过程建议全程挂代理），"
    "再从 ~/.codex/auth.json 复制 access_token 填入；令牌过期后需重新获取。"
    "默认代理 127.0.0.1:10808（v2rayN 混合端口），可在配置中修改或留空。"
)


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
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error):
        return None


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
        if not isinstance(used, int | float):
            return None
        seconds = window.get("limit_window_seconds")
        if isinstance(seconds, int | float) and seconds >= 86400:
            span = f"{seconds // 86400}天"
        elif isinstance(seconds, int | float):
            span = f"{seconds // 3600}小时"
        else:
            span = "未知周期"
        text = f"{title}（{span}）: 已用 {used:.0f}% · 剩余 {100 - used:.0f}%"
        reset = window.get("reset_at")
        if isinstance(reset, int | float):
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


@register_provider_adapter(
    "codex_chat_completion",
    CODEX_PROVIDER_DESC,
    default_config_tmpl={
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
        "custom_extra_body": {"reasoning_effort": "medium"},
    },
    provider_display_name="OpenAI Codex 订阅",
)
class ProviderCodex(ProviderOpenAIResponses):
    """ChatGPT Codex backend provider driven by a pasted OAuth access token."""

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        """Initialize the Codex provider with Codex-specific defaults.

        Args:
            provider_config: Provider source and model configuration.
            provider_settings: Global provider settings.
        """
        merged_config = dict(provider_config)
        merged_config.setdefault("api_base", CODEX_DEFAULT_API_BASE)
        merged_config.setdefault("model", CODEX_DEFAULT_MODEL)
        merged_config["custom_headers"] = {
            **CODEX_STATIC_HEADERS,
            **(merged_config.get("custom_headers") or {}),
        }
        super().__init__(merged_config, provider_settings)
        self._warn_token_state()

    def _warn_token_state(self) -> None:
        """Log a warning for malformed/expired tokens at startup."""
        for key in self.api_keys:
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

    def _codex_request_headers(self) -> dict[str, str]:
        """Build per-request headers derived from the currently selected key.

        Returns:
            Headers carrying the chatgpt-account-id matching the active token.
        """
        account_id = extract_codex_account_id(self.client.api_key or "")
        return {"chatgpt-account-id": account_id} if account_id else {}

    async def get_models(self) -> list[str]:
        """Return the static Codex model catalog (the backend has no list API).

        Returns:
            The hardcoded Codex model id list.
        """
        return list(CODEX_MODEL_CATALOG)

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
        payloads, context_query = await super()._prepare_chat_payload(*args, **kwargs)
        if not any(extract_codex_account_id(key) for key in self.api_keys):
            raise ValueError(
                "Codex 提供商的 Key 不是有效的访问令牌：请粘贴 Codex CLI 登录后 "
                "~/.codex/auth.json 中 eyJ 开头的 access_token 本体，"
                "而不是 account_id 等其他字段。"
            )
        payloads.setdefault("instructions", CODEX_DEFAULT_INSTRUCTIONS)
        payloads["include"] = ["reasoning.encrypted_content"]
        payloads["parallel_tool_calls"] = True
        return payloads, context_query

    async def _query_stream(
        self,
        payloads: dict,
        tools,
        *,
        request_max_retries: int | None = None,
    ):
        """Send a streaming request with the per-attempt account-id header.

        The header is recomputed on every call so key rotation picks up the
        account id matching the newly selected token.
        """
        payloads = dict(payloads)
        payloads["extra_headers"] = self._codex_request_headers()
        async for response in super()._query_stream(
            payloads,
            tools,
            request_max_retries=request_max_retries,
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
        token = self.chosen_api_key or (self.api_keys[0] if self.api_keys else "")
        if not token:
            raise ValueError("未配置 Codex 访问令牌，请先在提供商 Key 栏粘贴令牌。")
        account_id = extract_codex_account_id(token)
        if not account_id:
            raise ValueError(
                "无法从令牌解析 chatgpt-account-id，请确认粘贴的是 access_token 本体。"
            )

        api_base = (
            self.provider_config.get("api_base") or CODEX_DEFAULT_API_BASE
        ).rstrip("/")
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
        return resp.json()
