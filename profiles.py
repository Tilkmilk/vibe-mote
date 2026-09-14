# -*- coding: utf-8 -*-
"""预设方案：目标应用 × 输入法 → 一整套映射。

数据来源（都写清楚了，别凭印象改）：

  · WorkBuddy：**它自己的设置页 → 快捷键**（以该页为准）。原样对应：
    Ctrl+, 打开设置 / Ctrl+D 语音录制开关 /
    Ctrl+F 对话内搜索 / Enter 发送 / Shift+Enter 换行 / Ctrl+N 新建对话 /
    Esc 停止生成 / Ctrl+[ 上一个任务 / Ctrl+] 下一个任务 / Ctrl+B 左侧栏 /
    Ctrl+Shift+B 右侧产物面板 / F11 全屏 / Shift+Alt+W 唤起隐藏主窗口。

  · Codex = ChatGPT 桌面客户端：**官方 Commands 参考页**（Windows 一节）。
    最关键的几条：核准请求 = Enter、拒绝请求 = Esc；上一个/下一个聊天 =
    Ctrl+Shift+[ / ]；下一个需要关注的聊天 = Ctrl+Alt+A；新建聊天 = Ctrl+N；
    切换侧边栏 = Ctrl+B；切换底部面板 = Ctrl+J；切换终端 = Ctrl+`；
    搜索文件 = Ctrl+P；字号 = Ctrl+= / Ctrl+-；全屏 = F11。

  · 输入法语音键：开源参考实现 ZSTDJan/windows-remote-mic-app 的
    voice_hotkey_sync_windows.py（DEFAULT_PROVIDER_HOTKEYS）——
    搜狗 rctrl、微信输入法 lctrl+lwin、Windows 语音输入 win+h。
    千问 = 右 Alt，依据它自己的 voice_settings.json（windows.vk.165）。
"""

# 中英双语：界面上的名字与备注按当前语言取（机制见 i18n.py）。
import i18n

# 每个映射：type 取 key/combo/hold/holdcombo/mouse/text
def _k(v):
    return {"type": "key", "value": v, "mods": [], "text": ""}


def _c(v, *mods):
    return {"type": "combo", "value": v, "mods": list(mods), "text": ""}


def _wheel(direction):
    """上/下 = 滚轮，而且是**按住不放连续滚**（轻点滚一格，按住一直滚）。

    跟出厂默认保持一致（app.py 的 RECOMMENDED 也是 holdmouse）：
    不然"一键套预设"会把默认那个更好用的行为换回"按一下只滚一格"。
    """
    return {"type": "holdmouse", "value": direction, "mods": [], "text": ""}


# 「审批」这两个动作：ChatGPT/Codex 官方就是 Enter 批准、Esc 拒绝。
# 抽成函数，将来要换成 y/n 或 1/2 只改这一处。
def _approve_yes():
    return _k("ENTER")


def _approve_no():
    return _k("ESC")


def _text(s):
    """文本动作：按下就把这段字**打进去**。

    ⚠ 故意**不补回车**（按 OK 才是回车）—— 2026-09-15 跟需求方确认过：
    `/clear`、`/exit`、`commit + push` 这些打完字，由用户自己按 OK 提交，
    这样也能先看一眼、不满意就删掉，比自动回车安全。
    """
    return {"type": "text", "value": "", "mods": [], "text": s}


