# AGENTS.md —— 给 AI 助手的安装手册

> 这份文件是写给 **AI 助手**看的。用户会把本仓库地址发给你，让你帮忙安装。
> 🌐 [English](AGENTS.en.md)
> 请按这里给的步骤走，**不要把网上的通用教程套进来** —— 这个项目刻意避开了
> 「装驱动 / 开测试签名」那一套，照着做反而会出问题。

## 0. 这是什么

Windows 上的小工具：把 Google TV / Chromecast 蓝牙遥控器变成"按住说话 + 快捷键"设备。
纯用户态，**不装驱动、不写系统目录、不常驻管理员**。⚠ 但**按键映射**（方向键/OK/音量那些）要
**一次**提权：Frida 得往蓝牙 HID 驱动宿主里注入并注册临时服务；点一次「一键优化」后由最高权限
计划任务代劳、之后不再弹。**语音完全不需要管理员** —— 跟用户介绍时别把这两件事混成一句"不需要管理员"。
程序会在 `127.0.0.1:8787` 起一个网页控制台（只监听本机）。

**先跟用户确认三件事**（缺一件都装不成）：

1. 系统是 **Windows 10/11 x64**？
2. 遥控器是 **Google TV / Chromecast 蓝牙遥控器**（其他遥控器报文格式不同）？
3. 用哪种包？
   - `vibe-mote-offline-v1.zip`（约 61MB，**自带 Python，推荐**）→ 什么都不用装
   - `vibe-mote-v1.zip`（约 900KB）→ 需要 **Python 3.10+**，让用户从
     [python.org](https://www.python.org/downloads/) 装，**务必勾选 `Add python.exe to PATH`**，
     不要用 Microsoft Store 版本

## 1. 安装

**离线包**：解压 → 双击 `启动遥控器.vbs` → 完成（没别的步骤）。

**轻包**（目标机器有 Python 时）：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
# 可选：方向键/OK/音量这些要 frida（约 130MB）—— 先问用户要不要
.venv\Scripts\python.exe -m pip install -r requirements-keys.txt
```

装依赖卡住或超时（国内常见）时，按这个顺序换源，第一个成功就停：

```powershell
$mirrors = @(
  "https://mirrors.cloud.tencent.com/pypi/simple",
  "https://mirrors.ustc.edu.cn/pypi/simple",
  "https://mirrors.aliyun.com/pypi/simple",
  "https://mirrors.huaweicloud.com/pypi/simple",
  "https://pypi.org/simple"
)
foreach ($m in $mirrors) {
  .venv\Scripts\python.exe -m pip install -r requirements.txt -i $m --timeout 30 --retries 2
  if ($LASTEXITCODE -eq 0) { break }
}
```

> 注意：清华 TUNA 源会返回空索引（`from versions: none`），所以排在最后；
> 网易 163 的 PyPI 镜像已停止同步，**不要用**。
> 界面上的「全部安装」按钮会自动做上面这件事，通常比手敲更省事。

> ⚠ **按键映射不是必需项，先问清楚再装。** 它要 frida，而 frida 会往系统的蓝牙 HID 驱动宿主里注入
> （要一次提权、可能被 AV 误报/隔离、少数带内核反作弊的游戏可能起不来）。**只要语音就别装**。
> 离线包有两个版本：`-voice`（约 14MB，**不含 frida**）和完整版（约 61MB，已带 frida）。
> 用户拿的是 `-voice`、后来又想要按键映射，两条路：
> ① **联网**：点界面「设置 → 诊断 → 安装按键映射依赖」（自动走国内镜像，下载约 47MB）；
> ② **不联网**：让他把 `frida-*-win_amd64.whl` 放进程序目录的 `wheels\`，再点同一个按钮
> （装依赖时**优先用包内 wheels**）。两种方式装完，首次挂载都要一次管理员（或先点「一键优化」）。

## 2. 验证装好了

```powershell
# 2.1 启动（双击 启动遥控器.vbs 一样；--silent 表示不开浏览器）
.venv\Scripts\pythonw.exe app.py --silent

# 2.2 等 5 秒，问接口要状态
Invoke-RestMethod http://127.0.0.1:8787/api/state | ConvertTo-Json -Depth 5
```

判读 `/api/state`：

| 字段 | 期望 | 不对时怎么办 |
|---|---|---|
| `running` | `true` | 点 `/api/action {"action":"start"}`，或让用户在界面点「启动」 |
| `deps.ok` | `true` | 依赖没装全，看 `deps.missing` |
| `deps.keys_ok` | 装了 frida 才 `true` | 没装 frida 属正常 |
| `voice.connected` | `true` | 遥控器在休眠 → **让用户按一下遥控器任意键** |
| `keys.ready` | 装了 frida 才 `true` | 见上文 |

界面「设置 → **环境体检**」（或 `POST /api/action {"action":"check"}`）会逐项列出来，最省事。

日志同时写在程序目录的 `client.log`。如果双击后什么都没发生，**先读这个文件** ——
启动失败时启动器会弹提示并自动打开它。

## 3. 装完之后要告诉用户的三件事

1. **启动**：双击 `启动遥控器.vbs`（无黑框），浏览器会自动打开控制台
2. **输入法里把麦克风选成 `CABLE Output (VB-Audio Virtual Cable)`**（否则语音出不了字）。
   ⚠ **只改输入法这一处**：系统默认录音设备要留给真实麦克风 —— 默认设备设成虚拟声卡的话，
   微信语音/录音机也会拿到遥控器这一线，而且「内置麦克风直通」会把声卡输出再灌回声卡输入
   （输入法听到人声叠上一份约 115 ms 的延迟副本，识别会明显变差）
3. **千问输入法**：程序首次启动会**自动**处理（打开「允许注入」并重启一次千问进程），
   不需要用户手动设置。如果语音键仍无效，让他在「语音」页点一次那个按钮。

想让 UAC 不再出现：让用户点「设置 → **一键优化**」（弹**最后一次** UAC，
之后自启走最高权限计划任务，开机不再弹）。

## 4. 排障

```
第一步永远是：POST /api/action {"action":"check"}，看它说哪一层断了
```

| 日志现象 | 真因 | 处理 |
|---|---|---|
| 一直 `连接超时` | 遥控器休眠（它平时不广播，按键后才醒一小会） | 按一下遥控器任意键 —— 程序**一收到遥控器报文就会立刻重试**；连着失败时会退避到最多 45 秒一轮 |
| `缺少 ATVV 特征` + 只枚举到 4 个服务 | 有另一个客户端占着蓝牙通道 | 关掉其它客户端（含旧版），再点「重启」 |
| `挂载失败：PermissionDenied` | Frida 要一次提权被拒 | 让用户点「一键优化」，或重试一次 |
| 按语音键日志无「开始说话」 | 蓝牙没连上 | 看 `voice.connected` |
| `voice.connected` 一直是 false、日志一直刷「连接超时」，**但遥控器按键有用** | BLE 设备只接受一个连接，Windows 的 HID 把那条唯一的连接攥住不放 → 遥控器不再广播（`BleakScanner` 扫不到它，扫别的 BLE 设备正常） | 让用户点「设置 → 诊断 → **重置蓝牙电台**」；或让用户把遥控器**电池取出 10 秒**。**别**去动配对/驱动 |
| 有「开始说话」但输入法没反应 | 输入法不认程序注入的按键 | 千问：开「允许注入」；豆包：不支持，换输入法 |
| 装了 frida、但 `keys.ready` 一直 false / 日志说找不到 frida | **杀软把 frida 静默隔离了**（离线包里 frida 约 47MB，模块名 `frida-agent.dll`/`frida-helper-*.exe` 在多家 AV 特征库里） | 让用户把程序目录加进杀软白名单，再点「安装按键映射依赖」。**别**让他关 Defender |
| `挂载失败：TransportError: the connection is closed` 反复出现（几十~上百次），而语音一切正常 | **杀软 / EDR 拦了 Frida 的注入** —— **公司电脑上最常见**（真机日志里 145 次全是这个错误） | 让用户请 **IT 把程序目录加进白名单**（我们不改系统设置、不关 Defender、不需要驱动）。**别**建议关杀软，更别想绕过公司策略。放行之前：**语音可用、按键映射不可用**。让他点一次「设置 → 诊断 → 按键映射自检」，把这行证据写进日志再发给你 |
| 日志刷 `GATT Protocol Error: Insufficient Authentication`（或反复「缺少 ATVV 特征」） | 遥控器的链路**配对/加密没完成**（BLE 外设要求已加密的连接）—— 跟杀软、驱动都无关 | 让用户「设置 → 蓝牙和其他设备」**删除设备** → 按住遥控器【**返回 + 主页**】重新配对 → 回程序点「重启」。**别**动配对记录以外的任何东西 |
| 刚开机没声音 | 音频服务/声卡晚就绪 | **正常，等 5~10 秒**（程序每 5 秒重试） |
| 装虚拟声卡报 `Unknown Error 1072` | 服务卡在「待删除」状态（先前的清残留删了服务、没先停驱动）。它提示「请用管理员运行」是**误导** | **重启一次**再装；还不行就右键 `vbcable\VBCABLE_Setup_x64.exe` →「以管理员身份运行」→ 点 Install Driver |
| 双击后什么都没发生 | 启动失败 | 读 `client.log`（启动器也会弹提示并打开它） |
| 识别不连贯、像有回声 / 一直有底噪 | 系统默认录音设备被设成了 `CABLE Output`，而「内置麦克风直通」会跟着把声卡输出转回去 | 让用户把系统默认录音设备改回真实麦克风；虚拟声卡只出现在**输入法自己的**麦克风设置里 |

**别做这些**：不要建议用户装驱动、不要改注册表里的输入法配置、不要让程序常驻管理员 ——
这个项目不需要，做了反而出问题。

## 5. 清理 / 卸载（用户要求时）

界面「设置 → 清理 / 卸载」是**逐项勾选**的（默认什么都不勾），也可以命令行：

```powershell
.venv\Scripts\python.exe uninstall.py --items=venv,pycache   # 只删这两项
.venv\Scripts\python.exe uninstall.py --items=all            # 全删（含配置）
.venv\Scripts\python.exe uninstall.py --dry-run              # 只列清单，什么都不做
```

可选 id：`task, run, kill, frida_helper, frida_svc, venv, pycache, pythontxt, logs, config, qwen, cable`。
`task` / `frida_svc` / `cable` 需要管理员。

> **`cable`（卸载虚拟声卡）跟别的 id 都不是一类**，用户要勾之前先跟他讲清楚：
> 它是**系统级音频驱动**，OBS / 变声器可能也在用；官方**没有静默卸载**（程序只能把官方卸载器
> 拉起来，用户还得在它窗口里点「Remove Driver」）；按 VB-Audio 官方要求**卸完必须重启**。
> 它默认不勾、也不在推荐清理里；程序**故意不核实**卸载结果（核了就是误报"没删掉"）。

**彻底卸载整个程序**：① 上面勾「推荐清理」跑一次（断自启 + 清 frida 服务 + 删依赖），
② 然后直接删掉整个程序目录。程序是纯绿色部署，系统里不会留别的东西。
内置的 `runtime\` 属于程序本体，不在清理清单里。

## 6. 环境

Windows 10/11 x64 · Python 3.10 或更高 · 语音需要 VB-CABLE 虚拟声卡
（它自己的安装器需要一次管理员权限，与本程序无关）。

**VB-CABLE 怎么装**：两个 zip 里都带了官方安装器**和驱动文件**，让用户点界面首页清单
「虚拟声卡」那一行的**一键安装**（或「设置 → 虚拟声卡」那张卡）即可。有两件事要提前告诉他：
- 会弹一次 UAC（那是声卡安装器自己要求的，与本程序无关），**还要在他自己的窗口里点一下
  「Install Driver」** —— 官方安装器**不提供静默安装参数**（没有 `-i`/`-s`/`/S` 这类开关），
  所以这一步不可能全自动；
- 官方 readme 写明**装完必须重启一次**系统才算装完。

程序**首次启动会自动帮他拉起这一步**（只自动试一次，用户拒了 UAC 就不再自动弹）；
如果机器上依赖还没装（轻包首启），那一步不会触发，走界面上的「全部安装」即可（它含虚拟声卡）。
