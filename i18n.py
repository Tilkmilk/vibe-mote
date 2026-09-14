# -*- coding: utf-8 -*-
"""中英双语 —— 后端只负责「界面要显示的消息」。

分工：
  · 界面自身的文字（按钮/标题/提示）由前端 `web/index.html` 里的 I18N 字典负责；
  · 后端产生的、**会显示在界面上的**文字（依赖说明、预设方案名与备注、清理清单、
    角色状态备注、体检输出、动作回复）由这里负责；
  · **内部诊断日志保持中文**（角色日志、ATLL/音频细节、frida 细节）—— 那些是排障用的
    技术流水，双语的收益远低于维护成本，这一点在 README 里写明了。

语言从哪来：`config.json` 的 `"lang"`，由界面通过 `set-lang` 动作写入
（界面首次打开会按浏览器语言自动选一次，用户手动切过就记住）。
取不到就一律用中文 —— 与界面「检测不出来就用中文」的约定一致。

为什么不用字典式的 i18n 框架：这些字符串本来就在代码上下文里，抽成 key → 字典
反而离上下文更远、更容易漂移。所以只留一个极简的 `L(zh, en)`；
**数据表**（依赖清单、预设方案、清理清单）例外 —— 它们本身就是数据结构，
直接把中英两版写进表里，渲染时按语言取。
"""
from __future__ import annotations

import json
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")

_lock = threading.Lock()
_lang = "zh"
# 写 config.json 的**唯一**一把锁。
# ⚠ 为什么放在这里：写 config.json 的有两个地方 —— app.py 的 `save_config()` 和本文件的
# `save_to_config()`。它们在**同一个进程**里，而且界面上「保存设置」和「切语言」
# 完全可能同时发生。原来两边都用 `CONFIG + ".tmp"` 这一个临时文件名：
# A 写 tmp → B 把 tmp 覆盖掉 → A replace 走了 B 的内容 → B replace 时文件已经没了
# （异常被吞）→ 结果要么丢一次修改，要么**在程序目录里留下一个 config.json.tmp**
# （那里面会是用户完整的配置：蓝牙地址 + 整套键位）。
# app.py 已经 import 了 i18n，所以把锁放这里两边都能用。
CONFIG_LOCK = threading.Lock()


def normalize(lang):
    """只认 zh / en，其它（含空、None、检测不出来）一律回中文。"""
    s = str(lang or "").strip().lower()
    return "en" if s.startswith("en") else "zh"


def current():
    with _lock:
        return _lang


def set_lang(lang):
    global _lang
    with _lock:
        _lang = normalize(lang)
        return _lang


def load_from_config():
    """启动时从 config.json 读一次语言。"""
    global _lang
    try:
        # utf-8-sig：记事本另存为会加 BOM，用它读才不会 JSONDecodeError
        with open(CONFIG, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        with _lock:
            _lang = normalize(cfg.get("lang"))
    except Exception:
        pass
    return current()


def save_to_config(lang):
    """界面切语言时写回 config.json —— **只动 lang 这一项**，其它字段原样保留。"""
    v = set_lang(lang)
    # 跟 app.save_config() 共用一把锁 + 一个唯一的临时文件名（见 CONFIG_LOCK 的注释）
    with CONFIG_LOCK:
        try:
            cfg = {}
            if os.path.exists(CONFIG):
                with open(CONFIG, encoding="utf-8-sig") as f:
                    cfg = json.load(f)
            cfg["lang"] = v
            tmp = f"{CONFIG}.{os.getpid()}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                os.replace(tmp, CONFIG)
            except OSError:
                try:
                    os.remove(tmp)          # 别留垃圾
                except OSError:
                    pass
        except Exception:
            pass
    return v


def L(zh, en):
    """按当前语言取一条文案。"""
    return en if current() == "en" else zh


def pair(zh, en):
    """给数据表用：包成 (zh, en)，渲染时用 pick() 取。"""
    return (zh, en)


def pick(value):
    """从数据表里取当前语言的那一版；不是 (zh, en) 就原样返回。"""
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return value[1] if current() == "en" else value[0]
    return value
