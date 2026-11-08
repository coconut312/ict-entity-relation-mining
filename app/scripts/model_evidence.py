"""采集模型调用证据（用于合规声明）：模型名/响应id/trace/耗时。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from openai import OpenAI  # noqa: E402
from ict_parse import config  # noqa: E402

cfgs = {"文本抽取": config.text_config(), "视觉": config.vision_config()}
for label, (base, model, key) in cfgs.items():
    if not key:
        print(label, "无 key")
        continue
    c = OpenAI(**{"api_key": key, "base_url": base, "timeout": 60})
    t = time.time()
    r = c.chat.completions.create(model=model, messages=[{"role": "user", "content": "只回复:ok"}])
    dt = round(time.time() - t, 2)
    trace = ""
    try:
        h = r.response.headers
        trace = h.get("x-ds-trace-id") or h.get("X-Ds-Trace-Id") or ""
    except Exception:  # noqa: BLE001
        pass
    print(f"{label}: model={model} base={base} latency={dt}s resp_id={r.id} trace={trace}")
