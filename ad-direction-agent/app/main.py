"""广告方向决策子智能体 — FastAPI 应用入口"""

# ── Windows 事件循环修复（必须在任何 loop 创建之前）──────────────────────────
# ProactorEventLoop 下 asyncio.wait_for 取消不穿透 httpx 的 IOCP socket recv，
# 导致 LLM 调用超时不生效、高并发下信号量级联死锁。切 SelectorEventLoop 后取消正常。
import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
# ──────────────────────────────────────────────────────────────────────────────

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


@app.on_event("startup")
async def startup():
    import logging

    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(log_dir / "server.log", encoding="utf-8"),
            ],
        )

    from app.persistence.migrate_json_to_mysql import migrate_json_to_mysql

    n = migrate_json_to_mysql()
    if n:
        logging.getLogger(__name__).info("JSON → MySQL 迁移完成: %d ASIN", n)

    if settings.skills_enabled:
        from app.skills.validate import validate_all_skills

        validate_all_skills()


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

from app.api.llm import router as llm_router

# 四层工作流 API
from app.api.strategy import router as strategy_router
from app.api.tactics import router as tactics_router
from app.api.diagnosis import router as diagnosis_router
from app.api.execution import router as execution_router
from app.api.wizard import router as wizard_router
from app.api.long_term_config import router as lt_config_router

API_PREFIX = "/api/v1/agent/ad-direction"
app.include_router(llm_router, prefix=API_PREFIX, tags=["LLM"])

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

# Campaign 活动分析（调试）
from app.api.campaign import router as campaign_router
app.include_router(campaign_router, prefix=API_PREFIX, tags=["Campaign 活动分析"])

# 核心词判定（离线，7 天一次）
from app.api.core_keyword import router as core_keyword_router
app.include_router(core_keyword_router, prefix=API_PREFIX, tags=["核心词判定"])

# 决策批次管理
from app.api.decision import router as decision_router
app.include_router(decision_router, prefix=API_PREFIX, tags=["决策批次"])

# 版本公告
from app.api.announcements import router as announce_router
app.include_router(announce_router, prefix=API_PREFIX, tags=["公告"])

ANNOUNCE_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "announcements"
if ANNOUNCE_DIR.exists():
    app.mount("/announcements", StaticFiles(directory=str(ANNOUNCE_DIR)), name="announcements")


@app.get("/health")
async def health():
    return {"status": "ok"}

# 挂载 demo 目录，供前端页面直接通过 http 访问（避免 file:// 跨域问题）
DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"
if DEMO_DIR.exists():
    app.mount("/demo", StaticFiles(directory=str(DEMO_DIR), html=True), name="demo")
