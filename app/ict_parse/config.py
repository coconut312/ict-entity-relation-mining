"""集中式 API 配置：默认全部走 DeepSeek；可用环境变量 / 页面设置 / Reasonix .env 覆盖。

- 文本 LLM / 视觉 均默认 DeepSeek（`deepseek-flash`，base `https://api.deepseek.com/v1`）。
- 覆盖来源与优先级（高 → 低）：
    1. **进程环境变量**：文本 `LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY`；
       视觉 `QWEN_VL_BASE_URL`/`QWEN_VL_MODEL`/`QWEN_VL_API_KEY`。
       部署时用环境变量注入，优先级最高，页面上填的东西压不过它。
    2. **运行时设置文件** `app/out/platform/llm_settings.json` —— 任务三平台
       「模型配置 / API Key」页面写入（见 runtime_settings.py），改完即时生效、无需重启。
    3. **Reasonix `.env` 文件**（`%APPDATA%/reasonix/.env`，开发机的便利入口）。
       注意：`.env` 是文件、不是环境变量，所以排在页面输入之后 ——
       否则开发机上的 `.env` 会让「页面上填的 key」永远不生效。
    4. 代码默认值。
- key 候选名不变：`DEEPSEEK_API_KEY` / `LLM_API_KEY`；
  Qwen 侧 `QWEN_API_KEY` / `DASHSCOPE_API_KEY` / `QWEN_VL_API_KEY` / `MaaS_API_KEY`。
"""
from __future__ import annotations

import os

try:                                            # 允许 config.py 被当作顶层模块直接 import（历史脚本）
    from .runtime_settings import load_overrides
except ImportError:                             # pragma: no cover
    def load_overrides() -> dict:               # type: ignore[misc]
        return {}

_ENV_FILE = os.path.join(os.environ.get("APPDATA", ""), "reasonix", ".env")

# key 来源标签（任务三设置页直接展示给用户）
SOURCE_ENV = "环境变量"
SOURCE_WEB = "页面输入"
SOURCE_NONE = "未配置"


def _read_env_file() -> dict:
    d: dict[str, str] = {}
    try:
        with open(_ENV_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    except OSError:
        pass
    return d


def _get(name: str) -> str | None:
    """先看 Reasonix .env 文件（改完即时生效），再看进程环境变量。"""
    v = _read_env_file().get(name) or os.environ.get(name)
    return v or None


def _first(names: tuple[str, ...]) -> str | None:
    for k in names:
        v = _get(k)
        if v:
            return v
    return None


def _env_only(names: tuple[str, ...]) -> str | None:
    """只看**进程环境变量**（不读 Reasonix .env 文件）。

    这样「环境变量」与「.env 文件」才能分开排序：
    环境变量 > 运行时设置文件 > .env 文件。
    """
    for k in names:
        v = os.environ.get(k)
        if v:
            return v
    return None


# ---- 默认（DeepSeek）----
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1"
TEXT_MODEL = os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash"
VL_MODEL = os.environ.get("DEEPSEEK_VL_MODEL") or "deepseek-flash"
QWEN_VL_DEFAULT_MODEL = "qwen3-vl-flash"

_DS_KEYS = ("DEEPSEEK_API_KEY", "LLM_API_KEY")
_QWEN_KEYS = ("QWEN_API_KEY", "DASHSCOPE_API_KEY", "QWEN_VL_API_KEY", "MaaS_API_KEY")


def deepseek_key() -> str | None:
    return _first(_DS_KEYS)


def qwen_key() -> str | None:
    return _first(_QWEN_KEYS)


def _ui_only() -> bool:
    """`EDM_KEY_SOURCE=ui` 时**只认页面**「模型配置」里填的 Key，忽略环境变量与 .env。

    平台（`task3/backend/run.py`）启动时固定设 `ui` —— 即「必须用用户上传/填写的 Key 跑 LLM」；
    CLI（`scripts/run_llm.py` 等）默认 `auto`，行为不变（页面 > 环境变量 > .env）。
    """
    return (os.environ.get("EDM_KEY_SOURCE") or "").strip().lower() == "ui"


def _text_config() -> tuple[str, str, str | None, str]:
    """文本 LLM 的 (base, model, key, key 来源)。"""
    ov = load_overrides()
    base = os.environ.get("LLM_BASE_URL") or ov.get("base_url") or DEEPSEEK_BASE
    model = os.environ.get("LLM_MODEL") or ov.get("model") or TEXT_MODEL
    if _ui_only():                                  # 平台：只用页面 Key
        web_only = ov.get("api_key")
        key, src = (web_only, SOURCE_WEB) if web_only else (None, SOURCE_NONE)
        return base, model, key, src
    env_key = _env_only(_DS_KEYS) or _env_only(_QWEN_KEYS)
    web_key = ov.get("api_key")
    if env_key:
        key, src = env_key, SOURCE_ENV
    elif web_key:
        key, src = web_key, SOURCE_WEB
    else:
        file_key = deepseek_key() or qwen_key()        # 含 Reasonix .env
        key, src = (file_key, SOURCE_ENV) if file_key else (None, SOURCE_NONE)
    return base, model, key, src


def text_config() -> tuple[str, str, str | None]:
    """文本 LLM 的 (base, model, key)。默认 DeepSeek。"""
    base, model, key, _ = _text_config()
    return base, model, key


def text_config_source() -> str:
    """文本 key 的来源（'环境变量' / '页面输入' / '未配置'），供设置页展示。"""
    return _text_config()[3]


def _vision_config() -> tuple[str, str, str | None, str]:
    """视觉的 (base, model, key, key 来源)。默认 DeepSeek；设 QWEN_VL_BASE_URL 可切 Qwen。"""
    ov = load_overrides()
    base = (os.environ.get("QWEN_VL_BASE_URL") or ov.get("vision_base_url")
            or DEEPSEEK_BASE)
    is_ds = "deepseek" in base
    model = (os.environ.get("QWEN_VL_MODEL") or ov.get("vision_model")
             or (VL_MODEL if is_ds else QWEN_VL_DEFAULT_MODEL))
    # key 候选顺序与原来一致，只是在「进程环境变量」与「.env 文件」之间插入了页面设置
    vl = _env_only(("QWEN_VL_API_KEY",))
    env_key = ((vl or _env_only(_DS_KEYS) or _env_only(_QWEN_KEYS)) if is_ds
               else (vl or _env_only(_QWEN_KEYS) or _env_only(_DS_KEYS)))
    web_key = ((ov.get("api_key") or ov.get("vision_api_key")) if is_ds
               else (ov.get("vision_api_key") or ov.get("api_key")))
    if _ui_only():                                  # 平台：只用页面 Key
        key, src = (web_key, SOURCE_WEB) if web_key else (None, SOURCE_NONE)
        return base, model, key, src
    if env_key:
        return base, model, env_key, SOURCE_ENV
    if web_key:
        return base, model, web_key, SOURCE_WEB
    file_key = (deepseek_key() or qwen_key()) if is_ds else (qwen_key() or deepseek_key())
    return base, model, file_key, (SOURCE_ENV if file_key else SOURCE_NONE)


def vision_config() -> tuple[str, str, str | None]:
    """视觉的 (base, model, key)。默认 DeepSeek；设 QWEN_VL_BASE_URL 可切 Qwen。"""
    base, model, key, _ = _vision_config()
    return base, model, key


def vision_config_source() -> str:
    """视觉 key 的来源（'环境变量' / '页面输入' / '未配置'），供设置页展示。"""
    return _vision_config()[3]
