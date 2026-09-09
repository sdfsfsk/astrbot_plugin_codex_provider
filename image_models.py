"""Discover image models from OpenAI's public catalog without OAuth headers."""

import asyncio
import re
import time

import httpx

from astrbot import logger

IMAGE_MODEL_CATALOG_URL = "https://developers.openai.com/api/docs/models"
IMAGE_MODEL_CACHE_SECONDS = 6 * 60 * 60
IMAGE_MODEL_RETRY_SECONDS = 5 * 60
IMAGE_MODEL_CATALOG_MAX_BYTES = 2 * 1024 * 1024
FALLBACK_IMAGE_MODELS = (
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst",
    "gpt-image-2",
)


def normalize_image_model(value: str) -> str:
    """Validate a manual model ID or the automatic selection setting."""
    if not isinstance(value, str):
        raise ValueError("图片模型必须填写 auto 或 gpt-image- 开头的模型 ID。")
    model = value.strip().lower() or "auto"
    if model != "auto" and (
        len(model) > 100 or not re.fullmatch(r"gpt-image-[a-z0-9][a-z0-9._-]*", model)
    ):
        raise ValueError(
            "图片模型必须填写 auto 或有效的 gpt-image- 模型 ID，"
            "例如 gpt-image-2.5-flare；不能填写对话模型。"
        )
    return model


def parse_image_model_catalog(html: str) -> list[str]:
    """Order stable image aliases by numeric generation, then official order."""
    models = dict.fromkeys(
        re.findall(
            r"href=[\"'](?:https://developers\.openai\.com)?"
            r"/api/docs/models/(gpt-image-[a-z0-9.-]+)/?[\"']",
            html,
        )
    )
    candidates = []
    for model in models:
        version = re.fullmatch(r"gpt-image-(\d+(?:\.\d+)*)(?:-[a-z][a-z0-9-]*)?", model)
        if version and not re.search(r"-\d{4}-\d{2}-\d{2}$", model):
            candidates.append((tuple(map(int, version.group(1).split("."))), model))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        raise ValueError("Official catalog did not contain stable image model links")
    return [model for _, model in candidates]


class ImageModelDiscovery:
    """Cache a public catalog and coalesce concurrent image discovery calls."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._models: list[str] = []
        self._next_refresh = 0.0
        self._source = "builtin"
        self._warning = ""

    async def get_catalog(
        self, proxy: str | None = None, *, force_refresh: bool = False
    ) -> dict:
        """Fetch the catalog, keeping stale or built-in choices on failure."""
        async with self._lock:
            if force_refresh or time.monotonic() >= self._next_refresh:
                try:
                    async with httpx.AsyncClient(
                        proxy=proxy or None, timeout=15, follow_redirects=False
                    ) as client:
                        async with client.stream(
                            "GET",
                            IMAGE_MODEL_CATALOG_URL,
                            headers={"accept": "text/html"},
                        ) as response:
                            response.raise_for_status()
                            content = bytearray()
                            async for chunk in response.aiter_bytes():
                                content.extend(chunk)
                                if len(content) > IMAGE_MODEL_CATALOG_MAX_BYTES:
                                    raise ValueError(
                                        "Official catalog exceeded size limit"
                                    )
                    self._models = parse_image_model_catalog(content.decode("utf-8"))
                except (httpx.HTTPError, ValueError) as exc:
                    # Log only error type/status; never echo a response or proxy URL.
                    detail = (
                        f"HTTP {exc.response.status_code}"
                        if isinstance(exc, httpx.HTTPStatusError)
                        else type(exc).__name__
                    )
                    self._source = "stale" if self._models else "builtin"
                    self._warning = f"在线图片目录获取失败（{detail}）"
                    self._next_refresh = time.monotonic() + IMAGE_MODEL_RETRY_SECONDS
                    logger.warning("[Codex] Image model discovery failed: %s", detail)
                else:
                    self._source = "official"
                    self._warning = ""
                    self._next_refresh = time.monotonic() + IMAGE_MODEL_CACHE_SECONDS
            return {
                "models": list(self._models or FALLBACK_IMAGE_MODELS),
                "source": self._source,
                "warning": self._warning,
            }


image_model_discovery = ImageModelDiscovery()


async def resolve_image_model(setting: str, proxy: str | None = None) -> str:
    """Resolve auto using the catalog; manual IDs never trigger discovery."""
    model = normalize_image_model(setting)
    if model != "auto":
        return model
    catalog = await image_model_discovery.get_catalog(proxy)
    return catalog["models"][0]
