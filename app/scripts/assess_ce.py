"""评估 C 类、E 类对提取率得分的影响与可救性。"""
import json
import os

ROOT = "out/full"
QUOTE = ("报价", "一览", "分项", "标的", "开标")
OFFICE = {".doc", ".docx", ".docm", ".wps", ".rtf"}
IMG = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
F7 = ["item_name", "category", "brand", "spec", "unit_price", "quantity", "total_price"]


def rate(k):
    return 10 if k == 7 else 5 if k >= 5 else 3 if k >= 3 else 0


dirs = sorted(d for d in os.listdir(ROOT) if os.path.isdir(os.path.join(ROOT, d)))
C, E = [], []
c_reason = {"scanned": 0, "office": 0, "image": 0, "arc": 0, "stub": 0}
for aid in dirs:
    try:
        rec = json.load(open(os.path.join(ROOT, aid, "ir.json"), encoding="utf-8"))
        items = json.load(open(os.path.join(ROOT, aid, "items.json"), encoding="utf-8"))["items"]
    except Exception:
        continue
    files = rec.get("extracted_files", [])
    q = [a for a in files if any(k in (a.get("rel_path") or "") for k in QUOTE)]
    if not q:
        continue
    with_tbl = [a for a in q if a.get("tables")]
    has_att = any((it.get("source") or "").startswith("ATTACHMENT") for it in items)
    if with_tbl and not has_att:
        E.append((aid, items))
    elif not with_tbl:
        C.append((aid, items))
        a = q[0]; note = a.get("note") or ""; ext = (a.get("ext") or "").lower(); size = a.get("size") or 0
        if "not auto-extracted" in note:
            c_reason["arc"] += 1
        elif "scanned_suspect=True" in note:
            c_reason["scanned"] += 1
        elif ext in OFFICE:
            c_reason["office"] += 1
        elif ext in IMG:
            c_reason["image"] += 1
        elif size and size < 5000:
            c_reason["stub"] += 1
        else:
            c_reason["stub"] += 1  # 空壳/其他


def summ(rows):
    n = len(rows)
    if not n:
        return n, 0, 0
    tot = 0
    up = 0
    for _, items in rows:
        present = {f for f in F7 if any(it.get(f) not in (None, "") for it in items)}
        k = len(present)
        tot += rate(k)
        up += 10 - rate(k)
    return n, round(tot / n, 2), up


nC, avgC, upC = summ(C)
nE, avgE, upE = summ(E)
print(f"C 篇数 {nC}  当前篇均分 {avgC}  若全升7字段可加 {upC} 分(样本口径)")
print(f"  C 原因: {c_reason}")
print(f"E 篇数 {nE}  当前篇均分 {avgE}  若全升7字段可加 {upE} 分(样本口径)")
# 占全量比例
print(f"\nC+E = {nC+nE} 篇 / 1038 = {(nC+nE)/1038:.1%}")
