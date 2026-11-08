import os
from openai import OpenAI

c = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com/v1")
for m in ["deepseek-chat", "deepseek-v4-pro", "deepseek-flash"]:
    try:
        r = c.chat.completions.create(
            model=m, messages=[{"role": "user", "content": "只回复:ok"}], max_tokens=5, temperature=0
        )
        served = getattr(r, "model", None)
        print(f"[OK] {m:16s} -> {r.choices[0].message.content!r}  served_model={served}")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {m:16s} -> {type(e).__name__}: {str(e)[:140]}")
