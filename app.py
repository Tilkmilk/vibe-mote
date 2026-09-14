# -*- coding: utf-8 -*-
"""遥控器客户端 —— 单一入口（纯标准库，零第三方依赖也能起界面）。

设计上的三条硬规矩（都是为了「不弹黑框」和「好维护」）：

  1. **只有一个进程**。语音和按键都在本进程的线程里跑，不 Popen 任何子进程，
     所以不可能冒出控制台窗口。唯一会起子进程的地方是「安装依赖」（pip），
     那里显式加了 CREATE_NO_WINDOW。
  2. **界面只监听 127.0.0.1**。既安全，又不会触发 Windows 防火墙弹窗
     （监听 0.0.0.0 才会弹，而且弹了还会反复弹）。
  3. **配置以后端 config.json 为准**。前端不许用 localStorage 缓存配置再推回来
     —— 历史上这个 bug 把用户配置冲掉过。

启动方式：
    双击「启动遥控器.vbs」（无黑框）；或 python app.py [--silent] [--port N]
    --silent 只起服务不开浏览器（开机自启用这个）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import queue
import select
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))

# ★ 必须把自己的目录塞进 sys.path —— 离线全量包靠这行才能跑起来。
# 原因：官方 embeddable Python 带一个 `python313._pth`，一旦存在就进入**隔离模式**：
# sys.path 被换成 _pth 里列的几项（runtime\ + python313.zip + Lib\site-packages），
# **脚本所在目录不会自动加进去**（`_pth` 里的 `.` 指的是 runtime 目录本身，不是程序目录）。
# 后果：`import voice` / `import keys` 直接 ModuleNotFoundError → 进程当场退出；
# 而 pythonw 没有控制台，用户那端看到的就是"双击没反应"，毫无线索。
# 放在这里（模块级、所有延迟 import 之前）就一劳永逸，系统 Python / .venv 下也不会有副作用。
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 中英双语：界面自己管界面上的字，后端管"要显示在界面上的消息"（见 i18n.py）。
# 只依赖标准库，所以可以模块级导入 —— 不必像 voice/keys 那样延迟导入。
import i18n  # noqa: E402  （必须在 sys.path 之后就位）

APP_NAME = "VibeMote"
APP_VERSION = "1.1.0"          # 报 bug 时请带上这个版本号（界面「设置 → 界面」能看到）
WEB = os.path.join(HERE, "web")
CONFIG = os.path.join(HERE, "config.json")
REQUIREMENTS = os.path.join(HERE, "requirements.txt")
WHEELS = os.path.join(HERE, "wheels")
LOCKS = 49741                      # 单实例锁（只在 127.0.0.1）
LOCK_PORT = LOCKS
VENV = os.path.join(HERE, ".venv")
VENV_PY = os.path.join(VENV, "Scripts", "python.exe")
AUTOSTART_NAME = "VibeMote"
AUTOSTART_VBS = os.path.join(HERE, "启动遥控器.vbs")
# 启动器靠它判断"界面真的起来了"（内容：端口 + pid）
READY = os.path.join(HERE, "_ready.txt")
# 「上次是不是正常退出」的记录：正常退出时删掉（见 main 的清理段）
RUNSTATE = os.path.join(HERE, "_run_state.txt")

DEFAULT_PORT = 8787
CREATE_NO_WINDOW = 0x08000000

RECOMMENDED = {
    "ok":      {"type": "key",    "value": "ENTER",     "mods": [], "text": ""},
    "back":    {"type": "key",    "value": "ESC",       "mods": [], "text": ""},
    # 上/下 = 滚轮，而且是**按住不放连续滚**：轻点滚一格、按住一直滚。
    # 出厂默认就是这个（用户要的行为），"按一下只滚一格"反而是不好用的那个。
    "up":      {"type": "holdmouse", "value": "wheel_up",  "mods": [], "text": ""},
    "down":    {"type": "holdmouse", "value": "wheel_down", "mods": [], "text": ""},
    "left":    {"type": "combo",  "value": "LBRACKET",  "mods": ["CTRL", "SHIFT"], "text": ""},
    "right":   {"type": "combo",  "value": "RBRACKET",  "mods": ["CTRL", "SHIFT"], "text": ""},
    "home":    {"type": "combo",  "value": "A",         "mods": ["CTRL", "ALT"], "text": ""},
    "power":   {"type": "combo",  "value": "N",         "mods": ["CTRL"], "text": ""},
    "input":   {"type": "combo",  "value": "P",         "mods": ["CTRL"], "text": ""},
    "youtube": {"type": "combo",  "value": "BACKQUOTE", "mods": ["CTRL"], "text": ""},
    "mute":    {"type": "combo",  "value": "J",         "mods": ["CTRL"], "text": ""},
    "netflix": {"type": "combo",  "value": "R",         "mods": ["CTRL"], "text": ""},
    "volup":   {"type": "combo",  "value": "EQUAL",     "mods": ["CTRL"], "text": ""},
    "voldown": {"type": "combo",  "value": "MINUS",     "mods": ["CTRL"], "text": ""},
}
DEFAULT_CONFIG = {
    "_comment": "遥控器客户端配置。以后端这份为准；界面改完点保存会写回这里。",
    "remote_addr": "",
    "ui_port": DEFAULT_PORT,
    "profile": {"app": "workbuddy", "ime": "qwen"},
    "voice": {"mode": "hold", "key": "RALT", "mods": []},
    "audio": {"gain": 4.0, "agc": True, "relay": True},
    "keys": RECOMMENDED,
}
TYPES = ("key", "hold", "combo", "holdcombo", "mouse", "holdmouse", "text")

REMOTE_LABELS = {
    "power": "电源", "input": "输入源", "up": "上", "down": "下",
    "left": "左", "right": "右", "ok": "OK", "back": "返回", "home": "主页",
    "voice": "语音", "mute": "静音", "youtube": "YouTube", "netflix": "NETFLIX",
    "volup": "音量+", "voldown": "音量-",
}
MOUSE_LABELS = {"wheel_up": "滚轮上", "wheel_down": "滚轮下",
                "left": "鼠标左键", "right": "鼠标右键", "middle": "鼠标中键"}


def describe_mapping(m):
    """给日志用的人类可读写法。"""
    t, v = (m or {}).get("type"), (m or {}).get("value")
    if t == "mouse":
        return MOUSE_LABELS.get(v, str(v))
    if t == "holdmouse":
        # 滚轮按住是"连续滚"，这一点日志里要说清，否则用户会以为按住只滚一格
        tail = "（按住·连续）" if v in ("wheel_up", "wheel_down") else "（按住）"
        return MOUSE_LABELS.get(v, str(v)) + tail
    if t == "text":
        return f"文本「{str(m.get('text') or '')[:12]}」"
    mods = "+".join(m.get("mods") or [])
    name = (mods + "+" if mods else "") + str(v)
    return name + ("（按住）" if t in ("hold", "holdcombo") else "")


# ============================================================ 控制台编码
def _utf8_console():
    """中文 Windows 的控制台是 GBK，打印设备名里的 ® 会直接把进程打死。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ============================================================ 事件总线
class Bus:
    """日志环 + SSE 订阅者。所有角色都通过它往前推消息。

    ⚠ 为什么必须**同时写文件**：程序是用 `pythonw.exe` 启动的（为了不弹黑框），
    而 pythonw 下 `sys.stdout` 是 `None` —— `print()` 不会报错，但**什么都不输出**。
    于是"双击没反应"这类问题在用户手上完全没有线索：进程从哪一步退出、退出前报了什么，
    全都拿不到。所以从第一行日志开始就落盘到 `client.log`，再加两个崩溃钩子兜底。
    """
    LOG = os.path.join(HERE, "client.log")
    LOG_MAX = 512 * 1024          # 超过就重开一次，别让它无限长大
    MAX_SUBS = 8                  # SSE 订阅者上限（界面正常只开 1~2 条）

    def __init__(self, capacity=800):
        self.lines = []
        self.capacity = capacity
        self.subs = []
        self.lock = threading.Lock()
        self.fh = None
        self._log_tried = False
        # ⚠ **故意不在这里开文件**。本对象是模块级的（BUS = Bus()），
        #   要是构造时就落盘，那任何"只是 import 一下"的场景（状态探针、测试脚本）
        #   都会在程序目录里凭空多出一个 client.log —— 一眼看去像程序自己跑过了。
        #   改成**第一条日志到达时才开**；崩溃钩子也走 log()，所以 traceback 不会丢。

    def _open_log(self):
        self._log_tried = True
        try:
            if os.path.exists(self.LOG) and os.path.getsize(self.LOG) > self.LOG_MAX:
                os.remove(self.LOG)
            self.fh = open(self.LOG, "a", encoding="utf-8", buffering=1)
            self.fh.write(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} "
                          f"pid={os.getpid()} {sys.executable} =====\n")
        except Exception:
            self.fh = None

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with self.lock:
            if not self._log_tried:
                self._open_log()
            self.lines.append(line)
            if len(self.lines) > self.capacity:
                del self.lines[:len(self.lines) - self.capacity]
            if self.fh:
                try:
                    self.fh.write(line + "\n")
                except Exception:
                    self.fh = None
        print(line, flush=True)
        self.push({"ev": "log", "line": line})

    def trace(self, where, exc=None):
        """把未捕获异常写进日志（崩溃钩子用）。"""
        import traceback
        head = f"！！未捕获异常（{where}）："
        if exc is None:
            self.log(head)
            self.log(traceback.format_exc())
        else:
            self.log(head + f"{type(exc).__name__}: {exc}")
            try:
                self.log("".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__)))
            except Exception:
                pass

    def push(self, msg):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except Exception:
                pass

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.subs.append(q)
        return q

    def subscriber_count(self):
        with self.lock:
            return len(self.subs)

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def tail(self, n=200):
        with self.lock:
            return self.lines[-n:]


BUS = Bus()


# ============================================================ 配置
def _merge_defaults(cfg):
    out = dict(DEFAULT_CONFIG)
    out.update(cfg or {})
    prof = dict(DEFAULT_CONFIG["profile"])
    prof.update(cfg.get("profile") or {})
    out["profile"] = prof
    voice = dict(DEFAULT_CONFIG["voice"])
    voice.update(cfg.get("voice") or {})
    out["voice"] = voice
    audio = dict(DEFAULT_CONFIG["audio"])
    audio.update(cfg.get("audio") or {})
    out["audio"] = audio
    keys_map = cfg.get("keys")
    out["keys"] = dict(keys_map) if isinstance(keys_map, dict) else dict(RECOMMENDED)
    return out


def load_config():
    # ★ 加了重试：config.json 被另一个进程（**或者自己的保存动作**）正在写的那一刻，
    #   Windows 会直接抛 PermissionError（共享冲突）—— 原来一失败就"用默认值继续跑"，
    #   用户看到的是"我的设置莫名其妙变回默认了"（真机日志里出现过：
    #   `配置读取失败…PermissionError`，同一秒 config.json 正好被写入）。
    #   重试几次基本都能读到；真失败才退回默认值，并且日志写清楚。
    last = None
    for attempt in range(6):
        try:
            # utf-8-sig：记事本另存为 "UTF-8" 会加 BOM，用 utf-8 读会 JSONDecodeError
            # → 用户的配置被**静默重置成默认值**（还只留一行"配置读取失败"）。吃 BOM 最省事。
            with open(CONFIG, encoding="utf-8-sig") as f:
                return _merge_defaults(json.load(f))
        except FileNotFoundError:
            return _merge_defaults({})
        except Exception as e:
            last = e
            time.sleep(0.15 * (attempt + 1))        # 0.15→0.9 秒，够写入方收尾
    if last is not None:
        BUS.log(i18n.L(f"配置读取失败（重试 6 次仍失败），用默认值："
                       f"{type(last).__name__}: {last}",
                       f"Failed to read the config (6 retries), using defaults: "
                       f"{type(last).__name__}: {last}"))
    return _merge_defaults({})


def save_config(cfg):
    clean = {"_comment": DEFAULT_CONFIG["_comment"]}
    # ⚠ 语言必须在这里保住：save_config 只保留白名单字段，界面保存配置时不会带上 lang，
    #    不写这一行的话用户每次点「保存」都会把语言重置掉（表现为"切成英文又自己变回中文"）。
    clean["lang"] = i18n.current()
    clean["remote_addr"] = str(cfg.get("remote_addr") or DEFAULT_CONFIG["remote_addr"])
    clean["ui_port"] = int(cfg.get("ui_port") or DEFAULT_PORT)
    p = cfg.get("profile") or {}
    clean["profile"] = {"app": str(p.get("app") or DEFAULT_CONFIG["profile"]["app"]),
                        "ime": str(p.get("ime") or DEFAULT_CONFIG["profile"]["ime"])}
    v = cfg.get("voice") or {}
    clean["voice"] = {"mode": "tap" if v.get("mode") == "tap" else "hold",
                      "key": str(v.get("key") or "RALT"),
                      "mods": [str(x).upper() for x in (v.get("mods") or [])]}
    a = cfg.get("audio") or {}
    clean["audio"] = {"gain": float(a.get("gain", 4.0)),
                      "agc": bool(a.get("agc", True)),
                      "relay": bool(a.get("relay", True))}
    keys_map = {}
    voice_alias = None
    for k, m in (cfg.get("keys") or {}).items():
        if k.startswith("_") or not isinstance(m, dict):
            continue
        if m.get("type") not in TYPES or not isinstance(m.get("value"), str):
            continue
        # 界面在「按键映射」页也能点「语音」键 —— 那份写法存成 keys.voice。
        # 语音键其实不走 HID、由语音通道单独处理，所以这里把它归一到顶层 voice，
        # 免得同一个设置有两个真相。
        if k == "voice":
            if m.get("type") in ("hold", "holdcombo", "key", "combo"):
                voice_alias = {"mode": "hold" if m["type"].startswith("hold") else "tap",
                               "key": m["value"],
                               "mods": [str(x).upper() for x in (m.get("mods") or [])]}
            continue
        keys_map[k] = {"type": m["type"], "value": m["value"],
                       "mods": [str(x).upper() for x in (m.get("mods") or [])],
                       "text": str(m.get("text") or "")}
    clean["keys"] = keys_map
    if voice_alias:
        clean["voice"] = voice_alias
    # ★ 原子写：读的人不会读到半个文件。两处细节别改回去：
    #   · 临时文件名带 pid —— i18n.save_to_config() 也写 config.json，
    #     原来两边共用 `config.json.tmp`，并发时会互相 replace 掉，
    #     结果丢修改、还在程序目录里留下一个带完整配置的 .tmp；
    #   · 用 i18n.CONFIG_LOCK 串行化，保证「保存设置」和「切语言」不会互相覆盖。
    with i18n.CONFIG_LOCK:
        tmp = f"{CONFIG}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, CONFIG)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    return clean


# ============================================================ 依赖 / 环境
DEPS = [("bleak", i18n.pair("语音键与遥控器麦克风", "voice button + remote microphone")),
        ("sounddevice", i18n.pair("音频输出", "audio output")),
        ("cffi", i18n.pair("sounddevice 依赖", "a sounddevice dependency"))]
KEY_DEPS = [("frida", i18n.pair("按键映射", "key mapping"))]


def _venv_has(mod):
    """包内 .venv 里有没有这个顶层模块。

    ⚠ 别用 dist-info 目录名去反推包名：
    `sounddevice-0.5.6.dist-info` 里版本带点，rsplit("-",1) 得到的是
    `sounddevice-0.5.6`，永远匹配不上 `sounddevice`；而 bleak/cffi 因为刚好
    同时有同名目录才没露馅。结果就是**装成功了却报「缺 sounddevice」**，
    用户看到的是"点了没反应"。
    直接看真实可导入的形态（目录 / .py / .pyd）最可靠。
    """
    if not venv_python():
        return False
    sp = os.path.join(VENV, "Lib", "site-packages")
    try:
        entries = os.listdir(sp)
    except OSError:
        return False
    if mod in entries:                                  # bleak/ frida/ cffi/
        return True
    for suf in (".py", ".pyd", ".so"):                  # sounddevice.py
        if mod + suf in entries:
            return True
    return any(n.startswith(mod + ".") and n.endswith(".pyd") for n in entries)


def deps_state():
    """依赖是否齐全。

    ⚠ 要同时看**两个地方**，只看一个就会骗人：
      · 当前进程的解释器（app 自己跑在哪）
      · 包内 .venv（「安装依赖」装到哪）
    因为 app 很可能跑在系统 Python 上而依赖装在 .venv 里：那种情况其实装成功了，
    只看当前解释器就会一直显示"缺依赖"。这种状态标记 restart_needed，界面会提示重启。
    """
    proc_missing = [m for m, _ in DEPS if importlib.util.find_spec(m) is None]
    proc_missing_keys = [m for m, _ in KEY_DEPS if importlib.util.find_spec(m) is None]
    missing = [i18n.L(f"{m}（{i18n.pick(why)}）", f"{m} ({i18n.pick(why)})")
               for m, why in DEPS
               if importlib.util.find_spec(m) is None and not _venv_has(m)]
    missing_keys = [i18n.L(f"{m}（{i18n.pick(why)}）", f"{m} ({i18n.pick(why)})")
                    for m, why in KEY_DEPS
                    if importlib.util.find_spec(m) is None and not _venv_has(m)]
    restart_needed = (bool(venv_python()) and bool(proc_missing) and not missing)
    return {"ok": not missing, "missing": missing,
            "keys_ok": not missing_keys, "missing_keys": missing_keys,
            "in_venv": bool(venv_python()), "restart_needed": restart_needed,
            # 离线全量包：依赖预装在自带的 runtime\ 里（界面上要说明，
            # 否则用户会奇怪"为什么没有 .venv 也说依赖齐全"）
            "bundled": is_bundled_runtime()}


def _run_hidden(cmd, on_line, collect=None):
    """跑一个子进程并把输出逐行喂给前端。这是全程序唯一会起子进程的地方，
    所以必须带 CREATE_NO_WINDOW —— 否则就会冒出黑框。

    `collect`：可选的列表，用来留一份输出（给"失败时判因"用，比如认代理错误）。
    只留最后 200 行，别让一个话痨进程把内存占了。
    """
    p = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace",
                         creationflags=CREATE_NO_WINDOW)
    for line in p.stdout:
        s = line.rstrip()
        on_line(s)
        if collect is not None:
            collect.append(s)
            if len(collect) > 200:
                del collect[:len(collect) - 200]
    return p.wait()


