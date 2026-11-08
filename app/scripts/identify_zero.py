import json
import os

F7 = ["item_name", "category", "brand", "spec", "unit_price", "quantity", "total_price"]
IMG = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
OFFICE = {".doc", ".docx", ".pdf", ".xls", ".xlsx", ".wps"}

ids = [ln.strip() for ln in open("all_ids.txt", encoding="utf-8") if ln.strip()]
zero = []
for aid in ids:
    ip = f"out/full/{aid}/items.json"
    if not os.path.exists(ip):
        continue
    ext = json.load(open(ip, encoding="utf-8"))
    items = ext["items"]
    present = {f for f in F7 if any(it.get(f) not in (None, "") for it in items)}
    if len(present) == 0:
        zero.append(aid)

print("0 字段篇数:", len(zero))
no_att = scan = office = img = has_text = 0
prof = []
for aid in zero:
    r = json.load(open(f"out/full/{aid}/ir.json", encoding="utf-8"))
    fs = r.get("extracted_files", [])
    exts = {((a.get("ext") or "").lower()) for a in fs}
    notes = " ".join((a.get("note") or "") for a in fs)
    n_tables = sum(len(a.get("tables") or []) for a in fs)
    ct = r.get("content_text") or ""
    money = ("万" in ct or "元" in ct)
    if not fs:
        no_att += 1
    if any(e in IMG for e in exts):
        img += 1
    if any(e in OFFICE for e in exts):
        office += 1
    if "scanned" in notes.lower() or "scan" in notes.lower():
        scan += 1
    if money:
        has_text += 1
    prof.append((aid, len(fs), sorted(exts), n_tables, money, "scan" in notes.lower()))

print("无任何附件:", no_att, "| 含图片:", img, "| 含office/pdf:", office, "| 含扫描件标记:", scan, "| 正文含金额字样:", has_text)
print("\n前 40 篇画像 (id, 附件数, 扩展名, 表数, 正文含金额, 扫描):")
for x in prof[:40]:
    print("  ", x)

with open("scripts/_zero_ids.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(zero))
print("\n已写 scripts/_zero_ids.txt")