APPS = {
    "workbuddy": {
        "name": i18n.pair("WorkBuddy", "WorkBuddy"),
        "note": i18n.pair("按 WorkBuddy 自己的快捷键页做的（最准）",
                          "Built from WorkBuddy's own shortcuts page (most accurate)"),
        "keys": {
            "ok":      _k("ENTER"),                      # 发送消息
            "back":    _k("ESC"),                        # 停止生成
            "up":      _wheel("wheel_up"),
            "down":    _wheel("wheel_down"),
            "left":    _c("LBRACKET", "CTRL"),           # 上一个任务
            "right":   _c("RBRACKET", "CTRL"),           # 下一个任务
            "home":    _c("N", "CTRL"),                  # 新建对话
            "power":   _c("W", "SHIFT", "ALT"),          # 唤起/隐藏主窗口
            "input":   _c("F", "CTRL"),                  # 对话内搜索
            "mute":    _c("B", "CTRL"),                  # 切换左侧栏
            "volup":   _c("B", "CTRL", "SHIFT"),         # 切换右侧产物面板
            "voldown": _k("F11"),                        # 全屏
            "youtube": _approve_yes(),                   # 审批：同意
            "netflix": _approve_no(),                    # 审批：拒绝
        },
    },
    "codex": {
        "name": i18n.pair("Codex / ChatGPT 客户端", "Codex / ChatGPT desktop app"),
        "note": i18n.pair("按 OpenAI 官方 Commands 参考页（Windows）做的",
                          "Built from OpenAI's official Commands reference (Windows)"),
        "keys": {
            "ok":      _k("ENTER"),                      # 发送 / 批准请求
            "back":    _k("ESC"),                        # 停止 / 拒绝请求
            "up":      _wheel("wheel_up"),
            "down":    _wheel("wheel_down"),
            "left":    _c("LBRACKET", "CTRL", "SHIFT"),  # 上一个聊天
            "right":   _c("RBRACKET", "CTRL", "SHIFT"),  # 下一个聊天
            "home":    _c("N", "CTRL"),                  # 新建聊天
            "power":   _k("F11"),                        # 全屏
            "input":   _c("A", "CTRL", "ALT"),           # 下一个需要关注的聊天
            "mute":    _c("B", "CTRL"),                  # 切换侧边栏
            "volup":   _c("EQUAL", "CTRL"),              # 增大字号
            "voldown": _c("MINUS", "CTRL"),              # 减小字号
            "youtube": _approve_yes(),
            "netflix": _approve_no(),
        },
    },
    "claude": {
        "name": i18n.pair("Claude Code", "Claude Code"),
        "note": i18n.pair("按 Claude Code 命令行的习惯做的：方向键导航，另三个键直接打斜杠命令/指令",
                          "Built around Claude Code's CLI habits: arrow-key navigation plus three "
                          "keys that type slash commands / a prompt"),
        "keys": {
            "ok":      _k("ENTER"),                      # 提交输入
            "back":    _k("BACKSPACE"),                  # 删一个字符
            "up":      _k("UP"),                         # ↓ 普通方向键（不用滚轮：菜单/历史更直观）
            "down":    _k("DOWN"),
            "left":    _k("LEFT"),
            "right":   _k("RIGHT"),
            "home":    _k("ESC"),                        # 取消当前输入 / 关掉补全
            "power":   _text("/exit"),                   # 打字：退出
            "input":   _c("TAB", "SHIFT"),               # 反向切换（权限模式 / 补全候选）
            "mute":    _k("ENTER"),                      # 和 OK 一样是回车（需求方确认：保持截图那套）
            "volup":   _wheel("wheel_up"),               # 侧边：按住不放连续滚
            "voldown": _wheel("wheel_down"),
            "youtube": _text("/clear"),                  # 打字：清空上下文
            "netflix": _text("commit + push"),           # 打字：让它提交并推送
        },
    },
    "generic": {
        "name": i18n.pair("通用（浏览器 / 任意程序）", "Generic (browser / any app)"),
        "note": i18n.pair("只用各程序都常见的键，不依赖具体 App",
                          "Only keys that are common across apps; not tied to one app"),
        "keys": {
            "ok":      _k("ENTER"),
            "back":    _k("ESC"),
            "up":      _wheel("wheel_up"),
            "down":    _wheel("wheel_down"),
            "left":    _c("PAGEUP", "CTRL"),             # 上一个标签页/会话
            "right":   _c("PAGEDOWN", "CTRL"),           # 下一个标签页/会话
            "home":    _c("T", "CTRL"),                  # 新标签页
            "power":   _k("F11"),                        # 全屏
            "input":   _c("L", "CTRL"),                  # 地址栏 / 行号
            "mute":    _c("B", "CTRL"),                  # 侧边栏
            "volup":   _c("EQUAL", "CTRL"),
            "voldown": _c("MINUS", "CTRL"),
            "youtube": _approve_yes(),
            "netflix": _approve_no(),
        },
    },
}

