"""核心词离线判定 — 独立服务入口。和主 Campaign 服务（8010）隔离，不共享 worker。

用法:
  python start_core_keyword_server.py --port 8015 --workers 2 --no-reload
"""

import argparse
import os
import subprocess
import sys

from pathlib import Path
from fastapi import FastAPI

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = str(PROJECT_ROOT / "ad-direction-agent")

app = FastAPI(title="核心词判定服务")

from app.api.core_keyword import router as core_keyword_router  # noqa: E402
app.include_router(core_keyword_router, prefix="/api/v1/agent/ad-direction", tags=["核心词判定"])


def main():
    parser = argparse.ArgumentParser(description="启动核心词判定服务")
    parser.add_argument("--port", type=int, default=8015)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--reload", action="store_true", default=False)
    parser.add_argument("--no-reload", dest="reload", action="store_false")
    args = parser.parse_args()

    os.environ.setdefault("CORE_KEYWORD_ANALYZE_ENABLED", "true")

    reload_flag = "--reload" if args.reload else ""
    workers_flag = f"--workers {args.workers}"
    cmd = (
        f"uvicorn start_core_keyword_server:app "
        f"--host {args.host} --port {args.port} "
        f"--log-level info "
        f"{workers_flag} "
        f"{reload_flag}"
    ).strip()

    print(f"[+] 核心词判定服务: {cmd}")
    proc = subprocess.Popen([sys.executable, "-m"] + cmd.split(), cwd=SCRIPT_DIR)
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n[*] 停止...")
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
