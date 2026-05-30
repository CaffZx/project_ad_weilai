"""DeepSeek API 异步客户端 — 多 Key 负载均衡"""

import json
import logging
import time
from collections.abc import AsyncGenerator

import httpx

from app.config.settings import settings
from app.llm.key_pool import ApiKeyPool

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

_key_pool: ApiKeyPool | None = None


def _build_key_pool() -> ApiKeyPool:
    cfg = settings.llm_config
    keys = cfg.get("api_keys", [])
    if not keys:
        single = cfg.get("api_key", "")
        keys = [single] if single else []
    if not keys:
        raise ValueError("未配置任何 DeepSeek API Key（DEEPSEEK_API_KEYS 或 DEEPSEEK_API_KEY）")
    state_path = cfg.get("state_path")
    return ApiKeyPool(keys, state_path=state_path)


def get_key_pool() -> ApiKeyPool | None:
    """懒加载 Key 池单例；未配置 key 时返回 None，不在 import 时抛错"""
    global _key_pool
    if _key_pool is None:
        try:
            _key_pool = _build_key_pool()
            logger.info("KeyPool 已初始化: %d 个 key", _key_pool.key_count)
        except ValueError as e:
            logger.error("KeyPool 初始化失败: %s", e)
            return None
    return _key_pool


class DeepSeekClient:
    """DeepSeek 大模型 API 客户端

    集成 ApiKeyPool，每次请求轮询获取 Key，失败自动切换重试。
    """

    def __init__(self):
        cfg = settings.llm_config
        self.base_url = cfg.get("base_url", "https://api.deepseek.com")
        self.model = cfg.get("model", "deepseek-v4-flash")
        self.timeout = cfg.get("timeout", 30)
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        response_format: dict | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """调用 DeepSeek Chat API，自动轮询 Key + 失败重试"""
        pool = get_key_pool()
        if pool is None or pool.key_count == 0:
            return json.dumps({
                "overall_analysis": "LLM API 密钥未配置。",
                "direction_analyses": [],
                "action_priorities": [],
                "risk_warnings": ["LLM API 未配置"],
            })

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            body["response_format"] = response_format
        if max_tokens:
            body["max_tokens"] = max_tokens

        client = await self._ensure_client()
        last_exc = None

        for attempt in range(MAX_RETRIES):
            api_key = pool.next_key()
            t0 = time.perf_counter()
            try:
                resp = await client.post(
                    f"{self.base_url}/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                if resp.status_code in (429, 401, 403):
                    pool.mark_failed(api_key, status_code=resp.status_code)
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                    continue
                resp.raise_for_status()
                pool.mark_success(api_key, latency=time.perf_counter() - t0)
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except httpx.TimeoutException as e:
                logger.warning("LLM 请求超时 (Key ...%s, 第%d次)", api_key[-8:], attempt + 1)
                pool.mark_failed(api_key, status_code=500)
                last_exc = e
                continue
            except (httpx.ConnectError, httpx.RemoteProtocolError,
                    httpx.ReadError) as e:
                logger.warning("LLM 网络错误 (Key ...%s, 第%d次): %s", api_key[-8:], attempt + 1, e)
                pool.mark_failed(api_key, status_code=500)
                last_exc = e
                continue
            except httpx.HTTPStatusError as e:
                # 非 429/401/403 HTTP 错误（如 500）
                pool.mark_failed(api_key, status_code=500)
                last_exc = e
                continue

        raise last_exc or RuntimeError("LLM 调用失败：重试次数耗尽")

    async def chat_stream(
        self,
        messages: list[dict],
        temperature: float = 0.3,
    ) -> AsyncGenerator[str, None]:
        """流式调用 DeepSeek Chat API，逐 token yield"""
        pool = get_key_pool()
        if pool is None or pool.key_count == 0:
            raise RuntimeError("LLM API 密钥未配置")

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }

        client = await self._ensure_client()
        last_exc = None

        for attempt in range(MAX_RETRIES):
            api_key = pool.next_key()
            t0 = time.perf_counter()
            try:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=90,
                ) as resp:
                    if resp.status_code in (429, 401, 403):
                        pool.mark_failed(api_key, status_code=resp.status_code)
                        await resp.aread()
                        last_exc = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}", request=resp.request, response=resp
                        )
                        continue
                    resp.raise_for_status()
                    pool.mark_success(api_key, latency=time.perf_counter() - t0)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        if payload.strip() == "[DONE]":
                            return
                        try:
                            chunk = json.loads(payload)
                            delta = chunk["choices"][0].get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                yield token
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
                    return
            except httpx.TimeoutException as e:
                logger.warning("LLM stream 超时 (Key ...%s, 第%d次)", api_key[-8:], attempt + 1)
                pool.mark_failed(api_key, status_code=500)
                last_exc = e
                continue
            except (httpx.ConnectError, httpx.RemoteProtocolError,
                    httpx.ReadError) as e:
                logger.warning("LLM stream 网络错误 (Key ...%s, 第%d次): %s", api_key[-8:], attempt + 1, e)
                pool.mark_failed(api_key, status_code=500)
                last_exc = e
                continue
            except httpx.HTTPStatusError as e:
                pool.mark_failed(api_key, status_code=500)
                last_exc = e
                continue

        raise last_exc or RuntimeError("LLM 流式调用失败：重试次数耗尽")


deepseek_client = DeepSeekClient()
