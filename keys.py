# -*- coding: utf-8 -*-
"""按键映射：Frida 旁路读遥控器 HID 报文 → 合成键盘 / 鼠标动作。

为什么必须走 Frida（定案，别再试别的路子）：
  谷歌遥控器的按键报告被 Windows 的 UMDF 蓝牙驱动（WUDFHost.exe）消费掉了 ——
  Raw Input 收不到、HID 设备直接 ReadFile 也是空的。唯一可行出口是钩住
  WUDFHost 里的 ntdll!NtDeviceIoControlFile，抄 IOCTL 0x80018483 的输出缓冲区。

报文格式：
  3 字节 Consumer Control： [0x02] [usage_lo] [usage_hi]
    usage 非 0  = 按下这个键
    02 00 00    = 空闲帧（收到它就代表全部松开）
  注意：同一个 WUDFHost 还服务别的蓝牙键鼠，它们吐 9 字节键盘报告 —— 按长度过滤。

清位（消灭「原生动作 + 映射动作」双发）：
  在钩子里把**已被映射**的 usage 原地写 0，驱动就以为没按键，原生动作不再发生，
  只剩我们合成的那一次。所以音量键不会顺带调系统音量、YouTube 不会打开应用。

本文件只用标准库 + frida（缺 frida 时整体降级，不影响语音）。
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
import winreg
from ctypes import wintypes

import i18n

HERE = os.path.dirname(os.path.abspath(__file__))
TAP_JS = os.path.join(HERE, "tap.js")

# ============================================================ 常量表
# 键名 -> 虚拟键码。界面里能选到的名字就是这张表的键。
VK = {
    # 修饰键（左右分开：右 Alt / 右 Ctrl 是输入法语音快捷键的常客）
    "CTRL": 0x11, "SHIFT": 0x10, "ALT": 0x12,
    "LCTRL": 0xA2, "RCTRL": 0xA3, "LSHIFT": 0xA0, "RSHIFT": 0xA1,
    "LALT": 0xA4, "RALT": 0xA5, "LWIN": 0x5B, "RWIN": 0x5C, "APPS": 0x5D,
    # 编辑与导航
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "ESC": 0x1B, "SPACE": 0x20,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22, "END": 0x23, "HOME": 0x24,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "INSERT": 0x2D, "DELETE": 0x2E, "CAPSLOCK": 0x14,
    "PRINTSCREEN": 0x2C, "NUMLOCK": 0x90, "SCROLLLOCK": 0x91, "PAUSE": 0x13,
    # 符号
    "SEMICOLON": 0xBA, "EQUAL": 0xBB, "COMMA": 0xBC, "MINUS": 0xBD,
    "PERIOD": 0xBE, "SLASH": 0xBF, "BACKQUOTE": 0xC0, "LBRACKET": 0xDB,
    "BACKSLASH": 0xDC, "RBRACKET": 0xDD, "QUOTE": 0xDE,
    # 小键盘
    "NUMPAD0": 0x60, "NUMPAD1": 0x61, "NUMPAD2": 0x62, "NUMPAD3": 0x63,
    "NUMPAD4": 0x64, "NUMPAD5": 0x65, "NUMPAD6": 0x66, "NUMPAD7": 0x67,
    "NUMPAD8": 0x68, "NUMPAD9": 0x69, "MULTIPLY": 0x6A, "ADD": 0x6B,
    "SUBTRACT": 0x6D, "DECIMAL": 0x6E, "DIVIDE": 0x6F,
    # 媒体
    "MEDIA_NEXT": 0xB0, "MEDIA_PREV": 0xB1, "MEDIA_STOP": 0xB2,
    "MEDIA_PLAY_PAUSE": 0xB3, "VOLUME_MUTE": 0xAD,
    "VOLUME_DOWN": 0xAE, "VOLUME_UP": 0xAF,
    # 浏览器 / 系统
    "BROWSER_BACK": 0xA6, "BROWSER_FORWARD": 0xA7, "BROWSER_REFRESH": 0xA8,
    "BROWSER_STOP": 0xA9, "BROWSER_SEARCH": 0xAA, "BROWSER_FAVORITES": 0xAB,
    "BROWSER_HOME": 0xAC,
}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK[_c] = ord(_c)
for _d in range(10):
    VK[f"DIGIT{_d}"] = 0x30 + _d
for _i in range(1, 25):
    VK[f"F{_i}"] = 0x6F + _i

# 必须带 KEYEVENTF_EXTENDEDKEY 的键。少了这个标志，Windows 会把
# 「右 Alt」当成左 Alt、把方向键当成小键盘数字键 —— 输入法就收不到语音快捷键。
EXT_VK = {
    0xA3, 0xA5,              # 右 Ctrl / 右 Alt
    0x5B, 0x5C, 0x5D,        # Win / RWin / 菜单键
    0x2D, 0x2E,              # Insert / Delete
    0x24, 0x23, 0x21, 0x22,  # Home / End / PgUp / PgDn
    0x26, 0x28, 0x25, 0x27,  # 方向键
    0x90, 0x2C, 0x6F,        # NumLock / PrintScreen / 小键盘除号
}

# 常用键扫描码（make code）。带上扫描码兼容性最好：有些输入法/游戏只认扫描码。
SCAN = {
    0x1B: 0x01, 0x31: 0x02, 0x32: 0x03, 0x33: 0x04, 0x34: 0x05, 0x35: 0x06,
    0x36: 0x07, 0x37: 0x08, 0x38: 0x09, 0x39: 0x0A, 0x30: 0x0B, 0xBD: 0x0C,
    0xBB: 0x0D, 0x08: 0x0E, 0x09: 0x0F, 0x51: 0x10, 0x57: 0x11, 0x45: 0x12,
    0x52: 0x13, 0x54: 0x14, 0x59: 0x15, 0x55: 0x16, 0x49: 0x17, 0x4F: 0x18,
    0x50: 0x19, 0xDB: 0x1A, 0xDD: 0x1B, 0x0D: 0x1C,
    0x41: 0x1E, 0x53: 0x1F, 0x44: 0x20, 0x46: 0x21, 0x47: 0x22, 0x48: 0x23,
    0x4A: 0x24, 0x4B: 0x25, 0x4C: 0x26, 0xBA: 0x27, 0xDE: 0x28, 0xC0: 0x29,
    0x5A: 0x2C, 0x58: 0x2D, 0x43: 0x2E, 0x56: 0x2F,
    0x42: 0x30, 0x4E: 0x31, 0x4D: 0x32, 0xBC: 0x33, 0xBE: 0x34, 0xBF: 0x35,
    0x20: 0x39, 0xA4: 0x38, 0xA5: 0x38,      # 左右 Alt 共用 0x38，靠扩展标志区分
    0x26: 0x48, 0x28: 0x50, 0x25: 0x4B, 0x27: 0x4D,   # 方向键与小键盘共用
    0x24: 0x47, 0x23: 0x4F, 0x21: 0x49, 0x22: 0x51,
    0x2D: 0x52, 0x2E: 0x53,
}

# 遥控器 Consumer Control usage -> 按键 id（受控采集，14/14 全命中）
USAGE_TO_KEY = {
    0x019E: "power", 0x0189: "input",
    0x0042: "up", 0x0043: "down", 0x0044: "left", 0x0045: "right",
    0x0041: "ok", 0x0224: "back", 0x0223: "home",
    0x00E9: "volup", 0x00EA: "voldown", 0x00E2: "mute",
    0x0077: "youtube", 0x0078: "netflix",
}
KEY_TO_USAGE = {v: k for k, v in USAGE_TO_KEY.items()}

# ============================================================ SendInput
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD, INPUT_MOUSE = 1, 0
WHEEL_DELTA = 120

user32 = ctypes.WinDLL("user32", use_last_error=True)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTunion(ctypes.Union):
    # ⚠ 64 位下这个 union 必须是 32 字节（MOUSEINPUT 最大）。
    # 写成 24 字节 → SendInput 直接返回 err=87，一个键都发不出去，而且不报原因。
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


def _kb(vk, up=False, scan=0, flags=0):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = KEYBDINPUT(wVk=vk, wScan=scan,
                          dwFlags=flags | (KEYEVENTF_KEYUP if up else 0),
                          time=0, dwExtraInfo=None)
    return inp


def _mouse(flags, data=0):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi = MOUSEINPUT(dx=0, dy=0, mouseData=data & 0xFFFFFFFF,
                          dwFlags=flags, time=0, dwExtraInfo=None)
    return inp


def _send(items):
    if not items:
        return False
    arr = (INPUT * len(items))(*items)
    return user32.SendInput(len(items), arr, ctypes.sizeof(INPUT)) == len(items)


def vk_of(name):
    return VK.get(str(name or "").strip().upper())


def key_down(vk):
    return _send([_kb(vk, scan=SCAN.get(vk, 0),
                      flags=KEYEVENTF_EXTENDEDKEY if vk in EXT_VK else 0)])


def key_up(vk):
    return _send([_kb(vk, up=True, scan=SCAN.get(vk, 0),
                      flags=KEYEVENTF_EXTENDEDKEY if vk in EXT_VK else 0)])


def mods_down(mods):
    return _send([_kb(vk_of(m), scan=SCAN.get(vk_of(m), 0),
                      flags=KEYEVENTF_EXTENDEDKEY if vk_of(m) in EXT_VK else 0)
                  for m in mods if vk_of(m)])


def mods_up(mods):
    return _send([_kb(vk_of(m), up=True, scan=SCAN.get(vk_of(m), 0),
                      flags=KEYEVENTF_EXTENDEDKEY if vk_of(m) in EXT_VK else 0)
                  for m in reversed(mods) if vk_of(m)])


def send_text(text):
    """逐 UTF-16 code unit 用 UNICODE 事件发文本（中文靠这条才能上屏）。

    单次 SendInput 有长度限制，分批发（每批 64 个 code unit）。
    """
    units = []
    for ch in text:
        raw = ch.encode("utf-16-le")
        for i in range(0, len(raw), 2):
            units.append(int.from_bytes(raw[i:i + 2], "little"))
    for i in range(0, len(units), 64):
        batch = []
        for u in units[i:i + 64]:
            batch.append(_kb(0, scan=u, flags=KEYEVENTF_UNICODE))
            batch.append(_kb(0, up=True, scan=u, flags=KEYEVENTF_UNICODE))
        _send(batch)
    return True


MOUSE_ACTIONS = {
    "wheel_up": 0x0800, "wheel_down": 0x0800,
    "left": 0x0002, "left_up": 0x0004,
    "right": 0x0008, "right_up": 0x0010,
    "middle": 0x0020, "middle_up": 0x0040,
}
# 「按住鼠标」松开时要发的配对动作
MOUSE_RELEASE = {"left": "left_up", "right": "right_up", "middle": "middle_up"}
# 按住期间要**连续重复**的鼠标动作：滚轮按住只动一格等于没用
MOUSE_REPEAT = ("wheel_up", "wheel_down")
REPEAT_INITIAL = 0.30        # 按住多久后开始连发（秒）
REPEAT_INTERVAL = 0.07       # 连发间隔（秒），约 14 次/秒


def mouse_action(name):
    name = str(name or "").lower()
    flags = MOUSE_ACTIONS.get(name)
    if not flags:
        return False
    if name == "wheel_up":
        return _send([_mouse(flags, WHEEL_DELTA)])
    if name == "wheel_down":
        return _send([_mouse(flags, -WHEEL_DELTA & 0xFFFFFFFF)])
    return _send([_mouse(flags)])


def key_is_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


# ============================================================ 映射执行
class Player:
    """把一条映射作用到「按下 / 松开」两个动作上。

    key / combo            —— 点一下（按下立刻抬起）
    hold / holdcombo       —— 遥控器键按住期间保持按下，松开遥控器才抬起
    holdmouse              —— 按住鼠标：滚轮按住**连续滚**，左右中键按住**不放**（拖拽用）
    mouse / text           —— 只在按下那一下触发
    """

    def __init__(self):
        self.held = None          # 当前 hold 住的键盘键：(mods, vk) 或 None
        self.held_mouse = None    # 当前按住的鼠标键名（left/right/middle）或 None
        self._rep_stop = None     # 滚轮连发的停止信号

    def _tap(self, mods, vk):
        if mods:
            mods_down(mods)
        key_down(vk)
        key_up(vk)
        if mods:
            mods_up(mods)

    def _hold_start(self, mods, vk):
        self._hold_stop()
        if mods:
            mods_down(mods)
        key_down(vk)
        self.held = (list(mods), vk)

    def _hold_stop(self):
        if not self.held:
            return
        mods, vk = self.held
        key_up(vk)
        if mods:
            mods_up(mods)
        self.held = None

    # ---------------------------------------------------------- 按住鼠标
    def _mouse_repeat_stop(self):
        if self._rep_stop is not None:
            self._rep_stop.set()
            self._rep_stop = None

    def _holdmouse_start(self, value):
        self._holdmouse_stop()           # 同时只按住一个鼠标动作
        if value in MOUSE_REPEAT:
            mouse_action(value)          # 先来一格（按下去就有反馈）
            stop = threading.Event()
            self._rep_stop = stop

            def loop():
                # 先等一小会儿再开始连发：轻点一下只滚一格，按住不放才连续滚
                if stop.wait(REPEAT_INITIAL):
                    return
                while not stop.wait(REPEAT_INTERVAL):
                    mouse_action(value)

            threading.Thread(target=loop, daemon=True, name="holdmouse").start()
        elif value in MOUSE_RELEASE:
            mouse_action(value)
            self.held_mouse = value

    def _holdmouse_stop(self):
        self._mouse_repeat_stop()
        if self.held_mouse:
            rel = MOUSE_RELEASE.get(self.held_mouse)
            if rel:
                mouse_action(rel)
            self.held_mouse = None

    def down(self, m):
        t = m.get("type")
        value = str(m.get("value") or "")
        mods = [str(x) for x in (m.get("mods") or [])]
        if t == "mouse":
            mouse_action(value)
            return
        if t == "holdmouse":
            self._holdmouse_start(value)
            return
        if t == "text":
            send_text(str(m.get("text") or ""))
            return
        vk = vk_of(value)
        if not vk:
            return
        if t in ("hold", "holdcombo"):
            self._hold_start(mods if t == "holdcombo" else [], vk)
        else:                                   # key / combo
            self._tap(mods if t == "combo" else [], vk)

    def up(self, m):
        t = m.get("type")
        if t in ("hold", "holdcombo"):
            self._hold_stop()
        elif t == "holdmouse":
            self._holdmouse_stop()

    def release_all(self):
        self._hold_stop()
        self._holdmouse_stop()


# ============================================================ Frida 旁路
BTHLE_ENUM = r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice"
HID_SVC_PREFIX = "{00001812-0000-1000-8000-00805f9b34fb}"
WUDF_DIAG = r"Device Parameters\WUDFDiagnosticInfo"
READ_IOCTL = 0x80018483


def hid_devices(vid="18D1", pid="9450", any_hid=False):
    """[(HostPid, 服务节点名)] —— BLE HID（0x1812）设备所在的驱动宿主。

    节点名里带着**这台设备是谁**：`..._Dev_VID&0218d1_PID&9450_REV&011b_<MAC>`，
    所以顺手把它返回出去，日志里就能写清"挂上的是哪台设备" —— 判断遥控器是原厂还是
    兼容款（真机见过：同一批山寨遥控器有的有语音服务、有的没有）全靠这一行。

    `any_hid=True`：不挑 VID/PID，任何 BLE HID 设备都要（**兼容款兜底** —— 它们可能用
    别的 VID/PID，甚至根本没写厂商信息，但按键照样走 HID，映射还能用）。
    """
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, BTHLE_ENUM) as root:
            i = 0
            while True:
                try:
                    svc = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                low = svc.casefold()
                if not low.startswith(HID_SVC_PREFIX.casefold()):
                    continue
                if not any_hid and (vid.lower() not in low or pid.lower() not in low):
                    continue
                with winreg.OpenKey(root, svc) as sk:
                    j = 0
                    while True:
                        try:
                            inst = winreg.EnumKey(sk, j)
                        except OSError:
                            break
                        j += 1
                        try:
                            with winreg.OpenKey(root, f"{svc}\\{inst}\\{WUDF_DIAG}") as dk:
                                out.append((int(winreg.QueryValueEx(dk, "HostPid")[0]), svc))
                        except (OSError, TypeError, ValueError):
                            continue
    except OSError:
        pass
    return out


def find_wudfhost_pid(vid="18D1", pid="9450", any_hid=False):
    """遥控器 HID 服务(0x1812) 所在 WUDFHost 进程的 PID（找不到返回 None）。"""
    devs = hid_devices(vid, pid, any_hid)
    return devs[0][0] if devs else None


def probe_attach():
    """能不能往一个**普通进程**（记事本）里注入？—— 把"环境阻止注入"和"我们自己的问题"分开。

    真机背景：公司电脑上日志反复 `TransportError: the connection is closed`，而**杀软界面里
    没有任何记录** —— 这不奇怪：EDR / Defender ASR / WDAC / AppLocker 这类策略是**静默拒绝**，
    记录只在企业控制台或事件日志里，用户界面上看不到。
    往记事本注入一下就能定性：连记事本都注入不进去 → 是这台机器在阻止注入，
    跟遥控器、蓝牙、我们的代码都无关；反过来则说明问题只在"注入蓝牙 HID 宿主"这一步。
    """
    try:
        import frida
    except Exception as e:
        return i18n.L(f"注入能力测试：本机没有 frida（{e!r}）",
                      f"Injection test: frida is not available here ({e!r})")
    import subprocess
    import sys
    # ⚠ 探测目标必须挑"一定能注的"：记事本在 Win11 上是打包应用（AppContainer），
    #   连本机 frida 正常的时候也注不进去 —— 拿它当探针会得出完全相反的结论（实测踩过）。
    #   用我们**自己那个解释器**跑一个空转进程：同权限、非打包，能注 = frida 这条路是通的。
    p = None
    try:
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             creationflags=0x08000000)
        time.sleep(2.0)
        s = frida.attach(p.pid)
        s.detach()
        return i18n.L("注入能力测试：往一个普通进程注入**成功** → frida 本身没问题，"
                      "问题只出在注入蓝牙 HID 宿主那一步（把那行自检发给开发者）。",
                      "Injection test: attaching to an ordinary process **succeeded** → frida itself "
                      "is fine, so the problem is specific to the Bluetooth HID host (send that "
                      "self-check line to the developer).")
    except Exception as e:
        return i18n.L(
            f"注入能力测试：往一个普通进程注入**失败**（{type(e).__name__}: {str(e)[:80]}）→ "
            f"这台机器在阻止注入（公司电脑很常见，而且**杀软界面可能一条记录都没有**，"
            f"记录只在企业控制台/事件日志里）。请让 IT 给程序目录放行；放行之前语音照常可用。",
            f"Injection test: attaching to an ordinary process **failed** "
            f"({type(e).__name__}: {str(e)[:80]}) → this machine is blocking injection (common on "
            f"corporate PCs, and the antivirus UI may show **no record at all** — those records live "
            f"in the enterprise console or the event log). Ask IT to allow the program folder; voice "
            f"keeps working until then.")
    finally:
        try:
            if p:
                p.kill()
        except Exception:
            pass


def wudfhost_pids():
    """列出机器上所有 WUDFHost.exe 的 PID。

    为什么要它：一台机器上常有**好几个** WUDFHost（蓝牙、指纹、摄像头各一个），
    而"遥控器的按键读调用"只出现在**服务它的那个**里面。真机上出现过：注册表查到的
    PID 换了（蓝牙重置/驱动重载），我们还挂在旧进程上 —— 界面显示"就绪"，
    实际一个报文都收不到。有了这张表，就能在收不到数据时换一个宿主再试。
    """
    import subprocess
    pids = []
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WUDFHost.exe",
                              "/NH", "/FO", "CSV"],
                             capture_output=True, timeout=15,
                             creationflags=0x08000000).stdout or b""
    except Exception:
        return pids
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            text = out.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = out.decode("utf-8", "replace")
    for line in text.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].lower().startswith("wudfhost") and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


class KeyTap(threading.Thread):
    """Frida 会话线程：读报文 → 解 usage → 施加映射。

    on_event(kind, payload) 由 app 传入，用来推给前端：
      ("key", {"id": "ok", "down": True})   遥控器按键事件（界面高亮用）
      ("log", "文本")
      ("status", {"ready": bool, "note": str})
    """

    def __init__(self, on_event, vidpid=("18D1", "9450")):
        super().__init__(daemon=True, name="KeyTap")
        self.on_event = on_event
        # 收到遥控器报文时的回调。语音角色用它来"立刻重连" —— 遥控器只在按键后
        # 醒一小会，这时候去连成功率最高；等退避睡满再连就错过了窗口。
        self.on_activity = None
        self.vidpid = vidpid
        self.player = Player()
        self.mapping = {}
        self.blocked_usages = []
        self.stop_flag = threading.Event()
        self.ready = False
        self.note = i18n.L("未启动", "Not started")
        self.last_seen = 0.0
        # 诊断/自愈用：钩子挂上了却收不到按键报文时，靠这几项判断"是不是宿主换了"
        self._hb = {}
        self._attached_pid = None
        self._attached_at = 0.0
        self._av_hinted = False
        self.reports = 0
        self._held = set()          # 当前按住的 usage（过滤长按连发）
        self._script = None
        self._session = None
        self._lock = threading.Lock()

    # ---------- 对外 ----------
    def set_mapping(self, mapping):
        with self._lock:
            self.mapping = dict(mapping or {})
        self._push_block()

    def _push_block(self):
        """把「已被映射的键」的 usage 下发给钩子做清位。"""
        with self._lock:
            usages = sorted({KEY_TO_USAGE[k] for k in self.mapping
                             if k in KEY_TO_USAGE})
        self.blocked_usages = usages
        if self._script:
            try:
                self._script.post({"type": "block", "usages": usages})
            except Exception:
                pass

    def _emit(self, kind, payload=None):
        try:
            self.on_event(kind, payload)
        except Exception:
            pass

    def _log(self, msg):
        self._emit("log", msg)

    # ---------- 主循环 ----------
    def run(self):
        try:
            import frida
        except ImportError:
            self.note = i18n.L("缺 frida（按键映射不可用，语音不受影响）",
                               "frida missing (key mapping unavailable; voice is unaffected)")
            self._emit("status", {"ready": False, "note": self.note})
            self._log("按键映射：未安装 frida，跳过（语音键仍可用）")
            return

        with open(TAP_JS, encoding="utf-8") as f:
            source = f.read()

        # ★ 重试节奏很重要：Frida 第一次挂载会要求**一次提权**（frida-helper）。
        #   如果失败就每 2 秒猛重试，就会把那个授权框反复弹出来 —— 用户看到的是
        #   「怎么一直弹」。所以这里改成指数退避，并且对「权限类失败」特别狠：
        #   连撞 3 次就退到 5 分钟一次，只提示一次，绝不刷屏。
        delay = 2.0
        perm_fails = 0
        hinted = False
        while not self.stop_flag.is_set():
            try:
                pid, dev = self._next_host_pid()
                if not pid:
                    self.note = i18n.L(
                        "没找到遥控器的 HID 驱动宿主（遥控器配对了吗？）",
                        "The remote's HID driver host was not found (is the remote paired?)")
                    self._emit("status", {"ready": False, "note": self.note})
                    if self.stop_flag.wait(min(delay, 10.0)):
                        break
                    continue

                if self._session is None:
                    self._log(f"按键映射：挂上蓝牙驱动宿主 WUDFHost (PID {pid})"
                              + (f" ← {dev}" if dev else "")
                              + ("（非原厂 VID/PID，按「任意 BLE HID 设备」兜底找到的）"
                                 if getattr(self, "_compat_remote", False) else ""))
                    self._session = frida.attach(pid)
                    self._script = self._session.create_script(source)
                    self._script.on("message", self._on_message)
                    self._script.load()
                    self._push_block()
                    self.last_seen = time.time()
                    self._attached_pid = pid
                    self._attached_at = time.time()
                    self._hb = {}
                    self._av_hinted = False
                    delay = 2.0                 # 成功后恢复灵敏重试
                    perm_fails = 0
                else:
                    # ★ 宿主没了/换了就得重新挂：蓝牙重置、驱动重载都会让 WUDFHost 换 PID，
                    #   而我们还挂在那个已经死掉的进程上 —— 界面照样显示"就绪"，
                    #   实际一个按键报文都收不到（真机上就是这么"看着正常却没用"的）。
                    now = (find_wudfhost_pid(*self.vidpid)
                           or find_wudfhost_pid(*self.vidpid, any_hid=True)
                           or self._attached_pid)
                    if self._attached_pid and (now != self._attached_pid
                                               or not self._pid_alive(self._attached_pid)):
                        self._log(f"按键映射：宿主换了或没了（PID {self._attached_pid} → {now}），"
                                  f"重新挂上去")
                        self._teardown_session()
                        continue
                    time.sleep(1.0)
            except Exception as e:
                self._session = None
                self._script = None
                self.ready = False
                name, text = type(e).__name__, str(e)
                perm = ("PermissionDenied" in name or "unable to access" in text
                        or "AccessDenied" in name)
                if perm:
                    perm_fails += 1
                    if perm_fails >= 3:
                        delay = 300.0           # 别再招惹授权框了
                        self.note = i18n.L(
                            "按键映射需要一次管理员授权（已暂停自动重试，"
                            "5 分钟后再试一次）",
                            "Key mapping needs a one-time administrator approval "
                            "(auto-retry paused; it will try again in 5 minutes)")
                        if not hinted:
                            hinted = True
                            self._log("按键映射：挂载需要提权，但授权没给 —— "
                                      "已停下不再反复弹框。想用按键映射就在界面"
                                      "「设置」里点『以管理员身份重启』（只需授权一次）。")
                    else:
                        self.note = i18n.L(
                            f"挂载失败（{perm_fails}/3）：需要一次提权授权",
                            f"Attach failed ({perm_fails}/3): one-time elevation required")
                        delay = min(delay * 2, 30.0)
                else:
                    self.note = i18n.L(f"挂载失败：{name}: {text}",
                                       f"Attach failed: {name}: {text}")
                    # ⚠ 最常见的一种：TransportError: the connection is closed ——
                    #   那是"注入通道被掐断"，在装了杀软/EDR 的机器（尤其公司电脑）上
                    #   几乎一定是安全软件拦的。这句术语对外行毫无意义，直接给一句人话。
                    if ("TransportError" in name or "connection is closed" in text) and not self._av_hinted:
                        self._av_hinted = True
                        self._log(i18n.L(
                            "按键映射：注入被中断了（TransportError）—— 在装了杀软/EDR 的机器上"
                            "（公司电脑尤其常见）几乎都是安全软件拦的。正规解法：请 IT 把"
                            "**程序目录**加进杀软白名单（我们不改系统设置、不关 Defender，"
                            "只需要放行注入）。放行之前语音功能不受影响，"
                            "只是方向键/OK/音量这些用不了。",
                            "Key mapping: the injection was cut off (TransportError) — on machines "
                            "with antivirus/EDR (very common on corporate PCs) this is almost always "
                            "the security software blocking it. The proper fix is to ask IT to "
                            "whitelist the program folder (we never change system settings or disable "
                            "Defender; we only need the injection allowed). Until then voice still "
                            "works — only D-pad/OK/volume are unavailable."))
                    delay = min(delay * 2, 60.0)
                self._emit("status", {"ready": False, "note": self.note})
                self._log(f"按键映射：{self.note}（{delay:.0f} 秒后重试）")
                if self.stop_flag.wait(delay):
                    break

    # ---------- 宿主选择 / 自愈 / 诊断 ----------
    def _next_host_pid(self):
        """该挂哪个 WUDFHost，以及这台设备是谁（节点名里带 VID/PID）。

        先按配置的 VID/PID 找原厂那款；找不到就**退一步认任意 BLE HID 设备** ——
        兼容款遥控器可能用别的 VID/PID（甚至不写厂商），但按键照样走 HID 服务，
        映射还能用；没有这一步，那些遥控器会直接报"没找到宿主"。
        """
        devs = hid_devices(*self.vidpid)
        if not devs:
            devs = hid_devices(*self.vidpid, any_hid=True)
            if devs:
                self._compat_remote = True
        if not devs:
            return None, ""
        return devs[0][0], devs[0][1]

    def _teardown_session(self):
        """断掉当前钩子但不停线程（宿主换了、或要换一个宿主时用）。"""
        try:
            if self._script:
                self._script.unload()
        except Exception:
            pass
        try:
            if self._session:
                self._session.detach()
        except Exception:
            pass
        self._script = None
        self._session = None
        self._attached_pid = None
        self.ready = False

    def selfcheck(self):
        """按需输出"钩子到底看到了什么"（界面「设置 → 诊断 → 按键映射自检」调它）。

        为什么要有它：真机上出现过"钩子挂上了、界面显示就绪、按键却完全没反应"，
        而日志里只有成功/失败 —— 分不清是"挂到了不服务这支遥控器的宿主"，
        还是"这支遥控器的报文格式跟预期不同"。这几个原始计数能一眼分辨：
          · 读调用 0 次 + 收到 0 条             → 宿主不对
          · 读调用 N 次、长度里从没出现 3 字节  → 报文格式不同（另一款遥控器）
          · 长度里有 3 字节 / 收到过报文        → HID 这条路是通的
        ⚠ 必须**按需**触发：遥控器空闲时本来就不发报文（实测：健康机器上 90 秒里
        只有别的蓝牙键鼠的 9 字节输出），所以定时判断"没有 3 字节 = 坏了"一定是误报。
        """
        if not self._attached_pid:
            return i18n.L("按键映射没有在运行（先看「运行依赖」里 frida 是否齐全）",
                          "Key mapping is not running (first check that frida is installed)")
        hb = self._hb or {}
        lens = hb.get("lens") or {}
        dist = "、".join(f"{k} 字节×{v}" for k, v in sorted(lens.items())) or "无"
        sent = int(hb.get("sent") or 0)
        ioctl = int(hb.get("ioctl") or 0)
        host_n = len(wudfhost_pids())
        tip = i18n.L("HID 这条路是通的。",
                     "The HID path is working.") if (sent or lens.get("3")) else i18n.L(
            "如果你刚刚连按了几个方向键、而这行仍是 0 条，把这行发给开发者即可定位"
            "（是宿主不对还是这支遥控器的报文格式不同）。",
            "If you just pressed a few D-pad keys and this still says 0, send this line to the "
            "developer — it tells whether the host is wrong or this remote uses a different format.")
        # 没有任何报文时，顺手做一次"注入能力测试"：把"这台机器不让注入"这条单独验掉，
        # 省得用户在杀软界面里翻记录（那里本来就看不到静默拦截）。
        extra = ""
        if not sent and not lens.get("3"):
            try:
                extra = " " + probe_attach()
            except Exception:
                pass
        # 这台遥控器**实际发过哪些键**（含没被映射、我们表里没有的）—— 换遥控器/兼容款
        # 时靠这一行认人：语音键在某些款上是 HID usage（而不是 ATVV），一眼就能看出来。
        seen = hb.get("seen") or {}
        if seen:
            items = []
            for u, n in sorted(seen.items(), key=lambda kv: -int(kv[1]))[:14]:
                try:
                    code = int(u)
                except (TypeError, ValueError):
                    continue
                name = USAGE_TO_KEY.get(code)
                items.append(f"{name or '未知'}(0x{code:04X})×{n}")
            if items:
                extra += " 已见按键：" + "、".join(items)
        return i18n.L(
            f"按键映射自检：挂在宿主 PID {self._attached_pid}（本机共 {host_n} 个 WUDFHost），"
            f"该宿主上「遥控器读」{ioctl} 次，输出长度：{dist}；已收到 {sent} 条按键报文。{tip}",
            f"Key-mapping self-check: attached to host PID {self._attached_pid} "
            f"({host_n} WUDFHost processes on this machine), {ioctl} “remote read” calls in that "
            f"host, output lengths: {dist}; {sent} key reports received. {tip}") + extra

    def _pid_alive(self, pid):
        """进程还在不在（ctypes 直接问系统，比每次起 tasklist 便宜）。

        ⚠ 两个坑都踩过：① 不声明 restype，64 位下 HANDLE 会被当 int32 截断，
        活着的进程被看成"没了"（于是每秒重挂一次）；② OpenProcess 失败不一定是
        "进程不存在"——权限不足也会失败，那种必须当"活着"，否则会疯狂重挂。
        """
        if not pid:
            return False
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
            if h:
                k32.CloseHandle(h)
                return True
            return ctypes.get_last_error() != 87           # 87 = 进程不存在
        except Exception:
            return True                                    # 问不出来就当它活着，别乱重挂

    def _on_message(self, message, data):
        if message.get("type") == "error":
            self._log(f"按键映射脚本错误：{str(message.get('description'))[:200]}")
            return
        payload = message.get("payload") or {}
        kind = payload.get("kind")
        if kind == "ready":
            self.ready = True
            self.note = i18n.L("运行中", "Running")
            self._emit("status", {"ready": True, "note": self.note})
            self._log("按键映射：就绪")
        elif kind == "report":
            self._on_report(payload.get("raw") or "")
        elif kind == "block_ack":
            self._log(f"按键映射：已屏蔽 {payload.get('count', 0)} 个键的原生动作")
        elif kind == "hb":
            self._hb = payload                 # 诊断用：看这个宿主到底读到了什么
        elif kind == "error":
            self._log(f"按键映射：{payload.get('message')}")

    def _on_report(self, raw):
        """raw 形如 '02 42 00'。"""
        self.last_seen = time.time()
        if self.on_activity:
            try:
                self.on_activity()
            except Exception:
                pass
        parts = raw.split()
        if len(parts) != 3:
            return                      # 遥控器固定 3 字节；9 字节是别的键鼠
        try:
            b = [int(x, 16) for x in parts]
        except ValueError:
            return
        usage = b[1] | (b[2] << 8)
        self.reports += 1

        if usage == 0:                  # 空闲帧 = 全部松开
            for u in list(self._held):
                self._release(u)
            return
        if usage in self._held:
            return                      # 长按连发：只认第一下
        self._held.add(usage)
        kid = USAGE_TO_KEY.get(usage)
        if not kid:
            return
        self._emit("key", {"id": kid, "down": True})
        m = self.mapping.get(kid)
        if m:
            try:
                self.player.down(m)
            except Exception as e:
                self._log(f"按键映射：执行 {kid} 失败 {type(e).__name__}: {e}")

    def _release(self, usage):
        self._held.discard(usage)
        kid = USAGE_TO_KEY.get(usage)
        if not kid:
            return
        self._emit("key", {"id": kid, "down": False})
        m = self.mapping.get(kid)
        if m:
            try:
                self.player.up(m)
            except Exception:
                pass

    def stop(self):
        self.stop_flag.set()
        self.player.release_all()
        try:
            if self._script:
                self._script.unload()
        except Exception:
            pass
        try:
            if self._session:
                self._session.detach()
        except Exception:
            pass
        self._script = None
        self._session = None
        self.ready = False
        self.note = i18n.L("已停止", "Stopped")
