# -*- coding: utf-8 -*-
"""一次性优化（需要管理员，由主程序用 UAC 拉起 —— **只会弹这一次**）。

干三件事：

 1. **清掉 Frida 的历史残留**。Frida 每次挂载不属于自己的进程时，都会尝试注册
    一个临时服务 `frida-<pid>-x86/x86_64` 来注入；注册服务需要管理员，于是每
    次尝试都会弹一次 UAC；没批准或中途中断就留下一条死服务。残留会随重启不断累积，
    攒到几十条服务 + 一堆 frida-helper 进程都很常见。

 2. **装一个「最高权限 + 登录时触发」的计划任务来做开机自启**。
    为什么不用更简单的 HKCU Run 项：普通权限启动时，Frida 装它的助手要提权，
    于是**每次开机都要弹一次 UAC**。改成最高权限的计划任务后，开机由任务计划
    程序以管理员身份静默启动 —— Frida 要的权限都是现成的，**再也不会弹**。

 3. 删掉 HKCU Run 里的普通权限自启项，避免两份同时启动抢遥控器的蓝牙通道。

过程写进同目录的 elevate_setup.log，非提权的主程序会读它并显示给你看。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import winreg
import xml.sax.saxutils as sx

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "elevate_setup.log")
XML = os.path.join(HERE, "_task.xml")
TASK_NAME = "VibeMote"
VBS = os.path.join(HERE, "启动遥控器.vbs")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
CREATE_NO_WINDOW = 0x08000000


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _dec(b: bytes) -> str:
    """子进程输出编码不能写死：schtasks/sc 吐 GBK，python 子进程吐 UTF-8。
    写死一个就会把中文变成「涓嶅彲鐢」那种乱码。"""
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


# ---------------------------------------------------------------- 1. 清残留
def cleanup_frida():
    total_killed = 0
    for exe in ("frida-helper-x86.exe", "frida-helper-x86_64.exe"):
        rc, out = run(["taskkill", "/IM", exe, "/F"])
        killed = out.lower().count("success") + out.count("成功")
        total_killed += killed
    if total_killed:
        log(f"已结束 {total_killed} 个 frida-helper 进程")
    else:
        log("没有 frida-helper 进程在跑")

    names = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services") as k:
            i = 0
            while True:
                try:
                    names.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
    except OSError as e:
        log(f"读服务列表失败：{e}")
        return
    targets = [n for n in names if n.lower().startswith("frida-")]
    log(f"发现 {len(targets)} 条 frida-* 残留服务，开始删除…")
    ok = 0
    for n in targets:
        rc, _ = run(["sc.exe", "delete", n])
        if rc == 0:
            ok += 1
    log(f"已删除 {ok}/{len(targets)} 条")


# ---------------------------------------------------------------- 2. 装任务
def task_xml(run_level="HighestAvailable"):
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    # ★ 必须带 `--boot`：它标记"这一份是开机自启拉起来的"，
    #   程序靠它关掉"首次启动自动拉起虚拟声卡安装器"那条路（见 app.py 里 auto_ok 的注释）。
    #   漏了的话，开机时如果声卡不在了就会在开机那一刻弹安装器 + UAC —— 那正是
    #   用户最不想要的"开机弹窗"。
    #   ★ 另外给登录触发器加了**每 5 分钟重复一次**（见下面 XML）：这台机器上真出现过
    #   "客户端不知什么时候没了、开机自启也没把它拉回来"（任务只在登录那一刻跑一次）。
    #   有了重复，进程若异常退出，最多 5 分钟就会自己回来；已经在跑时新进程看到端口在听
    #   会立刻退出，等于什么都没做（不弹窗、不刷日志）。
    args = sx.escape(f'"{VBS}" --silent --boot')
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>VibeMote - Google TV remote bridge (voice key + key mapping)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT8S</Delay>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </LogonTrigger>
    <BootTrigger>
      <Enabled>false</Enabled>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{sx.escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>{run_level}</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>{args}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def install_task(run_level="HighestAvailable", name=TASK_NAME):
    # 任务定义用 UTF-16 写，schtasks 读 XML 时按声明解码，中文路径不会乱
    with open(XML, "w", encoding="utf-16") as f:
        f.write(task_xml(run_level))
    rc, out = run(["schtasks", "/create", "/tn", name, "/xml", XML, "/f"])
    head = (out or "").strip().splitlines()
    log(f"计划任务：返回码 {rc}  {' / '.join(head[:2]) if head else ''}")
    try:
        os.remove(XML)
    except OSError:
        pass
    return rc == 0


# ---------------------------------------------------------------- 3. 清 Run 项
def remove_run_entry():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, TASK_NAME)
                log("已删除 HKCU Run 里的自启项（改用计划任务，避免两份同时启动）")
            except FileNotFoundError:
                log("HKCU Run 里本来就没有自启项")
    except OSError as e:
        log(f"处理 Run 项失败（不影响计划任务）：{e}")


# ------------------------------------------------------- 4. 用新代码重启客户端
UI_PORT = 8787


def port_owners(port):
    """谁正在监听这个端口（解析 netstat，不依赖任何第三方库）。"""
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


def restart_client():
    """结束旧客户端，再用计划任务拉起当前目录里的新代码。

    为什么必须由管理员进程来做：客户端自己是最高权限跑的，**普通权限杀不掉它**
    （Stop-Process / taskkill 在普通权限下都是 Access is denied）。所以「换了代码怎么
    生效」这件事，正好借这一次提权一起办掉；之后想重启，用界面「状态 → 重启」
    即可 —— 应用内的 restart-app 动作会以自身权限拉起新实例并接管，不再需要 UAC。
    """
    old = port_owners(UI_PORT)
    if not old:
        log("没有正在运行的客户端（跳过重启）")
    for pid in old:
        log(f"结束旧客户端 PID {pid}")
        run(["taskkill", "/PID", str(pid), "/T", "/F"])
    for _ in range(40):
        if not port_owners(UI_PORT):
            break
        time.sleep(0.25)
    rc, out = run(["schtasks", "/run", "/tn", TASK_NAME])
    log(f"用计划任务重新启动客户端：返回码 {rc}")
    for _ in range(40):
        if port_owners(UI_PORT):
            log(f"客户端已起来（PID {port_owners(UI_PORT)}），界面 http://127.0.0.1:{UI_PORT}/")
            return True
        time.sleep(0.5)
    log("客户端没在 20 秒内起来，可双击「启动遥控器.vbs」手动拉起")
    return False


def _ps(cmd, timeout=120):
    rc, out = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                  timeout=timeout)
    for line in (out or "").splitlines():
        if line.strip():
            log("  " + line.strip())
    return rc, out or ""


def reset_bluetooth():
    """禁用再启用蓝牙电台 —— 拆掉"被系统 HID 攥住"的那条 BLE 连接。

    BLE 外设只接受**一个**连接。Windows 的 HID 栈有时会把那条唯一的连接攥住不放，
    遥控器因此不再广播，而程序要自己建一条 GATT 连接才能收语音 → "永远连不上"
    （表现：日志一直刷连接超时，但按键还有用）。拔电池/关开蓝牙开关能解开，
    这个动作等于"用代码关开一次蓝牙电台"。

    ⚠⚠ 这里踩过一个**很严重**的坑：原先禁用+启用写在同一条 PowerShell 里、都带
    `-ErrorAction Stop` —— 只要"启用"那一步抛错（或者提权进程在中途被杀），
    电台就**留在禁用状态**，用户看到的是「**我的蓝牙设备全没了**」。
    所以现在：① 禁用、启用分成两次独立调用，各自成败互不影响；
    ② 启用**重试 3 次**；③ 最后**验证**电台状态，没回来就明确告诉他去哪儿手动启用。
    """
    # 蓝牙电台在 USB 上（如 Intel USB\VID_8087&PID_0033）；BTHLE\ 那些是设备，不能停。
    # 注意：禁用后 Status 会变成 Error/Unknown，所以"启用"这一步不能再用 Status -eq 'OK' 过滤。
    pick = ("$d = @(Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
            "Where-Object { $_.InstanceId -like 'USB*' });"
            "if ($d.Count -eq 0) { Write-Output 'NO-RADIO'; exit 3 };"
            "$d | ForEach-Object { Write-Output ('RADIO ' + $_.InstanceId + ' ' + $_.FriendlyName) }")
    rc, out = _ps(pick)
    if rc != 0 or "NO-RADIO" in out:
        log("✗ 没找到 USB 上的蓝牙电台（可能是别的方式接的，或驱动异常）—— 什么都没动。")
        return False
    if "RADIO " not in out:
        log("✗ 没能列出蓝牙电台 —— 什么都没动。")
        return False

    insts = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 2 and parts[0] == "RADIO":
            insts.append(parts[1])

    # ★ 第 0 步：**先把被禁用的电台启用回来**。
    #   真机上发生过：用户按提示"手动关开一次蓝牙开关"只关了没开回来（或者设备管理器里
    #   被禁用），结果 Windows 设置里的蓝牙开关**整个消失**、所有蓝牙设备都不见了 ——
    #   用户以为设备坏了。这一步就是那个状态的对症解药（而且是幂等的：正常时不做事）。
    en0 = ("Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
           "Where-Object { $_.InstanceId -like 'USB*' -and $_.Status -ne 'OK' } | ForEach-Object {"
           " Write-Output ('发现非正常状态的电台：' + $_.Status + ' / ' + $_.FriendlyName);"
           " try { Enable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false -ErrorAction Stop;"
           " Write-Output '  Enable-PnpDevice 成功' }"
           " catch { Write-Output ('  Enable-PnpDevice 失败: ' + $_.Exception.Message) } }")
    _ps(en0)
    for inst in insts:
        # pnputil 的 enable 更兼容（真机上 Enable-PnpDevice 也会"不支持"）
        _ps(f'pnputil /enable-device "{inst}"')
    time.sleep(2)

    # ★★ 安全闸门：如果一开始就是"非正常状态"（被禁用/Error），那么**只要启用成功就够了**,
    #    绝不再去"关开一次" —— 真机上这一步曾经把电台留在禁用状态，用户以为设备坏了。
    #    确认变好就收手，不做任何多余动作。
    rc_ok, chk0 = _ps("$d = @(Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
                      "Where-Object { $_.InstanceId -like 'USB*' -and $_.Status -eq 'OK' });"
                      "if ($d.Count -gt 0) { Write-Output 'RADIO-OK' } else { Write-Output 'RADIO-DOWN' }")
    if "RADIO-DOWN" in out or "Error" in out or "Unknown" in out or "Disabled" in out:
        if "RADIO-OK" in chk0:
            log("✓ 电台已被启用并正常工作（原来是禁用/异常状态，所以只做了启用，没有再关开）")
            return "enabled"
        log("… 启用后状态仍不对，继续尝试重启设备")

    # ★ 第 1 步：用 `pnputil /restart-device` 做"关开一次"。
    #   它是"重启设备"，**不存在"禁用后没启用回来"的窗口**，而且比
    #   Disable/Enable-PnpDevice 兼容（真机上 Disable-PnpDevice 直接报"不支持"
    #   HRESULT 0x8004100c，那条路根本走不通）。重启失败再退回禁用+启用。
    for inst in insts:
        log(f"用 pnputil 重启设备：{inst}")
        rc3, o3 = _ps(f'pnputil /restart-device "{inst}"')
        if rc3 == 0:
            log("✓ 设备已重启（这条路径不会把电台留在禁用状态）")
            return "restarted"
        log("  pnputil 这条路没成功，改用『禁用 + 启用』")

    dis = ("Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
           "Where-Object { $_.InstanceId -like 'USB*' } | ForEach-Object {"
           " Write-Output ('禁用 ' + $_.FriendlyName);"
           " try { Disable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false -ErrorAction Stop }"
           " catch { Write-Output ('禁用失败（继续）: ' + $_.Exception.Message) } }")
    _ps(dis)
    time.sleep(3)
    # ★ 启用是"必须成功"的那一步：独立调用 + 重试
    for attempt in range(1, 4):
        en = ("Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
              "Where-Object { $_.InstanceId -like 'USB*' } | ForEach-Object {"
              " Write-Output ('启用 ' + $_.FriendlyName);"
              " try { Enable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false -ErrorAction Stop }"
              " catch { Write-Output ('启用失败: ' + $_.Exception.Message) } }")
        _ps(en)
        time.sleep(3)
        rc2, chk = _ps("$d = @(Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
                       "Where-Object { $_.InstanceId -like 'USB*' -and $_.Status -eq 'OK' });"
                       "if ($d.Count -gt 0) { Write-Output 'RADIO-OK' } else { Write-Output 'RADIO-DOWN' }")
        if "RADIO-OK" in chk:
            log(f"✓ 蓝牙电台已恢复（第 {attempt} 次确认）")
            return True
        log(f"… 电台还没回来（第 {attempt} 次），再试一次启用")
    log("✗ 蓝牙电台没能自己回来。**别慌，设备还在**，手动两步就能救回：")
    log("  ① 设备管理器（Win+X → 设备管理器）→ 蓝牙 → 右键那个适配器 → **启用设备**")
    log("  ② 还不行就**完全关机**（开始菜单点「关机」，不是「重启」）再开机 —— ")
    log("     快速启动有时会让蓝牙设备开机后不枚举，完整关机一次必回来。")
    return False


def main():
    # 只重置蓝牙电台（不动自启/任务）：给界面上那个「重置蓝牙电台」按钮用
    if "--reset-bt" in sys.argv:
        try:
            open(LOG, "w", encoding="utf-8").close()
        except Exception:
            pass
        log("=== 重置蓝牙电台 ===")
        if not is_admin():
            log("✗ 当前不是管理员，什么都没做。请从界面点按钮并在 UAC 里点「是」。")
            log("DONE")
            return 1
        log("管理员权限 ✓（期间蓝牙设备会短暂断开，几秒后自动恢复）")
        ok = reset_bluetooth()
        if ok == "restarted":
            log("✓ 蓝牙电台已重启，等待遥控器自动重连…")
        elif ok == "enabled":
            log("⚠ 这台机器**不支持**用软件重启蓝牙电台（Windows 报「不支持 / 需要重启系统才能完成上一次操作」）——")
            log("   我已经确保它是**启用**状态；要真正「关开一次」请二选一：")
            log("   ① 把遥控器**电池取出 10 秒**再装回（推荐，最快）")
            log("   ② **重启电脑**（或完全关机再开机）—— 系统里还有一次待完成的蓝牙配置操作，重启后才干净")
        else:
            log("✗ 重置失败 —— 手动两步：① 设备管理器 → 蓝牙 → 右键适配器 → 「启用设备」；② 完全关机再开机")
        log("DONE")
        return 0 if ok else 1
    try:
        open(LOG, "w", encoding="utf-8").close()
    except Exception:
        pass
    log("=== 一次性优化开始 ===")
    if not is_admin():
        log("✗ 当前不是管理员，什么都没做。请从界面点「一键优化」并在 UAC 里点「是」。")
        log("DONE")
        return 1
    log("管理员权限 ✓")
    cleanup_frida()
    if install_task():
        remove_run_entry()
        log("✓ 开机自启已改为「最高权限计划任务」：以后开机静默以管理员身份启动，")
        log("  Frida 需要的权限现成，**不会再弹任何 UAC**。")
    else:
        log("✗ 计划任务没建成。可以只做第一步（清残留）也不影响使用；")
        log("  自启仍走 HKCU Run（那种方式每次开机 Frida 可能要你授权一次）。")
    restart_client()
    log("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