def system_proxy():
    """读系统（WinINET）里配的代理，返回 "host:port" 或 ""。

    ★ 为什么要读它：**pip 在 Windows 上会走系统代理**。而"代理软件没开、但系统里还留着
    127.0.0.1:7890"这种事极其常见（换网络、代理软件退出、重启之后都很容易这样）。
    这时所有 PyPI 源都连不上，而 pip 报的是
        ERROR: Could not find a version that satisfies the requirement bleak==3.0.2
               (from versions: none)
    —— 看着像"这个包不存在"，用户根本想不到是代理：
    五个国内镜像依次"没有这个包"，其实是本机 7890 端口没人听。
    所以这里主动读出来，失败时直接告诉用户。
    """
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            if not winreg.QueryValueEx(k, "ProxyEnable")[0]:
                return ""
            return str(winreg.QueryValueEx(k, "ProxyServer")[0] or "")
    except OSError:
        return ""


def looks_like_proxy_failure(text):
    """pip 的输出看起来是不是"被代理挡住了"。

    两种典型表现：
      · 直接报代理连不上：`ProxyError` / `Cannot connect to proxy` / WinError 10061；
      · 更阴的：源连不上时 pip 说的是 "from versions: none"（看着像包不存在）——
        这种情况配合"系统里确实配着代理"一起判断，否则会误报。
    """
    t = (text or "").lower()
    if "proxyerror" in t or "cannot connect to proxy" in t or "10061" in t:
        return True
    return bool(system_proxy()) and "from versions: none" in t


def venv_python():
    return VENV_PY if os.path.exists(VENV_PY) else None


def _pip_ok(py):
    """这个解释器能用 pip 吗？—— 半成品 .venv（建到一半没 pip）必须先识别出来，
    否则启动器会优先选它，然后「装依赖」永远失败。"""
    try:
        p = subprocess.run([py, "-m", "pip", "--version"],
                           capture_output=True, timeout=30,
                           creationflags=CREATE_NO_WINDOW)
        return p.returncode == 0
    except Exception:
        return False


def _rmtree_manual(path):
    """手工删目录。别用 shutil.rmtree —— 会被某些宿主劫持成「移到回收站」，
    在无桌面/沙箱环境下直接抛 safe-delete 错误。"""
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            try:
                os.unlink(os.path.join(root, f))
            except OSError:
                pass
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def ensure_venv(on_line):
    """确保有一个可用的包内 .venv，返回其中的 python；做不到就返回 None。

    为什么优先 .venv：装在 Program Files 里的 Python 往 site-packages 写东西
    需要管理员 —— 「装依赖」这个动作绝不该弹 UAC。包内 .venv 还能随目录一起拷。
    """
    py = venv_python()
    if py:
        if _pip_ok(py):
            on_line(i18n.L(f"使用包内虚拟环境：{py}",
                           f"Using the bundled virtualenv: {py}"))
            return py
        on_line(i18n.L("包内 .venv 不完整（没有 pip），删掉重建…",
                       "The bundled .venv is incomplete (no pip); deleting and rebuilding…"))
        _rmtree_manual(VENV)

    on_line(i18n.L("创建包内虚拟环境 .venv（不污染系统 Python，也不需要管理员）…",
                   "Creating the bundled virtualenv .venv (does not touch the system "
                   "Python and needs no administrator rights)…"))
    rc = _run_hidden([sys.executable, "-m", "venv", VENV], on_line)
    py = venv_python()
    if rc != 0 or not py or not _pip_ok(py):
        on_line(i18n.L("✗ 建 .venv 没成功，改用当前解释器 + --user 安装",
                       "✗ Failed to create .venv; falling back to the current interpreter "
                       "with --user"))
        if py:
            _rmtree_manual(VENV)        # 别留半成品害下一次启动
        return None
    return py


# 下载源：按顺序尝试，**上一个失败自动换下一个**。
#
# 为什么必须这样：默认走官方 pypi.org，国内很多人没 VPN —— 慢、断流、超时。
# 而且报错只有 "Read timed out"，用户完全不知道怎么办。
#
# ⚠ 这张表按 bleak==3.0.2 的实际可下载情况排序：
#      腾讯云    ✓ 1.1s      frida 索引 0.9s   ← 最快
#      中科大    ✓ 1.0s      frida 索引 8.6s
#      阿里云    ✓ 1.4s      frida 索引 6.2s
#      华为云    ✓ 1.1s
#      官方 PyPI ✓ 2.5s      frida 索引 2.4s   ← 国外/有代理时最稳，放兜底
#      清华 TUNA ✗ 返回空索引（from versions: none）→ 降级到最后
#      网易 163  ✗ 索引停更（bleak 只到 0.5.0）→ **不收录**，会装出老版本
#
# 想加/换源：直接改这张表（显示名, index-url）；空字符串 = 用 pip 默认（官方）。
PIP_MIRRORS = [
    ("腾讯云", "https://mirrors.cloud.tencent.com/pypi/simple"),
    ("中科大 USTC", "https://mirrors.ustc.edu.cn/pypi/simple"),
    ("阿里云", "https://mirrors.aliyun.com/pypi/simple"),
    ("华为云", "https://mirrors.huaweicloud.com/repository/pypi/simple"),
    ("清华 TUNA", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("官方 PyPI", ""),
]


def _pip_sources():
    """按尝试顺序返回 [(显示名, 额外参数)]。包内 wheels 目录排第一（纯离线）。"""
    src = []
    if os.path.isdir(WHEELS) and any(os.listdir(WHEELS)):
        src.append(("包内 wheels（纯离线）", ["--no-index", "--find-links", WHEELS]))
    for name, url in PIP_MIRRORS:
        src.append((name, ["-i", url] if url else []))
    return src


def run_pip(args, on_line, python=None, user=False):
    """装依赖：多源轮着试，第一个成功就停。"""
    py = python or venv_python() or sys.executable
    last = 1
    told_proxy = False
    sources = _pip_sources()
    for i, (name, extra) in enumerate(sources, 1):
        base = [py, "-m", "pip", "install", "--disable-pip-version-check",
                "--timeout", "30", "--retries", "2"]
        if user:
            base.append("--user")
        cmd = base + extra + args
        on_line(i18n.L(f"── 下载源 {i}/{len(sources)}：{name}",
                       f"── Download source {i}/{len(sources)}: {name}"))
        on_line("$ " + " ".join(cmd))
        buf = []
        rc = _run_hidden(cmd, on_line, collect=buf)
        if rc == 0:
            on_line(i18n.L(f"✓ 用「{name}」装好了", f"✓ Installed using “{name}”"))
            return 0
        last = rc
        # ★ 认出"其实是系统代理挡住了"。不认的话用户看到的是
        #   "Could not find a version … (from versions: none)" —— 看着像包不存在，
        #   完全想不到是本机的代理端口没人听。
        if not told_proxy and looks_like_proxy_failure("\n".join(buf)):
            told_proxy = True
            ps = system_proxy()
            on_line(i18n.L(
                f"⚠ 看起来不是「源里没有这个包」，而是**系统代理连不上**："
                f"你系统里配着代理 {ps or '（已开启但没写地址）'}，但那个代理软件现在没在跑。",
                f"⚠ This does not look like “the source lacks this package” but like a "
                f"**dead system proxy**: Windows has a proxy configured "
                f"({ps or 'enabled, no address'}) but the proxy app is not running."))
            on_line(i18n.L(
                "   两个办法（任选一个再点一次）：① 把代理软件打开；"
                "② 关掉系统代理：Windows 设置 → 网络和 Internet → 代理 → 关掉「使用代理服务器」。",
                "   Two options (pick one and click again): (1) start your proxy app; "
                "(2) turn the system proxy off: Windows Settings → Network & Internet → "
                "Proxy → disable “Use a proxy server”."))
        if i < len(sources):
            on_line(i18n.L(f"✗「{name}」这次没成（返回码 {rc}），自动换下一个源…",
                           f"✗ “{name}” did not work this time (exit code {rc}); "
                           f"trying the next source…"))
    on_line(i18n.L("✗ 所有下载源都失败了。三个办法：",
                   "✗ Every download source failed. Three options:"))
    on_line(i18n.L("   1) 换个网络（手机热点）再点一次；",
                   "   1) Switch networks (phone hotspot) and click again;"))
    on_line(i18n.L("   2) 手动指定源：在「设置 → 诊断」里看日志里最后一个失败的源名；",
                   "   2) Pick a source manually: the Logs page shows the last source "
                   "that failed;"))
    on_line(i18n.L("   3) 走纯离线：把 wheels 目录（见 说明.md 第五节）放到程序目录，"
                   "程序会自动优先用它，完全不联网。",
                   "   3) Go fully offline: put the wheels directory (see section 5 of "
                   "the README) next to the program — it is then preferred automatically, "
                   "with no network access at all."))
    if system_proxy() and not told_proxy:
        # 全失败 + 系统里配着代理 → 最可能就是这个（pip 的报错不会明说是代理）
        on_line(i18n.L(
            f"⚠ 另外注意：你系统里配着代理 {system_proxy()}。如果那个代理软件没在跑，"
            f"上面的「这个包不存在」其实全是它造成的 —— 打开代理软件，或关掉系统代理再试。",
            f"⚠ Also note: Windows has a proxy configured ({system_proxy()}). If that "
            f"proxy app is not running, all of the “package does not exist” errors above "
            f"are caused by it — start the proxy app, or turn the system proxy off and "
            f"try again."))
    return last


def is_bundled_runtime():
    """本程序是不是跑在**自带的 Python 运行时**上（离线全量包 / U 盘版）。

    为什么必须认出来：官方 embeddable 包**没有 pip、也建不了 venv**（缺 ensurepip）。
    不认的话，界面上的「安装依赖」会一路走到 `python -m venv` + `pip install`，
    最后报一堆看不懂的错 —— 而这个包里依赖本来就是预装好的，根本不该走到那一步。
    """
    try:
        exe = os.path.normcase(os.path.abspath(sys.executable or ""))
        rt = os.path.normcase(os.path.join(HERE, "runtime")) + os.sep
        return exe.startswith(rt)
    except Exception:
        return False


def _install(on_line, req):
    path = os.path.join(HERE, req)
    if not os.path.exists(path):
        on_line(i18n.L(f"找不到 {req}", f"{req} not found"))
        return 1
    if is_bundled_runtime():
        # 到这儿说明内置运行时里确实缺东西（正常情况不会）。嵌入式包没法就地 pip 安装，
        # 所以别硬试，直接给一条用户能走通的路。
        on_line(i18n.L("这是「离线全量包」：依赖内置在 runtime\\ 里，不能再往里面装。",
                       "This is the offline full bundle: the dependencies are built into "
                       "runtime\\ and nothing can be added to it."))
        on_line(i18n.L("修法：换用带联网安装的轻包（vibe-mote-v1.zip），"
                       "或者重新解压一份离线包（可能被误删了文件）。",
                       "Fix: switch to the light package with online installation "
                       "(vibe-mote-v1.zip), or unpack the offline bundle again "
                       "(files may have been deleted by accident)."))
        return 1
    py = ensure_venv(on_line)
    if py:
        return run_pip(["-r", path], on_line, python=py)
    # 兜底：装到当前解释器的用户目录（%APPDATA%\Python），同样不需要管理员
    return run_pip(["-r", path], on_line, python=sys.executable, user=True)


def install_deps(on_line):
    if deps_state()["ok"]:
        on_line(i18n.L("基础依赖已经齐全，不用重装 ✓",
                       "Base dependencies are all present; no reinstall needed ✓"))
        return 0
    rc = _install(on_line, "requirements.txt")
    if rc == 0:
        on_line(i18n.L("✓ 基础依赖装好了", "✓ Base dependencies installed"))
        if venv_python():
            on_line(i18n.L("→ 请关掉客户端，再双击「启动遥控器.vbs」"
                           "（启动器优先用包内 .venv，装好的依赖才生效）",
                           "→ Close the client, then double-click 启动遥控器.vbs "
                           "(the launcher prefers the bundled .venv, where the "
                           "dependencies went)"))
        else:
            on_line(i18n.L("→ 重启客户端后生效",
                           "→ Takes effect after restarting the client"))
    return rc


def install_keys_deps(on_line):
    """frida 约 130MB，单独一个动作 —— 要用按键映射时才装。"""
    if deps_state()["keys_ok"]:
        on_line(i18n.L("按键映射依赖（frida）已经齐全，不用重装 ✓",
                       "The key-mapping dependency (frida) is present; no reinstall needed ✓"))
        return 0
    rc = _install(on_line, "requirements-keys.txt")
    if rc == 0:
        on_line(i18n.L("✓ 按键映射依赖装好了（重启客户端后生效）",
                       "✓ Key-mapping dependencies installed (takes effect after "
                       "restarting the client)"))
    return rc


def find_cable_names():
    """虚拟声卡端点的名字（「环境体检」和「虚拟声卡」那张卡靠它判断能不能用）。

    ★ 必须跟 voice.find_cable() 用**同一套**判定。以前这里自己写了一遍
    `"cable input" in name`，voice 那边也写了一遍 —— 端点被 Windows 改名时
    （驱动被 PnP 重装后，渲染端点会变成「扬声器 (2- VB-Audio Virtual Cable)」）
    两边会同时失灵；更糟的是以后只修一边，就会出现"体检说没找到、清单说已装"
    这种自相矛盾的诊断。所以统一委托给 voice。
    """
    try:
        import voice
        return voice.cable_device_names()
    except Exception:
        return []


def cable_registry_present():
    """注册表探测：VB-CABLE 装了没有。**不依赖 sounddevice**。

    为什么必须有这个兜底：`find_cable_names()` 靠 sounddevice，
    而 sounddevice 就在"依赖"里 —— 当本进程是**系统 Python**、依赖却装在 `.venv`
    （`deps.restart_needed` 那种状态）时，`import sounddevice` 直接失败，
    于是明明装好了虚拟声卡也会被报成"没装"：横幅一直喊"还差虚拟声卡"、
    首页清单那一行标红，用户照着点还会去重装一遍。
    官方安装会留下两处痕迹，读它们跟 Python 依赖无关：
      · HKLM\\...\\Uninstall\\VB:VBCABLE {…}（Windows「应用和功能」里那条）
      · HKLM\\SYSTEM\\CurrentControlSet\\Services\\{VB-Cable, VBAudioVACMME}
    （uninstall.py 里有一份同样用途的探测，那个是独立进程、要单独能判断，所以是刻意重复。）
    """
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall") as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                if "vbcable" in sub.lower().replace(" ", ""):
                    return True
    except OSError:
        pass
    for svc in ("VB-Cable", "VBAudioVACMME"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Services" + "\\" + svc):
                return True
        except OSError:
            continue
    return False


# ============================================================ 虚拟声卡（VB-CABLE）
CABLE_DIR = os.path.join(HERE, "vbcable")
CABLE_SETUP = os.path.join(CABLE_DIR, "VBCABLE_Setup_x64.exe")
# 官方下载地址（VB-Audio 的虚拟声卡安装包，约 1.3MB）。仓库里不放这个二进制：
# 它是第三方闭源安装器，放进源码仓库有许可与体积问题；但要装就让它一键能装成，
# 所以包内没带安装器时从这里下。
CABLE_URL = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"
CABLE_HOME = "https://vb-audio.com/Cable/"
CABLE_CREDIT = "https://vb-cable.com/"

# ★★ 安装器**不是自包含的**，它从**自己所在的目录**读驱动文件：
#   只把 VBCABLE_Setup_x64.exe 放进包里，一键安装会**必然失败**，而本机因为早就装好了
#   虚拟声卡，所以一直没暴露。证据（对 VBCABLE_Setup_x64.exe 937,728 字节做的静态分析）：
#     · 整个二进制里**没有任何 INF 内容**（无 DriverVer / ServiceBinary / [Version] /
#       Signature / Manufacturer，ASCII 与 UTF-16 都查过）→ 它不是把驱动嵌在自己身上；
#     · 但有一组用于**运行时拼名字**的片段：
#       "vbMmeCable" + "64" + "_win10.inf" / "vbaudio_cable" + "64" + "_win10.sys"；
#     · 导入表里有 GetModuleFileNameA/W（取自己的目录）+ SetupDiGetINFClassA（要 INF 路径）
#       + UpdateDriverForPlugAndPlayDevicesA（装驱动）。
#   官方 readme.txt 也是这么说的：「Just UNZIP the package in a folder somewhere on your
#   hard-disk and launch the Setup program from there」。
#   所以：**必须把下面这几个文件一起发**，缺一个按钮就是坏的。
CABLE_REQUIRED = ("vbMmeCable64_win10.inf", "vbaudio_cable64_win10.sys")
CABLE_PACK_FILES = CABLE_REQUIRED + ("vbaudio_cable64_win10.cat", "readme.txt",
                                     "VBCABLE_Setup_x64.exe", "VBCABLE_ControlPanel.exe")
# 首启自动尝试装虚拟声卡，只试**一次**（用户拒了 UAC 就不要再每次开机弹他）。
# 删掉这个文件 = 允许下次启动再自动试一次。
CABLE_AUTOTRIED = os.path.join(HERE, "cable_autotried")
# 「退出后再删」那条分离进程留下结果的地方（uninstall.py 写，这里读一次就说清楚）
DEFERRED_RESULT = os.path.join(HERE, "_deferred_result.txt")
# 官方 Pack45 里那个安装器的 SHA256。**提权运行之前必须核对**（理由见 check_cable_installer）：
# vbcable\ 在程序目录里、普通用户就能写，不核对就等于"谁都能让本程序把一个自己写的
# exe 提升到管理员"。官方换新版本时这个值会失配 —— 那种情况靠数字签名兜底，不会卡死。
CABLE_SETUP_SHA256 = "734C35DFA6D98F48782A451633CEB471166EC70D60482FD89A1123D0EE3C4F41"
# 同一时间只允许一个虚拟声卡安装流程（三个入口可能几乎同时触发，见 install_cable）
CABLE_INSTALL_LOCK = threading.Lock()


def cable_files_missing():
    """包内缺哪些安装必需的文件（空 = 齐全）。"""
    return [n for n in CABLE_REQUIRED
            if not os.path.exists(os.path.join(CABLE_DIR, n))]


def cable_state():
    """虚拟声卡状态：装没装、包内文件全不全。界面据此决定按钮怎么写。

    `installed` = **现在真的能用**（枚举到输出端点）；`registered` = 注册表里装了。
    两者分开是因为它们会不一致，而混在一起会给出自相矛盾的诊断：
      · 依赖装在 .venv、本进程是系统 Python → `import sounddevice` 失败 →
        设备枚举不出来，但注册表说装了。这时若把两者取"或"就变成"已装"，
        而同一个体检里的 do_check 又照设备列表打出"没找到 VB-CABLE ✗" —— 用户懵。
      · 设备被禁用 / 只装了半边 / 卸载残留 → `registered=True` 但 `installed=False`，
        这时该提示"修一下/重启一次"，而不是"已装、不用管"。
    """
    names = find_cable_names()
    return {"installed": bool(names),
            "registered": cable_registry_present(),
            "device": (names[0] if names else ""),
            "bundled": os.path.exists(CABLE_SETUP),
            "files_ok": not cable_files_missing(),
            "missing_files": cable_files_missing(),
            "url": CABLE_URL,
            "home": CABLE_HOME,
            "credit": CABLE_CREDIT}


def _cable_fetch_pack(on_line):
    """从官网下一份官方包，把需要的文件解到 vbcable\\。返回 0 = 齐了。"""
    on_line(i18n.L("从 VB-Audio 官网下载安装包（约 1.3MB）…",
                   "Downloading the installer package from VB-Audio (~1.3MB)…"))
    try:
        import io
        import urllib.request
        import zipfile
        with urllib.request.urlopen(CABLE_URL, timeout=60) as r:
            data = r.read()
        zf = zipfile.ZipFile(io.BytesIO(data))
        # zip 里的文件名大小写/路径不一定规整，按 basename 小写索引
        byname = {}
        for n in zf.namelist():
            byname.setdefault(os.path.basename(n.replace("/", "\\")).lower(), n)
        os.makedirs(CABLE_DIR, exist_ok=True)
        got = []
        for n in CABLE_PACK_FILES:
            src = byname.get(n.lower())
            if not src:
                continue
            # ★ 先写 .part 再原子改名：直接写最终文件名的话，中途失败（磁盘满 / 被杀 /
            #   杀软插手）会留下**半截文件**，而 cable_files_missing() 只 exists() 一下就
            #   认为"有了"，下一次点击就把半截 exe 提权跑起来。
            final = os.path.join(CABLE_DIR, n)
            tmp = final + ".part"
            with zf.open(src) as fsrc, open(tmp, "wb") as dst:
                dst.write(fsrc.read())
            os.replace(tmp, final)
            got.append(n)
        missing_now = [n for n in CABLE_PACK_FILES if n not in got]
        if missing_now:
            # ★ 官方包改结构（比如 Pack46 换了文件名）时**必须说出来**。
            #   原来的写法是"✓ 已解出 0 个文件"然后当作成功 —— 于是会拿 vbcable\ 里
            #   上一版的残留继续用，装的不是你以为的那一版。
            on_line(i18n.L("✗ 官方包里没找到这些文件：" + "、".join(missing_now),
                           "✗ These files are not in the official package: "
                           + ", ".join(missing_now)))
            on_line(i18n.L("   （官方可能改了打包结构。可以从官网手动下载解压到 vbcable\\）",
                           "   (The vendor may have changed the package layout. You can "
                           "download it manually and unzip it into vbcable\\)"))
            return 1
        on_line(i18n.L(f"✓ 已解出 {len(got)} 个文件到 vbcable\\：",
                       f"✓ Extracted {len(got)} file(s) into vbcable\\: ")
                + "、".join(got))
    except Exception as e:
        on_line(i18n.L(f"✗ 下载失败：{type(e).__name__}: {e}",
                       f"✗ Download failed: {type(e).__name__}: {e}"))
        on_line(i18n.L(f"   手动下载：{CABLE_URL}",
                       f"   Download manually: {CABLE_URL}"))
        on_line(i18n.L("   解压后把**整个包**里的 VBCABLE_Setup_x64.exe、vbMmeCable64_win10.inf、"
                       "vbaudio_cable64_win10.sys 一起放进程序目录的 vbcable\\ 里，再点一次本按钮",
                       "   Unzip it and put VBCABLE_Setup_x64.exe, vbMmeCable64_win10.inf and "
                       "vbaudio_cable64_win10.sys — **the whole set** — into the program's "
                       "vbcable\\ folder, then click this button again"))
    return 0 if not cable_files_missing() and os.path.exists(CABLE_SETUP) else 1


def install_cable(on_line):
    """装虚拟声卡：包内带安装器就直接提权运行；没带（或不全）就先去官网补齐再运行。

    为什么要提权：VB-CABLE 是内核音频驱动，它**自己的安装器**要求管理员 ——
    这次 UAC 与本程序无关（本体是纯用户态），日志里要讲清楚，免得用户以为程序在要权限。

    ⚠ 这里只能"把官方安装器拉起来"，**不能静默装**：该 exe 没有任何命令行开关
    （二进制里没有 -i / -s / -h / /S / usage / silent 之类的串），它是个必须人点
    「Install Driver」的向导。官方 readme 还写明装完**必须重启**系统才算装完。
    """
    # ★ 闸门：同一时间只允许一个安装流程。三个入口（界面按钮 / 「全部安装」第③步 /
    #   首启自动）都可能几乎同时触发，原来会**弹两次 UAC、开两个向导**去动同一套
    #   .inf/.sys，通常两个都装不成。非阻塞拿锁，拿不到就直说"已经在装了"。
    if not CABLE_INSTALL_LOCK.acquire(blocking=False):
        on_line(i18n.L("虚拟声卡的安装已经在进行了，这次不重复拉（看上面的输出）",
                       "The virtual-audio-cable install is already in progress; not "
                       "starting another one (see the output above)"))
        return 0
    try:
        # 再复查一次：用户可能在自动流程的 6 秒等待里已经自己点了按钮装完了。
        if cable_state()["installed"]:
            on_line(i18n.L("虚拟声卡已经在用了，跳过 ✓",
                           "The virtual audio cable is already in use, skipping ✓"))
            return 0
        return _install_cable_locked(on_line)
    finally:
        CABLE_INSTALL_LOCK.release()


def _install_cable_locked(on_line):
    exe = CABLE_SETUP
    if not os.path.exists(exe) or cable_files_missing():
        miss = cable_files_missing()
        if miss:
            on_line(i18n.L("安装器不完整（缺 " + "、".join(miss) + "），先去官网补全…",
                           "The installer is incomplete (missing " + ", ".join(miss)
                           + "); fetching the full set from the official site…"))
        if _cable_fetch_pack(on_line) != 0:
            on_line(i18n.L("✗ 没凑齐安装必需的文件，一键安装做不了。"
                           "（只放一个 VBCABLE_Setup_x64.exe 是不够的 —— 它要从自己所在目录读 "
                           "vbMmeCable64_win10.inf 和 vbaudio_cable64_win10.sys）",
                           "✗ The files needed to install are still missing, so one-click "
                           "install cannot work. (Dropping in VBCABLE_Setup_x64.exe alone is not "
                           "enough — it reads vbMmeCable64_win10.inf and "
                           "vbaudio_cable64_win10.sys from its own folder.)"))
            return 1

    # ★ 提权运行之前必须确认"这就是那个安装器"（详见 check_cable_installer 的注释：
    #   vbcable\ 普通用户就能写，不查就等于给低权限程序一条提权路）。
    ok_sig, why = check_cable_installer(exe)
    on_line(i18n.L(f"校验安装器：{why}", f"Verifying the installer: {why}"))
    if not ok_sig:
        on_line(i18n.L("✗ 拒绝提权运行它 —— 这个 exe 既不是官方那一版，也没有有效数字签名。"
                       "请从 VB-Audio 官网手动下载安装，或把官方 vbcable 包重新解压到 vbcable\\",
                       "✗ Refusing to run it elevated — this exe is neither the official "
                       "revision nor validly signed. Install it manually from the VB-Audio "
                       "site, or unzip the official vbcable package into vbcable\\ again"))
        return 1

    on_line(i18n.L("用管理员权限启动 VB-CABLE 安装器（会弹一次 UAC，点「是」）…",
                   "Launching the VB-CABLE installer elevated (one UAC prompt — click Yes)…"))
    try:
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, "", CABLE_DIR, 1)
    except Exception as e:
        on_line(i18n.L(f"✗ 启动安装器失败：{e!r}", f"✗ Could not start the installer: {e!r}"))
        return 1
    if rc <= 32:
        on_line(i18n.L(f"✗ 提权被拒绝或失败（ShellExecute 返回 {rc}）",
                       f"✗ Elevation denied or failed (ShellExecute returned {rc})"))
        return 1
    on_line(i18n.L("安装器窗口出来后，点它上面的「Install Driver」，等它说 Installation Complete。",
                   "When the installer window appears, click “Install Driver” in it and wait "
                   "for “Installation Complete”."))
    on_line(i18n.L("⚠ VB-Audio 官方 readme 写明：装完**必须重启一次**系统才算装完。",
                   "⚠ VB-Audio's own readme states that you **must reboot** to finalize the "
                   "installation."))
    on_line(i18n.L("重启后回「设置 → 环境体检」确认虚拟声卡已被识别；"
                   "再在输入法里把麦克风选成 “CABLE Output (VB-Audio Virtual Cable)”。",
                   "After rebooting, come back to Settings → Environment check to confirm the "
                   "cable is detected, then select “CABLE Output (VB-Audio Virtual Cable)” as "
                   "the microphone in your input method editor."))
    on_line(i18n.L("（本程序每 5 秒重试一次找设备：万一没重启设备就出现了，它会自己接上，不用管。）",
                   "(This program retries every 5 seconds: if the device shows up without a "
                   "reboot, it will pick it up on its own.)"))
    return 0


def _sha256_file(path):
    try:
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest().upper()
    except OSError:
        return ""


def _authenticode_ok(path):
    """WinVerifyTrust：这个 exe 有没有**有效的** Authenticode 签名（含证书链）。

    纯 ctypes，不启外部进程 —— 策略受限的机器上也能用；故意设成
    「不联网查吊销 + 只用本地缓存」，免得在离线机器上卡住。
    返回 (是否有效, 说明)。
    """
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD),
                    ("pcwszFilePath", wintypes.LPCWSTR),
                    ("hFile", wintypes.HANDLE),
                    ("pgKnownSubject", ctypes.POINTER(GUID))]

    class WINTRUST_DATA(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pPolicyCallbackData", ctypes.c_void_p),
            ("pSIPClientData", ctypes.c_void_p),
            ("dwUIChoice", wintypes.DWORD),
            ("fdwRevocationChecks", wintypes.DWORD),
            ("dwUnionChoice", wintypes.DWORD),
            ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)),
            ("dwStateAction", wintypes.DWORD),
            ("hWVTStateData", wintypes.HANDLE),
            ("pwszURLReference", wintypes.LPCWSTR),
            ("dwProvFlags", wintypes.DWORD),
            ("dwUIContext", wintypes.DWORD),
            ("pSignatureSettings", ctypes.c_void_p),
        ]

    # WINTRUST_ACTION_GENERIC_VERIFY_V2 {00AAC56B-CD44-11d0-8CC2-00C04FC295EE}
    action = GUID(0x00AAC56B, 0xCD44, 0x11D0,
                  (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    fi = WINTRUST_FILE_INFO(ctypes.sizeof(WINTRUST_FILE_INFO),
                            ctypes.c_wchar_p(os.path.abspath(path)), None, None)
    wd = WINTRUST_DATA()
    wd.cbStruct = ctypes.sizeof(WINTRUST_DATA)
    wd.dwUIChoice = 2                     # WTD_UI_NONE：绝不弹窗
    wd.fdwRevocationChecks = 0            # WTD_REVOKE_NONE：不联网
    wd.dwUnionChoice = 1                  # WTD_CHOICE_FILE
    wd.pFile = ctypes.pointer(fi)
    wd.dwStateAction = 1                  # WTD_STATEACTION_VERIFY
    wd.dwProvFlags = 0x1000 | 0x100       # CACHE_ONLY_URL_RETRIEVAL | SAFER_FLAG
    try:
        wt = ctypes.windll.wintrust
        hr = wt.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(wd))
        # 必须收尾释放状态，否则句柄泄漏
        wd.dwStateAction = 2              # WTD_STATEACTION_CLOSE
        try:
            wt.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(wd))
        except Exception:
            pass
        if hr == 0:
            return True, "签名有效"
        return False, "0x%08X" % (hr & 0xFFFFFFFF)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_cable_installer(path):
    """★ 提权运行之前必须过这一关：**这个 exe 是不是我们认得的那个**。

    为什么非做不可 —— 这是一条真实的提权路径：
      `vbcable\\` 就在程序目录里，**普通用户权限就能写**。一个低权限的恶意程序
      把 `VBCABLE_Setup_x64.exe` 换成自己的东西，然后等用户点「一键安装虚拟声卡」，
      我们的 `ShellExecuteW(None, "runas", exe, ...)` 就会把它提升到管理员。
      UAC 框上显示的还是用户预期的那个名字 —— 从"普通用户进程"到"管理员"就这么一步。

    判定规则（两选一即可，这样官方以后发新版本也不会把一键安装卡死）：
      · SHA256 命中我们审过的那份官方文件；**或者**
      · 有有效的 Authenticode 签名（证书链可信）—— 官方包是 BUREL VINCENT 签的。
    返回 (是否放行, 给用户看的一行说明)。
    """
    if not path or not os.path.exists(path):
        return False, i18n.L("文件不存在", "file does not exist")
    digest = _sha256_file(path)
    if digest and digest == CABLE_SETUP_SHA256:
        return True, i18n.L(f"SHA256 与官方文件一致（{digest[:16]}…）",
                            f"SHA256 matches the official file ({digest[:16]}…)")
    ok, why = _authenticode_ok(path)
    if ok:
        return True, i18n.L(f"不是我们审过的那一版，但有有效数字签名（{why}）—— 放行",
                            f"Not the revision we audited, but it carries a valid digital "
                            f"signature ({why}) — allowed")
    return False, i18n.L(
        f"既不是官方文件（SHA256={digest[:16] or '读取失败'}…），也没有有效数字签名（{why}）",
        f"Neither the official file (SHA256={digest[:16] or 'unreadable'}…) nor validly "
        f"signed ({why})")


