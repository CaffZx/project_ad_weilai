"""启动脚本 — 一键启动所有服务

步骤:
  1. [可选] 启动 Docker 基础设施（MySQL + Redis）
  2. 检查目标端口是否被占用
  3. 启动 uvicorn

用法:
  python start_server.py                        # 仅启动 Python 服务（默认）
  python start_server.py --with-infra           # 一键启动：Docker 基础设施 + Python 服务
  python start_server.py --with-infra --all     # 完整 Docker Compose（含 agent 容器）
  python start_server.py --port 8010 --no-reload
  python start_server.py --port 8010 --no-reload --workers 4
  python start_server.py --data-source mock
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def find_pid_by_port(port: int) -> list[str]:
    """通过 netstat 查找占用指定端口的进程名和 PID（仅 Windows 开发机用）"""
    if sys.platform != "win32":
        return []
    try:
        output = subprocess.check_output(
            "netstat -ano",
            shell=True, text=True, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return []

    results = []
    for line in output.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            parts = line.strip().split()
            if parts:
                pid = parts[-1]
                if pid.isdigit():
                    results.append(pid)
    return results


def docker_compose_up(services: list[str] | None = None) -> bool:
    """启动 Docker Compose 服务，返回是否成功"""
    compose_file = PROJECT_ROOT / "docker-compose.yml"
    if not compose_file.exists():
        print(f"[SKIP] docker-compose.yml 未找到: {compose_file}")
        return False

    cmd = ["docker", "compose", "-f", str(compose_file), "up", "-d", "--wait"]
    if services:
        cmd.extend(services)
    else:
        # 等待 healthcheck 通过（--wait 需要 Docker Compose v2）
        pass

    print(f"[+] 启动 Docker 基础设施...")
    print(f"    {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        if result.returncode == 0:
            print(f"[OK] Docker 服务已就绪")
            if result.stdout.strip():
                print(result.stdout.strip())
            return True
        # --wait 可能不支持，降级为手动等待
        if "unknown flag" in result.stderr.lower() or result.returncode != 0:
            # 重试不带 --wait
            cmd_no_wait = [c for c in cmd if c != "--wait"]
            print(f"    (降级) {' '.join(cmd_no_wait)}")
            result2 = subprocess.run(cmd_no_wait, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
            if result2.returncode == 0:
                print(f"[OK] Docker 服务已启动，等待健康检查...")
                _wait_docker_healthy(services)
                return True
            print(f"[ERR] Docker Compose 失败:\n{result2.stderr}")
            return False
        print(f"[ERR] Docker Compose 失败:\n{result.stderr}")
        return False
    except FileNotFoundError:
        print("[SKIP] Docker 未安装或不在 PATH 中")
        return False


def _wait_docker_healthy(services: list[str] | None = None, timeout: int = 60):
    """轮询等待 Docker 服务 healthy"""
    compose_file = PROJECT_ROOT / "docker-compose.yml"
    svc_list = services or []
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "ps", "--format", "json"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            healthy = True
            for line in result.stdout.strip().split("\n"):
                try:
                    import json
                    svc = json.loads(line)
                    name = svc.get("Service", "")
                    if svc_list and name not in svc_list:
                        continue
                    if svc.get("Health") not in ("healthy", ""):
                        healthy = False
                        break
                except (json.JSONDecodeError, KeyError):
                    pass
            if healthy:
                print("[OK] 所有服务健康检查通过")
                return
        time.sleep(2)
    print("[WARN] 等待健康检查超时，继续启动...")


def check_port(port: int) -> bool:
    """检查端口是否空闲，被占用则打印信息返回 False"""
    pids = find_pid_by_port(port)
    if not pids:
        return True

    print(f"[WARN]  端口 {port} 已被占用，PID: {', '.join(pids)}")
    print(f"   请手动释放端口后重试:")
    print(f"   > netstat -ano | findstr :{port}")
    print(f"   > taskkill /F /PID <pid>")
    return False


def start_uvicorn(port: int, reload: bool, host: str, data_source: str | None, workers: int):
    """启动 uvicorn 服务"""
    script_dir = str(PROJECT_ROOT / "ad-direction-agent")

    env = os.environ.copy()
    if data_source:
        env["DATA_SOURCE"] = data_source
    # PYTHONPATH: purpose-agent 跨项目依赖
    purpose_dir = str(PROJECT_ROOT / "ad-purpose-agent")
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{purpose_dir}{os.pathsep}{existing_path}" if existing_path else purpose_dir

    if reload and workers > 1:
        print("[WARN] --reload 与 --workers>1 不兼容，已忽略 reload")
        reload = False
    reload_flag = "--reload" if reload else ""
    workers_flag = f"--workers {workers}" if workers > 1 else ""
    cmd = (
        f"uvicorn app.main:app "
        f"--host {host} --port {port} "
        f"--log-level info "
        f"{workers_flag} "
        f"{reload_flag}"
    ).strip()

    print(f"\n[+] 启动服务: {cmd}")
    if workers > 1:
        print(f"    进程数: {workers}")
    if data_source:
        print(f"    数据源: {data_source}")
    print(f"    主看板:             http://localhost:{port}/demo/ad-asisitant-agent.html")
    print(f"    Campaign 活动分析:   http://localhost:{port}/demo/campaign_test.html")
    print()

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m"] + cmd.split(),
            env=env,
            cwd=script_dir,
        )
        proc.wait()
    except KeyboardInterrupt:
        print("\n[*] 正在停止服务...")
        proc.terminate()
        proc.wait()
        print("[OK] 服务已停止")


def main():
    parser = argparse.ArgumentParser(description="启动广告方向决策服务")
    parser.add_argument("--port", type=int, default=8010, help="服务端口 (默认: 8010)")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认: 0.0.0.0)")
    parser.add_argument("--reload", action="store_true", default=True, help="热重载 (默认开启)")
    parser.add_argument("--no-reload", dest="reload", action="store_false", help="关闭热重载")
    parser.add_argument("--workers", type=int, default=1,
                        help="uvicorn worker 进程数（生产建议 2–4；与 --reload 互斥）")
    parser.add_argument("--data-source", choices=["db", "mock", "csv"],
                        default=None, help="数据源 (默认取 settings.py 中的配置)")
    parser.add_argument("--with-infra", action="store_true", default=False,
                        help="启动前先拉起 Docker 基础设施（MySQL + Redis）")
    parser.add_argument("--all", action="store_true", default=False,
                        help="完整 docker compose up（包含 agent 容器，跳过本地 uvicorn）")

    args = parser.parse_args()

    if args.all:
        print("[+] 完整 Docker Compose 模式")
        subprocess.run(
            ["docker", "compose", "-f", str(PROJECT_ROOT / "docker-compose.yml"), "up", "-d"],
            cwd=str(PROJECT_ROOT),
        )
        return

    if args.with_infra:
        if not docker_compose_up(["mysql-state", "redis-cache"]):
            print("[WARN] Docker 基础设施启动失败，继续尝试启动 Python 服务...")

    if not check_port(args.port):
        sys.exit(1)

    if args.workers < 1:
        print("[ERR] --workers 必须 >= 1")
        sys.exit(1)

    start_uvicorn(
        port=args.port,
        reload=args.reload,
        host=args.host,
        data_source=args.data_source,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
