"""生成 E 类（有报价表且有表格、但整篇无 ATTACHMENT item）的 id 列表 -> e_ids.txt"""
import json
import os

ROOT = "out/full"
QUOTE = ("报价", "一览", "分项", "标的", "开标")
dirs = sorted(d for d in os.listdir(ROOT) if os.path.isdir(os.path.join(ROOT, d)))
E = []
for aid in dirs:
    try:
        rec = json.load(open(os.path.join(ROOT, aid, "ir.json"), encoding="utf-8"))
        items = json.load(open(os.path.join(ROOT, aid, "items.json"), encoding="utf-8"))["items"]
    except Exception:
        continue
    q = [a for a in rec.get("extracted_files", []) if any(k in (a.get("rel_path") or "") for k in QUOTE)]
    if not q:
        continue
    with_tbl = [a for a in q if a.get("tables")]
    has_att = any((it.get("source") or "").startswith("ATTACHMENT") for it in items)
    if with_tbl and not has_att:
        E.append(aid)

with open("e_ids.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(E))
print(f"E 类 {len(E)} 篇 -> e_ids.txt")