def is_elevated():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _read_python_txt():
    """读 python.txt 里那一行解释器路径。

    ⚠ 编码：启动器（vbs）是用 **ANSI/GBK** 读这个文件的，而这里有中文路径时
    用 UTF-8 读会得到乱码、`os.path.exists` 直接为假 —— 两边读法不一致就是个雷。
    所以先按 UTF-8 试，不成就按系统 ANSI 再来一次。
    """
    p = os.path.join(HERE, "python.txt")
    try:
        raw = open(p, "rb").read()
    except OSError:
        return None
    for enc in ("utf-8-sig", "utf-8", "mbcs", "gbk"):
        try:
            line = raw.decode(enc).strip().splitlines()[0].strip()
        except (UnicodeDecodeError, LookupError):
            continue
        except IndexError:
            return None
        if line and os.path.exists(line):
            return line
    return None


def host_pythonw():
    """跑「清理脚本 / 一次性优化」该用哪个解释器 —— **必须在 .venv 和 runtime 之外**。

    ⚠ 必须排除 .venv 和 runtime 里的解释器：以前这里用 sys.executable，而客户端自己就跑在
    `.venv\\Scripts\\pythonw.exe` 上，于是清理脚本也由那个 pythonw.exe 启动 ——
    而 Windows 上**正在运行的镜像文件是锁住的**，结果 .venv 永远删不干净：
    日志里是「✗ 删不掉 .venv（可能还有进程占着）」，最后只剩一个
    `Scripts\\pythonw.exe` 的空壳。更糟的是下次双击启动器会优先选这个壳
    → 界面根本打不开，"双击没反应"。

    离线全量包（内置 `runtime\\`）是同一个问题的翻版：清理脚本会跑在
    `runtime\\pythonw.exe` 上，于是那 154MB 的运行时死活删不掉。所以这里一并排除。
    真的一个外部解释器都没有时（离线包 + 目标机没装 Python），只能退回包内的 ——
    那种情况由 `uninstall.py` 的「退出后再删」兜底，不再是死路。
    """
    def outside_app_env(p):
        if not p or not os.path.exists(p):
            return False
        try:
            a = os.path.normcase(os.path.abspath(p))
            for d in (VENV, os.path.join(HERE, "runtime")):
                if a.startswith(os.path.normcase(os.path.abspath(d)) + os.sep):
                    return False
            return True
        except Exception:
            return False

    line = _read_python_txt()
    if line and outside_app_env(line):
        return line
    cands = [os.path.join(os.path.dirname(sys.executable), "pythonw.exe")]
    for ver in ("313", "312", "311", "310"):
        cands.append(os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Programs", "Python",
            f"Python{ver}", "pythonw.exe"))
        cands.append(rf"C:\Python{ver}\pythonw.exe")
    for c in cands:
        if outside_app_env(c):
            return c
    # 一个外部解释器都没有：退回包内的（清理脚本会走"退出后再删"那条路）
    for p in (os.path.join(VENV, "Scripts", "pythonw.exe"),
              os.path.join(HERE, "runtime", "pythonw.exe")):
        if os.path.exists(p):
            return p
    return sys.executable


def best_pythonw():
    """该用哪个解释器重启自己 —— 必须跟启动器（vbs）同一套优先级。

    ⚠ 不能用 sys.executable：当 app 跑在系统 Python、
    依赖却装在 .venv 里时，重启只会拉起又一个没依赖的自己 —— 死循环，
    用户看到的就是"重启了还是缺依赖"。
    """
    v = os.path.join(VENV, "Scripts", "pythonw.exe")
    if os.path.exists(v):
        return v
    line = _read_python_txt()
    if line:
        return line
    w = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return w if os.path.exists(w) else sys.executable


def relaunch_elevated():
    """以管理员身份重新拉起自己 —— **只会弹一次 UAC**。

    Frida 首次挂载 WUDFHost 可能要一次提权。与其让它在后台反复重试、把授权框
    弹个不停，不如给用户一个明确的一次性入口：点一下 → 弹一次 → 点「是」。
    """
    try:
        import ctypes
    except Exception:
        return False, i18n.L("无法提权（ctypes 不可用）",
                             "Cannot elevate (ctypes unavailable)")
    exe = sys.executable
    params = f'"{os.path.join(HERE, "app.py")}" --silent --takeover'
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, HERE, 1)
    except Exception as e:
        return False, i18n.L(f"提权启动失败：{e!r}", f"Elevation launch failed: {e!r}")
    if rc <= 32:
        return False, i18n.L(f"提权被拒绝或失败（ShellExecute 返回 {rc}）",
                             f"Elevation was denied or failed "
                             f"(ShellExecute returned {rc})")
    return True, i18n.L("已拉起管理员实例。请在 UAC 里点「是」，约 5 秒后刷新本页面",
                        "Administrator instance launched. Click “Yes” in the UAC "
                        "prompt, then refresh this page in about 5 seconds")


