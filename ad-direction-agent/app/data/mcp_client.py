"""MCP Streamable HTTP client (JSON-RPC over POST /mcp)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from app.config.settings import settings

logger = logging.getLogger(__name__)


class McpClientError(RuntimeError):
    """MCP protocol or tool-level failure."""


def _auth_headers() -> dict[str, str]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    token = settings.mcp_gateway_token
    if not token:
        return headers
    name = settings.mcp_gateway_header_name
    if name.lower() == "authorization":
        headers["Authorization"] = f"Bearer {token}"
    else:
        headers[name] = token
    return headers


def _next_id() -> int:
    if not hasattr(_next_id, "_counter"):
        _next_id._counter = 0  # type: ignore[attr-defined]
    _next_id._counter += 1  # type: ignore[attr-defined]
    return _next_id._counter  # type: ignore[attr-defined]


def parse_jsonrpc_body(text: str) -> dict[str, Any]:
    """Parse JSON body or SSE `data:` lines into one JSON-RPC object."""
    stripped = text.strip()
    if not stripped:
        raise McpClientError("MCP 响应为空")
    if stripped.startswith("{"):
        return json.loads(stripped)
    messages: list[dict[str, Any]] = []
    for line in stripped.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                messages.append(json.loads(payload))
    if not messages:
        raise McpClientError(f"无法解析 MCP SSE 响应: {text[:300]}")
    # Prefer last message with matching id or any result/error.
    for msg in reversed(messages):
        if "result" in msg or "error" in msg:
            return msg
    return messages[-1]


def unwrap_tool_payload(result: dict[str, Any]) -> Any:
    """Extract tool return value from tools/call result."""
    if "error" in result:
        err = result["error"]
        raise McpClientError(err.get("message") or str(err))
    body = result.get("result") or {}
    if body.get("isError"):
        texts = []
        for block in body.get("content") or []:
            if isinstance(block, dict) and block.get("text"):
                texts.append(str(block["text"]))
        raise McpClientError("; ".join(texts) or "MCP 工具返回错误")
    structured = body.get("structuredContent")
    if structured is not None:
        return structured
    chunks: list[Any] = []
    for block in body.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text") is not None:
            text = str(block["text"]).strip()
            if not text:
                continue
            try:
                chunks.append(json.loads(text))
            except json.JSONDecodeError:
                chunks.append(text)
        elif "data" in block:
            chunks.append(block["data"])
    if not chunks:
        return body
    if len(chunks) == 1:
        return chunks[0]
    return chunks


class StreamableHttpMcpInvoker:
    """MCP Streamable HTTP transport with session reuse."""

    def __init__(self) -> None:
        if not settings.mcp_gateway_url:
            raise RuntimeError("MCP 网关未配置: mcp_gateway_url")
        self._endpoint = settings.mcp_gateway_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=settings.mcp_timeout)
        self._session_id: str | None = None
        self._init_lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        headers = _auth_headers()
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    async def _post(self, message: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.post(
            self._endpoint,
            headers=self._headers(),
            json=message,
        )
        # notifications may return 202 with empty body
        if resp.status_code == 202:
            return {}
        if resp.is_error and resp.status_code not in (200,):
            resp.raise_for_status()
        if not resp.text.strip():
            return {}
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        parsed = parse_jsonrpc_body(resp.text)
        if "error" in parsed:
            err = parsed["error"]
            raise McpClientError(err.get("message") or str(err))
        return parsed

    async def _ensure_initialized(self) -> None:
        if self._session_id:
            return
        async with self._init_lock:
            if self._session_id:
                return
            init = {
                "jsonrpc": "2.0",
                "id": _next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "ad-direction-agent",
                        "version": settings.app_version,
                    },
                },
            }
            await self._post(init)
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            if not self._session_id:
                raise McpClientError("MCP 初始化未返回 mcp-session-id")

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        await self._ensure_initialized()
        req = {
            "jsonrpc": "2.0",
            "id": _next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        parsed = await self._post(req)
        if not parsed:
            raise McpClientError(f"MCP 工具无响应: {tool_name}")
        return unwrap_tool_payload(parsed)

    async def aclose(self) -> None:
        await self._client.aclose()
