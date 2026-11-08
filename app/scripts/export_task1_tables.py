"""导出任务一交付表：全量 LLM 抽取结果 -> deliverables/（受版本控制，可直接打包提交）。

赛题六、6（2）要求提交「官方数据集的提取结果，包括任务一提取的数据表」。
落盘位置刻意放在 deliverables/（而不是被 .gitignore 排除的 out/），便于随源码提交。

用法:
    uv run python scripts/export_task1_tables.py
    uv run python scripts/export_task1_tables.py --src rule     # 导出规则层结果做对照
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ict_parse.clean import (clean_brand, clean_purchaser,  # noqa: E402
                             derive_fields, normalize_item_fields)
# 标的行噪音过滤（**唯一实现**在 ict_parse/noise.py）：把《中小企业声明函》的行业分类词、
# 表格行标签/字段名（`总价:`/`投标人名称`/`总下浮率：（…）`）、纯标点（`……`）、
# 全空行、与项目名同名的标题行从**出口**判掉。后置过滤，不重跑 LLM（0 次调用）。
from ict_parse.noise import (NoiseReport, enabled as noise_enabled,  # noqa: E402
                             filter_items, format_report)

ROOT = "out/full"
OUTDIR = "deliverables"
F7 = ["item_name", "category", "brand", "spec", "unit_price", "quantity", "total_price"]

ITEM_COLS = ["announcement_id", "package_no", "supplier_name", "is_winner", "item_name",
             "category", "category_code", "item_type", "brand", "spec", "quantity", "unit",
             "unit_price", "total_price", "pricing_mode", "discount_rate", "price_unit",
             "derived", "needs_review", "source", "engine"]
ANN_COLS = ["announcement_id", "notice_type", "project_name", "project_code", "purchaser",
            "region", "publish_time", "project_amount", "pricing_mode", "n_winners", "n_bidders",
            "n_items", "engine"]


def _count_names(v):
    """winners/bidders -> 条数。

    抽取层正常给的是**名字列表**（`announcement.winners = ['甲','乙']`），条数 = 列表长度。
    但存量/历史结果里有三种异常形态，直接 `len()` 会静默算错（曾把单个名字当字符串数成
    "字符数"，或把 None 当 0）：
      * `None` / 缺键            -> 0
      * 单个名字字符串 `'甲公司'` -> 1（不是 len=3）
      * 带空值的列表              -> 只数非空项
    """
    if v is None:
        return 0
    if isinstance(v, str):
        return 1 if v.strip() else 0
    try:
        return sum(1 for x in v if str(x or "").strip())
    except TypeError:
        return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", choices=["llm", "rule"], default="llm",
                    help="llm=items_llm.json(交付用)；rule=items.json(对照用)")
    ap.add_argument("--out", default=ROOT)
    args = ap.parse_args()

    fname = "items_llm.json" if args.src == "llm" else "items.json"
    suffix = "" if args.src == "llm" else "_rule"
    os.makedirs(OUTDIR, exist_ok=True)
    ids = [ln.strip() for ln in open("all_ids.txt", encoding="utf-8") if ln.strip()]

    item_rows, ann_rows = [], []
    engines: dict[str, int] = {}
    n_missing = 0
    noise_rep = NoiseReport()          # 逐条命中统计（每规则命中数 + 样例 5 条）
    for aid in ids:
        p = os.path.join(args.out, aid, fname)
        if not os.path.exists(p):
            n_missing += 1
            continue
        ext = json.load(open(p, encoding="utf-8"))
        ann, items = ext["announcement"], ext.get("items") or []
        eng = str(ext.get("engine") or "")
        engines[eng.split("(")[0]] = engines.get(eng.split("(")[0], 0) + 1
        # 交付表（llm 层）在**导出时就清洗**：品牌多值串取首个、采购单位代码式值置空。
        # 规则层（rule）保持原样 —— 它是 rule-vs-LLM 对照的基线，清洗会改变已引用的 0.885 结论。
        cleaning = args.src == "llm"
        purchaser = clean_purchaser(ann.get("purchaser")) if cleaning else ann.get("purchaser")
        # ---- 标的行噪音过滤（唯一实现 = ict_parse/noise.py；先过滤再写表）----
        # 两层都过滤（同一套判据），规则/交付层的差异只在清洗，不在噪音判据 ——
        # 否则 items_task1_rule.csv 仍会带 `工业`/`总价:` 之类的行，对照结论会被噪音污染。
        items = filter_items(items, project_name=ann.get("project_name") or "",
                             purchaser=ann.get("purchaser") or "",
                             report=noise_rep, aid=aid)
        ann_rows.append({
            "announcement_id": aid, "notice_type": ann.get("notice_type"),
            "project_name": ann.get("project_name"), "project_code": ann.get("project_code"),
            "purchaser": purchaser, "region": ann.get("region"),
            "publish_time": ann.get("publish_time"), "project_amount": ann.get("project_amount"),
            "pricing_mode": ann.get("pricing_mode"),
            "n_winners": _count_names(ann.get("winners")),
            "n_bidders": _count_names(ann.get("bidders")),
            "n_items": len(items), "engine": eng,
        })
        for it in items:
            r = {"announcement_id": aid, "pricing_mode": ann.get("pricing_mode"), "engine": eng}
            r.update({c: it.get(c) for c in ITEM_COLS if c not in ("announcement_id", "pricing_mode", "engine")})
            if cleaning:
                r["brand"] = clean_brand(r.get("brand"))
                # 输入侧归一：去标的编号/项目符号、分类去尾部「类」
                normalize_item_fields(r)
                # 《官方回答》口径：能推定的必须计算填入（数量=总价/单价 等），并标 derived=True
                derive_fields(r)
            item_rows.append({c: r.get(c) for c in ITEM_COLS})

    ip = os.path.join(OUTDIR, f"items_task1{suffix}.csv")
    ap_ = os.path.join(OUTDIR, f"announcements_task1{suffix}.csv")
    with open(ip, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ITEM_COLS)
        w.writeheader()
        w.writerows(item_rows)
    with open(ap_, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ANN_COLS)
        w.writeheader()
        w.writerows(ann_rows)

    n = len(item_rows)
    nn = {f: sum(1 for r in item_rows if r.get(f) not in (None, "")) for f in F7 + ["supplier_name"]}
    nw = sum(int(r["n_winners"] or 0) for r in ann_rows)
    nb = sum(int(r["n_bidders"] or 0) for r in ann_rows)
    print(f"来源 {fname} | engine 分布: {engines} | 缺 {n_missing} 篇")
    print(format_report(noise_rep, title=f"标的行噪音过滤（导出 {fname}）",
                        show_samples=5))
    print(f"明细表 -> {ip}（{n} 行 / {len(ann_rows)} 篇）")
    print(f"公告表 -> {ap_}（{len(ann_rows)} 篇）")
    print(f"  中标方: n_winners>0 的公告 {sum(1 for r in ann_rows if int(r['n_winners'] or 0) > 0)} 篇"
          f" / Σn_winners = {nw} / Σn_bidders = {nb}")
    print("字段非空率:", {f: (round(v / n, 3) if n else 0) for f, v in nn.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