# 输入法 → 语音键。status: ok=已确认 / try=照参考实现填、未确认 / no=已知不行
IMES = {
    "qwen": {
        "name": i18n.pair("千问输入法", "Qianwen IME"),
        "spec": {"mode": "hold", "key": "RALT", "mods": []},
        "status": "ok",
        "note": i18n.pair("按住右 Alt。依据它自己的配置（windows.vk.165）。"
                         "需要在「语音」页开一次「允许注入」，否则它会丢弃程序发的按键。",
                         "Hold Right Alt. Based on its own config (windows.vk.165). "
                         "You must turn on “allow injection” once on the “Voice” page, "
                         "otherwise it discards the keys this program sends."),
    },
    "sogou": {
        "name": i18n.pair("搜狗语音输入", "Sogou voice input"),
        "spec": {"mode": "hold", "key": "RCTRL", "mods": []},
        "status": "ok",
        "note": i18n.pair("按住右 Ctrl。已确认：搜狗语音助手自己的日志里写着 "
                         "`[KeyboardService] Loaded long press hotkey from store: RightCtrl`，"
                         "而且 longPressHotkeyEnabled/freespeakEnabled 都是 true。"
                         "它跟千问的语音键（右 Alt）**不冲突**，可以同时装。"
                         "（搜狗那个设置界面偶尔会被卡在新手引导里打不开，但语音键照旧是这个。）",
                         "Hold Right Ctrl. Confirmed: Sogou's own voice-assistant log says "
                         "`[KeyboardService] Loaded long press hotkey from store: RightCtrl`, "
                         "and longPressHotkeyEnabled/freespeakEnabled are both true. "
                         "It does **not conflict** with Qianwen's voice key (Right Alt), "
                         "so both can be installed at the same time. "
                         "(Sogou's settings window occasionally gets stuck in its "
                         "onboarding wizard, but the voice key is still this one.)"),
    },
    "wechat": {
        "name": i18n.pair("微信输入法", "WeChat IME"),
        "spec": {"mode": "hold", "key": "LWIN", "mods": ["CTRL", "LWIN"]},
        "status": "try",
        "note": i18n.pair("按住 Ctrl+Win（微信输入法 2.1.2 迁移后的默认）。"
                         "它不提供静默改键接口，改完要在它自己的设置里核对一致。",
                         "Hold Ctrl+Win (the default after the WeChat IME 2.1.2 migration). "
                         "It offers no silent rebinding API, so check that its own settings "
                         "agree with this."),
    },
    "windows": {
        "name": i18n.pair("Windows 语音输入（系统自带）", "Windows voice typing (built in)"),
        "spec": {"mode": "tap", "key": "H", "mods": ["LWIN"]},
        "status": "ok",
        "note": i18n.pair("按一下 Win+H。系统固定，且是**开关式**不是按住，所以模式必须是「按一下」。"
                         "中文识别质量一般。",
                         "Tap Win+H. Fixed by the OS — and it is a **toggle**, not "
                         "push-to-talk, so the mode must be “tap”. Recognition quality "
                         "is mediocre."),
    },
    "doubao": {
        "name": i18n.pair("豆包输入法", "Doubao IME"),
        "spec": {"mode": "hold", "key": "RALT", "mods": []},
        "status": "no",
        "note": i18n.pair("按住右 Alt。⚠ 已知不行：豆包会拦程序注入的按键"
                         "（要挂进它的进程里抹掉注入标记，本程序不做这种事）。",
                         "Hold Right Alt. ⚠ Known not to work: Doubao blocks injected "
                         "keystrokes (getting around it would mean hooking its process "
                         "to wipe the injection flag — this program does not do that)."),
    },
    "custom": {
        "name": i18n.pair("自定义（不动现有语音设置）", "Custom (leave the voice settings alone)"),
        "spec": None,
        "status": "ok",
        "note": i18n.pair("保持你当前在「语音」页里的设置不变。",
                         "Keeps whatever you currently have on the “Voice” page."),
    },
}

DEFAULT_APP = "workbuddy"
DEFAULT_IME = "qwen"


def catalog():
    """给前端用的完整目录（含每个键的中文名与说明）。"""
    return {
        "apps": [{"id": k, "name": i18n.pick(v["name"]), "note": i18n.pick(v["note"]),
                  "keys": v["keys"]} for k, v in APPS.items()],
        "imes": [{"id": k, "name": i18n.pick(v["name"]), "spec": v["spec"],
                  "status": v["status"], "note": i18n.pick(v["note"])}
                 for k, v in IMES.items()],
        "default_app": DEFAULT_APP,
        "default_ime": DEFAULT_IME,
    }


def build(app_id, ime_id, current=None):
    """返回 (keys, voice)。voice 为 None 表示「保持现有」。"""
    app = APPS.get(app_id) or APPS[DEFAULT_APP]
    ime = IMES.get(ime_id) or IMES[DEFAULT_IME]
    keys = {k: dict(v) for k, v in app["keys"].items()}   # 深拷贝一份
    voice = None if ime["spec"] is None else dict(ime["spec"])
    return keys, voice