def qwen_flag_state():
    """千问输入法是否已允许「程序注入的按键」。它默认会丢弃注入按键。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            v, _ = winreg.QueryValueEx(k, "QIANWEN_IME_UTILITY_VOICE_HOOK_ALLOW_INJECTED")
            return str(v) not in ("", "0")
    except Exception:
        return False


def qwen_installed():
    """千问输入法装了吗？只读目录/卸载信息，不启动任何东西、不写注册表。"""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    lad = os.environ.get("LOCALAPPDATA", "")
    for p in (os.path.join(pf, "QianwenIME"), os.path.join(pf86, "QianwenIME"),
              os.path.join(lad, "QianwenIME") if lad else ""):
        if p and os.path.isdir(p):
            return True
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    with winreg.OpenKey(root, sub) as k:
                        n = winreg.QueryInfoKey(k)[0]
                except OSError:
                    continue
                for i in range(n):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                            dn, _ = winreg.QueryValueEx(sk, "DisplayName")
                    except OSError:
                        continue
                    s = str(dn).lower().replace(" ", "")
                    if "qianwen" in s or "千问" in s:
                        return True
    except Exception:
        pass
    return False


# 用户在「清理」里删过千问开关 → 留个标记，之后不再自动加回来。
# 为什么要有它：否则用户删了、下次启动又冒出来，看着像个删不掉的 bug。
QWEN_OPTOUT = os.path.join(HERE, "qwen_optout")


def auto_qwen_flag(on_line):
    """**首次启动就替用户把千问「允许注入」打开** —— 这才是默认该有的样子。

    为什么必须自动化：这个开关决定"语音键在千问里到底出不出字"。
    把它做成设置页里的一个按钮，结果就是用户按了语音键没反应、也不知道还有这么个开关。
    写 HKCU\\Environment 不需要管理员、随时可以删，
    没有任何理由让用户自己去找。
    """
    if os.path.exists(QWEN_OPTOUT):
        return False
    if not qwen_installed():
        return False                     # 没装千问就别往注册表留垃圾
    if qwen_flag_state():
        return False                     # 已经是开的，不折腾（也就不重启输入法）
    on_line(i18n.L("千问输入法：检测到已安装，自动打开「允许注入」"
                   "（不开的话千问会丢弃程序发的语音快捷键）",
                   "Qianwen IME: found, turning on “allow injection” automatically "
                   "(without it Qianwen discards the voice hotkey this program sends)"))
    enable_qwen_flag(on_line)
    return True


def qwen_exe(name):
    """找到千问某个 exe 的完整路径（版本子目录会变，挑版本号最大的那个）。"""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    lad = os.environ.get("LOCALAPPDATA", "")
    hits = []
    for root in (os.path.join(pf, "QianwenIME"), os.path.join(pf86, "QianwenIME"),
                 (os.path.join(lad, "QianwenIME") if lad else "")):
        if not root or not os.path.isdir(root):
            continue
        direct = os.path.join(root, name)
        if os.path.exists(direct):
            hits.append(("", direct))
        try:
            subs = os.listdir(root)
        except OSError:
            continue
        for sub in subs:
            p = os.path.join(root, sub, name)
            if os.path.exists(p):
                hits.append((sub, p))
    if not hits:
        return None
    # 版本号倒序：0.9.0.32 > 0.9.0.9（按数字段比，别按字符串比）
    def key(item):
        return [int(x) if x.isdigit() else 0 for x in item[0].replace("-", ".").split(".")]
    hits.sort(key=key, reverse=True)
    return hits[0][1]


def frida_leftovers():
    """Frida 的历史残留：几条 frida-* 服务、几个 frida-helper 进程。

    为什么要专门看这个：Frida 每挂载一个不属于自己的进程（我们的目标 WUDFHost
    是 SYSTEM），都会注册一条临时服务 `frida-<helper pid>-x86*/x86_64` ——
    而注册服务需要管理员，所以**普通权限下每挂一次就弹一次 UAC**。
    客户端重启一次就重新挂一次，于是"重启几次就弹几次"。残留会随重启不断累积
    （攒到几十条很常见）。
    一次性优化（`一键优化`）就是干这个的：清残留 + 让自启以最高权限跑，之后一次都不弹。
    """
    svc = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services") as k:
            i = 0
            while True:
                try:
                    n = winreg.EnumKey(k, i)
                except OSError:
                    break
                if n.lower().startswith("frida-"):
                    svc.append(n)
                i += 1
    except OSError:
        pass
    help_n = 0
    for exe in ("frida-helper-x86.exe", "frida-helper-x86_64.exe"):
        if _proc_running(exe):
            help_n += 1
    return {"services": len(svc), "helpers": help_n, "names": svc}


def _proc_running(name):
    """进程在不在（只读，tasklist）。名字要带 .exe。"""
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                             capture_output=True, timeout=15,
                             creationflags=CREATE_NO_WINDOW).stdout or b""
        return name.lower().encode() in out.lower()
    except Exception:
        return False


def _broadcast_env_change():
    """告诉已运行的进程"环境变量变了"（标准做法）。失败无所谓。"""
    try:
        import ctypes
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x1A, 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 2000, None)
    except Exception:
        pass


def enable_qwen_flag(on_line):
    """打开千问的「允许注入」，**并且真的把它重启到生效**。

    三步缺一不可，少一步就等于没做：
      1. 写 `HKCU\\Environment`（静默、不需要管理员）；
      2. **同步更新本进程的 os.environ** —— 子进程继承的是父进程的环境块，
         只写注册表的话，下面拉起来的千问读到的还是旧值，重启等于白重启；
      3. **杀完必须拉回来**。千问的 `QianwenIMEServer.exe` / `QianwenIMEUiClient.exe`
         被杀之后**不会自己回来**（维护服务是 Automatic 但不管保活），
         只杀不拉就是"把用户的输入法弄瘸了"。
         那两个 exe 本身是启动器壳：跑一下就派生真正的进程然后自己退出，所以直接拉它们就行。
    """
    import winreg
    name = "QIANWEN_IME_UTILITY_VOICE_HOOK_ALLOW_INJECTED"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, "1")
    os.environ[name] = "1"                      # ← 关键：让下面拉起的子进程继承到
    _broadcast_env_change()
    on_line(i18n.L(f"已写入用户环境变量 {name}=1",
                   f"Wrote user environment variable {name}=1"))

    exes = [qwen_exe("QianwenIMEServer.exe"), qwen_exe("QianwenIMEUiClient.exe")]
    killed = False
    for exe in ("QianwenIMEServer.exe", "QianwenIMEUiClient.exe"):
        try:
            rc = subprocess.run(["taskkill", "/IM", exe, "/F"],
                                creationflags=CREATE_NO_WINDOW,
                                capture_output=True, timeout=20).returncode
            if rc == 0:
                killed = True
        except Exception:
            pass
    # 杀掉后要等一下，否则新进程可能被旧实例的残留挡回去
    if killed:
        time.sleep(1.5)

    launched, failed = [], []
    for p in exes:
        if not p:
            continue
        try:
            subprocess.Popen([p], cwd=os.path.dirname(p),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_GROUP
            launched.append(os.path.basename(p))
        except Exception:
            failed.append(os.path.basename(p))      # 多半是"需要提升"——见下面的兜底核实

    # 别信"启动成功"，隔几秒回头核实（QianwenIMEUiClient.exe 带 requireAdministrator
    # 清单，非管理员的我们根本拉不起来，但千问的维护服务会自己把它捞回来）。
    if killed or failed:
        time.sleep(4)
        for p in exes:
            if not p:
                continue
            base = os.path.basename(p)
            if _proc_running(base):
                if base in failed:
                    failed.remove(base)
                if base not in launched:
                    launched.append(base)
    if launched:
        on_line(i18n.L("已重启千问输入法（" + "、".join(launched) + "），新开关已生效",
                       "Restarted the Qianwen IME (" + ", ".join(launched)
                       + "); the new switch is now in effect"))
    if failed:
        on_line(i18n.L("⚠ 这几个千问进程没起来：" + "、".join(failed)
                       + "（通常由千问的维护服务自动拉起；若语音键仍无效，手动打开一次千问即可）",
                       "⚠ These Qianwen processes did not come back: " + ", ".join(failed)
                       + " (its maintenance service usually restarts them; if the voice key still "
                         "does nothing, just open Qianwen once by hand)"))
    if not killed and not launched:
        on_line(i18n.L("千问当时没在跑，下次它启动时会带上新开关",
                       "Qianwen was not running; it will pick up the new switch next time it starts"))


# ============================================================ 自启动
def autostart_value():
    # --boot：告诉启动器/app"这一份是开机自启拉起来的"，app 靠它关掉
    # "自动弹 UAC 装虚拟声卡"那条路（见 main() 里 auto_ok 的注释）。
    return f'wscript.exe "{AUTOSTART_VBS}" --silent --boot'


def autostart_state():
    """开机自启是否已设置。两种方式任一存在即算：HKCU Run，或最高权限计划任务。

    计划任务那条是为了**彻底不弹 UAC**：普通权限启动时，Frida 给助手提权会弹框；
    以最高权限的计划任务启动，权限现成，一次都不会弹。
    """
    if plan_task_exists():
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            v, _ = winreg.QueryValueEx(k, AUTOSTART_NAME)
            return bool(v)
    except Exception:
        return False


def plan_task_exists(name=AUTOSTART_NAME):
    """只看 Tasks 目录里的文件 —— 不需要管理员，比 schtasks 可靠。"""
    p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                     "System32", "Tasks", name)
    return os.path.exists(p)


ELEVATE_PY = os.path.join(HERE, "elevate_setup.py")
ELEVATE_LOG = os.path.join(HERE, "elevate_setup.log")
UNINSTALL_PY = os.path.join(HERE, "uninstall.py")
UNINSTALL_LOG = os.path.join(HERE, "uninstall.log")
UNINSTALL_RESULT = os.path.join(HERE, "_uninstall_result.json")


def relaunch_elevated_script(script, wait_hide=True):
    """用 UAC 提权跑一个脚本 —— **只弹这一次**。"""
    try:
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script}"', HERE,
            0 if wait_hide else 1)
    except Exception as e:
        return False, i18n.L(f"提权失败：{e!r}", f"Elevation failed: {e!r}")
    if rc <= 32:
        return False, i18n.L(f"提权被拒绝或失败（ShellExecute 返回 {rc}）",
                             f"Elevation was denied or failed "
                             f"(ShellExecute returned {rc})")
    return True, i18n.L("已请求管理员权限", "Administrator rights requested")


def reset_bluetooth_radio():
    """重置蓝牙电台（要管理员）：拆掉可能"卡住"的那条 BLE 连接。

    为什么需要（真机踩过）：BLE 外设只接受**一个**连接。Windows 的 HID 栈有时会把
    那条唯一的连接攥住不放 —— 遥控器从此不再广播，而程序要自己建一条 GATT 连接
    才能收语音，于是"永远连不上"（表现：日志一直刷连接超时，但按键还有用）。
    拔电池/关开蓝牙开关能解开；这个动作就等于"用代码关开一次蓝牙电台"。
    """
    try:
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{ELEVATE_PY}" --reset-bt', HERE, 0)
    except Exception as e:
        return False, i18n.L(f"提权失败：{e!r}", f"Elevation failed: {e!r}")
    if rc <= 32:
        return False, i18n.L(f"提权被拒绝或失败（ShellExecute 返回 {rc}）",
                             f"Elevation was denied or failed (ShellExecute returned {rc})")
    return True, i18n.L("已请求管理员权限：正在重置蓝牙电台，几秒后会自动重连",
                        "Administrator rights requested: resetting the Bluetooth radio; "
                        "it reconnects automatically in a few seconds")


def run_reset_bt(on_line):
    """已经以管理员身份在跑：直接执行提权脚本的 --reset-bt，把输出喂给界面。"""
    import subprocess
    on_line(i18n.L("重置蓝牙电台：禁用 → 等 3 秒 → 启用（期间蓝牙设备会短暂断开）…",
                   "Resetting the Bluetooth radio: disable → wait 3 s → enable "
                   "(Bluetooth devices drop briefly)…"))
    try:
        p = subprocess.run([sys.executable, ELEVATE_PY, "--reset-bt"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=HERE, timeout=120)
        for ln in (p.stdout or "").splitlines():
            on_line(ln)
        if p.stderr:
            on_line((p.stderr or "").strip()[:300])
        on_line(i18n.L("完成 —— 几秒后会自动重连遥控器",
                       "Done — the remote reconnects automatically in a few seconds"))
    except Exception as e:
        on_line(i18n.L(f"失败：{e!r}", f"Failed: {e!r}"))


def follow_elevate_log():
    """提权后的进程没有 stdout 给我们，所以它把过程写进 elevate_setup.log，
    这里跟着读出来显示在界面上（daemon 线程，进程退出就一起走）。"""
    deadline = time.time() + 120
    seen = 0
    while time.time() < deadline:
        if os.path.exists(ELEVATE_LOG):
            try:
                with open(ELEVATE_LOG, encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
            except Exception:
                lines = []
            for ln in lines[seen:]:
                BUS.log(ln)
            seen = len(lines)
            if lines and lines[-1].strip().endswith("DONE"):
                BUS.push({"ev": "state",
                          "state": state_snapshot(load_config(), ROLES)})
                return
        time.sleep(0.5)
    BUS.log(i18n.L("（等提权进程结束超时；稍后可直接看 elevate_setup.log）",
                   "(Timed out waiting for the elevated process; you can read elevate_setup.log later)"))


def run_elevate_setup(on_line):
    """已经在管理员身份下就直接跑（省掉一次 UAC）。"""
    import elevate_setup
    rc = elevate_setup.main()
    try:
        with open(ELEVATE_LOG, encoding="utf-8", errors="replace") as f:
            for ln in f.read().splitlines():
                on_line(ln)
    except Exception:
        pass
    return rc


def follow_uninstall_log():
    """跟着读卸载日志。卸载脚本最后会结束本进程，所以这里大概率读不到结尾 —— 正常。"""
    deadline = time.time() + 180
    seen = 0
    while time.time() < deadline:
        if os.path.exists(UNINSTALL_LOG):
            try:
                with open(UNINSTALL_LOG, encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
            except Exception:
                lines = []
            for ln in lines[seen:]:
                BUS.log(ln)
            seen = len(lines)
            if lines and lines[-1].strip().endswith("DONE"):
                # 卸载脚本最后会把本进程杀掉，所以这里要**抢在死之前**把结果推给界面，
                # 否则用户那边只会看到"忽然读不到状态"，以为程序崩了。
                msg = i18n.L("已按你勾选的项清理完毕",
                             "Cleanup of the items you selected is complete")
                failed = []
                kills = True
                try:
                    with open(UNINSTALL_RESULT, encoding="utf-8-sig") as f:
                        d = json.load(f)
                    items = d.get("items") or []
                    failed = d.get("failed") or []
                    kills = bool(d.get("kills_client", True))
                    msg = i18n.L(f"已清理 {d.get('count', len(items))} 项：",
                                 f"Cleaned {d.get('count', len(items))} item(s): ") \
                        + i18n.L("、", ", ").join(items)
                    if failed:
                        # 报成功但东西还在 —— 必须如实说，否则用户下次启动失败会更懵
                        msg += i18n.L("；但有 ", "; but ") \
                            + i18n.L(f"{len(failed)} 项没删掉：",
                                     f"{len(failed)} item(s) were not removed: ") \
                            + i18n.L("、", ", ").join(failed)
                except Exception:
                    pass
                finally:
                    # 读完就删：它是清理脚本留下的"回执"，我们已经把内容推给界面了。
                    # 留着的话，"清理干净"的程序目录里会多一个没人再读的 json。
                    try:
                        os.remove(UNINSTALL_RESULT)
                    except OSError:
                        pass
                BUS.push({"ev": "uninstalled", "msg": msg, "failed": failed,
                          "kills_client": kills})
                if kills:
                    BUS.log(i18n.L("清理完成 ✅ —— 客户端马上退出（页面读不到状态是正常的）",
                                   "Cleanup finished ✅ — the client is about to exit "
                                   "(losing the status feed is normal)"))
                else:
                    BUS.log(i18n.L("清理完成 ✅ —— 客户端继续运行，页面不用关。",
                                   "Cleanup finished ✅ — the client keeps running; "
                                   "leave this page open."))
                if failed:
                    BUS.log(i18n.L("⚠ 没删掉的项：", "⚠ Items not removed: ")
                            + i18n.L("、", ", ").join(failed)
                            + i18n.L(" —— 多半还有进程占着，关掉相关程序后再点一次「清理」。",
                                     " — something is probably still holding them; close "
                                     "the related programs and click “Cleanup” again."))
                time.sleep(1.2)          # 留点时间让 SSE 推出去再被结束
                return
        time.sleep(0.5)


def set_autostart(on, on_line=None):
    """用当前用户的 Run 项：**不需要管理员，所以永远不会弹 UAC**。

    （不用「启动文件夹 + VBS」或计划任务：它们会因为权限/编码各种翻车，
     而 Run 项在登录时以当前用户身份静默启动，最稳。）
    """
    import winreg
    key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            if on:
                if not os.path.exists(AUTOSTART_VBS):
                    return False, i18n.L(f"找不到启动脚本：{AUTOSTART_VBS}",
                                         f"Launcher script not found: {AUTOSTART_VBS}")
                val = autostart_value()
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, val)
                got, _ = winreg.QueryValueEx(k, AUTOSTART_NAME)   # 回读校验
                if got != val:
                    return False, i18n.L("写入后回读不一致，自启可能没生效",
                                         "Read-back after writing does not match; "
                                         "autostart may not be active")
                BUS.log(i18n.L(f"已写入开机自启：{val}", f"Autostart written: {val}"))
                return True, i18n.L("已设置开机自启（当前用户，无需管理员）",
                                    "Autostart enabled (current user, no administrator needed)")
            try:
                winreg.DeleteValue(k, AUTOSTART_NAME)
                return True, i18n.L("已取消开机自启", "Autostart disabled")
            except FileNotFoundError:
                return True, i18n.L("本来就没设置", "It was not enabled in the first place")
    except PermissionError as e:
        return False, i18n.L(f"没有权限写自启项（被安全策略挡了）：{e}",
                             f"No permission to write the autostart entry "
                             f"(blocked by security policy): {e}")
    except OSError as e:
        return False, i18n.L(f"写自启项失败：{type(e).__name__}: {e}",
                             f"Failed to write the autostart entry: "
                             f"{type(e).__name__}: {e}")


# ============================================================ 角色管理
class Roles:
    """把语音和按键两个角色装在本进程的线程里，并提供启动/停止/重启。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.voice = None
        self.keytap = None
        self.lock = threading.Lock()

    def _on_event(self, kind, payload=None):
        if kind == "log":
            BUS.log(payload)
        elif kind == "key":
            p = payload or {}
            kid, down = p.get("id"), bool(p.get("down"))
            BUS.push({"ev": "key", "id": kid, "down": down})
            # 按键事件必须同时写一行日志：用户「按了半天没反应」时，
            # 只有日志能区分「根本没读到」和「读到了但没映射」。
            if down:
                label = REMOTE_LABELS.get(kid, str(kid))
                if kid == "voice":
                    spec = self.cfg.get("voice") or {}
                    how = "按一下" if spec.get("mode") == "tap" else "按住"
                    BUS.log(f"[按键] 语音 → {how} {spec.get('key')}")
                else:
                    m = (self.cfg.get("keys") or {}).get(kid)
                    BUS.log(f"[按键] {label} → "
                            + (describe_mapping(m) if m else "（未映射）"))
        elif kind == "status":
            BUS.push({"ev": "state", "state": state_snapshot(self.cfg, self)})

    def start(self):
        d = deps_state()
        if not d["ok"]:
            return False, i18n.L(
                "缺依赖（" + "、".join(d["missing"]) +
                "）—— 先在「设置 → 诊断」点「安装基础依赖」",
                "Missing dependencies (" + ", ".join(d["missing"]) +
                ") — click “Install base dependencies” under “Settings → Diagnostics” first")
        if d.get("restart_needed"):
            return False, i18n.L(
                "依赖已经装进包内 .venv 了，但当前进程用的是另一个 Python —— "
                "点「重启整个客户端」才生效",
                "The dependencies are in the bundled .venv, but this process runs on "
                "a different Python — click “Restart the whole client” to apply")
        # 千问「允许注入」开关：语音键能不能出字的前提，必须在角色起来**之前**弄好，
        # 否则会出现"刚启动那几秒按了没反应"。它由 _ensure_qwen_flag() 负责（见那里的注释）。
        self._ensure_qwen_flag()
        with self.lock:
            if self.voice is None:
                import voice as voice_mod
                self.voice = voice_mod.VoiceRole(
                    self._on_event, addr=self.cfg.get("remote_addr"),
                    audio=self.cfg.get("audio"), voice_spec=self.cfg.get("voice"),
                    relay=bool((self.cfg.get("audio") or {}).get("relay", True)))
                self.voice.start()
            if self.keytap is None and importlib.util.find_spec("frida") is not None:
                import keys as keys_mod
                self.keytap = keys_mod.KeyTap(self._on_event)
                self.keytap.set_mapping(self.cfg.get("keys") or {})
                if self.voice is not None:
                    # 遥控器一发报文就叫醒语音角色去重连：它只在按键后醒一小会，
                    # 而这正是唯一能连上的窗口（退避最多 45 秒，靠等会全错过）。
                    self.keytap.on_activity = self.voice.poke
                self.keytap.start()
        BUS.log(i18n.L("客户端已启动", "Client started"))
        BUS.push({"ev": "state", "state": state_snapshot(self.cfg, self)})
        return True, i18n.L("已启动", "Started")

    def _ensure_qwen_flag(self):
        """语音键在千问里能不能出字，取决于这个全局开关 —— 起角色前先弄好。

        ⚠ 刻意**放在"启动角色"这条必经之路上**（而不是 main() 里无条件调用）：
          `--no-start` 的语义是"只开界面、什么都不启动"，而 main() 里无条件调用会让
          **打包自检**也去改用户 HKCU 里的全局环境变量、还顺带重启一次千问输入法 ——
          自检不该留下这种副作用（本机构建完自检完，机器就不"干净"了）。
          放在这里还有个附带好处：`--no-start` 起来之后在界面点「启动」，开关照样会被弄好。
        """
        try:
            auto_qwen_flag(BUS.log)
        except Exception as e:
            BUS.log(i18n.L(
                f"自动打开千问注入开关失败（不影响使用，可在设置页手动点）：{e!r}",
                f"Could not turn on Qianwen's injection switch automatically (not fatal — "
                f"you can click it on the Settings page): {e!r}"))
        try:
            self._warn_ime_mismatch()
        except Exception:
            pass

    def _warn_ime_mismatch(self):
        """预设里的输入法和"本机实际装的"对不上 —— 那语音键就是白按。

        真机踩过两次：预设被切成「微信输入法」之后，遥控器发的是 **Ctrl+Win**，而千问
        只认 **RALT** —— 连接、音频、音频帧全都正常，唯独千问的浮层不弹，用户看到的是
        "语音键没反应 / 千问不能用了"，而且这个选择**存在配置里**，重启后照旧。
        预设本来就把握手键绑在输入法上，所以启动时对一次、对不上直接说清楚。
        """
        ime = str((self.cfg.get("profile") or {}).get("ime") or "")
        v = self.cfg.get("voice") or {}
        key = str(v.get("key") or "").upper()
        mods = [str(m).upper() for m in (v.get("mods") or [])]
        if not qwen_installed():
            return
        if key == "RALT" and not mods:
            return                      # 千问认的就是「按住 RALT」，没问题
        spec = "+".join(mods + [key]) if key else "（空）"
        BUS.log(i18n.L(
            f"⚠ 本机装的是**千问**输入法，它只认「按住 RALT」，而现在注入的是 {spec}"
            + (f"（预设选的是「{ime}」）" if ime else "")
            + " —— 按语音键不会出字。到「按键映射 → 预设方案」把输入法切回「千问输入法」即可。",
            f"⚠ This machine has the **Qianwen** IME, which only answers “hold RALT”, but {spec} "
            f"is being injected" + (f" (preset: “{ime}”)" if ime else "")
            + " — the voice key will do nothing. Switch the preset's IME back to Qianwen."))

    def stop(self, quiet=False):
        with self.lock:
            if self.voice:
                self.voice.stop()
                self.voice = None
            if self.keytap:
                self.keytap.stop()
                self.keytap = None
        if not quiet:
            BUS.log(i18n.L("客户端已停止", "Client stopped"))
            BUS.push({"ev": "state", "state": state_snapshot(self.cfg, self)})
        return True, i18n.L("已停止", "Stopped")

    def restart(self):
        self.stop(quiet=True)
        time.sleep(0.4)
        return self.start()

    def apply_config(self, cfg):
        """配置保存后热更新：映射即时生效，不用重启。

        ★ 遥控器地址也要一起热更新：以前这里只管按键映射和语音键，地址改了却要
        重启客户端才生效 —— 用户看到的是"地址填对了、语音键还是没反应"。
        """
        self.cfg = cfg
        addr = str(cfg.get("remote_addr") or "").strip()
        changed = False
        with self.lock:
            if self.keytap:
                self.keytap.set_mapping(cfg.get("keys") or {})
            if self.voice:
                self.voice.set_voice_spec(cfg.get("voice") or {})
                changed = self.voice.set_addr(addr)
        if changed:
            BUS.log(i18n.L(
                f"遥控器地址已更新为 {addr or '（空）'} —— 立刻按新地址重连（无需重启）",
                f"Remote address updated to {addr or '(empty)'} — reconnecting now"))
        BUS.log(i18n.L("配置已生效（映射热更新，无需重启）",
                       "Config applied (mappings update live, no restart needed)"))

    @property
    def running(self):
        return self.voice is not None


