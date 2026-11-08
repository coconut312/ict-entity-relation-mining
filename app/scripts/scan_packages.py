# -*- coding: utf-8 -*-
"""Q2 分包识别 —— 全库量化脚本（有据可依的识别率）。

规则唯一来源：`ict_parse/packages.py`（判据见该模块 docstring）。
本脚本只做统计，不写任何数据。

用法：
    python scripts/scan_packages.py                       # 默认平台库
    python scripts/scan_packages.py --db <platform.db>    # 指定副本库
    python scripts/scan_packages.py --csv deliverables/items_task1.csv
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from ict_parse import packages as pk                                     # noqa: E402

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(APP, "out", "platform", "platform.db")
DEFAULT_FULL = os.path.join(APP, "out", "full")
NO = pk.NO_PACKAGE

# 旁证：附件文件名里的包标识（只用于"是否还有更多线索"的统计，不用于行级归属）
_RE_FILE = re.compile(r"第\s*(?:\d{1,3}|[一二三四五六七八九十]{1,3})\s*(?:采购包|合同包|包件|标包|分包|标段|包)"
                      r"|(?:采购包|合同包|包件|标包|分包|标段)\s*[（(]?\s*(?:\d{1,3}|[一二三四五六七八九十]{1,3})"
                      r"|包\s*[（(]?\s*(?:\d{1,3}|[一二三四五六七八九十]{1,3})")


def load_rows(db: str | None, csv_path: str | None):
    if csv_path:
        import csv as _csv
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            rows = list(_csv.DictReader(f))
        order = []
        for r in rows:
            if r["announcement_id"] not in order:
                order.append(r["announcement_id"])
        return order, rows
    con = sqlite3.connect(db)
    notices = [r[0] for r in con.execute("SELECT announcement_id FROM t1_notice")]
    items = [{"announcement_id": a, "package_no": p, "item_name": n}
             for a, p, n in con.execute("SELECT announcement_id, package_no, item_name FROM t1_item")]
    con.close()
    return notices, items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--full", default=DEFAULT_FULL, help="out/full 目录（附件名旁证）")
    ap.add_argument("--examples", type=int, default=10)
    a = ap.parse_args()

    order, items = load_rows(a.db, a.csv)
    by = collections.defaultdict(list)
    for it in items:
        by[it["announcement_id"]].append(it)

    st = collections.Counter()
    multi_ex, single_ex, attach_only = [], [], []
    for aid in order:
        rows = by.get(aid, [])
        labels, groups = pk.labels_of_notice(rows)
        suffixes = pk.uuid_suffixes(groups)
        real = [l for l in groups if l != NO]
        st["notices"] += 1
        st["items"] += len(rows)
        if len(groups) >= 2:                       # 判为分包（≥2 个包组，含「未分包」组）
            st["notices_multi"] += 1
            st["items_in_multi"] += len(rows)
            st["pkg_in_multi"] += len(real)
            if len(real) >= 2:
                st["notices_true_multi"] += 1
            else:
                st["notices_one_plus_unlabeled"] += 1
            if len(multi_ex) < a.examples:
                multi_ex.append((aid, list(groups.keys()),
                                 [pk.uuid_for(aid, suffixes[l])[:26] for l in list(groups)[:4]]))
        elif len(real) == 1:
            st["notices_single"] += 1
            st["items_in_single"] += len(rows)
            if len(single_ex) < a.examples:
                single_ex.append((aid, list(groups.keys())))
        else:
            st["notices_none"] += 1
            # 旁证：附件文件名里是否有包标识（说明"还有线索但我们不猜行级归属"）
            att = os.path.join(a.full, aid, "attachments")
            if os.path.isdir(att) and any(_RE_FILE.search(fn) for fn in os.listdir(att)):
                st["notices_none_but_attach_hint"] += 1
                if len(attach_only) < a.examples:
                    attach_only.append(aid)

    n, m = st["notices"], st["items"]
    print("数据源: %s" % (a.csv or a.db))
    print("公告 %d 篇 / 标的 %d 行" % (n, m))
    print("-" * 72)
    print("判为分包（≥2 个包组） : %4d 篇 (%.2f%%)  涉及标的 %d 行 (%.2f%%)  包数 %d"
          % (st["notices_multi"], 100.0 * st["notices_multi"] / max(n, 1),
             st["items_in_multi"], 100.0 * st["items_in_multi"] / max(m, 1), st["pkg_in_multi"]))
    print("    其中 ≥2 个真包    : %4d 篇" % st["notices_true_multi"])
    print("    其中 1 个包 + 无标识行 : %4d 篇（也必须分开挂 UUID，否则同一 UUID 会对应两个包名）"
          % st["notices_one_plus_unlabeled"])
    print("单个包（有名/有号）   : %4d 篇 (%.2f%%)  涉及标的 %d 行 (%.2f%%)  → 不判为分包（UUID 无后缀）"
          % (st["notices_single"], 100.0 * st["notices_single"] / max(n, 1), st["items_in_single"],
             100.0 * st["items_in_single"] / max(m, 1)))
    print("未识别到包（=未分包） : %4d 篇 (%.2f%%)"
          % (st["notices_none"], 100.0 * st["notices_none"] / max(n, 1)))
    print("  其中附件文件名里另有包标识线索（未用于行级归属）: %d 篇" % st["notices_none_but_attach_hint"])
    print("-" * 72)
    if multi_ex:
        print("多包样例（包名 / 三表共用 UUID）:")
        for aid, labs, uuids in multi_ex:
            print("   %s  %s  %s" % (aid, labs, uuids))
    if single_ex:
        print("单包样例（填原文包名、UUID 不加后缀）:")
        for aid, labs in single_ex:
            print("   %s  %s" % (aid, labs))
    if attach_only:
        print("仅有附件名线索、未判分包的样例: %s" % attach_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
