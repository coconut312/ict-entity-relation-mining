import json
import os
import shutil

K = ("报价", "明细", "标的", "一览", "开标")
hit = []
for d in os.listdir("out/full"):
    p = f"out/full/{d}/ir.json"
    if not os.path.exists(p) or not os.path.isdir(f"out/full/{d}"):
        continue
    r = json.load(open(p, encoding="utf-8"))
    for af in r.get("extracted_files", []):
        nm = af.get("rel_path") or ""
        if af.get("ext") == ".docx" and any(k in nm for k in K):
            hit.append(d)
            break

for d in hit:
    shutil.rmtree(f"out/full/{d}", ignore_errors=True)

with open("scripts/_reparse_ids.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(sorted(hit)) + "\n")
print("已删除待重解析:", len(hit), "篇")