ROLES: Roles | None = None


def state_snapshot(cfg, roles=None):
    roles = roles or ROLES
    d = deps_state()
    cable = cable_state()
    # 虚拟声卡是语音的**硬性前提**（没它语音出不了字），但**不并进 deps["missing"]**：
    # deps["ok"] 还管着"要不要启动语音/按键角色"，而按键映射根本不需要声卡 ——
    # 并进去会变成"没装声卡就整个不启动"，那是更糟的结果。所以单独给 cable_ok，
    # 由界面的「首次准备」把这几件事一起摆出来。
    # cable_ok 取"设备 + 注册表"两者之或：**装了就别再劝人重装**。
    # 另外单独给 cable_usable（设备真的能用）与 cable_ghost（注册表有、设备没有）：
    # 后者是"卸完还没重启 / 驱动被禁用 / 只装了半边"那种状态 —— 语音同样是坏的，
    # 顶部横幅必须说话，但要说的**不是**"点安装"而是"先重启一次"。
    d = dict(d,
             cable_ok=bool(cable["installed"] or cable["registered"]),
             cable_usable=bool(cable["installed"]),
             cable_ghost=bool(cable["registered"] and not cable["installed"]))
    v = getattr(roles, "voice", None)
    k = getattr(roles, "keytap", None)
    return {
        "running": bool(roles and roles.running),
        "remote_addr": cfg.get("remote_addr"),
        "voice": {
            "mode": (cfg.get("voice") or {}).get("mode", "hold"),
            "key": (cfg.get("voice") or {}).get("key", "RALT"),
            "mods": (cfg.get("voice") or {}).get("mods", []),
            "ready": bool(v and v.voice.ok),
            "connected": bool(v and v.connected),
            "note": (v.note if v else i18n.L("未启动", "Not started")),
        },
        "keys": {
            "ready": bool(k and k.ready),
            "note": (k.note if k else (i18n.L("未安装 frida", "frida not installed")
                                       if not d["ok"]
                                       else i18n.L("未启动", "Not started"))),
            "blocked_count": len(getattr(k, "blocked_usages", []) or []),
        },
        "deps": d,
        "autostart": autostart_state(),
        "qwen_flag": qwen_flag_state(),
        # 界面要靠这个决定"要不要摆那个按钮"：没装千问的机器上摆一个点了没反应的
        # 按钮比不摆更糟（用户会以为程序坏了）。
        "qwen_installed": qwen_installed(),
        "qwen_optout": os.path.exists(QWEN_OPTOUT),
        # 当前语言（zh/en）。界面靠它确认"后端跟界面说的是同一种话"。
        "lang": i18n.current(),
        # 虚拟声卡：语音的物理前提。界面靠这个决定"要不要显示安装按钮"。
        "cable": cable,
        "frida_leftovers": frida_leftovers(),
        "elevated": is_elevated(),
        # 「一键优化」做过没有 = 最高权限计划任务在不在。做了之后 Frida 不再弹 UAC。
        "optimized": plan_task_exists(),
        "versions": {"python": sys.version.split()[0], "app": APP_VERSION,
                     "bundled": is_bundled_runtime(),
                     "runtime": (i18n.L("内置运行时（离线包）",
                                        "Bundled runtime (offline bundle)")
                                 if is_bundled_runtime()
                                 else (".venv" if venv_python()
                                       else i18n.L("系统 Python", "system Python")))},
        "home": HERE,          # 界面会显示它 —— 防止"跑的是另一份副本"这种事故
    }


# ============================================================ 后台任务
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_SEQ = [0]
MAX_JOBS = 40                  # 同时在内存里保留的任务上限（超了淘汰最老的）
RUNNING = {}                   # 标题 -> jid：同一个任务不许并发跑两份


def new_job(title):
    # ⚠ 原来的 id 是 `int(time.time()*1000) % 1000000` —— 同一毫秒内起两个任务会**撞 id
    #   并互相覆盖**（真实但难撞）。加一个自增序号就不会了，而且仍然按时间递增。
    with JOBS_LOCK:
        JOB_SEQ[0] += 1
        jid = f"job{int(time.time() * 1000) % 1000000}{JOB_SEQ[0] % 1000}"
        JOBS[jid] = {"title": title, "lines": [], "done": False}
        # 淘汰：JOBS 原来只增不减，每个任务还能挂 4000 行 —— 攒久了就是纯泄漏。
        # 从最老的开始扔（dict 保持插入顺序）；**正在跑的那些不扔**（RUNNING 里那几条），
        # 否则用户会看到"任务在跑但输出一片空白"。RUNNING 的条数 = 不同动作标题数（十来个），
        # 所以这个上限是硬的。
        if len(JOBS) > MAX_JOBS:
            live = set(RUNNING.values())
            for old in list(JOBS):
                if len(JOBS) <= MAX_JOBS:
                    break
                if old in live or old == jid:
                    continue
                JOBS.pop(old, None)
    return jid


def job_line(jid, line):
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if j and len(j["lines"]) < 4000:
            j["lines"].append(line)
    BUS.push({"ev": "job", "job": jid, "line": line, "done": False})


def job_done(jid):
    with JOBS_LOCK:
        if jid in JOBS:
            JOBS[jid]["done"] = True
    BUS.push({"ev": "job", "job": jid, "line": "", "done": True})


def background(title, fn):
    """起一个后台任务。**同一个标题不许并发跑两份** —— 返回已经在跑的那个 jid。

    为什么必须去重（这不是防 DoS，是防真损坏）：重复点「全部安装」/「安装基础依赖」
    会同时起两个 `pip install --target .venv` 进程，往同一个目录里写 —— 轻则报错，
    重则把 .venv 装成半成品（而启动器会优先选那个半成品，表现为"双击没反应"）。
    """
    with JOBS_LOCK:
        running = RUNNING.get(title)
        if running and not JOBS.get(running, {}).get("done"):
            BUS.log(i18n.L(f"「{title}」已经在跑了，这次不重复起",
                           f"“{title}” is already running; not starting another one"))
            return running
    jid = new_job(title)
    with JOBS_LOCK:
        RUNNING[title] = jid
    BUS.log(i18n.L(f"开始：{title}", f"Started: {title}"))

    def runner():
        try:
            rc = fn(lambda s: job_line(jid, s))
            job_line(jid, i18n.L(f"完成（返回码 {rc}）", f"Done (return code {rc})")
                     if rc is not None else i18n.L("完成", "Done"))
        except Exception as e:
            job_line(jid, i18n.L(f"出错：{type(e).__name__}: {e}",
                                 f"Error: {type(e).__name__}: {e}"))
        finally:
            job_done(jid)
            with JOBS_LOCK:
                if RUNNING.get(title) == jid:
                    RUNNING.pop(title, None)      # 跑完了才允许同名任务再来一份
            BUS.log(i18n.L(f"结束：{title}", f"Finished: {title}"))
            BUS.push({"ev": "state", "state": state_snapshot(load_config(), ROLES)})

    threading.Thread(target=runner, daemon=True, name=f"job-{title}").start()
    return jid


