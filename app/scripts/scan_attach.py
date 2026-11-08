import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ict_parse.extract import _nh

KW = ("报价", "一览", "明细", "标的", "开标", "成交")
PRICE_KW = ("单价", "总价", "合价", "金额", "报价", "响应报价", "价")

used = skipped = 0
skip_ex = []
scanned = 0
for d in os.listdir("out/full"):
    p = f"out/full/{d}/ir.json"
    if not os.path.exists(p):
        continue
    r = json.load(open(p, encoding="utf-8"))
    for a in r.get("extracted_files", []):
        tbls = a.get("tables") or []
        if not tbls:
            continue
        nm = a.get("rel_path") or ""
        has_price = False
        for t in tbls:
            if not t:
                continue
            for h in t[0]:
                if h and any(k in _nh(h) for k in PRICE_KW):
                    has_price = True
                    break
            if has_price:
                break
        if not has_price:
            continue
        scanned += 1
        if any(k in nm for k in KW):
            used += 1
        else:
            skipped += 1
            if len(skip_ex) < 12:
                skip_ex.append((d, nm.split("\\")[-1][:46]))
print("附件表格含价格列的文件数:", scanned)
print("  被采用(文件名含报价/明细/一览/标的/开标/成交):", used)
print("  被跳过(文件名不含关键字):", skipped)
print("  跳过示例:")
for x in skip_ex:
    print("    ", x[0], "|", x[1])
