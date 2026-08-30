"""Credential storage, endpoint policy, and refresh-coordination tests."""

import asyncio
import base64
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_codex_provider import codex_auth, codex_source


def _access_token(account_id: str, exp: int) -> str:
    """Build a non-secret JWT-shaped credential for tests."""

    def encode(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return (
        f"{encode({'alg': 'none'})}."
        f"{encode({'exp': exp, 'https://api.openai.com/auth': {'chatgpt_account_id': account_id}})}."
        "signature"
    )


def _provider(token: str):
    provider = codex_source.ProviderCodex.__new__(codex_source.ProviderCodex)
    provider.api_keys = [token]
    provider.chosen_api_key = token
    provider.client = SimpleNamespace(api_key=token)
    provider.provider_config = {"proxy": None}
    return provider


def test_auth_store_is_versioned_and_uses_plugin_data(tmp_path, monkeypatch) -> None:
    """Credentials must not depend on the process working directory."""
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    store = {
        "version": codex_auth.AUTH_STORE_VERSION,
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
        "expires_at": int(time.time()) + 3600,
        "managed_token_hashes": [codex_auth.token_fingerprint("access-secret")],
    }

    codex_auth.save_auth_store(store)

    path = codex_auth._auth_store_path()
    assert path == (
        tmp_path
        / "data"
        / "plugin_data"
        / "astrbot_plugin_codex_provider"
        / "codex_auth.json"
    )
    assert codex_auth.load_auth_store() == store
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    else:
        raw = path.read_text(encoding="utf-8")
        assert "windows-dpapi-current-user" in raw
        assert "access-secret" not in raw
        assert "refresh-secret" not in raw
    codex_auth.clear_auth_store()
    assert codex_auth.load_auth_store() == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI migration")
def test_windows_plaintext_store_is_migrated_to_dpapi(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    store = {
        "version": codex_auth.AUTH_STORE_VERSION,
        "access_token": "legacy-access",
        "refresh_token": "legacy-refresh",
        "expires_at": int(time.time()) + 3600,
        "managed_token_hashes": [codex_auth.token_fingerprint("legacy-access")],
    }
    path = codex_auth._auth_store_path()
    path.write_text(json.dumps(store), encoding="utf-8")

    assert codex_auth.load_auth_store() == store
    raw = path.read_text(encoding="utf-8")
    assert "windows-dpapi-current-user" in raw
    assert "legacy-access" not in raw
    assert "legacy-refresh" not in raw


def test_token_response_requires_complete_positive_fields() -> None:
    with pytest.raises(ValueError, match="访问令牌"):
        codex_auth.tokens_to_store({"refresh_token": "r", "expires_in": 3600})
    with pytest.raises(ValueError, match="刷新令牌"):
        codex_auth.tokens_to_store({"access_token": "a", "expires_in": 3600})
    with pytest.raises(ValueError, match="有效期"):
        codex_auth.tokens_to_store(
            {"access_token": "a", "refresh_token": "r", "expires_in": 0}
        )


def test_auth_store_rejects_malformed_documents(tmp_path, monkeypatch) -> None:
    """Malformed token files must fail closed instead of looking signed out."""
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    path = codex_auth._auth_store_path()
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON 对象"):
        codex_auth.load_auth_store()


@pytest.mark.parametrize(
    "value",
    [
        "http://chatgpt.com/backend-api/codex",
        "https://user@chatgpt.com/backend-api/codex",
        "https://chatgpt.com:444/backend-api/codex",
        "https://127.0.0.1/backend-api/codex",
        "https://chatgpt.com/backend-api/codex?next=evil",
        "https://evil.example/backend-api/codex",
    ],
)
def test_oauth_api_base_rejects_non_official_destinations(value) -> None:
    """OAuth bearer tokens must never follow a configurable destination."""
    with pytest.raises(ValueError, match="仅允许访问官方地址"):
        codex_source._validated_codex_api_base(value)


def test_oauth_api_base_accepts_normalized_official_destination() -> None:
    assert (
        codex_source._validated_codex_api_base(
            "HTTPS://CHATGPT.COM:443/backend-api/codex/"
        )
        == codex_source.CODEX_DEFAULT_API_BASE
    )


def test_provider_error_detail_redacts_credentials() -> None:
    detail = codex_source._safe_provider_detail(
        '{"access_token":"opaque-access","refresh_token":"opaque-refresh",'
        '"message":"Bearer opaque-bearer"}'
    )
    assert "opaque-access" not in detail
    assert "opaque-refresh" not in detail
    assert "opaque-bearer" not in detail


def test_jwt_decoder_rejects_non_object_payload() -> None:
    token = (
        base64.urlsafe_b64encode(b"{}").decode().rstrip("=")
        + "."
        + base64.urlsafe_b64encode(b"[]").decode().rstrip("=")
        + ".signature"
    )
    assert codex_source.decode_codex_token_payload(token) is None
    assert codex_source.extract_codex_account_id(token) is None
    assert codex_source.codex_token_expiry(token) is None


@pytest.mark.asyncio
async def test_provider_initialization_disables_sdk_retries(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    token = _access_token("account-a", int(time.time()) + 3600)
    provider = codex_source.ProviderCodex(
        {
            "id": "test",
            "type": "codex_chat_completion",
            "key": [token],
            "api_base": codex_source.CODEX_DEFAULT_API_BASE,
            "model": "gpt-5.6-sol",
            "proxy": "",
        },
        {},
    )
    try:
        assert provider.client.max_retries == 0
    finally:
        await provider.terminate()


@pytest.mark.asyncio
async def test_provider_marks_legacy_rotated_config_copy_as_oauth_managed(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    old_config = _access_token("account-a", int(time.time()) + 60)
    stored = _access_token("account-a", int(time.time()) + 3600)
    codex_auth.save_auth_store(
        {
            "version": codex_auth.AUTH_STORE_VERSION,
            "access_token": stored,
            "refresh_token": "refresh-a",
            "expires_at": int(time.time()) + 3600,
        }
    )
    provider = codex_source.ProviderCodex(
        {
            "id": "test",
            "type": "codex_chat_completion",
            "key": [old_config],
            "api_base": codex_source.CODEX_DEFAULT_API_BASE,
            "model": "gpt-5.6-sol",
            "proxy": "",
        },
        {},
    )
    try:
        assert provider._active_token() == stored
        managed = codex_auth.load_auth_store()["managed_token_hashes"]
        assert codex_auth.token_fingerprint(old_config) in managed
        assert codex_auth.token_fingerprint(stored) in managed
    finally:
        await provider.terminate()


@pytest.mark.asyncio
async def test_refresh_is_shared_and_adopted_by_all_instances(
    tmp_path,
    monkeypatch,
) -> None:
    """Concurrent model instances must rotate a refresh token only once."""
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    expired = _access_token("account-a", int(time.time()) - 10)
    refreshed = _access_token("account-a", int(time.time()) + 3600)
    codex_auth.save_auth_store(
        {
            "version": codex_auth.AUTH_STORE_VERSION,
            "access_token": expired,
            "refresh_token": "refresh-a",
            "expires_at": int(time.time()) - 10,
        }
    )
    refresh = AsyncMock(
        return_value={
            "access_token": refreshed,
            "refresh_token": "refresh-b",
            "expires_in": 3600,
        }
    )
    monkeypatch.setattr(codex_source, "refresh_access_token", refresh)
    first = _provider(expired)
    second = _provider(expired)

    await asyncio.gather(
        first._maybe_refresh_token(),
        second._maybe_refresh_token(),
    )

    refresh.assert_awaited_once_with("refresh-a", None)
    assert first._active_token() == refreshed
    assert second._active_token() == refreshed
    assert codex_auth.load_auth_store()["refresh_token"] == "refresh-b"


@pytest.mark.asyncio
async def test_inflight_refresh_cannot_resurrect_logged_out_store(
    tmp_path,
    monkeypatch,
) -> None:
    """CAS must discard a refresh response after logout deleted its source."""
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    expired = _access_token("account-a", int(time.time()) - 10)
    refreshed = _access_token("account-a", int(time.time()) + 3600)
    codex_auth.save_auth_store(
        {
            "version": codex_auth.AUTH_STORE_VERSION,
            "access_token": expired,
            "refresh_token": "refresh-a",
            "expires_at": int(time.time()) - 10,
        }
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_refresh(refresh_token, proxy):
        started.set()
        await release.wait()
        return {
            "access_token": refreshed,
            "refresh_token": "refresh-b",
            "expires_in": 3600,
        }

    monkeypatch.setattr(codex_source, "refresh_access_token", delayed_refresh)
    provider = _provider(expired)
    refresh_task = asyncio.create_task(provider._maybe_refresh_token())
    await started.wait()

    codex_auth.clear_auth_store()
    release.set()
    await refresh_task

    assert codex_auth.load_auth_store() == {}
    assert provider._active_token() == expired


@pytest.mark.asyncio
async def test_refresh_rejects_cross_account_store(tmp_path, monkeypatch) -> None:
    """A manual account token must not consume another account's refresh token."""
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    active = _access_token("account-a", int(time.time()) - 10)
    stored = _access_token("account-b", int(time.time()) - 10)
    codex_auth.save_auth_store(
        {
            "version": codex_auth.AUTH_STORE_VERSION,
            "access_token": stored,
            "refresh_token": "refresh-b",
            "expires_at": int(time.time()) - 10,
        }
    )
    refresh = AsyncMock()
    monkeypatch.setattr(codex_source, "refresh_access_token", refresh)
    provider = _provider(active)

    await provider._maybe_refresh_token()

    refresh.assert_not_awaited()
    assert provider._active_token() == active
