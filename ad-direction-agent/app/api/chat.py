"""AI 对话接口 — SSE 流式输出"""

import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.llm.client import deepseek_client
from app.llm.kb_loader import kb

router = APIRouter()

CHAT_SYSTEM_PROMPT = """你是 Amazon 广告优化助手。

## 业务术语与标签体系（理解用户提问时参考）
{kb_content}

## 当前页面数据上下文
{context}

回答要求：简洁、可操作、引用具体指标；不足时说明缺失；中文。
"""


class ChatRequest(BaseModel):
    asin: str
    message: str
    context: dict = {}


@router.post("/chat")
async def chat_stream(req: ChatRequest):
    context_str = json.dumps(req.context, ensure_ascii=False, indent=2)
    system_prompt = CHAT_SYSTEM_PROMPT.format(
        kb_content=kb.build("chat"), context=context_str
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": req.message},
    ]

    async def event_generator():
        try:
            async for token in deepseek_client.chat_stream(messages, temperature=0.3):
                data = json.dumps({"token": token}, ensure_ascii=False)
                yield f"data: {data}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            error_data = json.dumps({"error": str(e)}, ensure_ascii=False)
            yield f"data: {error_data}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