# ============================================================ 环境体检
def do_check(on_line):
    cfg = load_config()
    on_line(f"{APP_NAME} v{APP_VERSION}")
    on_line(i18n.L(f"Python：{sys.version.split()[0]}  ({sys.executable})",
                   f"Python: {sys.version.split()[0]}  ({sys.executable})"))
    on_line(i18n.L(f"包目录：{HERE}", f"Package directory: {HERE}"))
    vpy = venv_python()
    if is_bundled_runtime():
        on_line(i18n.L("运行方式：**离线全量包**（自带 Python 运行时 + 预装依赖，"
                       "目标机器不需要装 Python，也不需要联网）",
                       "Mode: **offline full bundle** (bundles a Python runtime + "
                       "pre-installed dependencies; the target machine needs neither "
                       "Python nor internet access)"))
    else:
        on_line(i18n.L("包内虚拟环境：", "Bundled virtualenv: ")
                + (i18n.L(f"有（{vpy}）", f"yes ({vpy})") if vpy
                   else i18n.L("没有 —— 点「安装基础依赖」会自动创建",
                               "none — clicking “Install base dependencies” creates one "
                               "automatically")))
    d = deps_state()
    if d["ok"]:
        on_line(i18n.L("基础依赖（语音）：齐全 ✓",
                       "Base dependencies (voice): all present ✓"))
    else:
        for m in d["missing"]:
            on_line(i18n.L(f"基础依赖（语音）：缺 {m}",
                           f"Base dependencies (voice): missing {m}"))
    if d["keys_ok"]:
        on_line(i18n.L("按键映射依赖（frida）：齐全 ✓",
                       "Key-mapping dependency (frida): present ✓"))
    else:
        on_line(i18n.L("按键映射依赖（frida）：缺 ",
                       "Key-mapping dependency (frida): missing ")
                + i18n.L("、", ", ").join(d["missing_keys"])
                + i18n.L("（不装也可以用语音键）",
                         " (the voice button works without it)"))
    cables = find_cable_names()
    cab = cable_state()
    if cables:
        on_line(i18n.L(f"虚拟声卡：{cables[0]} ✓",
                       f"Virtual audio cable: {cables[0]} ✓"))
        on_line(i18n.L("  ⚠ 输入法里的麦克风要选 “CABLE Output (VB-Audio Virtual Cable)”",
                       "  ⚠ In your input method editor, select "
                       "“CABLE Output (VB-Audio Virtual Cable)” as the microphone"))
    elif cab["registered"]:
        # 注册表说装了、但枚举不到能用的输出端点。**必须跟 cable_state 说一致**
        # （以前这里只看设备列表，而别处取"设备或注册表"，同一个体检里会一个说
        #  "已装"、一个说"没找到"，用户完全不知道该信谁）。
        on_line(i18n.L("虚拟声卡：注册表里装了 VB-CABLE，但**现在枚举不到可用的输出端点** ✗",
                       "Virtual audio cable: VB-CABLE is registered but **no usable output "
                       "endpoint is enumerated right now** ✗"))
        on_line(i18n.L("  常见原因：设备被禁用了 / 只装了半边 / 卸载残留 / 刚装完还没重启。"
                       "先重启一次；还不行就在设备管理器里看 “VB-Audio Virtual Cable”，"
                       "或从界面点「安装虚拟声卡」重装一次",
                       "  Usual causes: the device is disabled / only one half got installed / "
                       "uninstall leftovers / installed but not rebooted yet. Reboot once; if "
                       "it still fails, look for “VB-Audio Virtual Cable” in Device Manager, "
                       "or reinstall from the UI's install button"))
    else:
        on_line(i18n.L("虚拟声卡：没找到 VB-CABLE ✗（遥控器麦克风没地方出声）",
                       "Virtual audio cable: VB-CABLE not found ✗ (the remote "
                       "microphone has nowhere to play)"))
        miss = cable_files_missing()
        if miss:
            # 只放一个 exe 是不够的 —— 安装器要从自己所在目录读 .inf/.sys。
            # 这里明说，免得用户点了按钮只看到一个装不上的报错。
            on_line(i18n.L("  ⚠ 包内安装器不全（缺 " + "、".join(miss)
                           + "）：点按钮时程序会先去官网补齐，也可以手动把整个包解压到 "
                             "vbcable\\ 里",
                           "  ⚠ The bundled installer is incomplete (missing "
                           + ", ".join(miss) + "): the program will fetch the full set from "
                             "the official site when you click the button, or you can unzip "
                             "the whole package into vbcable\\ yourself"))
        if cable_state()["bundled"]:
            on_line(i18n.L("  装法：点界面「安装虚拟声卡（VB-CABLE）」—— 包内已带安装器，需要一次管理员",
                           "  How to install: click “Install the virtual audio cable "
                           "(VB-CABLE)” in the UI — the installer is bundled and needs "
                           "administrator rights once"))
        else:
            on_line(i18n.L("  装法：点界面「安装虚拟声卡（VB-CABLE）」—— 会自动从官网下载（约 1.3MB）再安装，"
                           "需要一次管理员",
                           "  How to install: click “Install the virtual audio cable "
                           "(VB-CABLE)” in the UI — it downloads from the official site "
                           "(~1.3MB) and installs; needs administrator rights once"))
            on_line(i18n.L(f"  手动下载：{CABLE_URL}", f"  Manual download: {CABLE_URL}"))
        on_line(i18n.L("  ⚠ VB-Audio 官方要求：装完**必须重启一次**系统才算装完",
                       "  ⚠ VB-Audio's own requirement: you **must reboot** to finalize "
                       "the installation"))
    try:
        import keys as keys_mod
        pid = keys_mod.find_wudfhost_pid()
        if pid:
            on_line(i18n.L(f"蓝牙 HID 驱动宿主：PID {pid} ✓（遥控器在 HID 通道上活着）",
                           f"Bluetooth HID driver host: PID {pid} ✓ (the remote is alive "
                           f"on the HID channel)"))
        else:
            on_line(i18n.L("蓝牙 HID 驱动宿主：没找到 —— 遥控器配对了吗？按一下遥控器键试试",
                           "Bluetooth HID driver host: not found — is the remote paired? "
                           "Press a button on the remote and try again"))
    except Exception as e:
        on_line(i18n.L(f"蓝牙 HID 驱动宿主：查询失败 {type(e).__name__}",
                       f"Bluetooth HID driver host: query failed {type(e).__name__}"))
    # ★ 蓝牙电台本身的状态：被禁用时**所有蓝牙设备都会从列表里消失**（用户会以为"设备全没了"）。
    #   真机上出现过（重置电台那一步如果中途被打断就会这样）—— 这里主动查一次并给出手动恢复步骤。
    try:
        import subprocess as _sp
        rc, out = 0, ""
        try:
            p = _sp.run(["powershell", "-NoProfile", "-Command",
                         "$d = @(Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
                         "Where-Object { $_.InstanceId -like 'USB*' });"
                         "if ($d.Count -eq 0) { 'NO-RADIO' } else {"
                         " $d | ForEach-Object { $_.Status + ' | ' + $_.FriendlyName } }"],
                        capture_output=True, timeout=25, creationflags=CREATE_NO_WINDOW)
            out = ""
            raw = (p.stdout or b"") + (p.stderr or b"")
            for enc in ("utf-8", "gbk", "mbcs"):     # 中文设备名会是 GBK，写死 utf-8 会乱码
                try:
                    out = raw.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                out = raw.decode("utf-8", "replace")
        except Exception as e:
            out = f"QUERY-FAIL {e!r}"
        text = (out or "").strip()
        broken = []
        for line in text.splitlines():
            low = line.lower()
            if "|" in line and ("error" in low or "unknown" in low or "disabled" in low
                                or "已禁用" in line or "错误" in line):
                broken.append(line.strip())
        if "NO-RADIO" in text:
            on_line(i18n.L("蓝牙电台：没找到 USB 上的蓝牙适配器 —— 设备管理器里看看它是否被禁用/掉了",
                           "Bluetooth radio: no USB Bluetooth adapter found — check Device Manager "
                           "whether it is disabled or missing"))
        elif broken:
            on_line(i18n.L(
                f"⚠ 蓝牙电台状态不对（{broken[0]}）—— **所有蓝牙设备都会消失**。救回来："
                f"设备管理器 → 蓝牙 → 右键那个适配器 → 「启用设备」；不行就**完全关机**"
                f"（不是重启）再开机。",
                f"⚠ Bluetooth radio is not healthy ({broken[0]}) — **every Bluetooth device "
                f"disappears** while it is like this. Fix: Device Manager → Bluetooth → "
                f"right-click the adapter → Enable device; if that fails, do a full **shutdown** "
                f"(not a restart) and power on."))
        elif text and "|" in text:
            on_line(i18n.L(f"蓝牙电台：正常（{text.splitlines()[0]}）",
                           f"Bluetooth radio: OK ({text.splitlines()[0]})"))
    except Exception:
        pass
    # Frida 残留 / UAC 会不会反复弹 —— 用户最容易被这个吓到，必须明说
    fl = frida_leftovers()
    if fl["services"] or fl["helpers"]:
        on_line(i18n.L(
            f"Frida 残留：{fl['services']} 条 frida-* 服务、{fl['helpers']} 个 helper 进程",
            f"Frida leftovers: {fl['services']} frida-* service(s), "
            f"{fl['helpers']} helper process(es)"))
        if is_elevated():
            on_line(i18n.L("  当前已是管理员，挂载不会再弹 UAC；点「一键优化」可以顺手清掉这些残留",
                           "  Already running as administrator, so attaching will not "
                           "prompt for UAC again; “One-click optimization” can clear "
                           "these leftovers too"))
        else:
            on_line(i18n.L("  ⚠ 普通权限下**每次挂载都会重新注册服务 → 每挂一次弹一次 UAC**"
                           "（重启客户端也算一次）",
                           "  ⚠ Without administrator rights **every attach re-registers "
                           "the service → one UAC prompt per attach** (restarting the "
                           "client counts as one too)"))
            on_line(i18n.L("  修法：点「一键优化」—— 弹**最后一次** UAC，之后自启走最高权限计划任务，"
                           "再也不会弹",
                           "  Fix: click “One-click optimization” — the **last** UAC "
                           "prompt, after which autostart runs from a highest-privilege "
                           "scheduled task and never prompts again"))
    elif os.path.exists(os.path.join(VENV, "Lib", "site-packages", "frida")):
        on_line(i18n.L("Frida 残留：无 ✓（没有多余的服务和 helper 进程）",
                       "Frida leftovers: none ✓ (no extra services or helper processes)"))
    on_line(i18n.L(f"界面端口：{cfg.get('ui_port')}（只监听 127.0.0.1）",
                   f"UI port: {cfg.get('ui_port')} (listening on 127.0.0.1 only)"))
    on_line(i18n.L(f"开机自启：{'已设置' if autostart_state() else '未设置'}",
                   f"Autostart: {'enabled' if autostart_state() else 'not set'}"))
    if is_elevated():
        on_line(i18n.L("当前权限：管理员", "Privileges: administrator"))
    else:
        on_line(i18n.L("当前权限：普通用户（够用。只有按键映射提示「需要提权」时，"
                       "才在设置页点一次「以管理员身份重启」）",
                       "Privileges: standard user (enough. Only when key mapping says "
                       "“elevation required” do you click “Restart as administrator” once "
                       "on the Settings page)"))
    if qwen_flag_state():
        on_line(i18n.L("千问「允许注入」开关：已开 ✓",
                       "Qianwen “allow injection” switch: on ✓"))
    elif os.path.exists(QWEN_OPTOUT):
        on_line(i18n.L("千问「允许注入」开关：未开 —— 你之前在「清理」里明确删过它，"
                       "所以程序不再自动打开",
                       "Qianwen “allow injection” switch: off — you explicitly deleted "
                       "it under “Cleanup”, so the program no longer turns it back on "
                       "automatically"))
        on_line(i18n.L("  想再开：设置页点「打开千问允许注入开关」（会重启千问输入法）",
                       "  To turn it on again: click “Turn on Qianwen's allow-injection "
                       "switch” on the Settings page (this restarts the Qianwen IME)"))
    elif not qwen_installed():
        on_line(i18n.L("千问「允许注入」开关：本机没装千问输入法，不需要",
                       "Qianwen “allow injection” switch: the Qianwen IME is not "
                       "installed on this machine, so it does not apply"))
    else:
        on_line(i18n.L("千问「允许注入」开关：未开 —— 千问会丢弃程序发的语音快捷键",
                       "Qianwen “allow injection” switch: off — Qianwen will discard the "
                       "injected voice hotkey"))
        on_line(i18n.L("  修法：重启一次客户端就会自动打开（或点设置页那个按钮）",
                       "  Fix: restart the client once and it turns on automatically "
                       "(or click that button on the Settings page)"))
    out_idx, in_idx = None, None
    try:
        import voice as voice_mod
        out_idx, in_idx = voice_mod.find_cable()
    except Exception:
        pass
    on_line(i18n.L(f"音频端点：出口={out_idx} 输入={in_idx}",
                   f"Audio endpoints: output={out_idx} input={in_idx}"))
    on_line(i18n.L("蓝牙通道提醒：同一时间只允许一个客户端。若另一个客户端在跑，"
                   "本客户端会看到「缺少 ATVV 特征」。",
                   "Bluetooth channel reminder: only one client at a time. If another "
                   "client is running, this one reports “ATVV characteristic missing”."))
    return 0


def do_scan(on_line, on_data=None):
    try:
        import asyncio
        from bleak import BleakScanner
    except ImportError:
        on_line(i18n.L("缺 bleak，无法扫描。先点「安装依赖」。",
                       "bleak is missing, cannot scan. Click “Install dependencies” first."))
        return 1
    on_line(i18n.L("扫描 6 秒……（遥控器空闲时不广播，先按一下遥控器键再扫）",
                   "Scanning for 6 seconds… (the remote does not advertise while idle — "
                   "press any key on it first)"))

    async def run():
        return await BleakScanner.discover(timeout=6.0, return_adv=True)

    try:
        found = asyncio.run(run())
    except Exception as e:
        on_line(i18n.L(f"扫描失败 {type(e).__name__}: {e}",
                       f"Scan failed {type(e).__name__}: {e}"))
        return 1
    items = []
    for addr, (dev, adv) in (found or {}).items():
        name = dev.name or adv.local_name or i18n.L("(未知)", "(unknown)")
        on_line(f"  {addr}  {name}  RSSI={adv.rssi}")
        items.append({"address": addr, "name": name, "rssi": adv.rssi})
    if not items:
        on_line(i18n.L("没扫到设备。遥控器不按键时不广播，这是正常的。",
                       "No devices found. The remote does not advertise unless a key is pressed — "
                       "that is normal."))
    if on_data is not None:
        on_data(items)
    return 0


