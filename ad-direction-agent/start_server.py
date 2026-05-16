"""启动脚本 — 端口冲突检测后启动 Uvicorn

步骤:
  1. 检查目标端口是否被占用
  2. 如被占用，打印警告并退出（需手动处理）
  3. 启动 uvicorn

用法:
  python start_server.py --port 8010 --reload
  python start_server.py --port 8010 --data-source mock
  python start_server.py --port 8010 --no-reload
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


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


def start_uvicorn(port: int, reload: bool, host: str, data_source: str | None):
    """启动 uvicorn 服务"""
    script_dir = os.path.dirname(os.path.abspath(__file__))

    env = os.environ.copy()
    if data_source:
        env["DATA_SOURCE"] = data_source
    # PYTHONPATH: purpose-adapter 跨项目依赖
    purpose_dir = str(Path(script_dir).parent / "ad-purpose-agent")
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{purpose_dir}{os.pathsep}{existing_path}" if existing_path else purpose_dir

    reload_flag = "--reload" if reload else ""
    cmd = (
        f"uvicorn app.main:app "
        f"--host {host} --port {port} "
        f"{reload_flag}"
    ).strip()

    print(f"\n[+] 启动服务: {cmd}")
    if data_source:
        print(f"    数据源: {data_source}")
    print(f"    主看板:        http://localhost:{port}/demo/ad-asisitant-agent.html")
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
    parser.add_argument("--data-source", choices=["db", "mock", "csv"],
                        default=None, help="数据源 (默认取 settings.py 中的配置)")

    args = parser.parse_args()

    if not check_port(args.port):
        sys.exit(1)

    start_uvicorn(
        port=args.port,
        reload=args.reload,
        host=args.host,
        data_source=args.data_source,
    )


if __name__ == "__main__":
    main()
