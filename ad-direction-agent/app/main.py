"""广告方向决策子智能体 — FastAPI 应用入口"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.config.settings import settings

# 导入规则模块（触发 @validation_rule 装饰器注册规则）
import app.rules.push_natural  # noqa: F401
import app.rules.expand_keywords  # noqa: F401
import app.rules.optimize_acos  # noqa: F401
import app.rules.balance_maintain  # noqa: F401
import app.rules.cross_tag  # noqa: F401

from app.rules import validate_registry

# 启动时校验规则注册表
validate_registry()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="广告方向决策子智能体 API — 标签驱动型广告辅助决策系统",
)


@app.on_event("shutdown")
async def shutdown():
    from app.llm.client import deepseek_client
    await deepseek_client.close()


# CORS — 允许本地 demo 页面跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 API 路由 — 旧端点（legacy）
from app.api.recommend import router as recommend_router
from app.api.validate import router as validate_router
from app.api.confirm import router as confirm_router
from app.api.llm import router as llm_router

# 注册 API 路由 — 新四层工作流
from app.api.strategy import router as strategy_router
from app.api.tactics import router as tactics_router
from app.api.diagnosis import router as diagnosis_router
from app.api.execution import router as execution_router
from app.api.wizard import router as wizard_router
from app.api.long_term_config import router as lt_config_router

API_PREFIX = "/api/v1/agent/ad-direction"
app.include_router(recommend_router, prefix=API_PREFIX, tags=["推荐(legacy)"])
app.include_router(validate_router, prefix=API_PREFIX, tags=["校验(legacy)"])
app.include_router(confirm_router, prefix=API_PREFIX, tags=["确认(legacy)"])
app.include_router(llm_router, prefix=API_PREFIX, tags=["LLM"])

# 新工作流端点
app.include_router(strategy_router, prefix=API_PREFIX, tags=["1.1 战略层"])
app.include_router(tactics_router, prefix=API_PREFIX, tags=["1.2 策略层"])
app.include_router(diagnosis_router, prefix=API_PREFIX, tags=["1.3 诊断层"])
app.include_router(execution_router, prefix=API_PREFIX, tags=["1.4 执行层"])
app.include_router(wizard_router, prefix=API_PREFIX, tags=["向导"])
app.include_router(lt_config_router, prefix=API_PREFIX, tags=["长期配置"])

# 反馈端点
from app.api.feedback import router as feedback_router
app.include_router(feedback_router, prefix=API_PREFIX, tags=["反馈"])

# AI 对话端点
from app.api.chat import router as chat_router
app.include_router(chat_router, prefix=API_PREFIX, tags=["AI对话"])


@app.get("/health")
async def health():
    return {"status": "ok"}

# 挂载 demo 目录，供前端页面直接通过 http 访问（避免 file:// 跨域问题）
DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"
if DEMO_DIR.exists():
    app.mount("/demo", StaticFiles(directory=str(DEMO_DIR), html=True), name="demo")
