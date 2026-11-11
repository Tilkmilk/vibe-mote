"""重启交接：等旧实例真正退出，再以同样权限拉起新实例。

为什么不用 app.py --takeover：
    在**已提权**的实例里点「重启」，restart-app 会用 ShellExecuteW 拉起
    `app.py --silent --takeover`（新进程是旧进程的子进程），新进程再去
    taskkill 旧进程 —— 真机实测这一步会把新旧两个进程一起带走，
    表现就是"点了重启，界面直接打不开、日志停在接管那一行"。
    改为：由本脚本等待端口释放，然后**正常**启动（不带 --takeover），
    新进程以 pythonw 直接拉起，不会互相杀。
"""

import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "app.py")
CREATE_NO_WINDOW = 0x08000000


def ui_port(default: int = 8787) -> int:
    """端口跟着 config.json 走 —— 用户改过 ui_port 时不能还按 8787 等。"""
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            v = json.load(f).get("ui_port")
        return int(v) if v else default
    except Exception:
        return default


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    port = ui_port()
    deadline = time.time() + 25
    while time.time() < deadline:
        if port_free(port):
            break
        time.sleep(0.4)
    time.sleep(0.8)                       # 让旧进程把锁文件也放掉
    subprocess.Popen([sys.executable, APP, "--silent"], cwd=HERE,
                     creationflags=CREATE_NO_WINDOW, close_fds=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
