import json
import os
import sys

ids = ["t20260311_26261128", "t20260408_26371938", "t20260506_26515417",
       "t20260707_26885504", "t20260709_26907667", "t20260710_26916870",
       "t20260810_27107304"]
Q = ("报价", "一览", "明细", "标的", "开标", "成交")
for aid in ids:
    p = f"out/full/{aid}/ir.json"
    if not os.path.exists(p):
        print(aid, "无 ir.json")
        continue
    r = json.load(open(p, encoding="utf-8"))
    print("=" * 80)
    print(aid, "|", (r.get("article_title") or "")[:50])
    print("  总中标金额:", r.get("summary", {}).get("总中标金额"))
    for i, g in enumerate(r.get("main_item_tables", [])):
        if g.get("headers"):
            print(f"  原文表{i} 表头: {' | '.join(g['headers'])[:160]}")
            for row in g["rows"][:2]:
                print("     ", " | ".join((c or "") for c in row)[:180])
    for a in r.get("extracted_files", []):
        nm = (a.get("rel_path") or "").split("\\")[-1]
        if a.get("tables") and any(k in nm for k in Q):
            for t in a["tables"][:1]:
                if t:
                    print(f"  附件表[{nm[:34]}] 表头: {' | '.join(t[0])[:180]}")
                    for row in t[1:3]:
                        print("     ", " | ".join((c or "") for c in row)[:180])
    try:
        e = json.load(open(f"out/full/{aid}/items.json", encoding="utf-8"))
        print("  RULE items:", [(it.get("item_name"), it.get("unit_price"), it.get("total_price")) for it in e["items"][:4]])
    except Exception as ex:
        print("  RULE items ERR", ex)
