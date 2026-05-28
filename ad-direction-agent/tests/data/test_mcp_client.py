from __future__ import annotations

import pytest

from app.data.mcp_client import McpClientError, parse_jsonrpc_body, unwrap_tool_payload


def test_parse_sse_tool_response():
    body = (
        "id:abc\n"
        "event:message\n"
        'data:{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"[{\\"asin\\":\\"B1\\"}]"}]}}'
    )
    msg = parse_jsonrpc_body(body)
    rows = unwrap_tool_payload(msg)
    assert rows == [{"asin": "B1"}]


def test_unwrap_tool_error():
    with pytest.raises(McpClientError):
        unwrap_tool_payload(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "parent_asin and parent_seller_sku required"}],
                    "isError": True,
                },
            }
        )
