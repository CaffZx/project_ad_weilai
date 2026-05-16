"""DeepSeek API 异步客户端 — 多 Key 负载均衡"""

import json
import logging
from collections.abc import AsyncGenerator

import httpx

from app.config.settings import settings
from app.llm.key_pool import ApiKeyPool

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


def _build_key_pool() -> ApiKeyPool:
    cfg = settings.llm_config
    keys = cfg.get("api_keys", [])
    if not keys:
        single = cfg.get("api_key", "")
        keys = [single] if single else []
    if not keys:
        raise ValueError("未配置任何 DeepSeek API Key（DEEPSEEK_API_KEYS 或 DEEPSEEK_API_KEY）")
    return ApiKeyPool(keys)


key_pool = _build_key_pool()


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
    ) -> str:
        """调用 DeepSeek Chat API，自动轮询 Key + 失败重试"""
        if key_pool.key_count == 0:
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

        client = await self._ensure_client()
        last_exc = None

        for attempt in range(MAX_RETRIES):
            api_key = key_pool.next_key()
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
                    key_pool.mark_failed(api_key, status_code=resp.status_code)
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                    continue
                resp.raise_for_status()
                key_pool.mark_success(api_key)
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except httpx.TimeoutException as e:
                logger.warning("LLM 请求超时 (Key ...%s, 第%d次)", api_key[-8:], attempt + 1)
                last_exc = e
                continue
            except (httpx.ConnectError, httpx.RemoteProtocolError,
                    httpx.ReadError) as e:
                logger.warning("LLM 网络错误 (Key ...%s, 第%d次): %s", api_key[-8:], attempt + 1, e)
                key_pool.mark_failed(api_key, status_code=500)
                last_exc = e
                continue
            except httpx.HTTPStatusError:
                # 非 429/401/403 HTTP 错误（如 500）
                key_pool.mark_failed(api_key, status_code=500)
                continue

        raise last_exc or RuntimeError("LLM 调用失败：重试次数耗尽")

    async def chat_stream(
        self,
        messages: list[dict],
        temperature: float = 0.3,
    ) -> AsyncGenerator[str, None]:
        """流式调用 DeepSeek Chat API，逐 token yield"""
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }

        client = await self._ensure_client()

        for attempt in range(MAX_RETRIES):
            api_key = key_pool.next_key()
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
                        key_pool.mark_failed(api_key, status_code=resp.status_code)
                        await resp.aread()
                        continue
                    resp.raise_for_status()
                    key_pool.mark_success(api_key)
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
            except httpx.TimeoutException:
                logger.warning("LLM stream 超时 (Key ...%s, 第%d次)", api_key[-8:], attempt + 1)
                continue

        raise RuntimeError("LLM 流式调用失败：重试次数耗尽")


deepseek_client = DeepSeekClient()
