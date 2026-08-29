"""Self-contained OpenAI Codex OAuth login for the plugin.

Implements the Codex device-authorization flow so users can obtain an access
token without installing the Codex CLI and without any local callback port
(the ChatGPT-registered localhost port may sit inside a reserved port range
on Windows). Also persists the refresh token and refreshes expired access
tokens automatically. Mirrors pi-ai's ``auth/oauth/openai-codex`` module.
"""

import asyncio
import json
import time
from pathlib import Path

import httpx

CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_DEVICE_USER_CODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
CODEX_DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
CODEX_DEVICE_VERIFICATION_URL = "https://auth.openai.com/codex/device"
CODEX_DEVICE_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
CODEX_DEVICE_CODE_TIMEOUT_SECONDS = 15 * 60


def _auth_store_path() -> Path:
    """Return the auth store path inside the plugin data directory."""
    data_dir = Path("data/astrbot_plugin_codex_provider")
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "codex_auth.json"


def load_auth_store() -> dict:
    """Load the persisted Codex OAuth credential (empty dict when absent)."""
    try:
        return json.loads(_auth_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_auth_store(store: dict) -> None:
    """Persist the Codex OAuth credential atomically."""
    path = _auth_store_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def clear_auth_store() -> None:
    """Remove the persisted Codex OAuth credential."""
    try:
        _auth_store_path().unlink()
    except OSError:
        pass


def tokens_to_store(tokens: dict) -> dict:
    """Normalize a token endpoint response into the auth store shape.

    Args:
        tokens: Parsed token response with access_token/refresh_token/expires_in.

    Returns:
        The dict to persist.
    """
    expires_in = tokens.get("expires_in")
    return {
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(expires_in) if expires_in else None,
    }


async def start_device_auth(proxy: str | None) -> dict:
    """Request a device code for the Codex login flow.

    Args:
        proxy: Optional HTTP/SOCKS proxy (auth.openai.com needs one in some
            regions).

    Returns:
        Dict with ``device_auth_id``, ``user_code`` and ``interval``.

    Raises:
        RuntimeError: If the device code endpoint rejects the request.
    """
    async with httpx.AsyncClient(proxy=proxy, timeout=30) as client:
        resp = await client.post(
            CODEX_DEVICE_USER_CODE_URL,
            json={"client_id": CODEX_OAUTH_CLIENT_ID},
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"获取设备码失败（HTTP {resp.status_code}）：{resp.text[:200]}"
        )
    data = resp.json()
    if not data.get("device_auth_id") or not data.get("user_code"):
        raise RuntimeError(f"设备码响应格式异常：{resp.text[:200]}")
    try:
        interval = int(data.get("interval") or 5)
    except (TypeError, ValueError):
        interval = 5
    return {
        "device_auth_id": data["device_auth_id"],
        "user_code": data["user_code"],
        "interval": max(interval, 1),
    }


async def _exchange_device_code(
    authorization_code: str, code_verifier: str, proxy: str | None
) -> dict:
    """Exchange the device-flow authorization code for OAuth tokens."""
    async with httpx.AsyncClient(proxy=proxy, timeout=30) as client:
        resp = await client.post(
            CODEX_OAUTH_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CODEX_OAUTH_CLIENT_ID,
                "code": authorization_code,
                "code_verifier": code_verifier,
                "redirect_uri": CODEX_DEVICE_REDIRECT_URI,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"令牌交换失败（HTTP {resp.status_code}）：{resp.text[:200]}"
        )
    return resp.json()


async def poll_device_auth(device: dict, proxy: str | None) -> dict:
    """Poll the device authorization endpoint until the user authorizes.

    Args:
        device: The dict returned by :func:`start_device_auth`.
        proxy: Optional HTTP/SOCKS proxy.

    Returns:
        The final token response containing access_token/refresh_token.

    Raises:
        TimeoutError: If the user does not authorize within 15 minutes.
        RuntimeError: If the flow is rejected or the exchange fails.
    """
    interval = device["interval"]
    deadline = time.time() + CODEX_DEVICE_CODE_TIMEOUT_SECONDS
    async with httpx.AsyncClient(proxy=proxy, timeout=30) as client:
        while time.time() < deadline:
            await asyncio.sleep(interval)
            resp = await client.post(
                CODEX_DEVICE_TOKEN_URL,
                json={
                    "device_auth_id": device["device_auth_id"],
                    "user_code": device["user_code"],
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                if not data.get("authorization_code") or not data.get("code_verifier"):
                    raise RuntimeError(f"设备授权响应格式异常：{resp.text[:200]}")
                return await _exchange_device_code(
                    data["authorization_code"], data["code_verifier"], proxy
                )
            if resp.status_code in (403, 404):
                continue  # authorization still pending
            error_code = ""
            try:
                error = resp.json().get("error")
                error_code = (
                    error.get("code", "") if isinstance(error, dict) else str(error)
                )
            except ValueError:
                pass
            if error_code == "deviceauth_authorization_pending":
                continue
            if error_code == "slow_down":
                interval += 5
                continue
            raise RuntimeError(
                f"设备授权失败（HTTP {resp.status_code}）：{resp.text[:200]}"
            )
    raise TimeoutError("等待授权超时（15 分钟），请重新 /codex_login")


async def refresh_access_token(refresh_token: str, proxy: str | None) -> dict:
    """Refresh an access token using a stored refresh token.

    Args:
        refresh_token: The stored OAuth refresh token.
        proxy: Optional HTTP/SOCKS proxy for the token request.

    Returns:
        The parsed token response containing fresh tokens.

    Raises:
        RuntimeError: If the refresh is rejected (re-login required).
    """
    async with httpx.AsyncClient(proxy=proxy, timeout=30) as client:
        resp = await client.post(
            CODEX_OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CODEX_OAUTH_CLIENT_ID,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"令牌刷新失败（HTTP {resp.status_code}），请重新 /codex_login"
        )
    return resp.json()
