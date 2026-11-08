"""字段级审计：找出「原文/附件里有证据，但抽取结果为空」的篇 —— 抠细节的靶子。

对每篇公告，对每个 7 字段：
  证据（raw_has）：ir 的「主要标的信息表」或**会被采用**的附件表里，
                  存在映射到该字段的列，且单元格有非占位值；
  结果（got）    ：items.json 里该篇有任何一条明细该字段非空。
  raw_has and not got  -> MISSED（可能是真 bug）

同时单独统计「证据只出现在被文件名/内容门控跳过的附件里」——那是设计取舍，不算 bug。

用法: uv run python scripts/audit_fields.py [--field spec] [--top 15]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ict_parse.extract import (  # noqa: E402
    HEADER_ISH,
    HEADER_MAP,
    RATE_MODES,
    _canon,
    _nh,
    _pricing_mode,
    _table_has_price,
    norm_money,
    norm_qty,
)

F7 = ["item_name", "category", "brand", "spec", "unit_price", "quantity", "total_price"]
ROOT = "out/full"
_PLACE = re.compile(r"招标文件|中标文件|磋商文件|采购文件|谈判文件|采购人要求|详见|详情查看|见附件|^null$|^none$")


def _real(v) -> bool:
    """单元格是否有「真值」（非空、非占位、非纯符号）。"""
    if v is None:
        return False
    s = str(v).strip()
    if not s or s in ("-", "—", "/", "无", "0", "null", "None"):
        return False
    return not _PLACE.search(s)


def _ev_ok(field: str, v) -> bool:
    """该单元格是否算「该字段的真证据」——滤掉表头泄漏、不可解析值。"""
    if not _real(v):
        return False
    s = str(v).strip()
    if s in HEADER_ISH or _nh(s) in HEADER_MAP:
        return False                      # 表头行泄漏（如 spec 列的值就是「规格型号」）
    if field in ("unit_price", "total_price"):
        return norm_money(s) is not None
    if field == "quantity":
        return norm_qty(s)[0] is not None
    return True


def _evidence(rec: dict, field: str) -> tuple[bool, str, bool]:
    """返回 (会被采用的表里有证据, 证据样例, 只有被跳过的附件里有证据)。"""
    hit_used = False
    sample = ""
    hit_skipped = False

    def scan(headers, rows, used: bool):
        nonlocal hit_used, sample, hit_skipped
        canon = _canon(headers)
        for j, c in enumerate(canon):
            if c != field:
                continue
            for r in rows:
                if j < len(r) and _ev_ok(field, r[j]):
                    if used:
                        hit_used = True
                        if not sample:
                            sample = str(r[j])[:60]
                    else:
                        hit_skipped = True
                    return

    for g in rec.get("main_item_tables", []):
        scan(g["headers"], g["rows"], True)

    for a in rec.get("extracted_files", []):
        nm = " ".join(x for x in (a.get("name"), a.get("rel_path")) if x)
        name_kw = any(k in nm for k in ("报价", "一览", "明细", "标的", "开标", "成交"))
        is_vlm = "VLM" in (a.get("note") or "")
        tables = a.get("tables") or []
        used = name_kw or any(_table_has_price(t) for t in tables) or (
            is_vlm and a.get("text") and "|" in a["text"])
        for t in tables:
            if len(t) >= 2:
                scan(t[0], t[1:], used)

    return hit_used, sample, hit_skipped


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--field", default=None, help="只看某个字段")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--out-ids", default=None, help="把 MISSED 的篇 id 写到文件")
    args = ap.parse_args()

    ids = [l.strip() for l in open("all_ids.txt", encoding="utf-8") if l.strip()]
    fields = [args.field] if args.field else F7
    missed = collections.defaultdict(list)
    skipped_only = collections.Counter()
    rate_nulled = collections.Counter()   # 率类报价方式：单价/总价故意置空，不算 bug
    checked = 0

    for aid in ids:
        ip = os.path.join(ROOT, aid, "ir.json")
        ep = os.path.join(ROOT, aid, "items.json")
        if not (os.path.exists(ip) and os.path.exists(ep)):
            continue
        rec = json.load(open(ip, encoding="utf-8"))
        items = json.load(open(ep, encoding="utf-8"))["items"]
        checked += 1
        rate_mode = _pricing_mode(rec) in RATE_MODES
        for f in fields:
            got = any(it.get(f) not in (None, "", []) for it in items)
            if got:
                continue
            used, sample, only_skipped = _evidence(rec, f)
            if used:
                if rate_mode and f in ("unit_price", "total_price"):
                    rate_nulled[f] += 1
                    continue
                missed[f].append((aid, sample))
            elif only_skipped:
                skipped_only[f] += 1

    print(f"审计篇数 {checked}\n")
    print("%-12s %8s %12s %12s" % ("字段", "MISSED(疑真bug)", "仅在跳过附件", "率类故意置空"))
    for f in fields:
        print("%-12s %8d %12d %12d" % (f, len(missed[f]), skipped_only.get(f, 0), rate_nulled.get(f, 0)))

    print()
    for f in fields:
        rows = missed[f]
        if not rows:
            continue
        print(f"=== {f} 有证据却为空（{len(rows)} 篇，列前 {args.top}）===")
        for aid, s in rows[: args.top]:
            print(f"   {aid}  证据样例={s!r}")
        if args.out_ids:
            with open(args.out_ids, "w", encoding="utf-8") as fp:
                fp.write("\n".join(a for a, _ in missed[f]))
            print(f"   -> 已写 {args.out_ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
