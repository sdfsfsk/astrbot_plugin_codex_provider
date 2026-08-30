"""Self-contained OpenAI Codex OAuth login and credential persistence.

The plugin uses OpenAI's headless device-authorization flow and stores the
refreshable credential under AstrBot's plugin-data directory. Credential writes
are versioned, encrypted for the current user with Windows DPAPI or owner-only
on POSIX, serialized with a file lock, flushed, and atomically replaced.
"""

import asyncio
import base64
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock

from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
)

CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_DEVICE_USER_CODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
CODEX_DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
CODEX_DEVICE_VERIFICATION_URL = "https://auth.openai.com/codex/device"
CODEX_DEVICE_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
CODEX_DEVICE_CODE_TIMEOUT_SECONDS = 15 * 60
AUTH_STORE_VERSION = 1
_PLUGIN_DATA_DIRNAME = "astrbot_plugin_codex_provider"
_AUTH_STORE_FILENAME = "codex_auth.json"


def token_fingerprint(token: str) -> str:
    """Return a non-secret stable fingerprint for OAuth-copy ownership."""
    return hashlib.sha256(token.encode()).hexdigest()


def _plugin_data_dir() -> Path:
    """Return the canonical plugin data directory and migrate the legacy path."""
    data_dir = Path(get_astrbot_plugin_data_path()) / _PLUGIN_DATA_DIRNAME
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        data_dir.chmod(0o700)
    legacy = Path(get_astrbot_data_path()) / _PLUGIN_DATA_DIRNAME / _AUTH_STORE_FILENAME
    target = data_dir / _AUTH_STORE_FILENAME
    if legacy.is_file() and target.exists():
        raise RuntimeError(
            "同时发现新旧两份 Codex 登录凭据，请备份后删除旧版凭据文件。"
        )
    if legacy.is_file():
        try:
            legacy.replace(target)
            if os.name != "nt":
                target.chmod(0o600)
        except FileNotFoundError:
            if not target.exists():
                raise
        except OSError as e:
            raise RuntimeError(
                "无法迁移旧版 Codex 登录凭据，请检查数据目录权限。"
            ) from e
    return data_dir


def _auth_store_path() -> Path:
    """Return the canonical OAuth credential document path."""
    return _plugin_data_dir() / _AUTH_STORE_FILENAME


def _auth_store_lock(path: Path) -> FileLock:
    """Return the cross-process lock associated with the credential document."""
    return FileLock(str(path.with_suffix(".lock")), timeout=10)


def _parse_auth_store(value: Any) -> dict:
    """Validate a versioned or legacy credential document without echoing it."""
    if not isinstance(value, dict):
        raise ValueError("Codex 登录凭据文件必须包含 JSON 对象。")
    version = value.get("version", AUTH_STORE_VERSION)
    if version != AUTH_STORE_VERSION:
        raise ValueError(f"不支持的 Codex 登录凭据版本：{version}")
    allowed = {
        "version",
        "access_token",
        "refresh_token",
        "expires_at",
        "managed_token_hashes",
    }
    if any(key not in allowed for key in value):
        raise ValueError("Codex 登录凭据文件包含未知字段。")
    access_token = value.get("access_token")
    refresh_token = value.get("refresh_token")
    expires_at = value.get("expires_at")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Codex 登录凭据缺少访问令牌。")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError("Codex 登录凭据缺少刷新令牌。")
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int | float)
        or not float(expires_at) > 0
    ):
        raise ValueError("Codex 登录凭据包含无效的过期时间。")
    managed_hashes = value.get(
        "managed_token_hashes",
        [token_fingerprint(access_token)],
    )
    if not isinstance(managed_hashes, list) or any(
        not isinstance(item, str)
        or len(item) != 64
        or any(char not in "0123456789abcdef" for char in item)
        for item in managed_hashes
    ):
        raise ValueError("Codex 登录凭据包含无效的 OAuth token 指纹。")
    return {
        "version": AUTH_STORE_VERSION,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(expires_at),
        "managed_token_hashes": sorted(
            set(managed_hashes) | {token_fingerprint(access_token)}
        ),
    }


def _windows_dpapi_protect(data: bytes) -> bytes:
    """Encrypt credential bytes for the current Windows user via DPAPI."""
    try:
        import win32crypt
    except ImportError as e:
        raise RuntimeError(
            "Windows 缺少 pywin32，无法安全保存 Codex OAuth 凭据。"
        ) from e
    try:
        return win32crypt.CryptProtectData(
            data,
            "AstrBot Codex OAuth",
            None,
            None,
            None,
            0x01,
        )
    except Exception as e:
        raise RuntimeError("Windows DPAPI 加密 Codex 凭据失败。") from e


