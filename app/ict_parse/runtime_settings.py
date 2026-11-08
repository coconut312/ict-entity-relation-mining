# -*- coding: utf-8 -*-
"""运行时 LLM 配置（任务三平台「模型配置 / API Key」页面写入，落盘保存）。

为什么单独一个文件：
    * `config.py` 的取值链是「进程环境变量 > 本文件 > Reasonix .env > 代码默认值」，
      本模块只负责这个 JSON 的读 / 写 / 清除，不掺业务语义；
    * 文件放在 `app/out/platform/llm_settings.json` —— `app/out/` 已被 .gitignore
      排除，密钥不会进版本库；
    * **本模块不打印、不记录任何 key 明文**，对外只提供末 4 位掩码。

容错要求（抽取主流程不能因为配置文件坏掉而失败）：
    * 文件不存在 / JSON 损坏 / 内容不是对象 / 字段类型不对 —— 一律当作「无覆盖」，
      返回空 dict，绝不抛异常；
    * 写入走「同目录临时文件 + os.replace」原子替换，并用进程内锁串行化，
      读方永远不会看到半截文件；
    * 权限尝试 `chmod 0600`，Windows 上失败直接忽略。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time

# app/ict_parse/runtime_settings.py -> app/
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 配置文件位置（可用 LLM_SETTINGS_PATH 覆盖，便于测试）
DEFAULT_PATH = os.path.join(_APP_DIR, "out", "platform", "llm_settings.json")

# 允许持久化的字段白名单：与 PUT /settings/model 的 body 一一对应，其余键忽略
FIELDS = ("api_key", "base_url", "model",
          "vision_api_key", "vision_base_url", "vision_model")

# 进程内锁：写入串行化（读取靠原子替换保证一致，见 save_overrides）
_LOCK = threading.RLock()

# 形如 "****1234" 的掩码：用户若把界面回显的掩码当 key 提交，直接忽略
_MASK_RE = re.compile(r"^\*{2,}[0-9A-Za-z]{0,4}$")


def settings_path() -> str:
    """配置文件路径。"""
    return os.environ.get("LLM_SETTINGS_PATH") or DEFAULT_PATH


def is_mask_value(value) -> bool:
    """是否是把回显掩码误当 key 提交的值。"""
    return bool(isinstance(value, str) and _MASK_RE.match(value.strip()))


def mask_key(key) -> str:
    """掩码：只保留末 4 位，前面固定 4 个星号。

    固定 4 个星号（而不是按真实长度生成）是为了连 key 长度都不泄露。
    """
    k = (key or "").strip()
    if not k:
        return ""
    return "****" + k[-4:]


def _clean(raw) -> dict:
    """白名单过滤：只保留非空字符串字段，其余丢弃。"""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k in FIELDS:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def load_overrides() -> dict:
    """读取运行时覆盖；文件不存在 / 损坏 / 类型不对都返回 {}。"""
    with _LOCK:
        try:
            with open(settings_path(), encoding="utf-8") as f:
                return _clean(json.load(f))
        except (OSError, ValueError):
            return {}


def _replace(tmp: str, path: str) -> None:
    """os.replace 的 Windows 兜底：杀毒/索引进程偶发占用时重试几次。"""
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.05)


def save_overrides(data: dict) -> dict:
    """整体写盘（原子替换 + 尽力 chmod 0600），返回实际写入的内容。"""
    payload = _clean(data)
    path = settings_path()
    parent = os.path.dirname(path)
    with _LOCK:
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".llm_settings_", suffix=".tmp",
                                   dir=parent or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            _replace(tmp, path)          # 原子替换：读方不会读到半截文件
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)        # Windows 上可能不支持，失败忽略
        except OSError:
            pass
    return payload


def update_overrides(patch: dict) -> dict:
    """按字段合并更新，返回写入后的完整内容。

    约定（与 PUT /settings/model 一致）：
      * 键不存在 / 值为 None   -> 保持原值（不修改）
      * 值为空字符串 ''        -> 清除该项（回落环境变量 / 默认值）
      * 非空字符串             -> 覆盖该项
      * 掩码值（****1234）     -> 视为误提交，忽略（避免把掩码写成真 key）
    """
    cur = load_overrides()
    for k, v in (patch or {}).items():
        if k not in FIELDS or not isinstance(v, str):
            continue
        v = v.strip()
        if v == "":
            cur.pop(k, None)
        elif is_mask_value(v):
            continue
        else:
            cur[k] = v
    return save_overrides(cur)


def clear_overrides() -> None:
    """删除运行时配置，全部回落环境变量 / 默认值。"""
    with _LOCK:
        try:
            os.remove(settings_path())
        except OSError:
            pass
