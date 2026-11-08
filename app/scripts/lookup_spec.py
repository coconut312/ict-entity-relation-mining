import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ict_parse.extract import _nh, HEADER_MAP

PLACE = re.compile(r"招标文件|中标文件|磋商文件|采购文件|响应|满足|详见|详情|见附件|^null$|^none$|^-+$")


def tab_cols(t):
    if not t or not t[0]:
        return {}
    cols = {}
    for j, h in enumerate(t[0]):
        f = HEADER_MAP.get(_nh(h))
        if f and f not in cols:
            cols[f] = j
    return cols


def specs_for(aid):
    """返回 [(name, spec, supplier)] 来自原文表与附件表。"""
    r = json.load(open(f"out/full/{aid}/ir.json", encoding="utf-8"))
    out = []
    tables = []
    for g in r.get("main_item_tables", []):
        tables.append((g.get("headers"), g.get("rows") or []))
    for a in r.get("extracted_files", []):
        for t in (a.get("tables") or []):
            if t:
                tables.append((t[0], t[1:]))
    for headers, rows in tables:
        cols = tab_cols([headers])
        ni, si, su = cols.get("item_name"), cols.get("spec"), cols.get("supplier_name")
        if si is None:
            continue
        for row in rows:
            if si >= len(row):
                continue
            sp = (row[si] or "").strip()
            nm = (row[ni] or "").strip() if (ni is not None and ni < len(row)) else ""
            sup = (row[su] or "").strip() if (su is not None and su < len(row)) else ""
            out.append((nm, sp, sup))
    return out


hit = []
for f in sorted(os.listdir("valset/gold")):
    if not f.endswith(".json"):
        continue
    aid = f[:-5]
    d = json.load(open("valset/gold/" + f, encoding="utf-8"))
    src = specs_for(aid)
    for i, it in enumerate(d.get("items", [])):
        s = it.get("spec")
        if not (s and PLACE.search(str(s))):
            continue
        name = (it.get("item_name") or "")
        cand = [(nm, sp) for (nm, sp, sup) in src if sp and not PLACE.search(sp)]
        # 名称相关的候选
        rel = [(nm, sp) for (nm, sp) in cand if nm and (nm in name or name in nm)]
        print(f"== {aid} #{i} {name!r}  当前spec={s!r}")
        print(f"     源里非占位 spec 共 {len(cand)} 个；名称相关 {len(rel)}: {rel[:5]}")
