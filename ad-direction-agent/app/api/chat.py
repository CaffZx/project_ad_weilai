"""AI 对话接口 — SSE 流式输出"""

import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.llm.client import deepseek_client

router = APIRouter()

CHAT_SYSTEM_PROMPT = """你是一个 Amazon 广告优化助手。用户正在查看广告数据面板，以下是当前页面的数据上下文：

{context}

请基于这些数据回答用户的问题。要求：
- 回答简洁、有针对性，直接给出可操作的建议
- 引用具体数据指标来支撑你的分析
- 如果数据不足以回答，明确说明需要哪些额外信息
- 使用中文回答"""


class ChatRequest(BaseModel):
    asin: str
    message: str
    context: dict = {}


@router.post("/chat")
async def chat_stream(req: ChatRequest):
    context_str = json.dumps(req.context, ensure_ascii=False, indent=2)
    system_prompt = CHAT_SYSTEM_PROMPT.format(context=context_str)

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
