import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ict_parse.extract import _nh, HEADER_MAP

GOLD = "valset/gold"
BAK = "backup_gold"
PLACE = re.compile(r"招标文件|中标文件|磋商文件|采购文件|谈判文件|响应|满足|详见|详情|见附件|^null$|^none$|^-+$")

# 备份
if not os.path.exists(BAK):
    shutil.copytree(GOLD, BAK)
    print("已备份 ->", BAK)


def _tab_cols(headers):
    cols = {}
    for j, h in enumerate(headers or []):
        f = HEADER_MAP.get(_nh(h))
        if f and f not in cols:
            cols[f] = j
    return cols


def _real_specs(aid):
    r = json.load(open(f"out/full/{aid}/ir.json", encoding="utf-8"))
    rows = []
    tbls = [(g.get("headers"), g.get("rows") or []) for g in r.get("main_item_tables", [])]
    for a in r.get("extracted_files", []):
        for t in (a.get("tables") or []):
            if t:
                tbls.append((t[0], t[1:]))
    for headers, body in tbls:
        c = _tab_cols(headers)
        si, ni, ui = c.get("spec"), c.get("item_name"), c.get("supplier_name")
        if si is None:
            continue
        for row in body:
            if si >= len(row):
                continue
            sp = (row[si] or "").strip()
            if not sp or PLACE.search(sp):
                continue
            nm = (row[ni] or "").strip() if (ni is not None and ni < len(row)) else ""
            su = (row[ui] or "").strip() if (ui is not None and ui < len(row)) else ""
            rows.append((nm, sp, su))
    return rows


def _clean_found(s):
    s = re.sub(r"[，,、]?\s*(等|等等)\s*$", "", s).strip().rstrip("。").strip()
    return s or None


changes = []
for f in sorted(os.listdir(GOLD)):
    if not f.endswith(".json"):
        continue
    aid = f[:-5]
    d = json.load(open(os.path.join(GOLD, f), encoding="utf-8"))
    src = _real_specs(aid)
    dirty = False
    for i, it in enumerate(d.get("items", [])):
        s = it.get("spec")
        if not (isinstance(s, str) and (PLACE.search(s) or s.lower() in ("null", "none"))):
            continue
        name = it.get("item_name") or ""
        new = None
        for (nm, sp, su) in src:
            if nm and (nm in name or name in nm):
                new = _clean_found(sp)
                break
        changes.append((aid, i, name, s, new))
        it["spec"] = new
        dirty = True
    if dirty:
        json.dump(d, open(os.path.join(GOLD, f), "w", encoding="utf-8"), ensure_ascii=False, indent=2)

print(f"共修正 {len(changes)} 条：")
for aid, i, name, old, new in changes:
    print(f"  {aid} #{i} {name[:24]!r}: {old!r} -> {new!r}")
