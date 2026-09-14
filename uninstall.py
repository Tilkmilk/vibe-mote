# -*- coding: utf-8 -*-
"""卸载清理：**逐项勾选**，默认什么都不勾。

设计约定（跟界面上的清单一一对应）：
  · ITEMS 是唯一真相来源，界面通过 /api/uninstall-items 读它，两边不会走偏。
  · **默认全部不勾** —— 没勾 = 不删。想删哪项自己勾。
  · 执行顺序固定（不是按勾选顺序），因为顺序错了会失败：
      先断自启 → 再杀进程 → 再删服务/文件 → 最后结束自己
    （不先杀进程就删不掉 .venv：Windows 上被加载的 DLL 是锁住的）

用法：
    python uninstall.py --items=venv,pycache        # 只删这两项
    python uninstall.py --items=all                 # 全删（含 config.json 和千问开关）
    python uninstall.py --items=task,venv --dry-run # 只列清单，什么都不做
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import winreg

HERE = os.path.dirname(os.path.abspath(__file__))
# ★ 跟 app.py 顶部同一件事：离线包用的是官方 embeddable Python，它的 `._pth` 会让
# sys.path 进入隔离模式、**不含脚本目录** —— 不自己塞一次，`import i18n` 当场炸。
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import i18n  # noqa: E402  （必须在 sys.path 之后就位）
LOG = os.path.join(HERE, "uninstall.log")
TASK_NAME = "VibeMote"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
QWEN_VAR = "QIANWEN_IME_UTILITY_VOICE_HOOK_ALLOW_INJECTED"
# 删掉千问开关时留的"别再自动打开"标记（app.py 的 auto_qwen_flag 认它）
QWEN_OPTOUT = os.path.join(HERE, "qwen_optout")
UI_PORT = 8787
LOCK_PORT = 49741
CREATE_NO_WINDOW = 0x08000000

# 虚拟声卡（VB-CABLE）：包内带的官方安装器 + 官方注册的卸载入口
CABLE_DIR = os.path.join(HERE, "vbcable")
CABLE_SETUP_NAME = "VBCABLE_Setup_x64.exe"
CABLE_UNINST_ROOT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
# 官方安装会留下的两处痕迹（探测用；**不看 sounddevice**，理由见 do_cable 上面那段）
CABLE_SVC_NAMES = ("VB-Cable", "VBAudioVACMME")

# 已经"安排到退出后再删"的目录（规范化绝对路径）。核实环节要认它，
# 否则会把"已安排，马上删"误报成"没删掉"。
DEFERRED = set()

# id, 界面上的名字, 说明, 是否需要管理员, 是否属于"推荐清理"
ITEMS = [
    ("task",         i18n.pair("取消开机自启（计划任务）", "Disable autostart (scheduled task)"),
                     i18n.pair("删除最高权限计划任务 VibeMote",
                               "Delete the highest-privilege scheduled task VibeMote"), True, True),
    ("run",          i18n.pair("取消开机自启（启动项）", "Disable autostart (Run key)"),
                     i18n.pair("删除 HKCU Run 里的 VibeMote",
                               "Delete VibeMote from the HKCU Run key"), False, True),
    ("kill",         i18n.pair("结束客户端进程", "Stop the client process"),
                     i18n.pair("语音桥接 + 按键映射进程",
                               "Voice bridge + key-mapping process"), False, True),
    ("frida_helper", i18n.pair("结束 frida 助手进程", "Stop frida helper processes"),
                     i18n.pair("frida-helper-x86 / x86_64",
                               "frida-helper-x86 / x86_64"), False, True),
    ("frida_svc",    i18n.pair("清理 frida 残留服务", "Remove leftover frida services"),
                     i18n.pair("HKLM 里的 frida-<pid>-x86* 服务",
                               "frida-<pid>-x86* services in HKLM"), True, True),
    ("venv",         i18n.pair("删除依赖（.venv）", "Delete dependencies (.venv)"),
                     i18n.pair("装过的 bleak / sounddevice / cffi / frida",
                               "bleak / sounddevice / cffi / frida as installed"), False, True),
    # ⚠ 这里**故意没有**「删除内置 Python 运行时（runtime）」这一项，别再把它加回来。
    # 原因：runtime 是离线包自带的 Python 发行版（解压后 154MB），
    #   · 它属于**程序本体**，跟 app.py 同级，不是"装出来的东西"；
    #   · 界面上那个「全部安装」装的是 pip 包（依赖），**装不回**一个 Python 发行版 ——
    #     用户一旦手滑勾了它，这台机器就再也不是"免安装"的了，而且没法一键恢复；
    #   · 不删它，包的免安装能力就一直在（这正是离线包存在的意义）。
    # 想彻底删干净：直接删整个程序目录即可 —— 纯绿色，系统里只有自启项和 frida 服务，
    # 那两项清理工具会处理。
    ("pycache",      i18n.pair("删除 __pycache__", "Delete __pycache__"),
                     i18n.pair("Python 字节码缓存", "Python bytecode cache"), False, True),
    ("pythontxt",    i18n.pair("删除 python.txt", "Delete python.txt"),
                     i18n.pair("指定解释器的指向文件（换机后本来就该重建）",
                               "Interpreter pointer file (meant to be rebuilt on a new machine)"), False, True),
    ("logs",         i18n.pair("删除日志", "Delete logs"),
                     i18n.pair("elevate_setup.log / uninstall.log",
                               "elevate_setup.log / uninstall.log"), False, True),
    ("config",       i18n.pair("删除 config.json", "Delete config.json"),
                     i18n.pair("你的按键映射与语音设置会丢，回到默认方案",
                               "Your keymap and voice settings are lost, and the default preset comes back"), False, False),
    ("qwen",         i18n.pair("删除千问「允许注入」开关", "Remove Qianwen’s “allow injection” switch"),
                     i18n.pair("删了千问会重新丢弃程序发的按键。程序会记住这次选择，之后不再自动打开",
                               "Qianwen will discard injected keystrokes again. The program remembers "
                               "this choice and will not turn the switch back on automatically"), False, False),
    # ⚠ 这一项跟别的**不是一类**，所以默认不勾、也**不进「推荐清理」**：
    #   · VB-CABLE 不是本程序装的（用户点按钮装的官方驱动），而且是**系统级**的 ——
    #     OBS / 变声器 / 直播工具可能也在用它，删了它们就没声了；
    #   · 官方**没有静默卸载**：Windows「应用和功能」里那条 UninstallString 就是
    #     VBCABLE_Setup_x64.exe 本身、**不带任何参数**（也没有 QuietUninstallString），
    #     只能点它窗口里的「Remove Driver」；
    #   · 官方 readme：卸载**必须重启**才算卸完。
    ("cable",        i18n.pair("卸载虚拟声卡（VB-CABLE）",
                               "Uninstall the virtual audio cable (VB-CABLE)"),
                     i18n.pair("系统级音频驱动，OBS / 变声器可能也在用它；会弹一次 UAC，"
                               "还要点它窗口里的「Remove Driver」，卸完必须重启。默认不勾",
                               "A system-wide audio driver that OBS / voice changers may also be "
                               "using. One UAC prompt, then click “Remove Driver” in its window; a "
                               "reboot is required afterwards. Off by default"), True, False),
    # ⚠ 这一项是给"装/卸卡住了"准备的**修复**动作，也默认不勾：
    #   · 现象：虚拟声卡"注册表里有、设备却枚举不到"，装也装不上（点 Install Driver 没反应）；
    #   · 原因：官方卸载器**永远不碰**这几处 —— 两个服务键、HKLM\SOFTWARE\VB-Audio、
    #     DriverStore 里的驱动包、C:\Windows\inf\oem*.inf。残留会把新一次安装挡住；
    #   · 用法：勾它清干净 → **重启一次** → 再点「一键安装虚拟声卡」。
    #   它不弹 GUI、不需要人点，但删驱动包要管理员，所以标了 admin。
    ("cable_residue", i18n.pair("清除虚拟声卡残留（注册表 / 驱动包）",
                                "Clear virtual-audio-cable leftovers (registry / driver store)"),
                     i18n.pair("官方卸载器不会清的那几处：两个服务键、HKLM\\SOFTWARE\\VB-Audio、"
                               "DriverStore 驱动包、oem*.inf。装/卸卡住时用它；清完要重启，再重装一次",
                               "What the official uninstaller never removes: the two service keys, "
                               "HKLM\\SOFTWARE\\VB-Audio, the DriverStore package and oem*.inf. Use it "
                               "when installing/uninstalling gets stuck; reboot, then reinstall"), True, False),
]
ITEM_IDS = [i[0] for i in ITEMS]
ADMIN_IDS = [i[0] for i in ITEMS if i[3]]
LABELS = {i[0]: i[1] for i in ITEMS}


def _utf8_console():
    """让 print 不因为 ✓ / ✗ / 🔒 崩掉 —— **这是必须的，不是美化**。

    ⚠ 注意：清理脚本是被 ShellExecuteW 拉起来的独立进程，它的 stdout 不保证
    是控制台、更不保证是 UTF-8。当 stdout 被重定向（管道 / 文件 / 某些宿主）时，
    Python 按 locale 的 ACP 编码 —— 中文 Windows 上是 **936(GBK)** ——
    而 `✓`(U+2713)、`✗`(U+2717)、`🔒` 都**不在 GBK 里**，
    于是 `print` 抛 UnicodeEncodeError，脚本在打印第一行状态（"管理员权限：✓"）
    的时候就崩了。用户看到的是"点了清理没反应 / 没删干净"。

    我这台机器的控制台代码页恰好是 65001，所以本地一直没暴露 ——
    是自动化测试把 stdout 接成管道之后才炸出来的。
    `app.py` 里早就有同样的 `_utf8_console()`，这里补上。
    """
    for s in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_utf8_console()          # 模块一导入就设好，别等到 main()


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        # 实在打不出去（被换成不支持的流）也不能让整个清理挂掉
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _dec(b: bytes) -> str:
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")


def run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return p.returncode, _dec(p.stdout or b"") + _dec(p.stderr or b"")
    except Exception as e:
        return -1, repr(e)


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def port_owners(port):
    rc, out = run(["netstat", "-ano"])
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


def rm_path(path, dry):
    """手工删除（不用 shutil.rmtree —— 它会被某些宿主劫持成"移到回收站"）。"""
    if not os.path.exists(path):
        return False
    if dry:
        return True
    if os.path.isdir(path):
        for root, dirs, files in os.walk(path, topdown=False):
            for fn in files:
                try:
                    os.unlink(os.path.join(root, fn))
                except OSError:
                    pass
            for dn in dirs:
                try:
                    os.rmdir(os.path.join(root, dn))
                except OSError:
                    pass
        try:
            os.rmdir(path)
        except OSError as e:
            log(i18n.L(f"  ✗ 删不掉 {os.path.basename(path)}（可能还有进程占着）：{e}",
                       f"  ✗ Cannot delete {os.path.basename(path)} (still in use?): {e}"))
            return False
    else:
        try:
            os.remove(path)
        except OSError as e:
            log(i18n.L(f"  ✗ 删不掉 {os.path.basename(path)}：{e}",
                       f"  ✗ Cannot delete {os.path.basename(path)}: {e}"))
            return False
    return True


# ---------------------------------------------------------------- 各项动作
#
# ⚠⚠ 预览（--dry-run）必须**真的什么都不做** —— 这里每个动作都要自己判断 dry。
# 反例：do_kill / do_run / do_frida_* / do_qwen 如果忘了看 dry，
# `uninstall.py --items=all --dry-run` 就会**把正在运行的客户端真的杀掉**、
# 顺手删掉 HKCU 自启项和千问开关，然后在日志里写「【预览，不会真删】」——
# 看着只是列了个清单，结果界面直接没了。
# 所以：dry 时只用**只读**手段探一下（注册表读、netstat、tasklist），然后说"会做什么"。
def do_task(dry):
    if dry:
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                         "System32", "Tasks", TASK_NAME)
        exists = os.path.exists(p)
        log(i18n.L(f"  （预览）会删除计划任务 {TASK_NAME}："
                   + ("存在，会删掉" if exists else "本来就不存在"),
                   f"  (preview) would delete the scheduled task {TASK_NAME}: "
                   + ("it exists and would be removed" if exists
                      else "it does not exist anyway")))
        return
    rc, out = run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])
    if rc == 0:
        log(i18n.L(f"  ✓ 已删除计划任务 {TASK_NAME}",
                   f"  ✓ Scheduled task {TASK_NAME} deleted"))
    elif any(k in out for k in ("找不到", "不存在")) or "cannot find" in out.lower():
        log(i18n.L(f"  - 计划任务 {TASK_NAME} 本来就不存在",
                   f"  - Scheduled task {TASK_NAME} did not exist anyway"))
    else:
        log(i18n.L(f"  ✗ 删计划任务失败（需要管理员）：{out.strip().splitlines()[:1]}",
                   f"  ✗ Could not delete the scheduled task (needs admin): "
                   f"{out.strip().splitlines()[:1]}"))


def do_run(dry):
    if dry:
        log(i18n.L("  （预览）会删除 HKCU Run 自启项："
                   + ("存在，会删掉" if _run_value_exists() else "本来就没有"),
                   "  (preview) would delete the HKCU Run autostart entry: "
                   + ("it exists and would be removed" if _run_value_exists()
                      else "it was not there anyway")))
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, TASK_NAME)
        log(i18n.L("  ✓ 已删除 HKCU Run 自启项",
                   "  ✓ HKCU Run autostart entry deleted"))
    except FileNotFoundError:
        log(i18n.L("  - HKCU Run 里本来就没有自启项",
                   "  - There was no autostart entry in HKCU Run"))
    except OSError as e:
        log(i18n.L(f"  ✗ 删 Run 项失败：{e}", f"  ✗ Could not delete the Run entry: {e}"))


def do_kill(dry):
    pids = sorted(set(port_owners(UI_PORT) + port_owners(LOCK_PORT)))
    me = os.getpid()
    targets = [p for p in pids if p != me]
    if not targets:
        log(i18n.L("  - 没有别的客户端进程在跑",
                   "  - No other client process is running"))
        return
    if dry:
        log(i18n.L(f"  （预览）会结束客户端进程：PID {'、'.join(map(str, targets))}（不会真杀）",
                   f"  (preview) would end the client process(es): PID "
                   f"{', '.join(map(str, targets))} (nothing is actually killed)"))
        return
    for pid in targets:
        # ⚠ 绝对不要加 /T！
        # 本脚本是**客户端拉起来的子进程**（ShellExecuteW），而 /T 会连同整棵子进程树
        # 一起杀 —— 那就把脚本自己也杀了，后面的删文件/删配置全都不会执行。
        # 加了 /T 会让日志停在"结束客户端进程"这一步，
        # .venv 和 config.json 都还在，
        # 用户看到的就是"卸了但没卸干净、回去还不提醒装依赖"。
        # 客户端没有需要连带杀的子进程（角色都在线程里），所以 /T 本来也没必要。
        rc, _ = run(["taskkill", "/PID", str(pid), "/F"])
        log(i18n.L(f"  {'✓ 已结束' if rc == 0 else '✗ 结束失败'} PID {pid}",
                   f"  {'✓ Ended' if rc == 0 else '✗ Failed to end'} PID {pid}"))
    # ★ 顺手删掉「我活着」的就绪标记（_ready.txt）。
    #   它是被杀掉的那个客户端留下的：正常退出时客户端自己会在 finally 里删它，
    #   但 taskkill /F 不会给它这个机会。留着虽然无害（启动器比的是修改时间），
    #   但"干净得像没装过"就谈不上了。
    if targets:
        try:
            rp = os.path.join(HERE, "_ready.txt")
            if os.path.exists(rp):
                os.remove(rp)
                log(i18n.L("  ✓ 已删掉残留的就绪标记 _ready.txt",
                           "  ✓ Removed the leftover readiness marker _ready.txt"))
        except OSError:
            pass


def do_frida_helper(dry):
    n = 0
    for exe in ("frida-helper-x86.exe", "frida-helper-x86_64.exe"):
        if dry:
            rc, out = run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"])
            if exe.lower() in out.lower():
                n += 1
            continue
        rc, _ = run(["taskkill", "/IM", exe, "/F"])
        if rc == 0:
            n += 1
    if dry:
        log(i18n.L(f"  （预览）会结束 {n} 个 frida 助手进程（不会真杀）" if n
                   else "  - 没有 frida 助手进程在跑",
                   f"  (preview) would end {n} frida helper process(es) "
                   f"(nothing is actually killed)" if n
                   else "  - No frida helper process is running"))
        return
    log(i18n.L(f"  {'✓ 已结束 frida 助手进程' if n else '- 没有 frida 助手进程在跑'}",
               f"  {'✓ frida helper process(es) ended' if n
                  else '- No frida helper process is running'}"))


def do_frida_svc(dry):
    targets = _frida_services()
    if not targets:
        log(i18n.L("  - 没有 frida 残留服务", "  - No leftover frida service"))
        return
    if dry:
        log(i18n.L(f"  （预览）会删除 {len(targets)} 条 frida-* 服务："
                   + "、".join(targets[:5]) + ("…" if len(targets) > 5 else "")
                   + "（不会真删）",
                   f"  (preview) would delete {len(targets)} frida-* service(s): "
                   + ", ".join(targets[:5]) + ("…" if len(targets) > 5 else "")
                   + " (nothing is actually deleted)"))
        return
    ok = 0
    for n in targets:
        rc, _ = run(["sc.exe", "delete", n])
        if rc == 0:
            ok += 1
    if ok:
        log(i18n.L(f"  ✓ 已删除 {ok}/{len(targets)} 条 frida-* 服务",
                   f"  ✓ Deleted {ok}/{len(targets)} frida-* service(s)"))
    elif not is_admin():
        log(i18n.L(f"  ⚠ 有 {len(targets)} 条 frida-* 服务但**需要管理员才能删** —— "
                   f"从界面点「清理」（会自动提权）再来一次就干净了",
                   f"  ⚠ {len(targets)} frida-* service(s) exist but **deleting them needs "
                   f"admin** — click “Clean up” in the UI once more (it elevates for you) "
                   f"and they will be gone"))
    else:
        log(i18n.L(f"  ✗ 已删除 0/{len(targets)} 条（可能正被占用）",
                   f"  ✗ Deleted 0/{len(targets)} (they may be in use)"))


def _file_step(name, dry):
    """删文件/目录。对 .venv 这种可能被进程占着的，多试几次再放弃。

    ⚠ 光靠"多试几次"是不够的：真正让 .venv 删不掉的是**清理脚本自己就跑在
    .venv\\Scripts\\pythonw.exe 上**（正在运行的镜像被 Windows 锁住）。这个根源在
    app.py 那边解决 —— 它现在会用 .venv 之外的 Python 来启动本脚本。
    """
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        log(i18n.L(f"  - {name} 不存在", f"  - {name} does not exist"))
        return False
    for attempt in range(6):
        if rm_path(path, dry):
            # ⚠ 预览时**不能**说"已删除" —— 整个日志别的行都写着"（预览）会…"，
            # 只有这里说"已删除 X"，用户会以为预览真的删了东西（config.json 尤其吓人）。
            log(i18n.L(f"  （预览）会删除 {name}" if dry else f"  ✓ 已删除 {name}",
                       f"  (preview) would delete {name}" if dry else f"  ✓ Deleted {name}"))
            return True
        if not os.path.exists(path):
            log(i18n.L(f"  ✓ 已删除 {name}", f"  ✓ Deleted {name}"))
            return True
        if attempt < 5:
            time.sleep(1.0)          # 等刚被杀掉的客户端彻底释放 DLL
    log(i18n.L(f"  ✗ {name} 没删掉（可能有进程还占着；先关掉客户端再试一次）",
               f"  ✗ Could not delete {name} (something may still hold it; "
               f"close the client and try again)"))
    return False


def _dir_size_mb(path):
    try:
        n = sum(os.path.getsize(os.path.join(r, f))
                for r, _, fs in os.walk(path) for f in fs)
        return n / 1024 / 1024
    except OSError:
        return 0.0


def _dir_holds_my_interpreter(path):
    """本进程是不是就跑在这个目录里的解释器上？

    Windows 不允许删除正在运行的映像文件（pythonw.exe），所以"删自己脚下的地板"
    必须换个做法 —— 否则就会重演 `.venv` 那种失败形态（报成功、其实剩下个空壳）。
    """
    try:
        exe = os.path.normcase(os.path.abspath(sys.executable or ""))
        d = os.path.normcase(os.path.abspath(path)) + os.sep
        return exe.startswith(d)
    except Exception:
        return False


# 退出后再删：写个 bat，让它反复重试 rmdir，直到那个目录真的消失。
# 为什么借 cmd.exe：这时候唯一可用的解释器就在待删目录里，只能用系统自带的东西；
# cmd.exe 必然存在（System32），不需要管理员，也不需要重启。
#
# ⚠ 四个必须避开的坑，别再改回去：
#  1. **别用 `tasklist | find "<pid>"` 判进程死活**。装了 Git for Windows 的机器上
#     PATH 里的 `find.exe` 是 GNU find，会报 "FIND: Parameter format not correct"，
#     于是"等进程退出"的循环被整个跳过 → rmdir 在进程还没退出时白跑一次 → 直接放弃
#     （60MB 的 runtime 就是这样没删掉的）。改成"反复重试 rmdir"最稳，
#     不依赖任何外部命令，也不用关心 PID。
#  2. **sleep 要用绝对路径的 ping**（`timeout` 需要有控制台，而我们是没有控制台的
#     分离进程；PATH 里的 ping 也可能是 Git/MSYS 的另一个版本）。
#  3. ★★ **bat 里绝对不能出现非 ASCII 的路径字面量。** cmd.exe 按"当前控制台代码页"
#     解析 .bat，而那个代码页跟 Python 的 mbcs(ACP) **不是一回事**，也跟写文件时用的
#     编码不一定一致。例如 ACP=936、OEMCP=936，但控制台代码页可能是 **65001** ——
#     于是 `encoding="mbcs"` 写出来的中文路径被 cmd 解成乱码，`rmdir` 删了个不存在的
#     路径、`if not exist 乱码` 为真 → **立刻打印成功然后退出**，真正的目录一动不动。
#     这不是"删不掉"，是"删错了还报成功"，比报错恶劣得多。
#     解决办法不是换编码（换哪种都可能踩另一种环境），而是**根本不放路径字面量**：
#     把 bat 放进程序目录，用 cmd 自己展开的 `%~dp0` 拼相对路径 —— 全是 ASCII。
#  4. ★ 删完要**留下结果**。上面第 3 条那种失败原来是完全静默的（日志里写着
#     "已安排删除…约 2 秒"，用户以为删干净了）。所以 bat 最后把结果写进
#     `_deferred_result.txt`，下次启动 app.py 会读出来如实报告。
_DELAYED_RESULT = os.path.join(HERE, "_deferred_result.txt")

_AFTER_EXIT_BAT = """set /a n=0
:retry
rmdir /s /q "{target}"
if not exist "{target}" goto ok
set /a n+=1
if %n% GEQ 90 goto fail
"%SystemRoot%\\System32\\ping.exe" -n 2 127.0.0.1 >nul
goto retry
:ok
echo OK {label}>"{result}"
goto end
:fail
echo FAIL {label}>"{result}"
:end
(goto) 2>nul & del "%~f0"
"""


def _delete_after_exit(path):
    """安排"本进程退出后再删这个目录"。返回是否安排成功。

    见上面第 3 条注释：bat 一律用 `%~dp0` + **相对**路径，不写非 ASCII 字面量。
    只有目标不在程序目录下（相对路径带 `..`）时才退回"UTF-8 写 + chcp 65001"。
    """
    pid = str(os.getpid())
    rel = None
    try:
        r = os.path.relpath(path, HERE)
        if not r.startswith("..") and all(ord(c) < 128 for c in r):
            rel = r.replace("/", "\\")
    except Exception:
        rel = None
    if rel:
        # ★ 放在程序目录里：这样 `%~dp0` 就是程序目录（cmd 运行时展开，跟编码无关），
        #   目标和结果文件都用它拼 —— bat 全文只剩 ASCII。
        bat = os.path.join(HERE, f"_cleanup_after_exit_{pid}.bat")
        target = f"%~dp0{rel}"
        result = "%~dp0_deferred_result.txt"
        head = "@echo off\n"
        label = rel
    else:
        # 退路：目标不在程序目录下。加 chcp 65001 再用 UTF-8 写（cmd 逐行读，
        # 第一行是纯 ASCII 的 chcp，后面的行就按新代码页解了）。
        bat = os.path.join(os.environ.get("TEMP", HERE), f"vibe-mote-cleanup-{pid}.bat")
        target = path
        result = _DELAYED_RESULT
        head = "@echo off\nchcp 65001 >nul\n"
        label = os.path.basename(path)
    cmd = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    if not os.path.exists(cmd):
        cmd = "cmd"
    try:
        with open(bat, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(head + _AFTER_EXIT_BAT.format(target=target, label=label,
                                                  result=result))
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：彻底脱离本进程，免得被一起结束
        subprocess.Popen([cmd, "/c", bat], close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 | 0x00000200)
        DEFERRED.add(os.path.normcase(os.path.abspath(path)))
        return True
    except Exception as e:
        log(i18n.L(f"  ✗ 安排后台删除失败：{e!r}",
                   f"  ✗ Could not schedule the background delete: {e!r}"))
        log(i18n.L(f"    手动删掉这个目录即可：{path}",
                   f"    You can just delete this folder by hand: {path}"))
        return False


def do_venv(dry):
    """删掉依赖目录 .venv。

    ⚠ 这里有"删自己脚下的地板"的可能：正常情况下清理脚本是用**系统 Python** 跑的
    （`app.host_pythonw()` 排除了 .venv / runtime），但如果这台机器上**一个外部解释器
    都没有**，脚本就只能跑在 .venv 里那个 pythonw.exe 上 —— 而 Windows 锁着正在运行的
    镜像，谁都删不掉自己。所以：发现脚本自身的解释器就在待删目录里，就改成退出后再删。
    （这个兜底机制原本是为 runtime 写的；runtime 已不再列入清理项，机制留着给 .venv 用。）
    """
    path = os.path.join(HERE, ".venv")
    if not os.path.exists(path):
        log(i18n.L("  - .venv 不存在", "  - .venv does not exist"))
        return False
    if _dir_holds_my_interpreter(path):
        if dry:
            log(i18n.L(f"  （预览）会删除 .venv（{_dir_size_mb(path):.0f}MB）；"
                       f"当前脚本就跑在它上面，会安排到本进程退出后再删",
                       f"  (preview) would delete .venv ({_dir_size_mb(path):.0f}MB); "
                       f"this script is running from it, so the delete would be scheduled "
                       f"for after this process exits"))
            return True
        if _delete_after_exit(path):
            log(i18n.L(f"  ✓ 已安排删除 .venv（{_dir_size_mb(path):.0f}MB）—— "
                       f"本进程退出后由后台命令自动删完，约 2 秒",
                       f"  ✓ Scheduled .venv for deletion ({_dir_size_mb(path):.0f}MB) — "
                       f"a background command finishes it after this process exits, ~2s"))
        return False          # 现在还在，刻意返回 False：别让核实环节误报"没删掉"
    return _file_step(".venv", dry)


def do_pycache(dry):
    _file_step("__pycache__", dry)


def do_pythontxt(dry):
    _file_step("python.txt", dry)


def do_logs(dry):
    for n in ("elevate_setup.log", "client.log"):
        if os.path.exists(os.path.join(HERE, n)):
            _file_step(n, dry)
    # 自己的日志留到写完再删（这里不删 uninstall.log）


def do_config(dry):
    _file_step("config.json", dry)
    # ★ 顺手清掉写 config.json 时可能留下的临时文件。它是**用户配置的一份完整拷贝**
    #   （蓝牙地址 + 整套键位），"清理干净"却把它留在程序目录里说不过去。
    #   它是配置的一部分，所以跟着 config 这一项走，而不是 logs。
    for n in os.listdir(HERE) if os.path.isdir(HERE) else []:
        if n.startswith("config.json.") and n.endswith(".tmp"):
            _file_step(n, dry)


def do_qwen(dry):
    if dry:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                winreg.QueryValueEx(k, QWEN_VAR)
            log(i18n.L(f"  （预览）会删除环境变量 {QWEN_VAR}：存在，会删掉"
                       f"（并记下「以后别再自动打开」）",
                       f"  (preview) would delete the environment variable {QWEN_VAR}: "
                       f"it exists and would be removed (and “do not auto-enable again” "
                       f"would be recorded)"))
        except OSError:
            log(i18n.L("  - 千问开关本来就没设",
                       "  - The Qianwen switch was not set anyway"))
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, QWEN_VAR)
        log(i18n.L(f"  ✓ 已删除环境变量 {QWEN_VAR}",
                   f"  ✓ Deleted the environment variable {QWEN_VAR}"))
        # 留个标记：程序默认会在启动时**自动**打开这个开关（不然用户按了语音键没反应
        # 还不知道为什么）。用户既然明确勾了删除，就得尊重这个选择 ——
        # 否则下次启动它又冒出来，在用户眼里就是个"删不掉的 bug"。
        try:
            open(QWEN_OPTOUT, "w", encoding="utf-8").write(
                i18n.L("用户明确删除了千问「允许注入」开关，程序不再自动打开。\n"
                       "想恢复：界面语音页点「打开千问『允许注入』」，或删掉本文件。\n",
                       "The user explicitly deleted Qianwen's “allow injection” switch; "
                       "the program will not auto-enable it again.\n"
                       "To restore: click “Enable Qianwen allow-injection” on the Voice "
                       "page, or delete this file.\n"))
            log(i18n.L("  ✓ 已记住这次选择（以后不再自动打开；界面按钮可以随时恢复）",
                       "  ✓ Your choice is remembered (it will not auto-enable again; "
                       "the UI button can turn it back on at any time)"))
        except OSError as e:
            log(i18n.L(f"  ⚠ 没能写下「不再自动打开」的标记：{e}",
                       f"  ⚠ Could not write the “do not auto-enable” marker: {e}"))
    except FileNotFoundError:
        log(i18n.L("  - 千问开关本来就没设",
                   "  - The Qianwen switch was not set anyway"))
    except OSError as e:
        log(i18n.L(f"  ✗ 删环境变量失败：{e}",
                   f"  ✗ Could not delete the environment variable: {e}"))


# ---------------------------------------------------------------- 虚拟声卡（VB-CABLE）
# 语音的物理前提，但**它不是本程序装的**（用户点界面按钮装的官方驱动），
# 而且是**系统级**的：OBS / 变声器 / 直播工具可能也在用它。
#
# ⚠ 探测**不依赖 sounddevice**：清理脚本经常跑在一个没装依赖的解释器上（系统 Python），
#   那时 `import sounddevice` 直接失败，用"找不到设备"判断就会得出"没装"的**错结论** ——
#   用户会对着一台明明装了驱动的机器看到"不存在"。所以只读注册表，跟音频依赖无关。
def _cable_uninstall_exe():
    """官方注册的卸载程序路径（就是 Windows「应用和功能」里那条 UninstallString）。

    官方 Pack45 在注册表里留下的那条：
      HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\VB:VBCABLE {87459874-1236-4469}
        DisplayName     = "VBCABLE, The Virtual Audio Cable"
        Publisher       = VB-Audio Software
        UninstallString = C:\\Program Files\\VB\\CABLE\\VBCABLE_Setup_x64.exe   ← **不带参数**
        （**没有** QuietUninstallString → 官方确实不提供静默卸载，只能点 GUI 按钮）
    """
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, CABLE_UNINST_ROOT) as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                if "vbcable" not in sub.lower().replace(" ", ""):
                    continue
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                        CABLE_UNINST_ROOT + "\\" + sub) as sk:
                        raw = str(winreg.QueryValueEx(sk, "UninstallString")[0]).strip()
                        # ★ 顺便核对"这一条确实是 VB-Audio 的"。光看子键名里有没有
                        #   "vbcable" 就提权执行它的 UninstallString 太宽了 —— 任何
                        #   往 HKLM\...\Uninstall 写过这种子键的软件都会被我们提权跑。
                        #   本机那条的 Publisher 是 "VB-Audio Software"、DisplayName 是
                        #   "VBCABLE, The Virtual Audio Cable"，以前这两个字段根本没读。
                        try:
                            pub = str(winreg.QueryValueEx(sk, "Publisher")[0])
                        except OSError:
                            pub = ""
                        try:
                            disp = str(winreg.QueryValueEx(sk, "DisplayName")[0])
                        except OSError:
                            disp = ""
                        blob = (pub + " " + disp).lower()
                        if not (("vb-audio" in blob) or ("vb-audio" in blob.replace(" ", ""))
                                or ("vbcable" in blob)):
                            continue
                except OSError:
                    continue
                # 可能是 "C:\...\setup.exe" /u 这种带参数的写法，把参数切掉。
                # 也展开 %ProgramFiles% 这类变量（官方那条是绝对路径，但别人写的
                # 可能是变量形式；不展开就会 os.path.exists 失败、静默退回包内那份）。
                raw = os.path.expandvars(raw)
                if raw.startswith('"'):
                    exe = raw[1:].split('"', 1)[0]
                else:
                    for sep in (" /", " -"):
                        if sep in raw:
                            raw = raw.split(sep, 1)[0]
                    exe = raw.strip()
                if exe and os.path.exists(exe):
                    return exe
    except OSError:
        pass
    return None


def cable_installed_probe():
    """装了没有 —— 只看注册表，不 import sounddevice。"""
    if _cable_uninstall_exe():
        return True
    for svc in CABLE_SVC_NAMES:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Services" + "\\" + svc):
                return True
        except OSError:
            continue
    return False


# 官方 Pack45 里那个安装器的 SHA256 —— 提权运行**包内那份**之前必须核对。
# ⚠ 这个常量在 app.py 里也有一份，**是刻意重复的**：清理脚本是独立进程，
#   不能 import app.py（那会把 client.log 又打开一遍，而清理流程可能刚删掉它）。
#   这个常量在 app.py 里也有一份，**是刻意重复的**：清理脚本是独立进程，
#   不能 import app.py（那会把 client.log 又打开一遍，而清理流程可能刚删掉它）。
#   ⚠ 两份必须保持同步，缺一个都会让"退回包内那份安装器"的分支直接 NameError。
CABLE_SETUP_SHA256 = "734C35DFA6D98F48782A451633CEB471166EC70D60482FD89A1123D0EE3C4F41"


def _sha256_file(path):
    """算文件的 SHA256（大写十六进制）。读不出来返回空串。

    ⚠ 这个函数**必须在这里也有一份**。app.py 里有一个同名的，但那是**另一个进程** ——
    清理脚本是独立跑的，不能去 import app.py（那会连带把 client.log 打开，
    而清理流程可能刚刚删掉它）。
    """
    try:
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest().upper()
    except OSError:
        return ""


def do_cable(dry):
    """卸载虚拟声卡（VB-CABLE）。

    ⚠ 跟别的项**不是一类**，日志里必须讲清楚，否则用户会以为"点一下就好了"：
      1. 它是**系统级音频驱动**，OBS / 变声器 / 直播工具可能也在用；
      2. 官方**没有静默卸载** —— Windows 那条 UninstallString 就是安装器本身、不带参数，
         人必须在它窗口里点一下「Remove Driver」；
      3. 官方 readme：卸载**必须重启**系统才算卸完。
    所以本函数只做两件事：把官方卸载器提权拉起来 + 把上面三条讲明白。

    ★ 它**不进 VERIFY**：用户还没点完 GUI，核实环节就会喊"没删掉" —— 那是误报。
    """
    if not cable_installed_probe():
        log(i18n.L("  - 没装 VB-CABLE，不用卸",
                   "  - VB-CABLE is not installed; nothing to uninstall"))
        return
    if dry:
        log(i18n.L("  （预览）会启动 VB-CABLE 卸载器（需要管理员；要在它窗口里点"
                   "「Remove Driver」，卸完必须重启）",
                   "  (preview) would launch the VB-CABLE uninstaller (needs admin; click "
                   "“Remove Driver” in its window; a reboot is required afterwards)"))
        return
    exe = _cable_uninstall_exe() or os.path.join(CABLE_DIR, CABLE_SETUP_NAME)
    if not os.path.exists(exe):
        log(i18n.L(f"  ✗ 找不到卸载器（官方注册的路径和包内 {CABLE_SETUP_NAME} 都不在）。"
                   f"可以从 Windows「应用和功能」里卸载 VBCABLE，或从官网下一份放回 vbcable\\",
                   f"  ✗ No uninstaller found (neither the officially registered path nor the "
                   f"bundled {CABLE_SETUP_NAME}). Uninstall VBCABLE from Windows “Apps & "
                   f"features”, or download the package from the official site into vbcable\\"))
        return
    log(i18n.L(f"  用：{exe}", f"  Using: {exe}"))

    # ★ 提权之前先确认"这个 exe 值不值得提权"。两类来源，判定不同：
    #   · 包内的 vbcable\ —— **普通用户就能写**。低权限的恶意程序把它换成自己的 exe，
    #     再等用户点这里，我们就会把它提升到管理员。所以必须核对 SHA256。
    #   · HKLM 注册表里那条 UninstallString 指的路径（通常是 C:\Program Files\VB\CABLE\）
    #     —— 那个键**只有管理员能写**，而且 Program Files 也不许普通用户改，
    #     来源本身就是可信的；而且不核哈希才能容忍官方以后升级版本。
    inside_pkg = os.path.normcase(os.path.abspath(exe)).startswith(
        os.path.normcase(os.path.abspath(HERE)) + os.sep)
    if inside_pkg:
        digest = _sha256_file(exe)
        if digest != CABLE_SETUP_SHA256:
            log(i18n.L(f"  ✗ 拒绝提权运行包内的 {CABLE_SETUP_NAME}：SHA256 与官方文件不符"
                       f"（{digest[:16] or '读取失败'}…）。它可能被换过 —— "
                       f"从官网重新解压一份到 vbcable\\，或用 Windows「应用和功能」卸载 VBCABLE",
                       f"  ✗ Refusing to run the bundled {CABLE_SETUP_NAME} elevated: its "
                       f"SHA256 does not match the official file "
                       f"({digest[:16] or 'unreadable'}…). It may have been swapped — unzip a "
                       f"fresh copy into vbcable\\, or uninstall VBCABLE from Windows "
                       f"“Apps & features”"))
            return
        log(i18n.L(f"  ✓ 包内卸载器 SHA256 核对通过（{digest[:16]}…）",
                   f"  ✓ Bundled uninstaller SHA256 verified ({digest[:16]}…)"))
    else:
        log(i18n.L("  （这个路径来自 HKLM 官方卸载项，只有管理员能写，直接用它）",
                   "  (This path comes from the official HKLM uninstall entry, which only an "
                   "administrator can write — using it as-is)"))
    log(i18n.L("  ⚠ 虚拟声卡是**系统级驱动**：OBS / 变声器 / 直播工具可能也在用它，"
               "删了它们就没声了。",
               "  ⚠ The virtual audio cable is a **system-wide driver**: OBS, voice changers "
               "and streaming tools may be using it too — removing it silences them."))
    log(i18n.L("  用管理员权限启动官方卸载器（会弹一次 UAC，点「是」）…",
               "  Launching the official uninstaller elevated (one UAC prompt — click Yes)…"))
    try:
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, "", os.path.dirname(exe), 1)
    except Exception as e:
        log(i18n.L(f"  ✗ 启动卸载器失败：{e!r}",
                   f"  ✗ Could not start the uninstaller: {e!r}"))
        return
    if rc <= 32:
        log(i18n.L(f"  ✗ 提权被拒绝或失败（ShellExecute 返回 {rc}）",
                   f"  ✗ Elevation denied or failed (ShellExecute returned {rc})"))
        return
    log(i18n.L("  ✓ 卸载器已启动：在它窗口里点「Remove Driver」，等它说驱动已移除。",
               "  ✓ The uninstaller has started: click “Remove Driver” in its window and wait "
               "for it to report the driver has been removed."))
    log(i18n.L("  ⚠ 卸完**必须重启一次**系统才算卸完（VB-Audio 官方 readme 的要求）。",
               "  ⚠ You **must reboot** afterwards to finalize it (VB-Audio's own readme)."))
    # ★ 等用户把那个窗口关掉再往下走。为什么要等：
    #   cable 排在最前面（见 ORDER 的注释），后面紧跟的就是"结束客户端进程" ——
    #   不等的话，用户还在向导弹窗里点，这边的控制台就已经断线了；而且"卸载"这件事
    #   本来就没完成，清理流程却已经宣称走完了。等它关掉，"卸载完成"和"继续收尾"
    #   在时间上才对得上。
    log(i18n.L("  ⏳ 等你把卸载器那个窗口关掉再继续（最多等 15 分钟）——"
               "在这之前我不会去结束客户端，免得你看着看着控制台就没了",
               "  ⏳ Waiting for you to close the uninstaller window before continuing "
               "(up to 15 minutes) — the client will not be stopped until then, so the "
               "console does not vanish under your hands"))
    if _wait_for_gui(CABLE_SETUP_NAME, 15 * 60):
        log(i18n.L("  ✓ 卸载器窗口已关闭，继续后面的清理",
                   "  ✓ The uninstaller window is closed; continuing with the rest"))
    else:
        log(i18n.L("  （没等到卸载器窗口，或者超过 15 分钟没关 —— 不再等了，继续后面的清理。"
                   "驱动如果没卸干净，重新勾这一项再来一次即可）",
                   "  (Did not see the uninstaller window, or it stayed open for over 15 "
                   "minutes — not waiting any longer; continuing. If the driver was not "
                   "fully removed, tick this item again)"))


CABLE_PROGRAM_DIR = r"C:\Program Files\VB"
CABLE_FILES_DIR = os.path.join(CABLE_PROGRAM_DIR, "CABLE")
# C:\Program Files\VB\CABLE 里**官方就这三个文件**。只有"一个不多一个不少"时才删这个目录 ——
# 多出任何东西就说明那不是我们认识的 VB-CABLE 安装，宁可不动。
CABLE_KNOWN_FILES = {"VBCABLE_Setup_x64.exe", "VBCABLE_ControlPanel.exe",
                     "vbMmeCable64_win10.inf"}

# ============================================================================
# ⚠⚠ 允许删的注册表路径：**写死的白名单**。
#   绝不从命令行/界面/配置里拼一个键路径进来删 —— "清注册表"最怕的就是多删一位。
#   路径只从这里来，删之前再 assert 一次在名单里。
#
#   特别注意 `HKLM\SOFTWARE\VB-Audio` 是**厂商级**键：Voicemeeter 也挂在它下面
#   （Banana / Potato 会写 VB-Audio\Voicemeeter 之类）。所以这里删的是
#   **`VB-Audio\Cable` 这一支**（里面只有 VBAudioCableWDM_* 这些 VB-CABLE 自己的设置），
#   且只有"删完之后厂商键空了"才顺手删那个空壳；只要还有别的子键，**一律不碰**。
# ============================================================================
CABLE_SVC_KEY_FMT = r"HKLM\SYSTEM\CurrentControlSet\Services" + "\\" + "%s"
CABLE_VENDOR_KEY = r"HKLM\SOFTWARE\VB-Audio"
CABLE_VENDOR_SUBKEY = r"Cable"
CABLE_UNINST_ROOT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
CABLE_REG_KEYS = [CABLE_SVC_KEY_FMT % s for s in CABLE_SVC_NAMES] + \
                 [CABLE_VENDOR_KEY + "\\" + CABLE_VENDOR_SUBKEY]


def _reg_enum(root, path, kind="subkey"):
    """枚举 HKLM 下某个键的子键名 / 值名（打不开就返回空列表）。"""
    out = []
    try:
        with winreg.OpenKey(root, path) as k:
            n = winreg.QueryInfoKey(k)[0 if kind == "subkey" else 1]
            for i in range(n):
                out.append(winreg.EnumKey(k, i) if kind == "subkey"
                           else winreg.EnumValue(k, i)[0])
    except OSError:
        pass
    return out


def _sc_state(name):
    """向 SCM 问这个服务的状态，形如 "4  RUNNING" / "3  STOP_PENDING"；问不到返回 ""。

    用来发现那种"注册表键已经删了、SCM 里还挂着"的**待删除**影子 —— 它就是
    安装时报 1072 的根因（见 do_cable_residue 里那段注释）。
    """
    rc, out = run(["sc", "query", name], timeout=60)
    m = re.search(r"STATE\s*:\s*(\d+)\s+(\S+)", out or "")
    return ("%s %s" % (m.group(1), m.group(2))) if m else ""


def _cable_uninstall_entries():
    """`HKLM\\...\\Uninstall` 下属于 VB-CABLE 的那几条。

    ⚠ 不能按子键名里有 "vbcable" 就删：任何软件都可能往这个根下写这种名字。
      这里跟 `_cable_uninstall_exe()` 用**同一套确认**（Publisher / DisplayName 里
      必须有 vb-audio / vbcable），确认不过的一律不删。返回完整 HKLM 路径。
    （漏了这一处是个真缺口：清完残留、设备也删了，可 app 的 `cable_registry_present()`
      仍然看到这条 → 界面永远停在"虚拟声卡现在用不了"那个横幅上，用户出不来。）
    """
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, CABLE_UNINST_ROOT) as root:
            n = winreg.QueryInfoKey(root)[0]
            for i in range(n):
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                if "vbcable" not in sub.lower().replace(" ", ""):
                    continue
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                        CABLE_UNINST_ROOT + "\\" + sub) as sk:
                        blob = ""
                        for v in ("Publisher", "DisplayName"):
                            try:
                                blob += " " + str(winreg.QueryValueEx(sk, v)[0])
                            except OSError:
                                pass
                except OSError:
                    continue
                # ★ 必须看到 **vb-audio**（厂商名），不能只看 "vbcable"：
                #   DisplayName 里带 vbcable 字样的第三方条目完全可能存在，
                #   只按它判断就会去删别人的卸载项。官方那条的 Publisher 是
                #   "VB-Audio Software"，所以这个判据既准又够。
                #   （名字过滤只是廉价的预筛，真正的判据在这一行。）
                low = blob.lower().replace(" ", "")
                if "vb-audio" in low:
                    out.append("HKLM\\" + CABLE_UNINST_ROOT + "\\" + sub)
    except OSError:
        pass
    return out


def _cable_svc_key_exists(name):
    """服务键在不在（单独抽出来：测试要能替换掉"真实注册表"这一层）。"""
    try:
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Services" + "\\" + name).Close()
        return True
    except OSError:
        return False


def _cable_svc_guard(name):
    """删服务键之前问一句"这真的是 VB-CABLE 的服务吗"。返回 (能不能删, 理由)。

      · 名字必须在 CABLE_SVC_NAMES 白名单里（调用方已保证，这里再兜一次）；
      · ImagePath / DisplayName / Description 里出现 **voicemeeter** → 绝不删（那是别人的声卡）；
      · ImagePath 有内容 → 必须指向 VB-CABLE 的驱动（vbaudio_cable64 / vbmmecable64）；
      · ImagePath 为空（半卸后 SCM 都不认的孤儿键）→ 允许删，日志里说明白。
    """
    if name not in CABLE_SVC_NAMES:
        return False, "不在白名单里"
    path = r"SYSTEM\CurrentControlSet\Services" + "\\" + name
    vals = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            for v in ("ImagePath", "DisplayName", "Description"):
                try:
                    vals[v] = str(winreg.QueryValueEx(k, v)[0])
                except OSError:
                    vals[v] = ""
    except OSError:
        return False, "键不存在"
    if "voicemeeter" in " ".join(vals.values()).lower():
        return False, "里面有 Voicemeeter 字样，是别人家的声卡，不删"
    ip = vals.get("ImagePath", "").strip()
    if not ip:
        return True, "孤儿服务键（ImagePath 为空、SCM 已经不认它）"
    low = ip.lower()
    if "vbaudio_cable64" in low or "vbmmecable64" in low:
        return True, "ImagePath 指向 VB-CABLE 驱动"
    return False, "ImagePath 不指向 VB-CABLE 驱动（%s…），不删" % ip[:40]


def _cable_driver_packages():
    r"""从 `pnputil /enum-drivers` 里挑出 VB-CABLE 的驱动包名。

    ⚠ 不能写死 `oem634.inf`：那只在"已发布"时存在，而我们要处理的恰恰是
      "只剩 staged、发布名已经没了"的半卸状态 —— 写死的话命令会报
      "没有匹配的驱动包"，而 DriverStore 那份**原地留着**，脚本看上去却跑完了。
      返回 Published Name 和 Original Name 两种写法，逐个试（pnputil 两种都收）。
    ⚠ 判据是 **vbmmecable / vbaudio_cable** 这些 VB-CABLE 专属字样，
      不能只看 "Provider 是 VB-Audio" —— Voicemeeter 的 Provider 也是 VB-Audio，
      按厂商匹配会把人家的驱动包删掉。
    ⚠ 名字**不能按英文标签解析**：pnputil 的输出是**本地化**的，中文系统上打的是
      「发布名称 / 原始名称」而不是 "Published Name / Original Name"。所以在块里抓
      `.inf` 结尾的 token —— 跟语言无关。
    ⚠⚠ 分块必须容忍 CRLF：块之间是 `\r\n\r\n`，用 `split("\n\n")` **切不开**
      （两个 \n 中间夹着 \r），于是整份清单被当成一个块，里面"恰好"含 vbmmecable
      就通过筛选 —— 结果把**全机器几百个驱动包**都收了进来。干跑预览一眼看出来
      （列了几百个 .inf），但真跑就是灾难。现在用 `re.split(r"\r?\n\s*\r?\n")`，
      并且**加了一条数量上限**：VB-CABLE 只有 1 个驱动包，匹配到超过 4 个
      一律视为解析出错、直接返回空（宁可不删，也不乱删）。
    """
    rc, out = run(["pnputil", "/enum-drivers"], timeout=90)
    names = []
    for blk in re.split(r"\r?\n\s*\r?\n", out):
        if "vbmmecable" not in blk.lower() and "vbaudio_cable" not in blk.lower():
            continue
        for m in re.finditer(r"[^\s\\/:]+\.inf", blk, re.I):
            names.append(m.group(0).strip())
    seen, uniq = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            uniq.append(n)
    if len(uniq) > 4:
        log(i18n.L(f"  ✗ 解析 pnputil 输出时匹配到 {len(uniq)} 个驱动包（正常只有 1 个），"
                   f"判定为解析出错 —— **一个都不删**，请把这个情况反馈给作者",
                   f"  ✗ Parsing pnputil matched {len(uniq)} driver packages (normally just 1); "
                   f"treating it as a parse error — **deleting nothing**. Please report this"))
        return []
    return uniq


def _cable_residue_state():
    """官方卸载器**永远不碰**的那几处，逐项探一遍（给日志和"清完没清完"用）。"""
    out = []
    for svc in CABLE_SVC_NAMES:
        if not _cable_svc_key_exists(svc):
            # ★ 注册表键没了、SCM 里还挂着 = "待删除"影子。它**不会**自己消失，
            #   而且会让下一次安装报 1072 —— 必须列进残留清单，不能当"干净"。
            st = _sc_state(svc)
            if st:
                out.append("SCM 待删除 %s（%s）" % (svc, st))
            continue
        ok, why = _cable_svc_guard(svc)
        out.append("service %s%s" % (svc, "" if ok else "（不删：%s）" % why))
    subs = _reg_enum(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VB-Audio", "subkey")
    if CABLE_VENDOR_SUBKEY in subs:
        out.append(r"HKLM\SOFTWARE\VB-Audio\Cable")
    others = [s for s in subs if s != CABLE_VENDOR_SUBKEY]
    if others:
        out.append(r"HKLM\SOFTWARE\VB-Audio 下还有别的（" + ",".join(others) + "）不动")
    out += ["driver " + n for n in _cable_driver_packages()]
    for k in _cable_uninstall_entries():
        out.append(k + "（应用和功能里那条）")
    if os.path.isdir(CABLE_FILES_DIR):
        try:
            files = sorted(os.listdir(CABLE_FILES_DIR))
        except OSError:
            files = []
        if files and set(files) <= CABLE_KNOWN_FILES:
            out.append(CABLE_FILES_DIR + "（官方那三个文件）")
        else:
            out.append(CABLE_FILES_DIR + "（内容不是官方那三个，不动）")
    return out


def do_cable_residue(dry):
    """清除虚拟声卡残留（`cable_residue`）。

    为什么需要它：官方卸载器只会删掉**设备**和「应用和功能」里那条，
    服务键、`HKLM\\SOFTWARE\\VB-Audio\\Cable`、DriverStore 驱动包、`oem*.inf` 全都留着。
    这些残留会把**下一次安装**挡住（现象：注册表里有、设备枚举不出来，
    在官方安装器里点 Install Driver 没反应）。清干净 → 重启 → 再装一次就好。

    ★★ 安全底线（用户专门叮嘱过"清注册表一定注意，别清错了"）★★
      · 能删的注册表路径是**写死的白名单** `CABLE_REG_KEYS`，删之前 assert 一次，
        路径**绝不**从命令行/界面/配置里来；
      · `HKLM\\SOFTWARE\\VB-Audio` 是**厂商级**键，Voicemeeter 也挂在下面 ——
        只删 `VB-Audio\\Cable` 这一支；只有"删完之后厂商键空了"才顺手删空壳，
        底下还有别的子键就**一律不碰**；
      · 服务键删之前用 `_cable_svc_guard()` 验一遍（有 Voicemeeter 字样、
        或 ImagePath 不指向 VB-CABLE 驱动 → 拒绝删）；
      · 驱动包按 **vbmmecable / vbaudio_cable** 字样匹配，不看"Provider 是 VB-Audio"
        （Voicemeeter 的 Provider 也是 VB-Audio）；
      · `C:\\Program Files\\VB\\CABLE` 只在"文件恰好是官方那三个"时才删，
        `VB\\Voicemeeter` 之类绝不碰。

    它**不进 VERIFY**（不是"勾了就该消失"的东西），也不需要人点 GUI。
    """
    before = _cable_residue_state()
    if not before:
        log(i18n.L("  - 没有 VB-CABLE 残留，不用清",
                   "  - No VB-CABLE leftovers found; nothing to clear"))
        return
    log(i18n.L("  发现残留：" + "、".join(before),
               "  Leftovers found: " + ", ".join(before)))
    if dry:
        log(i18n.L("  （预览）只会删这几处（白名单写死，别处一律不动）："
                   "服务键 VB-Cable / VBAudioVACMME、HKLM\\SOFTWARE\\VB-Audio\\Cable、"
                   "DriverStore 里的 VB-CABLE 驱动包、C:\\Program Files\\VB\\CABLE"
                   "（仅当里面恰好是官方那三个文件）。"
                   "**Voicemeeter 的键和目录一个都不会碰。** 清完要重启再重装",
                   "  (preview) would only delete these (hard-coded allowlist, nothing else): the "
                   "service keys VB-Cable / VBAudioVACMME, HKLM\\SOFTWARE\\VB-Audio\\Cable, the "
                   "VB-CABLE driver packages in the DriverStore, and C:\\Program Files\\VB\\CABLE "
                   "(only when it holds exactly the three official files). **Voicemeeter keys and "
                   "folders are never touched.** Reboot and reinstall afterwards"))
        return

    # ---------------------------------------------------------------- 1) 服务键
    # ★★ 顺序必须是 **先 stop、再 delete**。为什么（实测踩到，代价是用户录视频时卡住）：
    #   一个**正在运行的内核驱动**被 `sc delete` 之后，SCM 会把它标成"待删除"
    #   （ERROR_SERVICE_MARKED_FOR_DELETE = 1072），而且这个状态**在驱动卸载前一直不消**。
    #   用户随后去跑官方安装器，安装器要创建同名服务 → 直接弹
    #   「Unknown Error 1072 … Be sure to run this program in Administrator mode」——
    #   那句提示是**误导**（他本来就是管理员跑的），真因是我们留下的"待删除"服务。
    #   先 stop 能让驱动尽快卸载；卸不掉（STOP_PENDING 卡住）就只剩重启一条路，
    #   所以这里要**明确告诉用户"必须重启"**，而不是让他对着 1072 干瞪眼。
    session_restart_needed = False
    for svc in CABLE_SVC_NAMES:
        if not _cable_svc_key_exists(svc):
            # 注册表键没了，但 SCM 里可能还留着"待删除"的影子 —— 探测一下。
            # 这个影子**删不掉**（sc delete 会回 1072），只能靠重启清掉，
            # 所以这里只报状态并让用户知道"必须重启"，别让他反复点安装。
            st = _sc_state(svc)
            if st:
                log(i18n.L(f"  ⚠ SCM 里还留着 {svc}（状态 {st}，注册表键已删）—— "
                           f"这就是「待删除」状态，**必须重启一次**才能创建同名服务",
                           f"  ⚠ {svc} is still known to the SCM (state {st}, registry key "
                           f"already gone) — that is the “marked for deletion” state; "
                           f"**a reboot is required** before the same service can be created"))
                session_restart_needed = True
            continue
        ok, why = _cable_svc_guard(svc)
        if not ok:
            log(i18n.L(f"  · 跳过服务 {svc}：{why}", f"  · Skipping service {svc}: {why}"))
            continue
        run(["sc", "stop", svc], timeout=90)          # ★ 先停，别直接 delete
        time.sleep(1.0)
        st = _sc_state(svc)
        if st and "STOP" in st.upper() and "STOPPED" not in st.upper():
            log(i18n.L(f"  ⚠ {svc} 停不下来（{st}）—— 重启一次系统后它就自然没了",
                       f"  ⚠ {svc} will not stop ({st}) — a reboot will clear it"))
            session_restart_needed = True
        run(["sc", "delete", svc], timeout=60)
        if _cable_svc_key_exists(svc):     # SCM 不认的孤儿键 sc delete 删不掉，得直接删键
            hklm_path = CABLE_SVC_KEY_FMT % svc
            assert hklm_path in CABLE_REG_KEYS, hklm_path       # ★ 白名单守卫
            run(["reg", "delete", hklm_path, "/f"], timeout=60)
        log(i18n.L(f"  ✓ 服务键 {svc} 已删（{why}）",
                   f"  ✓ Service key {svc} removed ({why})"))

    # ---------------------------------------------------------------- 2) 厂商键（只删 Cable 那一支）
    subs = _reg_enum(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VB-Audio", "subkey")
    if CABLE_VENDOR_SUBKEY in subs:
        key = CABLE_VENDOR_KEY + "\\" + CABLE_VENDOR_SUBKEY
        assert key in CABLE_REG_KEYS, key                       # ★ 白名单守卫
        run(["reg", "delete", key, "/f"], timeout=60)
        log(i18n.L(r"  ✓ HKLM\SOFTWARE\VB-Audio\Cable 已删（只有 VB-CABLE 自己的设置）",
                   r"  ✓ HKLM\SOFTWARE\VB-Audio\Cable removed (only VB-CABLE's own settings)"))
    others = [s for s in subs if s != CABLE_VENDOR_SUBKEY]
    if others:
        log(i18n.L(r"  · HKLM\SOFTWARE\VB-Audio 下还有 " + "、".join(others)
                   + "（Voicemeeter 之类），**一个都没动**",
                   r"  · HKLM\SOFTWARE\VB-Audio still has " + ", ".join(others)
                   + " (Voicemeeter etc.) — **left untouched**"))
    elif _reg_enum(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VB-Audio", "subkey") == [] \
            and _reg_enum(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VB-Audio", "value") == []:
        run(["reg", "delete", CABLE_VENDOR_KEY, "/f"], timeout=60)
        log(i18n.L(r"  ✓ HKLM\SOFTWARE\VB-Audio 已经空了，顺手删掉这个空壳键",
                   r"  ✓ HKLM\SOFTWARE\VB-Audio is now empty; removed the empty shell key"))

    # ------------------------------------------------- 2.5) 「应用和功能」里那条卸载项
    # ⚠ 必须删：app 的 cable_registry_present() 会看到它 → 界面一直停在
    #   "虚拟声卡现在用不了"那个横幅上，用户怎么清都出不来。
    #   只删**验明正身过**的那几条（名字含 vbcable + Publisher/DisplayName 是 VB-Audio）。
    for key in _cable_uninstall_entries():
        assert key.startswith("HKLM\\" + CABLE_UNINST_ROOT + "\\"), key   # ★ 只许在这个根下
        run(["reg", "delete", key, "/f"], timeout=60)
        log(i18n.L("  ✓ 删掉「应用和功能」里那条：" + key.rsplit("\\", 1)[-1],
                   "  ✓ Removed the “Apps & features” entry: " + key.rsplit("\\", 1)[-1]))

    # ---------------------------------------------------------------- 3) 驱动包    # 一个包会有两个名字（发布的 oemNN.inf + 原始的 vbmmecable64_win10.inf），
    # pnputil 只认**发布名**。所以先试发布名，成功就停；两个都失败才报错 ——
    # 不然日志里会出现一行没意义的"✗ 指定的文件不是安装的 OEM INF"。
    names = _cable_driver_packages()
    names.sort(key=lambda n: (not n.lower().startswith("oem"), n.lower()))
    for name in names:
        rc, out = run(["pnputil", "/delete-driver", name, "/uninstall", "/force"], timeout=180)
        tail = (out or "").strip().splitlines()
        if rc == 0:
            log(i18n.L(f"  ✓ 驱动包已删（{name}）",
                       f"  ✓ Driver package removed ({name})"))
            break
        log(i18n.L(f"  · 驱动包 {name} 这个写法删不掉（{tail[-1][:60] if tail else '?'}），换下一个名字",
                   f"  · Driver package {name} could not be removed this way "
                   f"({tail[-1][:60] if tail else '?'}); trying the other name"))
    else:
        if names:
            log(i18n.L("  ✗ 驱动包两种写法都没删掉（多半是正被占用或权限不足）",
                       "  ✗ Neither name worked (usually in use, or not enough rights)"))

    # ---------------------------------------------------------------- 4) 程序目录（只认官方那三个文件）
    if os.path.isdir(CABLE_FILES_DIR):
        try:
            files = sorted(os.listdir(CABLE_FILES_DIR))
        except OSError:
            files = []
        if files and set(files) <= CABLE_KNOWN_FILES:
            for fn in files:
                try:
                    os.remove(os.path.join(CABLE_FILES_DIR, fn))
                except OSError as e:
                    log(i18n.L(f"  ✗ 删不掉 {fn}：{e}", f"  ✗ Could not delete {fn}: {e}"))
            try:
                os.rmdir(CABLE_FILES_DIR)
                log(i18n.L(r"  ✓ 删掉 C:\Program Files\VB\CABLE（里面恰好是官方那三个文件）",
                           r"  ✓ Removed C:\Program Files\VB\CABLE (exactly the three official files)"))
            except OSError:
                pass
        elif files:
            log(i18n.L(r"  · C:\Program Files\VB\CABLE 里还有别的东西（"
                       + "、".join(files[:5]) + "），**没动它**",
                       r"  · C:\Program Files\VB\CABLE holds other files ("
                       + ", ".join(files[:5]) + "); **left alone**"))
    if os.path.isdir(CABLE_PROGRAM_DIR):
        try:
            left = os.listdir(CABLE_PROGRAM_DIR)
        except OSError:
            left = []
        if not left:
            try:
                os.rmdir(CABLE_PROGRAM_DIR)
                log(i18n.L(r"  ✓ C:\Program Files\VB 空了，删掉",
                           r"  ✓ C:\Program Files\VB is empty; removed"))
            except OSError:
                pass
        else:
            log(i18n.L(r"  · C:\Program Files\VB 下还有 " + "、".join(left)
                       + "（Voicemeeter 之类），**没动**",
                       r"  · C:\Program Files\VB still has " + ", ".join(left)
                       + " (Voicemeeter etc.); **left alone**"))

    after = _cable_residue_state()
    if after:
        log(i18n.L("  ⚠ 还剩：" + "、".join(after) + "（多半是权限不足或驱动包正被占用）",
                   "  ⚠ Still present: " + ", ".join(after)
                   + " (usually a permissions or in-use problem)"))
    else:
        log(i18n.L("  ✓ 残留已清干净。**现在重启一次系统**，然后点「一键安装虚拟声卡」重新装",
                   "  ✓ Leftovers cleared. **Reboot now**, then click “install the virtual audio "
                   "cable” again"))
    if session_restart_needed:
        log(i18n.L("  ⚠⚠ 这次**必须重启**：还有个服务停在「待删除 / 停不下来」的状态，"
                   "不重启的话官方安装器会报 `Unknown Error 1072`（它提示「请用管理员运行」是误导，"
                   "你本来就是管理员）。重启后再装即可。",
                   "  ⚠⚠ **A reboot is required this time**: a service is stuck in the "
                   "“marked for deletion / will not stop” state. Without a reboot the official "
                   "installer fails with `Unknown Error 1072` (its “run as administrator” hint is "
                   "misleading — you already are). Reboot, then install again."))
    log(i18n.L("  （重启这一步省不掉：Windows 要重启后才会重新枚举音频设备）",
               "  (The reboot cannot be skipped: Windows only re-enumerates audio devices "
               "after a restart)"))


def _wait_for_gui(image_name, seconds=900):
    """等一个 GUI 进程消失（用户把窗口关了）。返回 True = 等到了。

    用 `_proc_seen()`（tasklist /FI，不带管道、不碰 GNU find —— 那个坑见文件头）。
    先确认它真的起来了再等：用户拒了 UAC 的话进程根本不存在，那就别在这儿空等 15 分钟。
    """
    try:
        time.sleep(1.5)                       # 给它一点时间起来
        if not _proc_seen(image_name):
            return False
        deadline = time.time() + seconds
        while time.time() < deadline:
            time.sleep(2.0)
            if not _proc_seen(image_name):
                return True
    except Exception:
        return False
    return False


ACTIONS = {
    "task": do_task,
    "run": do_run,
    "kill": do_kill,
    "frida_helper": do_frida_helper,
    "frida_svc": do_frida_svc,
    "venv": do_venv,

    "pycache": do_pycache,
    "pythontxt": do_pythontxt,
    "logs": do_logs,
    "config": do_config,
    "qwen": do_qwen,
    "cable": do_cable,
    "cable_residue": do_cable_residue,
}
# 执行顺序（固定）。★ cable **必须排在最前面**，别改回最后：
#   · `kill` / `venv` 会把客户端结束掉 → 控制台当场断线；
#   · cable 是唯一一个"要人在窗口里点一下、再重启"的项，说明必须让用户看得见；
#   · 所以顺序是：先把交互式的声卡卸载做完（并且**等它窗口关掉**才继续），
#     再去断自启、杀进程、删文件。
#   （改之前是反的：kill 在第 3 步，cable 在第 12 步 —— 用户勾「推荐清理」+ 声卡时，
#     控制台早没了，等向导弹出来他根本看不到"要点 Remove Driver、卸完要重启"那几行。）
ORDER = ["cable", "cable_residue", "task", "run", "frida_helper", "frida_svc",
         "kill", "venv", "pycache", "pythontxt", "logs", "config", "qwen"]

# 这几个勾了就会把客户端也结束掉（界面要据此告诉用户"页面马上失联"）：
#   · kill    —— 就是要结束它
#   · venv    —— 不先结束进程，DLL 被锁着删不掉
KILLS_CLIENT = {"kill", "venv"}

RESULT = os.path.join(HERE, "_uninstall_result.json")


def _notify_done(items, stuck=()):
    """把结果写成一个文件，给主程序读。

    为什么用文件：本脚本是独立进程，而且**最后一步就是把主程序杀掉** ——
    没法再打电话回去告诉它"我干完了"。主程序那边有线程在跟 uninstall.log，
    看到 DONE 就会读这个文件，然后把结果推成 SSE 事件告诉界面。
    """
    try:
        import json
        # kills_client：这次清理会不会把界面（客户端）也结束掉 —— 主程序要靠这个
        # 决定跟用户说"页面马上失去连接"还是"客户端还在，可以继续用"。
        # ⚠ 必须跟 main() 最后那步真正执行 kill 的条件**完全一致**，否则界面会撒谎。
        kills = bool(KILLS_CLIENT & set(items))
        with open(RESULT, "w", encoding="utf-8") as f:
            json.dump({"count": len(items),
                       "items": [i18n.pick(LABELS.get(i, i)) for i in items],
                       "failed": [i18n.pick(LABELS.get(i, i)) for i in stuck],
                       "kills_client": kills,
                       "time": time.strftime("%H:%M:%S")}, f, ensure_ascii=False)
    except Exception:
        pass


# 清理完要**回头核实**的项：勾了、但实际还在 = 没删掉。
# 为什么要核实而不是信执行结果：.venv 会出现"报成功、其实还剩一个
# Scripts\pythonw.exe 的壳"，日志最后还写「无（干净）」—— 用户下次双击启动器，
# 启动器选中这个壳，界面根本打不开，人也完全不知道发生了什么。
VERIFY = [
    ("task",      "计划任务 VibeMote",  lambda: os.path.exists(os.path.join(
        os.environ.get("WINDIR", r"C:\Windows"), "System32", "Tasks", TASK_NAME))),
    ("run",       "启动项 VibeMote",    lambda: _run_value_exists()),
    ("venv",      ".venv",                lambda: os.path.exists(os.path.join(HERE, ".venv"))),
    ("pycache",   "__pycache__",          lambda: os.path.exists(os.path.join(HERE, "__pycache__"))),
    ("pythontxt", "python.txt",           lambda: os.path.exists(os.path.join(HERE, "python.txt"))),
    ("logs",      "elevate_setup.log",    lambda: os.path.exists(os.path.join(HERE, "elevate_setup.log"))),
    ("config",    "config.json",          lambda: os.path.exists(os.path.join(HERE, "config.json"))),
    ("frida_svc", "frida-* 残留服务",     lambda: bool(_frida_services())),
]

# 不勾就会留着的东西（正常，只是告诉用户"这些还在"）
KEEPABLE = [("venv", ".venv"),
            ("config", "config.json"), ("pythontxt", "python.txt")]


def _run_value_exists():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, TASK_NAME)
        return True
    except OSError:
        return False


def _frida_services():
    names = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services") as k:
            i = 0
            while True:
                try:
                    n = winreg.EnumKey(k, i)
                except OSError:
                    break
                if n.lower().startswith("frida-"):
                    names.append(n)
                i += 1
    except OSError:
        pass
    return names


def check_stuck(items):
    """返回 [item_id]：勾了却依然存在的项。"""
    stuck = []
    for iid, _name, probe in VERIFY:
        if iid not in items:
            continue
        try:
            if probe():
                stuck.append(iid)
        except Exception:
            pass
    return stuck


def presence():
    """每一项现在"在不在、多大" —— 界面要拿它提示用户。

    为什么需要：不提示的话，用户对着一堆本来就不存在的项瞎勾
    （比如轻包上「删除依赖（.venv）」永远是"不存在"）。
    """
    out = {}
    for iid, _name, probe in VERIFY:
        try:
            out[iid] = {"exists": bool(probe())}
        except Exception:
            out[iid] = {"exists": None}
    p = os.path.join(HERE, ".venv")
    if os.path.exists(p):
        out["venv"]["size_mb"] = round(_dir_size_mb(p), 1)
    # VERIFY 里没有的项，单独探一下
    try:
        out["kill"] = {"exists": bool(port_owners(UI_PORT))}
    except Exception:
        out["kill"] = {"exists": None}
    try:
        out["frida_helper"] = {"exists": any(
            _proc_seen(e) for e in ("frida-helper-x86.exe", "frida-helper-x86_64.exe"))}
    except Exception:
        out["frida_helper"] = {"exists": None}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            winreg.QueryValueEx(k, QWEN_VAR)
        out["qwen"] = {"exists": True}
    except OSError:
        out["qwen"] = {"exists": False}
    # 虚拟声卡：**故意不在 VERIFY 里**（它不是"勾了就该消失"的东西 —— 卸载要人点 GUI + 重启，
    # 放进去会让核实环节误报"没删掉"），但界面一定要显示它在不在。
    try:
        out["cable"] = {"exists": bool(cable_installed_probe())}
    except Exception:
        out["cable"] = {"exists": None}
    # 残留项：界面要能显示"现在到底有没有残留可清"，没有就不该让用户瞎点
    try:
        out["cable_residue"] = {"exists": bool(_cable_residue_state())}
    except Exception:
        out["cable_residue"] = {"exists": None}
    return out


def _proc_seen(name):
    rc, out = run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"])
    return name.lower() in out.lower()


def parse_items(argv):
    for a in argv:
        if a.startswith("--items="):
            raw = a.split("=", 1)[1].strip()
            if raw == "all":
                # ★ `all` **不含 cable**。cable 是系统级驱动、要靠人在 GUI 里点
                #   「Remove Driver」并重启，把它算进"全删"会让 `--items=all` 这种
                #   本来可以无人值守跑完的命令突然变成"弹 UAC + 开向导 + 要重启"。
                #   想连它一起卸就显式写：--items=all,cable
                #   （界面上的「全选」是显式列 id，不受这里影响，仍然会带上它。）
                # ★ `all` 也**不含 cable_residue**：那是"装/卸卡住了"时的修复动作，
                #   不是日常清理。想清就显式写：--items=all,cable_residue
                return [i for i in ITEM_IDS if i not in ("cable", "cable_residue")]
            picked = [x.strip() for x in raw.split(",") if x.strip()]
            return [x for x in picked if x in ITEM_IDS]
    return []


def main():
    # 清理脚本是**独立进程**，语言得自己读一次（清单名字按当前语言出）。
    try:
        i18n.load_from_config()
    except Exception:
        pass
    dry = "--dry-run" in sys.argv
    items = parse_items(sys.argv)
    try:
        if not dry:
            open(LOG, "w", encoding="utf-8").close()
            if os.path.exists(RESULT):
                os.remove(RESULT)          # 清掉上一次的结果，免得主程序读到旧的
    except Exception:
        pass

    log("=" * 58)
    log(i18n.L("  遥控器客户端 · 清理", "  VibeMote · Cleanup")
        + (i18n.L("  【预览，不会真删】", "  [PREVIEW — nothing will actually be deleted]")
           if dry else ""))
    log("=" * 58)
    log(i18n.L(f"包目录：{HERE}", f"Program folder: {HERE}"))
    log(i18n.L(f"管理员权限：{'✓' if is_admin() else '✗（计划任务/frida 服务可能删不掉）'}",
               f"Administrator: {'✓' if is_admin()
                                else '✗ (the scheduled task / frida services may resist)'}"))

    if not items:
        log(i18n.L("这次什么都没勾选 —— 没有做任何改动。",
                   "Nothing was ticked — no changes were made."))
        log(i18n.L("（要删哪项，在界面上勾上再点；命令行用 --items=venv,pycache）",
                   "(Tick what you want in the UI and click again; on the command line use "
                   "--items=venv,pycache)"))
        log(i18n.L("可选项：", "Available items: ")
            + "、".join(f"{i}({i18n.pick(LABELS[i])})" for i in ITEM_IDS))
        log("DONE")
        return 0

    log(i18n.L("本次将处理：", "This run will handle:"))
    for i in ORDER:
        if i in items:
            log(f"  · {i18n.pick(LABELS[i])}"
                + (i18n.L("  [需要管理员]", "  [needs admin]") if i in ADMIN_IDS else ""))

    n = 0
    for i in ORDER:
        if i not in items:
            continue
        n += 1
        log(f"{n}) {i18n.pick(LABELS[i])}")
        try:
            ACTIONS[i](dry)
        except Exception as e:
            log(i18n.L(f"  ✗ 出错：{type(e).__name__}: {e}",
                       f"  ✗ Error: {type(e).__name__}: {e}"))

    if dry:
        # 预览到这里就结束：不核实、不写结果文件、更不结束自己。
        # （核实逻辑看到东西还在会喊"没删掉"，预览下这是废话，反而像失败。）
        log(i18n.L("（预览结束：没有删任何文件、没有结束任何进程、没有改注册表）",
                   "(End of preview: no file was deleted, no process was ended, "
                   "no registry key was changed)"))
        log("DONE")
        return 0

    # 回头核实：勾了但还在的项。**别只看执行结果**（会报成功、其实剩个壳）。
    stuck = check_stuck(items)
    if stuck:
        log(i18n.L("⚠ 这些你要删、但**没删掉**：", "⚠ You asked to delete these, but they **survived**: ")
            + "、".join(i18n.pick(LABELS[i]) for i in stuck))
        log(i18n.L("  常见原因：还有进程占着（先关掉客户端 / 别的窗口），或需要管理员。",
                   "  Common causes: a process still holds them (close the client / other "
                   "windows first), or admin rights are needed."))
        log(i18n.L("  重新点一次「清理」通常就好了；顽固的话注销一次再试。",
                   "  Clicking “Clean up” once more usually fixes it; if it is stubborn, "
                   "sign out and back in and retry."))
    kept = [n for i, n in KEEPABLE
            if i not in items and os.path.exists(os.path.join(HERE, n))]
    if kept:
        log(i18n.L("保留（你没勾）：", "Kept (you did not tick them): ") + "、".join(kept))
    if not stuck and not kept:
        log(i18n.L("清理后目录里已干净。", "The program folder is clean now."))
    if DEFERRED:
        log(i18n.L("⏳ 已安排到本进程退出后自动删除：",
                   "⏳ Scheduled for automatic deletion after this process exits: ")
            + "、".join(os.path.basename(p.replace(os.sep, "/").rstrip("/")) for p in DEFERRED))
        log(i18n.L("   （正在运行的 pythonw.exe 被 Windows 锁着，谁也删不掉自己，所以交给"
                   "后台命令接着删；约 2 秒后完成，不需要你做任何事）",
                   "   (Windows locks a running pythonw.exe, so a process cannot delete "
                   "itself; a background command finishes the job — about 2 seconds, "
                   "nothing for you to do)"))
    if {"venv", "kill"} & set(items):
        # 只删了依赖/结束了进程：Python 还在（系统 Python 或包内 runtime），装回依赖即可
        log(i18n.L("重新安装：双击「启动遥控器.vbs」→ 界面点「安装基础依赖」",
                   "To reinstall: double-click 启动遥控器.vbs, then click "
                   "“Install base dependencies” in the UI"))
    else:
        log(i18n.L("客户端没有被结束，界面可以继续用。",
                   "The client was not ended; the UI keeps working."))
    if os.path.exists(os.path.join(HERE, "runtime")):
        # 明确告诉用户"内置 Python 不在清理范围内" —— 免得他担心刚才的清理把免安装能力弄没了，
        # 或者去找一个根本不存在的「删除 Python」选项。
        log(i18n.L("说明：内置 Python 运行时（runtime\\）属于程序本体，**不在清理范围内、一直保留** ——"
                   " 这样这台机器始终是免安装版。要彻底删掉，直接删整个程序目录。",
                   "Note: the bundled Python runtime (runtime\\) is part of the program "
                   "itself — **it is never in the cleanup list and always stays**, so this "
                   "machine keeps its no-install setup. To remove it for good, just delete "
                   "the whole program folder."))
    _notify_done([i for i in ORDER if i in items], stuck)
    log("DONE")

    # 最后才结束主程序自己（这样界面上还能看到上面的过程）
    if KILLS_CLIENT & set(items):
        if dry:
            log(i18n.L("（预览：正常运行时这里会让客户端退出）",
                       "(preview: on a real run the client would exit here)"))
        else:
            for pid in port_owners(UI_PORT):
                # 同样不加 /T —— 否则会把本脚本自己一起带走
                run(["taskkill", "/PID", str(pid), "/F"])
            if port_owners(UI_PORT):
                log(i18n.L("  - 客户端仍在运行（可能不是本程序启动的），没强杀",
                           "  - The client is still running (maybe it was not started by "
                           "this program); not forcing it"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
