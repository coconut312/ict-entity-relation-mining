"""基于已生成的 ir.json 重跑规则抽取层（不重解压附件，快）。"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ict_parse import extract  # noqa: E402

ROOT = os.environ.get("OUT", "out/sample50")
IDS = [ln.strip() for ln in open(os.environ.get("IDS", "sample50.txt"), encoding="utf-8") if ln.strip()]
COLS = ["item_name", "category", "category_code", "brand", "spec", "quantity", "unit",
        "unit_price", "total_price", "supplier_name", "is_winner", "package_no", "item_type",
        "source", "needs_review", "derived"]

n = 0
rows = []
ann_rows = []
F7 = ["item_name", "category", "brand", "spec", "unit_price", "quantity", "total_price"]
dist = {}
for aid in IDS:
    ir = os.path.join(ROOT, aid, "ir.json")
    if not os.path.exists(ir):
        continue
    rec = json.load(open(ir, encoding="utf-8"))
    ext = extract.build(rec)
    with open(os.path.join(ROOT, aid, "items.json"), "w", encoding="utf-8") as f:
        json.dump(ext, f, ensure_ascii=False, indent=2, default=str)
    n += 1
    items = ext["items"]
    for it in items:
        r = {"announcement_id": aid}
        r.update({c: it.get(c) for c in COLS})
        rows.append(r)
    present = {f for f in F7 if any(it.get(f) not in (None, "") for it in items)}
    dist[len(present)] = dist.get(len(present), 0) + 1
    ann_rows.append({"id": aid, "n_items": len(items), "n_winners": len(ext["announcement"]["winners"]),
                     "n_bidders": len(ext["announcement"]["bidders"])})

# 聚合 CSV（对占用做容错）
def save(path, fieldnames, data):
    try:
        f = open(path, "w", encoding="utf-8-sig", newline="")
    except PermissionError:
        path = path.replace(".csv", "_new.csv")
        f = open(path, "w", encoding="utf-8-sig", newline="")
    w = csv.DictWriter(f, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(data)
    f.close()
    return path


p1 = save(os.path.join(ROOT, "items.csv"), ["announcement_id"] + COLS, rows)
p2 = save(os.path.join(ROOT, "announcements.csv"), ["id", "n_items", "n_winners", "n_bidders"], ann_rows)
m = len(rows)
nn = {f: sum(1 for r in rows if r.get(f) not in (None, "")) for f in F7 + ["supplier_name"]}
print(f"已对 {n} 篇重跑抽取")
print(f"明细 -> {p1} ({m} 行)；公告 -> {p2} ({len(ann_rows)} 篇)")
print("明细字段非空率:", {f: (round(v / m, 3) if m else 0) for f, v in nn.items()})
print("篇级 7 字段达成(字段数:篇数):", dict(sorted(dist.items())))
