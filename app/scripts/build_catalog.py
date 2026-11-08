"""把《政府采购品目分类目录.pdf》解析成结构化表：code,name,desc,level,path。"""
import csv
import os
import re
from pathlib import Path

import pdfplumber

SRC = (Path(os.environ.get("ICT_WS") or str(Path(__file__).resolve().parents[2]))
       / "政府采购品目分类目录.pdf")
OUT = Path(__file__).resolve().parent.parent / "data" / "gov_category.csv"

CODE_RE = re.compile(r"^[ABC][0-9]{0,12}$")


def parse():
    recs = {}  # code -> (name, desc)
    with pdfplumber.open(str(SRC)) as pdf:
        for page in pdf.pages:
            for t in page.extract_tables():
                for row in t:
                    if not row or not row[0]:
                        continue
                    code = re.sub(r"\s+", "", row[0])
                    if not CODE_RE.match(code):
                        continue
                    name = re.sub(r"\s+", "", row[1] or "")
                    desc = re.sub(r"\s+", "", row[2] or "") if len(row) > 2 else ""
                    if not name:
                        continue
                    recs.setdefault(code, (name, desc))
    return recs


def level_of(code):
    return len(code) // 2  # A=0, A01=1, A0101=2, A010101=3, ...


def build():
    recs = parse()
    codes = sorted(recs, key=len)  # 短的先算，便于建路径
    path = {}
    for code in codes:
        parents = [c for c in recs if len(c) < len(code) and code.startswith(c)]
        parent = max(parents, key=len) if parents else None
        nm = recs[code][0]
        path[code] = (path.get(parent, "") + "/" + nm) if parent else nm
    rows = [
        {"code": c, "name": recs[c][0], "desc": recs[c][1], "level": level_of(c), "path": path[c]}
        for c in sorted(recs, key=lambda x: (x[0], len(x)))
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["code", "name", "desc", "level", "path"])
        w.writeheader()
        w.writerows(rows)
    print(f"records: {len(rows)}  -> {OUT}")
    print("level 分布:", {lv: sum(1 for r in rows if r['level'] == lv) for lv in sorted({r['level'] for r in rows})})
    print("示例:")
    for r in rows[:2] + [x for x in rows if x["name"] in ("服务器", "防火墙", "台式计算机", "交换设备")][:4]:
        print("  ", r["code"], r["name"], "|", r["path"])


if __name__ == "__main__":
    build()
