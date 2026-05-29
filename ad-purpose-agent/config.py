# ad-purpose-agent unified config entry
# 统一从 ad-direction-agent/.env 注入配置，purpose 不再维护独立的 .env

import os
from pathlib import Path

_sibling_env = Path(__file__).resolve().parent.parent / "ad-direction-agent" / ".env"
if _sibling_env.exists():
    with open(_sibling_env, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip("\"'")
                if key not in os.environ:
                    os.environ[key] = val

# -- LLM --
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

# -- Server --
SERVER_HOST = os.getenv("P1_SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("P1_SERVER_PORT", "8080"))

# -- 兄弟项目路径（ad-direction-agent 统一数据层） --
SIBLING_PROJECT = os.getenv("SIBLING_PROJECT", "ad-direction-agent")

# -- 广告目的中文映射（集中定义，供 main.py / ai_skills.py 共用） --
TARGET_TYPE_MAP = {
    "Traffic": "流量获取",
    "Conversion": "转化收割",
    "Ranking": "排名卡位",
    "Profit": "利润守卫",
    "Clearance": "清仓止损",
}
