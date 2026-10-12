"""Start the local app once, wait for readiness, then open the browser."""
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8765"


def ready():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL + "/openapi.json", timeout=1) as response:
            return json.load(response).get("info", {}).get("title") == "Datara Profile Generator"
    except (OSError, ValueError):
        return False


def main():
    os.chdir(ROOT)
    runtime = ROOT / "data" / "launcher"
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "startup.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not ready():
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", 8765)) == 0:
                    raise RuntimeError("8765 端口被其他服务占用，请关闭占用程序后重试。")
            with (runtime / "server.log").open("a") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "datara.app:app",
                     "--host", "127.0.0.1", "--port", "8765"],
                    cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            for _ in range(120):
                if ready():
                    break
                if process.poll() is not None:
                    raise RuntimeError(f"启动失败，请查看 {runtime / 'server.log'}")
                time.sleep(0.25)
            else:
                process.terminate()
                raise RuntimeError(f"启动超时，请查看 {runtime / 'server.log'}")
        subprocess.run(["open", URL], check=True)
        print("Datara 已打开。服务在后台运行，可以关闭此终端窗口。")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        if sys.stdin.isatty():
            input("按回车关闭窗口…")
        sys.exit(1)
