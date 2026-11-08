"""把已有抽取结果里的数值字段原地规范化（整数值 -> int），无需重调 LLM。

背景：`norm_money` 原先一律返回 float，JSON 里写成 `264800.0`；
而人工 gold/官方数据表写 `264800` → 同一个数值被判成 FP+FN。
本脚本对**已生成的结果**做同样规范化，避免为了格式重跑一遍 LLM。

用法: uv run python scripts/normalize_results.py [--out out/full] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ict_parse.extract import _canon_num  # noqa: E402

NUMF = ("quantity", "unit_price", "total_price", "discount_rate")
FILES = ("items_llm.json", "items.json")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/full")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ids = [ln.strip() for ln in open("all_ids.txt", encoding="utf-8") if ln.strip()]
    changed_files = changed_vals = 0
    for aid in ids:
        for fn in FILES:
            p = os.path.join(args.out, aid, fn)
            if not os.path.exists(p):
                continue
            ext = json.load(open(p, encoding="utf-8"))
            hit = 0
            for it in ext.get("items") or []:
                # 0 元不是有效价格：「中标金额：0【按实际发生结算】」应置空（官方口径②）
                for f in ("unit_price", "total_price"):
                    if it.get(f) == 0:
                        it[f] = None
                        hit += 1
                for f in NUMF:
                    v = it.get(f)
                    if not isinstance(v, float):
                        continue
                    nv = _canon_num(v)
                    # 注意：不能用 `nv != v` 判断——Python 里 857000 != 857000.0 是 False，
                    # 整型化会因此静默失效。整数结果一律写回；小数只在四舍五入确有变化时写回。
                    if isinstance(nv, int) or nv != v:
                        it[f] = nv
                        hit += 1
            if hit:
                changed_files += 1
                changed_vals += hit
                if not args.dry_run:
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(ext, f, ensure_ascii=False, indent=2, default=str)
    print(f"规范化 {'(dry-run) ' if args.dry_run else ''}文件 {changed_files} 个 / 数值 {changed_vals} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