# ============================================================ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "VibeMote"
    protocol_version = "HTTP/1.1"

    # ★★ 本地 HTTP 服务必须自己防「借刀」。界面跑在 127.0.0.1 上、没有登录态，
    #    所以**任何网页**都能朝它发请求。两种攻击都是真的，各挡一层：
    #
    #  1) DNS rebinding：攻击者的域名解析到 127.0.0.1，浏览器就以为"同源"，
    #     于是能读响应、也能免预检地发 POST。→ 用 **Host 头白名单**挡：
    #     rebinding 时 Host 是攻击者的域名，不是 127.0.0.1/localhost。
    #
    #  2) 跨站简单请求（CSRF）：`fetch(url, {method:'POST', mode:'no-cors',
    #     headers:{'Content-Type':'text/plain'}, body:'{"action":"uninstall",…}'})`
    #     —— text/plain 属于 CORS 安全列表里的值，**不触发预检**，浏览器会直接把请求发出去；
    #     而我们的 _body() 本来不看 Content-Type，直接 json.loads ——
    #     于是攻击者读不到响应，但**动作已经真的做了**（装驱动弹 UAC、删配置、提权重启）。
    #     → 用 **要求 Content-Type: application/json** 挡：这会强制预检，
    #       而我们**不返回任何 CORS 头**，预检必然失败，请求根本发不出去。
    #       再叠一层 Origin 白名单，双保险。
    #
    # 代价：非浏览器客户端（启动器、命令行、curl）不发 Origin，所以"没有 Origin 就放行"，
    # 但**必须**带对 Content-Type 才能 POST。
    ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
    MAX_BODY = 1 << 20            # 1 MiB：配置/动作的 body 连 1KB 都用不到

    def log_message(self, *a):        # 别把访问日志打到 stderr
        pass

    # ---- 请求来源闸门 ----
    def _host_ok(self):
        h = (self.headers.get("Host") or "").strip().lower()
        if not h:
            return False
        if h.startswith("["):                 # IPv6 形如 [::1]:8787
            host = h.split("]", 1)[0] + "]"
        elif ":" in h:
            host = h.split(":", 1)[0]
        else:
            host = h
        return host in self.ALLOWED_HOSTS

    def _origin_ok(self):
        o = (self.headers.get("Origin") or "").strip().lower()
        if not o:
            return True                       # 非浏览器客户端不发 Origin
        if o == "null":
            return False                      # file:// 页面 / 沙箱 iframe
        try:
            u = urlparse(o)
        except Exception:
            return False
        return ((u.scheme or "") in ("http", "https")
                and (u.hostname or "") in ("127.0.0.1", "localhost", "::1"))

    def _fetch_site_ok(self):
        """Sec-Fetch-Site 兜底：`<img src="http://127.0.0.1:8787/api/events">` 这类
        **不发 Origin** 的跨站请求，只能靠这个头挡（现代浏览器都会带）。

        为什么单拎出来：`/api/events` 是长连接，服务端为每条连接挂一个线程 +
        一个队列等着。跨站页面用 `<img>`/EventSource 挂着不放，就能把线程耗光 ——
        而 `<img>` 的请求**没有 Origin**，上面那道闸门看不到它。
        没有这个头（老浏览器、curl、启动器）就放行 —— 它们不是"网页借刀"的载体。
        """
        s = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if not s:
            return True
        return s in ("same-origin", "none")

    def _guard(self, need_json=False):
        """通过返回 True；不通过时已经回过 4xx 了。"""
        if not self._host_ok():
            self._json({"ok": False, "msg": i18n.L(
                "拒绝：请求的 Host 不是本机地址（DNS 重绑定防护）",
                "Refused: the request Host is not this machine "
                "(DNS-rebinding protection)")}, 403)
            return False
        if not self._origin_ok():
            self._json({"ok": False, "msg": i18n.L(
                "拒绝：请求来自站外页面（跨站请求防护）",
                "Refused: the request came from another site's page "
                "(cross-site request protection)")}, 403)
            return False
        if not self._fetch_site_ok():
            self._json({"ok": False, "msg": i18n.L(
                "拒绝：请求是站外页面发起的（跨站防护）",
                "Refused: the request was initiated by another site's page "
                "(cross-site protection)")}, 403)
            return False
        if need_json:
            ct = (self.headers.get("Content-Type") or "").lower()
            if "application/json" not in ct:
                self._json({"ok": False, "msg": i18n.L(
                    "拒绝：POST 必须带 Content-Type: application/json"
                    "（跨站简单请求防护 —— 否则任何网页都能借这个服务做事）",
                    "Refused: POST must send Content-Type: application/json "
                    "(cross-site simple-request protection — otherwise any web page "
                    "could drive this service)")}, 415)
                return False
        return True

    # ---- 工具 ----
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 三层纯纵深防御（界面现在没有注入点，这几条是给"万一以后有"兜底的）：
        #  · nosniff：别让浏览器把 JSON/纯文本猜成 HTML 去执行；
        #  · X-Frame-Options: DENY：别让别的网页把本机控制台嵌进 iframe 里
        #    做点击劫持（诱导用户点「开始清理」）。Host/Origin 闸门已经会挡掉
        #    跨站 iframe 的文档请求，这条是第二道。
        #  · Referrer-Policy：别把本机地址泄漏给外站。
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        # ★ 上限：Content-Length 是客户端说了算的。不设限的话，一句
        #   `Content-Length: 2000000000` 就能让本进程按这个大小往内存里读 ——
        #   本地服务被人（或一个卡死的页面）拖爆内存不值得。
        if n < 0 or n > self.MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except FileNotFoundError:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    # ---- GET ----
    def do_GET(self):
        # ★ 原来这里**没有任何异常处理**：`/api/log?n=abc` 的 int() 会抛出去，
        #   连接被直接掐断（用户在页面上看到"连接被关闭"，而 socket 层只是打了条 traceback）。
        #   跟 do_POST 一样兜住，并且把 n 夹在合理范围里。
        try:
            self._do_GET()
        except Exception as e:
            try:
                self._json({"ok": False, "msg": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass

    def _do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        cfg = load_config()
        if path in ("/", "/index.html"):
            return self._file(os.path.join(WEB, "index.html"),
                              "text/html; charset=utf-8")
        if path == "/api/ping":
            return self._json({"ok": True})
        if path == "/api/state":
            return self._json(state_snapshot(cfg, ROLES))
        if path == "/api/config":
            return self._json({"remote_addr": cfg["remote_addr"],
                               "profile": cfg["profile"],
                               "voice": cfg["voice"], "audio": cfg["audio"],
                               "keys": cfg["keys"]})
        if path == "/api/profiles":
            import profiles
            return self._json(profiles.catalog())
        if path == "/api/uninstall-items":
            import uninstall as un_mod
            # 带上"现在在不在 / 多大"：界面上会显示「（154 MB）」「（不存在）」，
            # 用户才知道哪些勾了真有用 —— 离线包上最容易误会（.venv 永远不存在，
            # 真正占地方的是 runtime\）。
            try:
                pres = un_mod.presence()
            except Exception:
                pres = {}
            return self._json({"items": [
                {"id": i[0], "label": i18n.pick(i[1]), "desc": i18n.pick(i[2]),
                 "admin": i[3], "recommend": i[4],
                 "present": pres.get(i[0], {})} for i in un_mod.ITEMS]})
        if path == "/api/log":
            try:
                n = int((qs.get("n") or ["200"])[0])
            except (TypeError, ValueError):
                n = 200
            n = max(1, min(n, 2000))          # 夹住：别让 ?n=-1 / ?n=999999 玩出花样
            return self._json({"ok": True, "lines": BUS.tail(n)})
        if path == "/api/job":
            jid = (qs.get("id") or [""])[0]
            with JOBS_LOCK:
                j = JOBS.get(jid)
                if not j:
                    return self._json({"ok": False,
                                       "msg": i18n.L("没有这个任务", "No such job")})
                return self._json({"ok": True, "lines": list(j["lines"]),
                                   "done": j["done"]})
        if path == "/api/events":
            return self._sse()
        return self._send(404, b"not found", "text/plain; charset=utf-8")

    def _sse(self):
        # ★ 订阅者上限。SSE 是长连接，服务端每条连接挂一个线程 + 一个队列等着；
        #   没有上限时，一个反复刷新的页面（或几个被塞进页面里的 <img>/iframe）
        #   就能把线程耗光。正常的界面只开 1~2 条（多标签页也就几条）。
        n = BUS.subscriber_count()
        if n >= BUS.MAX_SUBS:
            return self._send(503, b"too many event streams", "text/plain; charset=utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = BUS.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            last_ping = time.time()
            while True:
                # ★ 边推边盯着对端活没活。原来直接 `q.get(timeout=15)`：页面早关了也要等
                #   满 15 秒、等下一次心跳写失败，才走到 finally 释放订阅名额 ——
                #   而名额是有限的（MAX_SUBS=8）。被别人反复占满的这段时间里，
                #   用户自己的界面会连不上实时流（表现为"页面不动了"）。
                #   所以每 0.5 秒 select 一下：对端关闭/发来数据都能立刻看出来。
                try:
                    ready, _, _ = select.select([self.connection], [], [], 0.5)
                except Exception:
                    break
                if ready:
                    try:
                        if self.connection.recv(1, socket.MSG_PEEK) == b"":
                            break                      # 对端已关闭 → 立刻释放名额
                        self.connection.recv(4096)     # 客户端多发了东西：读掉丢弃
                    except Exception:
                        break
                try:
                    msg = q.get_nowait()
                except queue.Empty:
                    if time.time() - last_ping >= 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_ping = time.time()
                    continue
                data = json.dumps(msg, ensure_ascii=False)
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except Exception:
            pass
        finally:
            BUS.unsubscribe(q)

    # ---- POST ----
    def do_POST(self):
        try:
            self._do_POST()
        except Exception as e:
            # 任何路由异常都必须变成一条 JSON 回复：直接断连接会让用户看到
            # 「连接被关闭」而完全不知道发生了什么。
            try:
                self._json({"ok": False, "msg": f"{type(e).__name__}: {e}"})
            except Exception:
                pass

    def _do_POST(self):
        path = urlparse(self.path).path
        # ★ 两道闸门放在最前面：Host/Origin 白名单 + 必须 application/json。
        #   没有这两条，任何网页都能 drive-by 让本机做危险动作（装驱动弹 UAC、删配置、
        #   提权重启）—— 因为 mode:'no-cors' 的 text/plain POST 不触发预检。
        if not self._guard(need_json=True):
            return
        body = self._body()
        if body is None:
            return self._json({"ok": False, "msg": i18n.L(
                "请求体过大（上限 1MB）", "Request body too large (limit 1MB)")}, 413)
        cfg = load_config()
        if path == "/api/config":
            if not isinstance(body, dict):
                return self._json({"ok": False,
                                   "msg": i18n.L("配置格式不对", "Bad config format")})
            merged = load_config()
            if "keys" in body:
                merged["keys"] = body["keys"] if isinstance(body["keys"], dict) else {}
            if "voice" in body and isinstance(body["voice"], dict):
                merged["voice"] = body["voice"]
            if "audio" in body and isinstance(body["audio"], dict):
                merged["audio"] = body["audio"]
            if "remote_addr" in body:
                merged["remote_addr"] = body["remote_addr"]
            clean = save_config(merged)
            if ROLES:
                ROLES.apply_config(_merge_defaults(clean))
            return self._json({"ok": True, "msg": i18n.L(
                f"已保存 {len(clean['keys'])} 条映射",
                f"Saved {len(clean['keys'])} key mappings")})
        if path == "/api/action":
            return self._action(body)
        return self._send(404, b"not found", "text/plain; charset=utf-8")

    def _action(self, body):
        act = (body or {}).get("action")
        cfg = load_config()
        if act == "apply-profile":
            import profiles
            app_id = str((body or {}).get("app") or "").strip()
            ime_id = str((body or {}).get("ime") or "").strip()
            keys, voice = profiles.build(app_id, ime_id)
            merged = load_config()
            merged["keys"] = keys
            if voice:
                merged["voice"] = voice
            merged["profile"] = {"app": app_id, "ime": ime_id}
            clean = save_config(merged)
            if ROLES:
                ROLES.apply_config(_merge_defaults(clean))
            na = i18n.pick(profiles.APPS.get(app_id, {}).get("name", app_id))
            ni = i18n.pick(profiles.IMES.get(ime_id, {}).get("name", ime_id))
            BUS.log(i18n.L(f"已应用预设：{na} + {ni}", f"Preset applied: {na} + {ni}"))
            return self._json({
                "ok": True,
                "msg": i18n.L(
                    f"已应用「{na} + {ni}」：{len(clean['keys'])} 个按键映射",
                    f"Applied “{na} + {ni}”: {len(clean['keys'])} key mappings")
                       + (i18n.L("，语音键已同步", ", voice key synced") if voice
                          else i18n.L("（语音键保持原样）", " (voice key unchanged)")),
                "data": {"keys": clean["keys"], "voice": clean["voice"]}})
        if act == "uninstall":
            if not os.path.exists(UNINSTALL_PY):
                return self._json({"ok": False,
                                   "msg": i18n.L("缺少 uninstall.py", "uninstall.py is missing")})
            import uninstall as un_mod
            picked = [x for x in ((body or {}).get("items") or [])
                      if x in un_mod.ITEM_IDS]
            if not picked:
                return self._json({"ok": False, "msg": i18n.L(
                    "还没勾选任何要删除的项目", "No items are selected for deletion yet")})
            # 先自己把角色停掉，释放蓝牙通道和 frida，省得卸载脚本杀进程时打架
            if ROLES:
                ROLES.stop(quiet=True)
            try:
                if os.path.exists(UNINSTALL_LOG):
                    os.remove(UNINSTALL_LOG)
            except OSError:
                pass
            import ctypes
            params = f'"{UNINSTALL_PY}" --items=' + ",".join(picked)
            # 只有勾了「需要管理员」的项（计划任务 / frida 服务）才提权 ——
            # 只删 __pycache__、日志这种也弹 UAC 就太烦了。
            needs_admin = any(x in un_mod.ADMIN_IDS for x in picked)
            verb = "open" if (is_elevated() or not needs_admin) else "runas"
            # ⚠ 用 host_pythonw()（.venv 之外的解释器），**不能用 sys.executable**：
            # 本进程就跑在 .venv 的 pythonw.exe 上，用它启动清理脚本会锁住自己，
            # 导致勾了「删除依赖（.venv）」也永远删不掉（详见 host_pythonw 注释）。
            hostpy = host_pythonw()
            try:
                rc = ctypes.windll.shell32.ShellExecuteW(
                    None, verb, hostpy, params, HERE, 0)
            except Exception as e:
                return self._json({"ok": False, "msg": i18n.L(
                    f"启动清理失败：{e!r}", f"Failed to start cleanup: {e!r}")})
            if rc <= 32:
                return self._json({"ok": False, "msg": i18n.L(
                    f"启动清理被拒绝（返回码 {rc}）",
                    f"Cleanup launch was refused (return code {rc})")})
            names = i18n.L("、", ", ").join(
                i18n.pick(un_mod.LABELS.get(x, x)) for x in picked)
            BUS.log(i18n.L(f"开始清理（{len(picked)} 项）：{names}",
                           f"Starting cleanup ({len(picked)} items): {names}"))
            BUS.log(i18n.L(f"清理进程用的解释器：{hostpy}",
                           f"Interpreter used by the cleanup process: {hostpy}"))
            if "venv" in picked and os.path.normcase(os.path.abspath(hostpy)).startswith(
                    os.path.normcase(os.path.abspath(VENV))):
                BUS.log(i18n.L("⚠ 本机只有 .venv 里的解释器可用，.venv 可能删不干净；"
                               "装一个系统 Python 后重试即可。",
                               "⚠ The only interpreter on this machine lives in .venv, "
                               "so .venv may not be deleted cleanly; install a system "
                               "Python and try again."))
            BUS.log(i18n.L("提示：清理脚本最后会结束本客户端，届时这个页面会失去连接。",
                           "Note: the cleanup script stops this client at the end, so "
                           "this page will lose its connection."))
            threading.Thread(target=follow_uninstall_log, daemon=True).start()
            return self._json({"ok": True, "msg": i18n.L(
                f"已开始清理 {len(picked)} 项，过程见日志页",
                f"Cleanup of {len(picked)} items started; follow it on the Logs page")})
        if act == "start":
            ok, msg = ROLES.start()
            return self._json({"ok": ok, "msg": msg})
        if act == "stop":
            ok, msg = ROLES.stop()
            return self._json({"ok": ok, "msg": msg})
        if act == "restart":
            ok, msg = ROLES.restart()
            return self._json({"ok": ok,
                               "msg": i18n.L("已重启", "Restarted") if ok else msg})
        if act == "check":
            lines = []
            do_check(lines.append)
            for ln in lines:
                BUS.log(ln)
            return self._json({"ok": True, "msg": i18n.L(
                f"体检完成（{len(lines)} 项）",
                f"Environment check finished ({len(lines)} checks)"),
                               "data": lines})
        if act == "scan":
            items = []
            do_scan(BUS.log, items.extend)
            msg = (i18n.L(f"扫到 {len(items)} 个蓝牙设备",
                          f"Found {len(items)} Bluetooth device(s)") if items
                   else i18n.L("没扫到设备（遥控器空闲时不广播，按一下它再扫）",
                               "No devices found (the remote does not advertise while "
                               "idle — press a button on it and scan again)"))
            return self._json({"ok": True, "msg": msg, "data": items})
        if act == "install-deps":
            return self._json({"ok": True, "job": background(i18n.L(
                "安装基础依赖", "Install base dependencies"), install_deps)})
        if act == "install-keys":
            return self._json({"ok": True, "job": background(i18n.L(
                "安装按键映射依赖（frida）", "Install key-mapping dependencies (frida)"),
                install_keys_deps)})
        if act == "install-all":
            # 新用户在横幅上点「全部安装」走这条：一次把语音和按键依赖都装好，
            # 省得他装完语音发现方向键没反应、再去设置页翻第二个按钮。
            # ③ 虚拟声卡也算在里头：它是语音的硬性前提，不装的话"依赖齐全"是假的。
            def both(on_line):
                on_line(i18n.L("① 基础依赖（语音必需，约 10MB）",
                               "① Base dependencies (required for voice, ~10MB)"))
                rc = install_deps(on_line)
                if rc != 0:
                    on_line(i18n.L("基础依赖没装成，先不装按键映射了。",
                                   "Base dependencies failed; skipping the "
                                   "key-mapping ones."))
                    return rc
                on_line("")
                on_line(i18n.L("② 按键映射依赖 frida（可选，约 130MB）",
                               "② Key-mapping dependency frida (optional, ~130MB)"))
                rc2 = install_keys_deps(on_line)
                on_line("")
                if cable_state()["installed"]:
                    on_line(i18n.L("③ 虚拟声卡：已经在用了，跳过 ✓",
                                   "③ Virtual audio cable: already in use, skipping ✓"))
                else:
                    on_line(i18n.L("③ 虚拟声卡 VB-CABLE（语音的物理前提）",
                                   "③ VB-CABLE virtual audio cable (the physical prerequisite "
                                   "for voice)"))
                    install_cable(on_line)
                return rc2
            return self._json({"ok": True,
                               "job": background(i18n.L(
                                   "安装全部依赖（语音 + 按键映射 + 虚拟声卡）",
                                   "Install everything (voice + key mapping + virtual audio "
                                   "cable)"),
                                   both)})
        if act == "autostart":
            ok, msg = set_autostart(bool(body.get("on")))
            BUS.log(msg)
            return self._json({"ok": ok, "msg": msg})
        if act == "qwen-inject":
            # 用户点这个按钮 = 明确"我要开"，顺便把之前"别再自动开"的标记清掉。
            try:
                if os.path.exists(QWEN_OPTOUT):
                    os.remove(QWEN_OPTOUT)
                    BUS.log(i18n.L("已清除「不再自动打开千问开关」的标记",
                                   "Cleared the “do not auto-enable the Qianwen "
                                   "switch” marker"))
            except OSError:
                pass
            return self._json({"ok": True, "job": background(i18n.L(
                "打开千问允许注入开关",
                "Turn on Qianwen's allow-injection switch"), enable_qwen_flag)})
        if act == "set-lang":
            # 界面切语言时调这里：写进 config.json（只动 lang 一项），然后**立刻推一份新状态** ——
            # 界面靠这条 SSE 拿到"后端换语言后"的那批文案（依赖说明、预设备注、清单项…）。
            lang = i18n.save_to_config((body or {}).get("lang"))
            BUS.log(i18n.L("语言已切换：", "Language switched: ")
                    + ("English" if lang == "en" else "中文"))
            BUS.push({"ev": "state", "state": state_snapshot(load_config(), ROLES)})
            return self._json({"ok": True, "lang": lang})
        if act == "install-cable":
            # 语音的物理前提。包内带安装器就直接跑，没带就从官网下一份再跑；
            # 两种都要提权（VB-CABLE 是内核音频驱动，它自己的安装器要求管理员）。
            if cable_state()["installed"]:
                return self._json({"ok": False, "msg": i18n.L(
                    "虚拟声卡已经在用了（体检里能看到设备名），不用再装",
                    "The virtual audio cable is already in use (the environment check "
                    "shows the device name); no need to install it again")})
            return self._json({"ok": True, "job": background(i18n.L(
                "安装虚拟声卡（VB-CABLE）",
                "Install the virtual audio cable (VB-CABLE)"), install_cable)})
        if act == "setup-elevated":
            if not os.path.exists(ELEVATE_PY):
                return self._json({"ok": False, "msg": i18n.L(
                    "缺少 elevate_setup.py", "elevate_setup.py is missing")})
            if is_elevated():
                return self._json({"ok": True, "job": background(
                    i18n.L("一次性优化", "One-click optimization"),
                    run_elevate_setup)})
            try:
                if os.path.exists(ELEVATE_LOG):
                    os.remove(ELEVATE_LOG)
            except OSError:
                pass
            ok, msg = relaunch_elevated_script(ELEVATE_PY)
            if ok:
                BUS.log(i18n.L("已请求管理员权限做一次性优化（UAC 只会弹这最后一次）…",
                               "Requested administrator rights for one-click "
                               "optimization (this is the last UAC prompt)…"))
                threading.Thread(target=follow_elevate_log, daemon=True).start()
            return self._json({"ok": ok, "msg": msg})
        if act == "mic-selfcheck":
            # 「麦克风自检」：读一秒系统默认输入设备，报峰值。用来分清"系统就没声音"
            # 和"程序没读到"（用户报"所有麦克风电平都是 0"时第一件要做的事）。
            try:
                import voice as voice_mod
                msg = voice_mod.probe_mic(1.0)
            except Exception as e:
                msg = i18n.L(f"麦克风自检失败：{e!r}", f"Microphone self-check failed: {e!r}")
            BUS.log(msg)
            return self._json({"ok": True, "msg": msg})
        if act == "keys-selfcheck":
            # 「按键映射自检」：把钩子里那几个原始计数铺到日志里。给"界面显示就绪、
            # 按键却没反应"这种情况定位用 —— 一眼分清是挂错了宿主，还是这支遥控器的
            # 报文格式跟预期不同（公司电脑上则多半是杀软把注入拦了，见下面的提示）。
            kt = ROLES.keytap if ROLES else None
            if kt is None:
                msg = i18n.L("按键映射没在运行（先确认「运行依赖」里 frida 齐全）",
                             "Key mapping is not running (first check that frida is installed)")
            else:
                try:
                    msg = kt.selfcheck()
                except Exception as e:
                    msg = i18n.L(f"自检失败：{e!r}", f"Self-check failed: {e!r}")
            BUS.log(msg)
            return self._json({"ok": True, "msg": msg})
        if act == "reset-bt":
            # 重置蓝牙电台：BLE 外设只接受一个连接，系统 HID 有时会把那条唯一的连接
            # 攥住不放（遥控器从此不广播 → 程序永远连不上）。拔电池能解开，
            # 这个按钮等价于"用代码关开一次蓝牙电台"。要管理员，所以走提权脚本。
            if not os.path.exists(ELEVATE_PY):
                return self._json({"ok": False, "msg": i18n.L(
                    "缺少 elevate_setup.py", "elevate_setup.py is missing")})
            if is_elevated():
                return self._json({"ok": True, "job": background(
                    i18n.L("重置蓝牙电台", "Reset Bluetooth radio"), run_reset_bt)})
            try:
                if os.path.exists(ELEVATE_LOG):
                    os.remove(ELEVATE_LOG)
            except OSError:
                pass
            ok, msg = reset_bluetooth_radio()
            if ok:
                BUS.log(i18n.L("已请求管理员权限重置蓝牙电台（几秒后会自动重连）…",
                               "Requested administrator rights to reset the Bluetooth "
                               "radio (it reconnects automatically in a few seconds)…"))
                threading.Thread(target=follow_elevate_log, daemon=True).start()
            return self._json({"ok": ok, "msg": msg})
        if act == "restart-app":
            # 「重启自己」：用**启动器那套优先级**挑解释器（优先 .venv），
            # 拉起同样权限的新实例（--takeover 会结束旧的我），然后退出。
            import ctypes
            exe = best_pythonw()
            params = f'"{os.path.join(HERE, "app.py")}" --silent --takeover'
            try:
                if is_elevated():
                    rc = ctypes.windll.shell32.ShellExecuteW(None, "open", exe,
                                                             params, HERE, 0)
                else:
                    rc = ctypes.windll.shell32.ShellExecuteW(None, "open", exe,
                                                             params, HERE, 0)
            except Exception as e:
                return self._json({"ok": False, "msg": i18n.L(
                    f"重启失败：{e!r}", f"Restart failed: {e!r}")})
            if rc <= 32:
                return self._json({"ok": False, "msg": i18n.L(
                    f"重启失败（返回码 {rc}）",
                    f"Restart failed (return code {rc})")})
            BUS.log(i18n.L(
                f"正在重启客户端（用 {os.path.basename(exe)} 拉起新实例）…",
                f"Restarting the client (launching a new instance with "
                f"{os.path.basename(exe)})…"))
            threading.Thread(target=lambda: (time.sleep(2.5), os._exit(0)),
                             daemon=True).start()
            return self._json({"ok": True, "msg": i18n.L(
                "正在重启，约 5 秒后刷新本页面",
                "Restarting — refresh this page in about 5 seconds")})
        if act == "elevate":
            if is_elevated():
                return self._json({"ok": True, "msg": i18n.L(
                    "已经是管理员身份了，不用再提权",
                    "Already running as administrator; no need to elevate again")})
            ok, msg = relaunch_elevated()
            if ok:
                BUS.log(i18n.L("已请求以管理员身份重启（UAC 只弹这一次，点「是」即可）",
                               "Requested a restart as administrator (this is the only "
                               "UAC prompt — just click “Yes”)"))
                # 让位给新的管理员实例：等它起来再退出，避免端口冲突
                threading.Thread(target=lambda: (time.sleep(3.0), os._exit(0)),
                                 daemon=True).start()
            return self._json({"ok": ok, "msg": msg})
        if act == "test-voice":
            try:
                import voice as voice_mod
                role = getattr(ROLES, "voice", None)
                spec = cfg["voice"]
                vk = voice_mod.VoiceKey(spec)
                if not vk.ok:
                    return self._json({"ok": False, "msg": i18n.L(
                        f"认不出目标键 {spec}", f"Unrecognized target key {spec}")})
                if vk.mode == "tap":
                    vk.tap()
                    msg = i18n.L(f"已按一下 {vk.label}", f"Tapped {vk.label}")
                else:
                    vk.press()
                    time.sleep(0.4)
                    vk.release()
                    msg = i18n.L(f"已按住并松开 {vk.label}",
                                 f"Held and released {vk.label}")
                BUS.log(i18n.L("测试发键：", "Test key sent: ") + msg)
                return self._json({"ok": True, "msg": msg})
            except Exception as e:
                return self._json({"ok": False, "msg": f"{type(e).__name__}: {e}"})
        if act == "clear-log":
            BUS.lines.clear()
            return self._json({"ok": True,
                               "msg": i18n.L("已清空", "Cleared")})
        return self._json({"ok": False, "msg": i18n.L(
            f"不认识的动作 {act}", f"Unknown action {act}")})


# ============================================================ 单实例
def already_running(port):
    """端口上已经有一个实例在听 —— 那就只开浏览器，不再起第二个进程。"""
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def port_owners(port):
    """谁在监听这个端口（解析 netstat，纯标准库）。"""
    try:
        p = subprocess.run(["netstat", "-ano"], capture_output=True,
                           creationflags=CREATE_NO_WINDOW, timeout=20)
        out = p.stdout.decode("utf-8", "replace")
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") \
                and parts[3].upper() == "LISTENING":
            try:
                pids.append(int(parts[4]))
            except ValueError:
                pass
    return sorted(set(pids))


def python_processes():
    """[(pid, 命令行)] —— 进程名以 python 开头的那些。一次 WMI 查询，约 0.3~1 秒。

    只在"启动/接管"这条路径上用，不值得更快。
    """
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=25, creationflags=CREATE_NO_WINDOW)
    except Exception:
        return []
    raw = p.stdout or b""
    text = None
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text.strip() or "[]")
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for it in data:
        try:
            out.append((int(it.get("ProcessId")), str(it.get("CommandLine") or "")))
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def our_instances(procs=None):
    """**这一份程序**（同一个目录里的 app.py）正在跑的进程 PID，排除自己。

    为什么要有它：原来的接管只杀"占着界面端口"的那个进程 —— 可一个刚起来、还没绑上
    端口的实例不在这个名单里，于是**两个实例同时活着**：抢蓝牙（日志刷"缺少 ATVV 特征"）、
    界面显示的还可能是旧配置。真机上就是这么被坑的。
    这里按**命令行**认人：只认命令行里带本目录 app.py 的进程；别的 python 程序 ——
    包括**别的目录里的另一份 vibe-mote** —— 一概不碰。
    """
    if procs is None:
        procs = python_processes()
    mine = os.path.normcase(os.path.join(HERE, "app.py"))
    me = os.getpid()
    out = []
    for pid, cmd in procs:
        if pid == me:
            continue
        if mine in os.path.normcase(cmd):
            out.append(pid)
    return sorted(set(out))


def take_lock():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def _selftest():
    """`--selftest`：只验证"本目录的模块能不能 import"，不启动任何角色。

    为什么需要它：离线全量包用的是官方 embeddable Python，它的 `._pth` 会让
    sys.path 进入隔离模式、**不含脚本目录** —— 于是 `import voice` 直接炸，
    而 pythonw 没控制台，用户看到的就是"双击没反应"。
    光验证"界面能起来"（`--no-start`）是**抓不到**这个的：角色不启动就不会 import
    voice/keys。所以构建离线包时必须跑一次这个自检。
    """
    ok = True
    print(f"python: {sys.version.split()[0]}  ({sys.executable})")
    print(f"程序目录: {HERE}")
    print(f"sys.path: {sys.path}")
    for mod in ("voice", "keys", "profiles", "uninstall", "elevate_setup", "i18n"):
        try:
            __import__(mod)
            print(f"  ✓ import {mod}")
        except Exception as e:
            ok = False
            print(f"  ✗ import {mod} 失败：{type(e).__name__}: {e}")
    print("自检结果：" + ("全部通过 ✓" if ok else "有失败 ✗"))
    return 0 if ok else 1


def main():
    _utf8_console()
    ap = argparse.ArgumentParser(description="遥控器客户端")
    ap.add_argument("--silent", action="store_true", help="只起服务，不开浏览器")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-start", action="store_true", help="只开界面，不自动启动角色")
    ap.add_argument("--selftest", action="store_true",
                    help="只检查本目录模块能否 import（构建离线包时用），不启动任何东西")
    ap.add_argument("--takeover", action="store_true",
                    help="接管正在运行的实例（管理员重启时用）")
    # --boot：这一份是**开机自启**拉起来的（不是用户双击启动器）。
    # 它专门用来关掉"自动弹 UAC 装虚拟声卡"那条路 —— 开机时用户不在电脑前，
    # 弹了也没人点，还会变成每次开机都弹一次（用户完全不知道是谁要权限）。
    ap.add_argument("--boot", action="store_true",
                    help="由开机自启拉起（不主动弹任何提权框）")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    global ROLES
    cfg = load_config()
    # 语言要在启动任何会说话的东西之前定好（角色备注、启动日志都按它出）
    i18n.load_from_config()
    port = args.port or int(cfg.get("ui_port") or DEFAULT_PORT)
    url = f"http://127.0.0.1:{port}/"

    if already_running(port) and not args.takeover:
        if not args.silent:
            try:
                os.startfile(url)
            except Exception:
                webbrowser.open(url)
        return 0

    if args.takeover:
        # 接管：把**本目录的所有**旧实例结束掉再启动。只在这个显式参数下做，平时不会。
        # 客户端以最高权限运行时，普通权限杀不掉它 —— 所以这一步必须由
        # 「自己拉起的、同样提权的新实例」来做（见 restart-app 动作）。
        #
        # ★ 两处改过（真机踩坑）：
        #   ① 不能只杀"占着端口"的那个：刚起来还没绑端口的实例不在这名单里，
        #      于是两个实例同时活着抢蓝牙 → 日志刷「缺少 ATVV 特征」、界面还可能显示旧配置。
        #      现在按命令行把本目录的实例全找出来一起结束。
        #   ② 端口被**别的程序**占着时不能杀 —— 那不是我们的客户端。说清楚再退出。
        procs = python_processes()
        owners = set(port_owners(port))
        ours = set(our_instances(procs))
        foreign = owners - ours
        if foreign:
            detail = "、".join(f"PID {p}" for p in sorted(foreign))
            BUS.log(i18n.L(
                f"界面端口 {port} 被**另一个程序**占着（{detail}），它不是本目录的客户端 —— "
                f"我不会去杀它。先关掉它（或改 config.json 里的 ui_port）再启动。",
                f"UI port {port} is held by **another program** ({detail}) that is not this "
                f"folder's client — I will not kill it. Close it first (or change ui_port in "
                f"config.json), then start again."))
            return 3
        old = sorted(ours | owners)
        if old:
            BUS.log(i18n.L(f"接管模式：结束 {len(old)} 个旧实例 {old}",
                           f"Takeover: stopping {len(old)} old instance(s) {old}"))
            for pid in old:
                try:
                    # 不加 /T：旧实例的子进程里可能有正在跑的卸载脚本，
                    # 连坐杀掉它会让"卸载到一半就没了"。
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   creationflags=CREATE_NO_WINDOW, timeout=30)
                except Exception:
                    pass
            for _ in range(80):                 # 最多等 20 秒
                time.sleep(0.25)
                if not already_running(port) and not our_instances():
                    break
            left = our_instances()
            if left:
                BUS.log(i18n.L(
                    f"还有 {len(left)} 个旧实例没能结束（{left}）—— 先继续启动，"
                    f"但它们可能抢蓝牙；必要时手动结束它们。",
                    f"{len(left)} old instance(s) survived ({left}) — starting anyway, but they "
                    f"may fight over Bluetooth; kill them by hand if needed."))

    lock = take_lock()
    if lock is None and args.takeover:
        for _ in range(80):                 # 最多再等 20 秒
            time.sleep(0.25)
            lock = take_lock()
            if lock:
                BUS.log(i18n.L("已接管。", "Taken over."))
                break
    if lock is None:
        # 锁被占：多半已经有实例。端口在听就只开浏览器，否则等它起来。
        for _ in range(20):
            if already_running(port):
                if not args.silent:
                    try:
                        os.startfile(url)
                    except Exception:
                        webbrowser.open(url)
                return 0
            time.sleep(0.25)
        BUS.log(i18n.L("已有另一个实例在跑，但界面端口没起来。先关掉旧实例再试。",
                       "Another instance is already running, but its UI port is not up. "
                       "Close the old instance and try again."))
        return 1

    BUS.log("=" * 58)
    BUS.log(i18n.L("  遥控器客户端 启动", "  VibeMote client starting"))
    BUS.log("=" * 58)

    # 上一次「退出后再删」的结果。那条路是**分离进程**干的，当时没法回报成功没有 ——
    # 它会留下一个结果文件，这里读出来如实说（否则"报成功其实没删掉"会重演：
    # .venv 剩个壳 → 下次启动器优先选它 → 用户看到的是"双击没反应"）。
    try:
        if os.path.exists(DEFERRED_RESULT):
            _txt = open(DEFERRED_RESULT, encoding="utf-8", errors="replace").read().strip()
            os.remove(DEFERRED_RESULT)
            _what = _txt[3:].strip() if len(_txt) > 3 else "?"
            if _txt.upper().startswith("FAIL"):
                BUS.log(i18n.L(
                    f"⚠ 上次清理没能删掉「{_what}」（后台命令重试 90 次后放弃了）——"
                    f"手动删掉它即可",
                    f"⚠ The previous cleanup could not delete “{_what}” (the background "
                    f"command gave up after 90 retries) — just delete it by hand"))
            elif _txt:
                BUS.log(i18n.L(f"上次清理已完成：「{_what}」已删除",
                               f"The previous cleanup finished: “{_what}” was deleted"))
    except OSError:
        pass
    d = deps_state()
    if not d["ok"]:
        BUS.log(i18n.L("缺依赖：", "Missing dependencies: ")
                + i18n.L("、", ", ").join(d["missing"])
                + i18n.L("（界面里点「安装基础依赖」即可，不影响先打开界面）",
                         " (click “Install base dependencies” in the UI — the UI opens "
                         "without them)"))

    ROLES = Roles(cfg)
    # ⚠ 「千问允许注入开关」不在这里弄了 —— 它跟着**启动角色**走（Roles._ensure_qwen_flag），
    #   因为在这里无条件调用会让 `--no-start`（打包自检）也去改用户 HKCU 的全局环境变量
    #   并重启一次千问输入法。顺序要求（必须在角色之前）由 Roles.start() 自己保证。
    if not args.no_start and d["ok"]:
        ROLES.start()
    elif not args.no_start:
        BUS.log(i18n.L("依赖不全，先不启动语音/按键。装完依赖点「启动」。",
                       "Dependencies are incomplete, so voice/key mapping will not start "
                       "yet. Install them, then click “Start”."))

    # 首次启动：语音的物理前提（虚拟声卡）没装 → **自动帮你把官方安装器拉起来**。
    # 能自动的只有这一步：VB-CABLE 的安装器没有任何命令行开关，它是个必须人点
    # 「Install Driver」的向导，官方还要求装完重启。所以省掉的是"自己去找那个按钮"，
    # 不是那一次确认。
    #
    # ⚠ 四个前提，缺一不可：
    #   · `not args.no_start`：构建/自检时不要弹 UAC。
    #   · ★ `not args.boot`：**开机自启拉起的那一份绝对不能弹**。自启命令是
    #     `wscript 启动遥控器.vbs --silent --boot` —— 要是这里不看 --boot，
    #     开机 6 秒后就会凭空冒出一个 UAC，用户根本不在电脑前，而且会变成**每次开机都弹**。
    #   · `d["ok"]`：依赖没装（轻包首启）时 sounddevice 都导不进来，根本判断不了声卡在不在 ——
    #     那种情况走界面上的「全部安装」（它含虚拟声卡这一步）。
    #   · ★ `not cable_ok`（= **设备用不了、注册表里也没有**）：注意**不是**只看
    #     `cable_state()["installed"]`。只看设备的话，有两种情况会误判成"没装"从而弹 UAC：
    #       a) 依赖装在 .venv、本进程是系统 Python → 枚举不到设备，但驱动明明装着；
    #       b) **刚卸完还没重启**：设备没了，但服务键/DriverStore 还在 ——
    #          这时该说的是"先重启一次"，而不是再弹一次 UAC 叫人重装。
    #   · 标记文件：只自动试一次，用户拒了 UAC 就不再烦他。
    #     ★ 而且**写不进标记就不许自动弹**：写不进（Program Files / 只读盘 / ACL 过的 U 盘）
    #       意味着下次启动条件依然成立 → 退化成"每次开机都弹 UAC"。宁可这次也不弹。
    cable_now = cable_state()
    cable_ok = bool(cable_now["installed"] or cable_now["registered"])
    auto_ok = (not args.no_start and not args.boot and d["ok"]
               and not cable_ok
               and not os.path.exists(CABLE_AUTOTRIED))
    if auto_ok:
        try:
            with open(CABLE_AUTOTRIED, "w", encoding="utf-8") as f:
                f.write("本程序在首次启动时自动尝试安装过一次虚拟声卡（VB-CABLE）。\n"
                        "删掉本文件后，下次启动会再自动试一次。\n")
        except OSError as e:
            auto_ok = False
            BUS.log(i18n.L(
                f"（写不下「已自动试过」的标记：{e}）—— 这次不自动弹提权框，"
                f"想装虚拟声卡请在界面上点一下",
                f"(Could not write the “already auto-tried” marker: {e}) — not prompting "
                f"for elevation this time; click the button in the UI to install the cable"))

    if auto_ok:
        def _auto_cable():
            # 先等界面起来：不然用户只看到一个凭空冒出来的 UAC，不知道是谁要的权限。
            time.sleep(6.0)
            BUS.log(i18n.L(
                "首次启动：虚拟声卡（语音的物理前提）还没装，自动帮你把官方安装器拉起来…",
                "First run: the virtual audio cable (the prerequisite for voice) is not "
                "installed — starting its official installer for you…"))
            background(i18n.L("安装虚拟声卡（VB-CABLE）",
                              "Install the virtual audio cable (VB-CABLE)"), install_cable)

        threading.Thread(target=_auto_cable, daemon=True, name="auto-cable").start()

    httpd = Server(("127.0.0.1", port), Handler)
    BUS.log(i18n.L(f"界面地址：{url}（只监听本机，外网进不来）",
                   f"UI address: {url} (listening on this machine only; not reachable "
                   f"from the network)"))

    # 「上次是不是正常退出」诊断：这个文件正常退出时会被删掉，所以**启动时还看到它**
    # = 上一次是崩溃 / 被强杀 / 系统休眠关机没走正常关闭流程。真机上出现过"客户端
    # 不知什么时候就没了、日志里什么都没有"的情况，只能靠这个把话说清楚。
    try:
        if os.path.exists(RUNSTATE):
            try:
                old = open(RUNSTATE, encoding="utf-8").read().strip()
            except OSError:
                old = ""
            try:
                start_epoch = float([l for l in old.splitlines()
                                     if l.startswith("start_epoch=")][0].split("=", 1)[1])
            except Exception:
                start_epoch = 0.0
            # ★ 先排除"这次是被重启/关机结束的"—— 那属于**正常**，不该吓人。
            #   判据：本次开机时长 < 我们那次运行的时长 → 说明开机发生在我们启动之后。
            try:
                up = ctypes.windll.kernel32.GetTickCount64() / 1000.0
            except Exception:
                up = -1.0
            rebooted = start_epoch and up >= 0 and up < (time.time() - start_epoch)
            if rebooted:
                BUS.log(i18n.L("上次运行是被系统重启/关机结束的（正常）",
                               "The previous run ended with a system restart/shutdown (normal)"))
            else:
                BUS.log(i18n.L(
                    f"上次运行没有正常退出（记录：{old.replace(chr(10), ' / ')}）—— 可能是崩溃、"
                    f"被强杀，或系统休眠没走正常关闭流程。",
                    f"The previous run did not exit cleanly (record: "
                    f"{old.replace(chr(10), ' / ')}) — it may have crashed, been force-killed, or the "
                    f"system slept without a clean shutdown."))
        with open(RUNSTATE, "w", encoding="utf-8") as f:
            f.write(f"pid={os.getpid()}\nstart={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"start_epoch={time.time():.0f}\n")
    except OSError:
        pass

    # 「我起来了」标记：启动器（vbs）会先删掉它、启动后再等它出现。
    # 为什么用文件而不是让 vbs 去 HTTP 探测：vbs 里 CreateObject("MSXML2.XMLHTTP")
    # 在某些策略/精简系统上会被禁，那样"能跑"也会被判成"启动失败"。
    # 文件是 FileSystemObject 的事，vbs 本来就在用，没有额外依赖。
    try:
        with open(READY, "w", encoding="utf-8") as f:
            f.write(f"{port}\n{os.getpid()}\n")
    except OSError as e:
        BUS.log(i18n.L(f"（写就绪标记失败，不影响使用：{e}）",
                       f"(Failed to write the ready marker; not fatal: {e})"))

    if not args.silent:
        try:
            os.startfile(url)
        except Exception:
            webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        BUS.log(i18n.L("收到 Ctrl+C", "Received Ctrl+C"))
    finally:
        if ROLES:
            ROLES.stop(quiet=True)
        try:
            os.remove(READY)          # 别留着一个"我活着"的假标记
        except OSError:
            pass
        try:
            os.remove(RUNSTATE)       # 正常退出 → 下次启动就不会再报"上次没正常退出"
        except OSError:
            pass
        try:
            lock.close()
        except Exception:
            pass
    return 0


class Server(ThreadingHTTPServer):
    """浏览器关页面 / SSE 断线是常态，别把 ConnectionReset 打成一大片红字。"""

    daemon_threads = True
    allow_reuse_address = True
    # ★ 并发上限。ThreadingHTTPServer 是**每连接一个线程、且没有上限**：本地这个服务
    #   只要有人（或几个被塞进网页的 <img>/iframe/EventSource）不停开连接，
    #   线程就被耗光，用户看到的界面就"卡死了"。64 条对"一个浏览器 + 偶尔 curl"
    #   绰绰有余（SSE 长连接也占名额，但订阅数另有 MAX_SUBS=8 卡着）。
    MAX_WORKERS = 64

    def __init__(self, *a, **kw):
        self._slots = threading.Semaphore(self.MAX_WORKERS)
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            # 满了就**直接关掉**这条连接，不排队、不新开线程
            try:
                request.close()
            except Exception:
                pass
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        BUS.trace("HTTP 处理线程", exc)
        super().handle_error(request, client_address)


def _install_crash_hooks():
    """把未捕获异常写进 client.log。

    为什么必须有：程序跑在 pythonw 下（没有控制台），任何异常都只会
    "进程忽然没了"，用户看到的就是"双击没反应"。有了这两个钩子，
    至少日志里有一份完整 traceback 可以对着看。
    """
    def hook(exc_type, exc, tb):
        BUS.trace("主线程", exc if exc else exc_type)
    sys.excepthook = hook
    try:
        def thook(args):
            BUS.trace(f"线程 {getattr(args.thread, 'name', '?')}", args.exc_value)
        threading.excepthook = thook
    except Exception:
        pass


if __name__ == "__main__":
    try:
        _install_crash_hooks()
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:           # noqa: BLE001
        # 连 main() 都没跑起来（比如依赖/环境问题）—— 必须留下痕迹
        try:
            BUS.trace("启动", e)
            BUS.log(i18n.L(f"启动失败，日志在上面。日志文件：{Bus.LOG}",
                           f"Startup failed; the log is above. Log file: {Bus.LOG}"))
        except Exception:
            pass
        raise
