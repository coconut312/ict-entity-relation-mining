# -*- coding: utf-8 -*-
"""生成模型合规声明 model_manifest.json（赛题第五章 p11 + 第六章「声明模型版本与参数规模」）。

用法:
    uv run python scripts/model_manifest.py            # 生成声明
    uv run python scripts/model_manifest.py --probe    # 顺便向接口核验模型 id 是否存在

设计原则：**只写可核验的事实**，核验不了的一律标注
`verified: false` 与原因，绝不替模型方编造版本号/参数规模。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ict_parse import config  # noqa: E402
from ict_parse.llm_log import log_path, summarize  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "deliverables", "model_manifest.json")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rel_path(p: str) -> str:
    """交付件里的路径一律写成**相对仓库根**（`app/out/llm_calls.jsonl`）。

    绝对路径会把开发机的用户名/目录带进交付包（也会被路径扫描命中），
    而日志位置本来就是「仓库内固定相对路径 + LLM_LOG_PATH 可覆盖」。
    """
    try:
        return os.path.relpath(p, APP).replace("\\", "/")
    except ValueError:
        return p

# 已知公开信息的诚实标注（不臆测；核验不到就写未公开）
KNOWN = {
    "qwen3-vl-flash": {"family": "Qwen（阿里通义千问）", "scale": "未公开（Flash 轻量档）",
                       "origin": "开源国产大模型系列"},
    "deepseek-flash": {"family": "DeepSeek（深度求索）", "scale": "未公开",
                       "origin": "开源国产大模型系列，经 OpenAI 兼容接口调用"},
}


def describe(label: str, cfg: tuple, role: str, probe: bool) -> dict:
    base, model, key = cfg
    info = KNOWN.get(model, {})
    d = {
        "role": role,
        "label": label,
        "base_url": base,
        "model_id": model,
        "model_family": info.get("family", "未识别"),
        "parameter_scale": info.get("scale", "未公开"),
        "origin": info.get("origin", "未识别"),
        "api_style": "OpenAI 兼容 /chat/completions（temperature=0，response_format=json_object）",
        "credential_source": ("环境变量 LLM_API_KEY/DEEPSEEK_API_KEY 等"
                              if key else "未配置"),
        "model_id_verified": False,
        "verify_note": "未核验：模型 id 以调用方环境变量为准，本项目不假定其官方公开目录身份",
    }
    if probe and key:
        try:
            from openai import OpenAI
            c = OpenAI(api_key=key, base_url=base, timeout=30)
            ids = [m.id for m in c.models.list().data]
            d["model_id_verified"] = model in ids
            d["served_models_count"] = len(ids)
            d["served_models_sample"] = ids[:12]
            d["verify_note"] = ("已核验：/v1/models 返回该 id" if d["model_id_verified"]
                                else "该 id **不在** /v1/models 返回列表中（可能为第三方中转或别名）")
        except Exception as e:  # noqa: BLE001
            d["verify_note"] = f"核验失败（{type(e).__name__}: {str(e)[:100]}）"
    return d


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="向接口核验模型 id 是否被服务端提供")
    a = ap.parse_args()

    roles = [describe("文本实体抽取（主力）", config.text_config(), "text", a.probe),
             describe("扫描件/图片→文本（视觉）", config.vision_config(), "vision", a.probe)]
    log_stats = summarize()
    man = {
        "生成时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "用途声明": {
            "文本实体抽取": "从公告原文与附件文本中抽取公告级/标的级字段（item_name/category/"
                        "brand/spec/unit_price/quantity/total_price 等）",
            "附件解析": "PDF/docx/xlsx 文本抽取；扫描件走视觉模型（本批数据未启用）",
            "关系建模": "任务二图模型由抽取结果确定性构建（无模型参与）",
        },
        "合规要点": [
            "使用 Qwen/DeepSeek 系列开源国产大模型（赛题 p2/p4 要求）",
            "通过 OpenAI 兼容接口调用；接口地址与模型 id 均由环境变量决定，可切至赛方提供的云环境与 API Key",
            "全部推理参数固定 temperature=0、response_format=json_object，保证可复现",
            "每次调用写调用日志（model / resp_id / trace_id / 耗时 / token），供评审核验",
        ],
        "环境变量映射": {
            "文本": {"LLM_BASE_URL": "接口地址", "LLM_MODEL": "模型 id", "LLM_API_KEY": "密钥"},
            "视觉": {"QWEN_VL_BASE_URL": "接口地址", "QWEN_VL_MODEL": "模型 id",
                     "QWEN_VL_API_KEY": "密钥"},
            "其他": {"LLM_MAX_TOKENS": "单次上限（默认 8192）",
                     "LLM_LOG_PATH": f"调用日志路径（默认 {_rel_path(log_path())}，相对仓库根）"},
        },
        "模型与参数规模声明": roles,
        "调用日志": {
            "path": _rel_path(log_stats["path"]),
            "exists": log_stats["exists"],
            "calls_total": log_stats["total"],
            "by_model": log_stats["by_model"],
            "fields": ["ts", "role", "base", "model", "latency_ms", "ok", "err",
                       "resp_id", "trace_id", "prompt_tokens", "completion_tokens",
                       "total_tokens", "chars_in", "chars_out", "attempt"],
            "note": "历史全量抽取（1038 篇）在引入日志之前完成，故日志仅覆盖引入之后的重跑；"
                    "如需全量留痕可重跑：uv run python scripts/run_llm.py（FORCE=1）",
        },
        "待补事项": [
            "若赛方要求严格对应其提供的模型资源，请把 LLM_BASE_URL/LLM_MODEL/LLM_API_KEY 指向赛方云环境后重跑，并在本文件记录实际 id",
            "参数规模：本次所用模型 id 的官方参数规模未公开，故标注为未公开，不作推测",
        ],
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    print("已生成:", OUT)
    for r in roles:
        print("  %-22s model=%-18s base=%s" % (r["label"], r["model_id"], r["base_url"]))
        print("      核验: %s" % r["verify_note"])
    print("  调用日志: %s（存在=%s，已记录 %d 次）"
          % (_rel_path(log_stats["path"]), log_stats["exists"], log_stats["total"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
