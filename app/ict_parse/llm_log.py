# -*- coding: utf-8 -*-
"""LLM 调用日志（合规留痕）。

赛题 p11 与第六章要求声明「所用大模型的版本、参数规模」，评审也需可核验的调用证据。
本项目此前**没有任何调用日志**（`items_llm.json` 只存 announcement/items/engine），
本模块把每次调用的关键字段追加写入 JSONL，供合规声明与复核：

    app/out/llm_calls.jsonl   每行一条：
      ts            调用时间（本地时区 ISO）
      role          'text' | 'vision'
      base / model  实际使用的接口地址与模型名
      latency_ms    耗时
      ok / err      成功与否与错误摘要
      resp_id       响应 id（服务端返回，可用于向模型方对账）
      trace_id      服务商 trace 头（如 x-ds-trace-id）
      prompt_tokens / completion_tokens / total_tokens（若返回 usage）
      chars_in / chars_out   输入/输出字符数（无 usage 时的规模证据）
      attempt       应用层重试序号

日志路径可用 `LLM_LOG_PATH` 覆盖；写失败**绝不**影响抽取主流程（best-effort）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

_LOCK = threading.Lock()

_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "out", "llm_calls.jsonl")


def log_path() -> str:
    return os.environ.get("LLM_LOG_PATH") or _DEFAULT


def log_call(role: str, base: str, model: str, t0: float, ok: bool,
             resp=None, err: str = "", chars_in: int = 0, chars_out: int = 0,
             attempt: int = 0) -> None:
    """追加一条调用记录（best-effort：任何异常都吞掉，不影响抽取）。"""
    try:
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "role": role,
            "base": base,
            "model": model,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "ok": bool(ok),
            "err": (err or "")[:200],
            "chars_in": chars_in,
            "chars_out": chars_out,
            "attempt": attempt,
        }
        if resp is not None:
            rec["resp_id"] = getattr(resp, "id", "") or ""
            usage = getattr(resp, "usage", None)
            if usage is not None:
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    v = getattr(usage, k, None)
                    if v is not None:
                        rec[k] = v
            try:
                h = resp.response.headers  # type: ignore[attr-defined]
                rec["trace_id"] = (h.get("x-ds-trace-id") or h.get("X-Ds-Trace-Id") or "")
            except Exception:  # noqa: BLE001
                pass
        p = log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with _LOCK:
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def summarize() -> dict:
    """汇总日志：按 role/model 统计调用次数、成功率、延迟分位、token 合计。"""
    p = log_path()
    out: dict = {"path": p, "exists": os.path.exists(p), "total": 0, "by_model": {}}
    if not out["exists"]:
        return out
    agg: dict = {}
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            out["total"] += 1
            key = f'{r.get("role")}/{r.get("model")}'
            a = agg.setdefault(key, {"n": 0, "ok": 0, "lat": [], "tokens": 0})
            a["n"] += 1
            a["ok"] += 1 if r.get("ok") else 0
            a["lat"].append(r.get("latency_ms") or 0)
            a["tokens"] += r.get("total_tokens") or 0
    for k, a in agg.items():
        lat = sorted(a["lat"])
        out["by_model"][k] = {
            "calls": a["n"], "ok": a["ok"],
            "success_rate": round(a["ok"] / a["n"], 4) if a["n"] else 0,
            "latency_ms_p50": lat[len(lat) // 2] if lat else 0,
            "latency_ms_max": lat[-1] if lat else 0,
            "total_tokens": a["tokens"],
        }
    return out
