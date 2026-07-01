"""DeepSeek API 异步客户端 — 多 Key 负载均衡 + 服务级并发收口

本模块的单例 deepseek_client 是全服务唯一的 LLM 传输层：
- 1 个共享 httpx 连接池（reasoner 默认复用本单例，不再各自新建）
- 1 个进程级并发信号量（每个非流式 chat() 调用过闸）
- Key 轮询/冷却委托 ApiKeyPool
"""

import asyncio
import json
import logging
import random
import time
from collections.abc import AsyncGenerator

import httpx

from app.config.settings import settings
from app.llm.key_pool import ApiKeyPool

logger = logging.getLogger(__name__)

# LLM token 消耗 / 缓存命中 专用日志，独立写入 logs/llm_usage.log
# 每日轮转，保留 14 天历史
_usage_logger = logging.getLogger("llm.usage")
_usage_logger.propagate = False
_usage_handler: logging.Handler | None = None


def _ensure_usage_handler() -> None:
    global _usage_handler
    if _usage_handler is not None:
        return
    from logging.handlers import TimedRotatingFileHandler
    from pathlib import Path
    log_dir = Path(__file__).resolve().parents[3] / "logs"
    log_dir.mkdir(exist_ok=True)
    _usage_handler = TimedRotatingFileHandler(
        log_dir / "llm_usage.log", when="midnight", backupCount=14, encoding="utf-8",
    )
    _usage_handler.suffix = "%Y%m%d"
    _usage_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _usage_logger.addHandler(_usage_handler)
    _usage_logger.setLevel(logging.INFO)

# 重试总执行次数（首次 + 2 次重试）。重试前指数退避 + 抖动，打散高并发雷群。
MAX_RETRIES = 3

_key_pool: ApiKeyPool | None = None

# 服务级 LLM 并发信号量 —— 进程内，所有非流式 chat() 调用共用。
# 懒初始化：在运行的事件循环内首次取用时创建（避免 import 期无 loop 绑定）。
_llm_sem: asyncio.Semaphore | None = None


def _global_llm_sem() -> asyncio.Semaphore:
    global _llm_sem
    if _llm_sem is None:
        # 静态切分：每 worker 槽 = 总上限 // worker 数（NUM_WORKERS 须与 --workers 一致）。
        # 多 worker 下避免 N×总上限 把 DeepSeek 账号打爆。
        per_worker = max(1, settings.llm_global_concurrency // max(1, settings.num_workers))
        _llm_sem = asyncio.Semaphore(per_worker)
        logger.info(
            "LLM 并发闸初始化: 总上限=%d / worker数=%d → 本 worker 槽=%d",
            settings.llm_global_concurrency, settings.num_workers, per_worker,
        )
    return _llm_sem


def _retry_backoff(attempt: int) -> float:
    """重试退避秒数（attempt 从 1 起）：retry1≈0.5s、retry2≈1.0s，叠加 50–200ms 抖动。"""
    return 0.5 * attempt + random.uniform(0.05, 0.2)


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
            # 连接池显式配置（默认 max_connections=100，扛不住峰值 100+）；
            # 超时分离：连接快断（8s），读取给 LLM 生成留足（=llm_timeout）。
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=8.0, read=self.timeout, write=10.0, pool=5.0,
                ),
                limits=httpx.Limits(
                    max_connections=600,
                    max_keepalive_connections=50,
                    keepalive_expiry=30.0,
                ),
            )
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
        *,
        timeout_override: float | None = None,
        model: str | None = None,
        thinking: bool = False,
        reasoning_effort: str | None = None,
        label: str = "",
    ) -> str:
        """调用 DeepSeek Chat API，自动轮询 Key + 失败重试。
        timeout_override: 单次 HTTP 超时，不传则用实例默认值。"""
        pool = get_key_pool()
        if pool is None or pool.key_count == 0:
            return json.dumps({
                "overall_analysis": "LLM API 密钥未配置。",
                "direction_analyses": [],
                "action_priorities": [],
                "risk_warnings": ["LLM API 未配置"],
            })

        body = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            # 思考模式开关(DeepSeek OpenAI 格式)：强档调用点传 thinking=True
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if thinking and reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        if response_format:
            body["response_format"] = response_format
        if max_tokens:
            body["max_tokens"] = max_tokens

        client = await self._ensure_client()
        last_exc = None

        # 服务级并发闸：一个逻辑请求占一个槽，跨重试不释放（不加在 chat_stream）。
        async with _global_llm_sem():
            for attempt in range(MAX_RETRIES):
                if attempt > 0:
                    # 重试前指数退避 + 抖动，打散高并发雷群
                    await asyncio.sleep(_retry_backoff(attempt))
                api_key = pool.next_key()
                t0 = time.perf_counter()
                try:
                    resp = await client.post(
                        f"{self.base_url}/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        timeout=timeout_override or self.timeout,
                        json=body,
                    )
                    if resp.status_code in (429, 401, 403):
                        pool.mark_failed(api_key, status_code=resp.status_code)
                        last_exc = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}", request=resp.request, response=resp
                        )
                        continue
                    resp.raise_for_status()
                    latency = time.perf_counter() - t0
                    pool.mark_success(api_key, latency=latency)
                    data = resp.json()
                    # token 消耗 / 缓存命中：独立写 llm_usage.log
                    _ensure_usage_handler()
                    usage = data.get("usage", {})
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)
                    cached_tokens = usage.get("prompt_cache_hit_tokens", 0)
                    _usage_logger.info(
                        "%s prompt=%d completion=%d cached=%d total=%d model=%s %.1fs",
                        label or "unknown",
                        prompt_tokens, completion_tokens, cached_tokens,
                        prompt_tokens + completion_tokens,
                        model or self.model,
                        latency,
                    )
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