def _windows_dpapi_unprotect(data: bytes) -> bytes:
    """Decrypt credential bytes for the current Windows user via DPAPI."""
    try:
        import win32crypt
    except ImportError as e:
        raise RuntimeError("Windows 缺少 pywin32，无法读取 Codex OAuth 凭据。") from e
    try:
        return win32crypt.CryptUnprotectData(data, None, None, None, 0x01)[1]
    except Exception as e:
        raise RuntimeError(
            "Windows DPAPI 无法解密 Codex 凭据；凭据可能属于其他用户。"
        ) from e


def _serialize_auth_store(store: dict) -> str:
    """Serialize plaintext on POSIX and current-user DPAPI ciphertext on Windows."""
    inner = json.dumps(store, ensure_ascii=False, separators=(",", ":")).encode()
    if os.name != "nt":
        return json.dumps(store, ensure_ascii=False, indent=2) + "\n"
    protected = _windows_dpapi_protect(inner)
    document = {
        "version": AUTH_STORE_VERSION,
        "protection": "windows-dpapi-current-user",
        "payload": base64.b64encode(protected).decode("ascii"),
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _decode_auth_document(value: Any) -> tuple[dict, bool]:
    """Decode one store and report whether Windows plaintext needs migration."""
    if isinstance(value, dict) and value.get("protection") is not None:
        if os.name != "nt":
            raise RuntimeError("当前系统无法解密 Windows DPAPI Codex 凭据。")
        if (
            value.get("version") != AUTH_STORE_VERSION
            or value.get("protection") != "windows-dpapi-current-user"
            or not isinstance(value.get("payload"), str)
        ):
            raise ValueError("Codex DPAPI 凭据文件格式异常。")
        try:
            protected = base64.b64decode(value["payload"], validate=True)
            inner = json.loads(_windows_dpapi_unprotect(protected))
        except (ValueError, json.JSONDecodeError) as e:
            raise ValueError("Codex DPAPI 凭据内容格式异常。") from e
        return _parse_auth_store(inner), False
    return _parse_auth_store(value), os.name == "nt"


def _read_auth_store_unlocked(path: Path) -> dict:
    """Read one credential document while the caller owns its file lock."""
    if not path.exists():
        return {}
    if not path.is_file():
        raise RuntimeError("Codex 登录凭据路径不是普通文件。")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise PermissionError(
            "Codex 登录凭据权限过宽，请将文件权限设置为仅当前用户可读写。"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError("Codex 登录凭据文件不是有效 JSON。") from e
    except OSError as e:
        raise RuntimeError("无法读取 Codex 登录凭据文件。") from e
    store, needs_migration = _decode_auth_document(value)
    if needs_migration:
        _write_auth_store_unlocked(path, store)
    return store


def _write_auth_store_unlocked(path: Path, store: dict) -> None:
    """Atomically write one validated store while the caller owns its lock."""
    payload = _serialize_auth_store(store)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    tmp = Path(tmp_name)
    try:
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


def load_auth_store() -> dict:
    """Load and strictly validate the persisted Codex OAuth credential."""
    path = _auth_store_path()
    with _auth_store_lock(path):
        return _read_auth_store_unlocked(path)


def save_auth_store(store: dict) -> None:
    """Validate and atomically persist the Codex OAuth credential."""
    normalized = _parse_auth_store(store)
    path = _auth_store_path()
    with _auth_store_lock(path):
        _write_auth_store_unlocked(path, normalized)


def compare_and_save_auth_store(
    expected_access_token: str,
    expected_refresh_token: str,
    store: dict,
) -> bool:
    """Commit refreshed credentials only if the source generation is unchanged."""
    normalized = _parse_auth_store(store)
    path = _auth_store_path()
    with _auth_store_lock(path):
        current = _read_auth_store_unlocked(path)
        if (
            current.get("access_token") != expected_access_token
            or current.get("refresh_token") != expected_refresh_token
        ):
            return False
        _write_auth_store_unlocked(path, normalized)
        return True


def add_managed_token_hashes(
    expected_access_token: str,
    token_hashes: set[str],
) -> bool:
    """Remember historical OAuth config copies for selective future cleanup."""
    path = _auth_store_path()
    with _auth_store_lock(path):
        current = _read_auth_store_unlocked(path)
        if current.get("access_token") != expected_access_token:
            return False
        current["managed_token_hashes"] = sorted(
            set(current.get("managed_token_hashes", [])) | token_hashes
        )
        _write_auth_store_unlocked(path, _parse_auth_store(current))
        return True


def clear_auth_store() -> None:
    """Remove the persisted Codex OAuth credential under the store lock."""
    path = _auth_store_path()
    with _auth_store_lock(path):
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as e:
            raise RuntimeError("无法删除 Codex 登录凭据文件。") from e


def tokens_to_store(
    tokens: dict,
    *,
    previous_refresh_token: str = "",
    managed_token_hashes: set[str] | None = None,
) -> dict:
    """Normalize and validate a token endpoint response for persistence.

    Args:
        tokens: Parsed token response with access_token/refresh_token/expires_in.
        previous_refresh_token: Existing refresh token retained when a refresh
            response does not rotate it.
        managed_token_hashes: Historical OAuth access-token fingerprints retained
            for selective cleanup of old provider configuration copies.

    Returns:
        A validated versioned credential document.

    Raises:
        ValueError: If required token fields are absent or malformed.
    """
    if not isinstance(tokens, dict):
        raise ValueError("Codex 令牌响应格式异常。")
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token") or previous_refresh_token
    expires_in = tokens.get("expires_in")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Codex 令牌响应缺少访问令牌。")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError("Codex 令牌响应缺少刷新令牌。")
    try:
        expires_seconds = float(expires_in)
    except (TypeError, ValueError) as e:
        raise ValueError("Codex 令牌响应包含无效的有效期。") from e
    if not expires_seconds > 0:
        raise ValueError("Codex 令牌响应包含无效的有效期。")
    return {
        "version": AUTH_STORE_VERSION,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(time.time() + expires_seconds),
        "managed_token_hashes": sorted(
            set(managed_token_hashes or set()) | {token_fingerprint(access_token)}
        ),
    }


async def start_device_auth(proxy: str | None) -> dict:
    """Request a device code for the Codex login flow.

    Args:
        proxy: Optional HTTP/SOCKS proxy.

    Returns:
        Device authorization id, user code, and polling interval.

    Raises:
        RuntimeError: If the provider rejects or malforms the response.
    """
    async with httpx.AsyncClient(proxy=proxy, timeout=30) as client:
        resp = await client.post(
            CODEX_DEVICE_USER_CODE_URL,
            json={"client_id": CODEX_OAUTH_CLIENT_ID},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"获取设备码失败（HTTP {resp.status_code}）。")
    try:
        data = resp.json()
    except ValueError as e:
        raise RuntimeError("设备码响应不是有效 JSON。") from e
    if (
        not isinstance(data, dict)
        or not data.get("device_auth_id")
        or not data.get("user_code")
    ):
        raise RuntimeError("设备码响应格式异常。")
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
    authorization_code: str,
    code_verifier: str,
    proxy: str | None,
) -> dict:
    """Exchange a device-flow authorization code for validated OAuth tokens."""
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
        raise RuntimeError(f"令牌交换失败（HTTP {resp.status_code}）。")
    try:
        tokens = resp.json()
        tokens_to_store(tokens)
    except (ValueError, TypeError) as e:
        raise RuntimeError("令牌交换响应格式异常。") from e
    return tokens


async def poll_device_auth(device: dict, proxy: str | None) -> dict:
    """Poll the device authorization endpoint until authorization completes."""
    interval = device["interval"]
    deadline = time.monotonic() + CODEX_DEVICE_CODE_TIMEOUT_SECONDS
    async with httpx.AsyncClient(proxy=proxy, timeout=30) as client:
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            resp = await client.post(
                CODEX_DEVICE_TOKEN_URL,
                json={
                    "device_auth_id": device["device_auth_id"],
                    "user_code": device["user_code"],
                },
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError as e:
                    raise RuntimeError("设备授权响应不是有效 JSON。") from e
                if (
                    not isinstance(data, dict)
                    or not data.get("authorization_code")
                    or not data.get("code_verifier")
                ):
                    raise RuntimeError("设备授权响应格式异常。")
                return await _exchange_device_code(
                    data["authorization_code"],
                    data["code_verifier"],
                    proxy,
                )
            if resp.status_code in (403, 404):
                continue
            error_code = ""
            try:
                error = resp.json().get("error")
                error_code = (
                    error.get("code", "") if isinstance(error, dict) else str(error)
                )
            except (AttributeError, ValueError):
                pass
            if error_code == "deviceauth_authorization_pending":
                continue
            if error_code == "slow_down":
                interval += 5
                continue
            raise RuntimeError(f"设备授权失败（HTTP {resp.status_code}）。")
    raise TimeoutError("等待授权超时（15 分钟），请重新 /codex_login")


async def refresh_access_token(refresh_token: str, proxy: str | None) -> dict:
    """Refresh an access token using a stored refresh token."""
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError("Codex 刷新令牌为空。")
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
            f"令牌刷新失败（HTTP {resp.status_code}），请重新 /codex_login。"
        )
    try:
        tokens = resp.json()
        tokens_to_store(tokens, previous_refresh_token=refresh_token)
    except (ValueError, TypeError) as e:
        raise RuntimeError("令牌刷新响应格式异常，请重新 /codex_login。") from e
    return tokens
