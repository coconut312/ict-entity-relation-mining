"""视觉模型客户端（OpenAI 兼容；默认 DeepSeek deepseek-flash，可切 Qwen3-VL）。

环境变量：
    密钥（其一）: QWEN_API_KEY / DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / QWEN_VL_API_KEY / MaaS_API_KEY
    （切 Qwen 视觉：设 QWEN_VL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1、QWEN_VL_MODEL=qwen3-vl-flash）
    QWEN_VL_BASE_URL        默认 https://api.deepseek.com/v1
    QWEN_VL_MODEL           默认 deepseek-flash（可设 qwen3-vl-flash 等）
    QWEN_VL_THINKING        1/true 开启思考过程(enable_thinking)
    QWEN_VL_THINKING_BUDGET 默认 8192
    QWEN_VL_MAX_TOKENS      默认 4096
    QWEN_VL_TIMEOUT         默认 180s
"""
from __future__ import annotations

import base64
import os

from openai import OpenAI

from . import config

DEFAULT_BASE = config.DEEPSEEK_BASE
DEFAULT_MODEL = config.VL_MODEL

# 面向任务一的通用提示：把图还原成结构化表格文本
DEFAULT_PROMPT = (
    "把图片里的表格转成文本，只输出表格行，每行单元格用 | 分隔，第一行是表头；"
    "数值/单位/型号/品牌原样保留；不要输出任何说明、标题、markdown 或前后缀文字。"
)


def _key() -> str | None:
    return config.vision_config()[2]


def available() -> bool:
    return bool(_key())


def _client() -> OpenAI:
    key = _key()
    if not key:
        raise RuntimeError("未设置视觉 key（QWEN_API_KEY / DEEPSEEK_API_KEY / DASHSCOPE_API_KEY）")
    base = config.vision_config()[0]
    return OpenAI(api_key=key, base_url=base, timeout=float(os.environ.get("QWEN_VL_TIMEOUT", "180")))


def _img_part(path: str) -> dict:
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def images_to_text(image_paths: list[str], prompt: str = DEFAULT_PROMPT, model: str | None = None) -> str:
    """把一张或多张图片交给 Qwen3-VL-Flash，返回文本（仅最终回复，不含思考）。"""
    client = _client()
    model = model or config.vision_config()[1]
    content: list[dict] = [{"type": "text", "text": prompt}]
    content += [_img_part(p) for p in image_paths]
    kwargs: dict = {}
    if os.environ.get("QWEN_VL_THINKING", "").lower() in ("1", "true", "yes", "on"):
        kwargs["extra_body"] = {
            "enable_thinking": True,
            "thinking_budget": int(os.environ.get("QWEN_VL_THINKING_BUDGET", "8192")),
        }
    last = ""
    for attempt in range(3):
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            temperature=0,
            max_tokens=int(os.environ.get("QWEN_VL_MAX_TOKENS", "4096")),
            **kwargs,
        )
        last = resp.choices[0].message.content or ""
        if last.strip():
            return last
        import time as _t

        _t.sleep(0.6 * (attempt + 1))  # 空响应重试（deepseek 多图偶发）
    return last


def image_to_text(image_path: str, prompt: str = DEFAULT_PROMPT, model: str | None = None) -> str:
    return images_to_text([image_path], prompt, model)
