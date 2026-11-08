"""审计人工真值（gold）的数值是否能在原文/附件中找到依据。

用途：gold 是「算法标注 -> 人工核验」产物，可能存在笔误；本脚本把
gold 里**在原文找不到对应数字**的金额列出来，供人工复核（不自动修改）。

判定：把原文（正文+概要+表格+附件文本）去掉千分位/空白后，
用「元」与「万元」两种写法的多种格式去匹配该金额。

用法: uv run python scripts/audit_gold.py [--val-only] [--show 20]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = "out/full"
VDIR = "valset"
GOLD = os.path.join(VDIR, "gold")
MONEY = ("unit_price", "total_price")
_STRIP = str.maketrans({" ": "", ",": "", "，": "", "\u3000": ""})


def src_text(rec: dict) -> str:
    parts = [rec.get("content_text") or "", rec.get("article_title") or ""]
    for k, v in (rec.get("summary") or {}).items():
        parts.append(f"{k}{v}")
    for g in rec.get("main_item_tables", []):
        parts.extend(str(h) for h in g.get("headers", []))
        for r in g.get("rows", []):
            parts.extend(str(c) for c in r if c)
    for a in rec.get("extracted_files", []):
        parts.append(a.get("text") or "")
        parts.append(a.get("name") or "")
        for t in (a.get("tables") or []):
            for r in t:
                parts.extend(str(c) for c in r if c)
    return "".join(parts).translate(_STRIP)


def num_forms(v) -> set[str]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return set()
    out = set()
    if f == int(f):
        out.add(str(int(f)))
    out.add("%.2f" % f)
    out.add(("%.10f" % f).rstrip("0").rstrip("."))
    w = f / 10000.0                                  # 公告常以「万元」公示
    out.add(("%.10f" % w).rstrip("0").rstrip("."))
    out.add("%.4f" % w)
    out.add("%.6f" % w)
    return {x for x in out if x}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=20)
    args = ap.parse_args()

    ids = [f[:-5] for f in sorted(os.listdir(GOLD)) if f.endswith(".json")]
    bad = []
    checked = derived = 0
    for aid in ids:
        ip = os.path.join(ROOT, aid, "ir.json")
        if not os.path.exists(ip):
            continue
        rec = json.load(open(ip, encoding="utf-8"))
        text = src_text(rec)
        gold = json.load(open(os.path.join(GOLD, aid + ".json"), encoding="utf-8"))["items"]
        for it in gold:
            up, q, tp = it.get("unit_price"), it.get("quantity"), it.get("total_price")
            for f in MONEY:
                v = it.get(f)
                if v in (None, ""):
                    continue
                checked += 1
                forms = num_forms(v)
                if forms and any(x in text for x in forms):
                    continue                      # 原文里能找到 -> 有依据
                # 原文找不到时，再看是否为官方口径④允许的「逻辑推定」
                ok = False
                try:
                    if f == "total_price" and up and q and abs(float(up) * float(q) - float(v)) < 0.01:
                        ok = True
                    if f == "unit_price" and tp and q and abs(float(tp) / float(q) - float(v)) < 0.01:
                        ok = True
                except (TypeError, ValueError, ZeroDivisionError):
                    ok = False
                if ok:
                    derived += 1
                else:
                    bad.append((aid, it.get("item_name"), f, v))

    print(f"核对 gold 篇数 {len(ids)} | 金额字段 {checked} 个")
    print(f"  ├ 原文可查到依据：{checked - derived - len(bad)}")
    print(f"  ├ 合法逻辑推定(单价×数量 / 总价÷数量)：{derived}")
    print(f"  └ **原文找不到且非推定（存疑）：{len(bad)}**\n")
    for aid, nm, f, v in bad[: args.show]:
        print(f"  {aid}  {f}={v!r}  item={str(nm)[:30]}")
    if len(bad) > args.show:
        print(f"  ... 其余 {len(bad) - args.show} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
