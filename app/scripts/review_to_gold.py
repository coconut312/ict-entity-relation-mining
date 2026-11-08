"""把用户核验过的 review.csv（前 400 行对应公告）导出为 gold/<id>.json；先删原 gold 里的 json。"""
import csv
import json
import os

V = "valset"
G = os.path.join(V, "gold")
ITEM = ["item_name", "category", "brand", "spec", "quantity", "unit", "unit_price", "total_price",
        "discount_rate", "price_unit", "package_no", "supplier_name"]

rows = list(csv.DictReader(open(os.path.join(V, "review.csv"), encoding="utf-8-sig")))
first = rows[:400]
ids, seen = [], set()
for r in first:
    a = r["announcement_id"]
    if a not in seen:
        seen.add(a)
        ids.append(a)

byid = {}
for r in rows:
    byid.setdefault(r["announcement_id"], []).append(r)

os.makedirs(G, exist_ok=True)
deleted = 0
for f in os.listdir(G):
    if f.endswith(".json"):
        os.remove(os.path.join(G, f))
        deleted += 1

made = 0
for aid in ids:
    items = [{k: (r.get(k) or None) for k in ITEM} for r in byid[aid]]
    pm = byid[aid][0].get("pricing_mode") if byid[aid] else None
    json.dump({"announcement_id": aid, "pricing_mode": pm, "items": items},
              open(os.path.join(G, aid + ".json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    made += 1

print(f"删除旧 gold json: {deleted}")
print(f"导出 gold: {made} 篇（前 400 行涉及 {len(ids)} 个公告）")
print("ids 示例:", ids[:8])
