"""导出验证集：算法预标注 -> 待核验表(review.csv) + gold 模板(gold/<id>.json)。

人工核验方式：编辑 gold/<id>.json 的 items（改对/删错/补漏即可）；review.csv 便于逐行看。
"""
import csv
import json
import os

ROOT = "out/full"
VDIR = "valset"
GOLD = os.path.join(VDIR, "gold")
ITEM = ["item_name", "category", "brand", "spec", "quantity", "unit", "unit_price", "total_price",
        "discount_rate", "price_unit", "package_no", "supplier_name"]

HOWTO = """人工核验说明
1) 预测来自算法（DeepSeek LLM），已按公告写入 gold/<id>.json 的 items 作为“算法标注”。
2) 请对照原文/附件核验并改正 gold/<id>.json：
   - 字段值错 -> 改成正确值；
   - 多出的条目 -> 整条删除；
   - 漏掉的条目 -> 补一条（字段名同上）；
   - 服务类无品牌/型号/数量/单价 -> 置 null（不算错）。
3) review.csv 是同一批数据的“逐行视图”，可用于对照，不必回填。
   - pricing_mode/ discount_rate 列：标注报价方式（下浮率/费率/折扣率…）。率类项目的 unit_price/total_price 为空、discount_rate 为率值（如 6.5），**这是正确的**，不要当漏提。
4) 核验后跑：uv run python scripts/val_eval.py  -> 输出 准确率/精确率/召回率 + 赛题综合公式。
"""


def main():
    ids = [ln.strip() for ln in open(os.path.join(VDIR, "valset_ids.txt"), encoding="utf-8") if ln.strip()]
    strat = {r["id"]: r["stratum"] for r in csv.DictReader(open(os.path.join(VDIR, "valset.csv"), encoding="utf-8-sig"))}
    os.makedirs(GOLD, exist_ok=True)
    PRED = os.path.join(VDIR, "pred")
    os.makedirs(PRED, exist_ok=True)
    rows = []
    made = seeded = 0
    for aid in ids:
        p = os.path.join(ROOT, aid, "items_llm.json")
        if not os.path.exists(p):
            continue
        ext = json.load(open(p, encoding="utf-8"))
        items, ann = ext["items"], ext["announcement"]
        pred = {"announcement_id": aid, "pricing_mode": ann.get("pricing_mode"),
                "items": [{k: it.get(k) for k in ITEM} for it in items]}
        json.dump(pred, open(os.path.join(PRED, aid + ".json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)          # 预测快照（可覆盖）
        made += 1
        gpath = os.path.join(GOLD, aid + ".json")
        if os.environ.get("VALSET_SEED", "").lower() in ("1", "true", "yes") and not os.path.exists(gpath):
            # 仅当显式 VALSET_SEED=1 时才从预测 seed gold（默认不 seed，避免覆盖/污染人工核验）
            json.dump(pred, open(gpath, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            seeded += 1
        for it in items:
            row = {"announcement_id": aid, "stratum": strat.get(aid, ""),
                   "project_name": ann.get("project_name", ""), "pricing_mode": ann.get("pricing_mode", "")}
            for k in ITEM:
                row[k] = it.get(k)
            for k in ITEM:
                row["g_" + k] = ""  # gold 列（留空=接受预测）
            row["verdict"] = ""     # ok/fix/del
            rows.append(row)
    cols = ["announcement_id", "stratum", "project_name", "pricing_mode"] + ITEM + ["g_" + k for k in ITEM] + ["verdict"]
    with open(os.path.join(VDIR, "review.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(GOLD, "_howto.txt"), "w", encoding="utf-8") as f:
        f.write(HOWTO)
    n_gold = len([f for f in os.listdir(GOLD) if f.endswith(".json")])
    print(f"预测快照 {made} 篇 -> {VDIR}/pred/ | gold {n_gold} 篇（本次未改动）| "
          f"待核验表 {len(rows)} 行 -> {VDIR}/review.csv")


if __name__ == "__main__":
    main()
