# -*- coding: utf-8 -*-
"""Q2 三表自洽断言脚本（硬校验）。

校验对象：官方三张表（总表 / 标的表 / 投标人表）的 `(UUID, 标段/合同包)`。
两种输入：
    python scripts/assert_official_consistency.py --dir deliverables/official
    python scripts/assert_official_consistency.py --platform-db out/platform/platform.db
    python scripts/assert_official_consistency.py --csv-dir <任意导出的三表目录>

断言（任一失败 -> 退出码 1）
---------------------------
A 列名逐字对齐：总表 8 列 / 标的表 11 列 / 投标人表 9 列（无「备注」列）。
B 后缀格式：UUID = `主ID` 或 `主ID_01/_02…`（两位数字）；后缀仅出现在**该主 ID 在标的表
  里出现 ≥2 个包名（含「未分包」这一组）**的情形；「未分包」组永远不带后缀。
C 表内双射：按主 ID 分组，`UUID -> 包名` 与 `包名 -> UUID` 都必须是一一映射
  （同一包不能既挂 _01 又挂 _02，同一后缀不能对应两个包名）。
D 跨表一致：`标的表` 的键集合 == `投标人表` 的键集合；`总表` 的键集合 ⊇ 标的表，
  且总表多出的键只允许是「该主 ID 没有标的行」时的 `(主ID, 未分包)`。
E 同名同后缀：三表中同一 UUID 对应的包名必须完全一致（由 D 保证，仍显式再查一遍）。
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_PACKAGE = "未分包"
TOTAL_COLS = ["序号", "URL", "UUID", "标段/合同包", "项目名称", "采购单位", "中标供应商", "中标金额"]
ITEM_COLS = ["序号", "UUID", "标段/合同包", "产品服务名称", "品目", "品牌", "规格型号",
             "数量", "单价", "总价", "类别"]
BID_COLS = ["序号", "UUID", "标段/合同包", "名称", "类别", "通过审查", "报价（万元）", "得分", "未中标原因"]
_SUF = re.compile(r"^(?P<main>.+?)_(?P<n>\d{2})$")


def read_dir(d):
    out = {}
    for name, cols in (("总表", TOTAL_COLS), ("标的表", ITEM_COLS), ("投标人表", BID_COLS)):
        p = os.path.join(d, f"{name}.csv")
        with open(p, encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            out[name] = (list(rd.fieldnames or []), list(rd))
    return out


def from_platform(db):
    from ict_parse import official_tables as ot
    t = ot.build_from_platform(db)
    return {n: (list(ot.COLUMNS[n]), t[n]) for n in ot.SHEET_NAMES}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(APP, "deliverables", "official"))
    ap.add_argument("--platform-db", default=None)
    a = ap.parse_args()
    t = from_platform(a.platform_db) if a.platform_db else read_dir(a.dir)
    print("校验对象: %s" % (a.platform_db or a.dir))

    fails = []

    # A 列名
    for name, cols in (("总表", TOTAL_COLS), ("标的表", ITEM_COLS), ("投标人表", BID_COLS)):
        got = t[name][0]
        if got != cols:
            fails.append("A 列名不符 %s: %s" % (name, got))
    print("A 列名: 总表 %d 列 / 标的表 %d 列 / 投标人表 %d 列 %s"
          % (len(t["总表"][0]), len(t["标的表"][0]), len(t["投标人表"][0]),
             "OK" if not fails else "FAIL"))

    # 键集合 + 双射
    keys = {}
    dup = []
    for name in t:
        m_fwd, m_rev = defaultdict(set), defaultdict(set)
        ks = set()
        for r in t[name][1]:
            uuid, pkg = str(r["UUID"]), str(r["标段/合同包"])
            ks.add((uuid, pkg))
            main = _SUF.match(uuid)
            m = main.group("main") if main else uuid
            m_fwd[(m, uuid)].add(pkg)
            m_rev[(m, pkg)].add(uuid)
        keys[name] = ks
        for (m, uuid), s in m_fwd.items():
            if len(s) > 1:
                dup.append("%s: UUID %s 对应多个包 %s" % (name, uuid, sorted(s)))
        for (m, pkg), s in m_rev.items():
            if len(s) > 1:
                dup.append("%s: 主ID %s 的包 %r 对应多个 UUID %s" % (name, m, pkg, sorted(s)))
    if dup:
        fails.append("C 双射失败 %d 例，前 3: %s" % (len(dup), dup[:3]))
    print("C 表内双射 (UUID<->包名): %s%s" % ("OK" if not dup else "FAIL", "" if not dup else " " + str(dup[:2])))

    # B 后缀格式：文件级 = 标的表的 UUID 后缀必须与"该主 ID 的包数"一致
    pkg_per_main = defaultdict(set)
    for uuid, pkg in keys["标的表"]:
        main = _SUF.match(uuid)
        pkg_per_main[main.group("main") if main else uuid].add(pkg)
    bad_sfx = []
    for uuid, pkg in keys["标的表"]:
        m = _SUF.match(uuid)
        main = m.group("main") if m else uuid
        n = len(pkg_per_main[main])
        if m and n < 2:
            bad_sfx.append("%s：单包/未分包却带后缀（包=%s）" % (uuid, pkg))
        if (not m) and n >= 2 and pkg != NO_PACKAGE:
            bad_sfx.append("%s：%d 个包却无后缀（包=%s）" % (uuid, n, pkg))
    if bad_sfx:
        fails.append("B 后缀失败 %d 例，前 3: %s" % (len(bad_sfx), bad_sfx[:3]))
    print("B 后缀格式: %s（多组主ID %d 个）"
          % ("OK" if not bad_sfx else "FAIL " + str(bad_sfx[:2]),
             sum(1 for v in pkg_per_main.values() if len(v) >= 2)))

    # D 跨表
    it, bid, tot = keys["标的表"], keys["投标人表"], keys["总表"]
    only_bid = bid - it
    only_it = it - bid
    mains_with_items = {_SUF.match(u).group("main") if _SUF.match(u) else u for u, _ in it}
    extra_tot = {(u, p) for (u, p) in (tot - it)
                 if not ((_SUF.match(u).group("main") if _SUF.match(u) else u) not in mains_with_items
                         and p == NO_PACKAGE)}
    print("D 跨表: 标的表 %d 键 / 投标人表 %d 键 / 总表 %d 键；投标人表多出 %d，标的表多出 %d，"
          "总表多出(非'无标的行') %d" % (len(it), len(bid), len(tot), len(only_bid), len(only_it), len(extra_tot)))
    if only_bid:
        fails.append("D 投标人表有而标的表无 %d 例: %s" % (len(only_bid), sorted(only_bid)[:3]))
    if only_it:
        fails.append("D 标的表有而投标人表无 %d 例: %s" % (len(only_it), sorted(only_it)[:3]))
    if extra_tot:
        fails.append("D 总表多出非'无标的行' %d 例: %s" % (len(extra_tot), sorted(extra_tot)[:3]))

    # E 同一 UUID 的包名跨表一致
    name_of = defaultdict(set)
    for name in t:
        for uuid, pkg in keys[name]:
            name_of[uuid].add(pkg)
    clash = {u: sorted(s) for u, s in name_of.items() if len(s) > 1}
    if clash:
        fails.append("E 同一 UUID 跨表包名不一致 %d 例: %s" % (len(clash), list(clash.items())[:3]))
    print("E 同 UUID 跨表包名一致: %s" % ("OK" if not clash else "FAIL"))

    print("-" * 72)
    if fails:
        print("断言失败 %d 项:" % len(fails))
        for f in fails:
            print("  ✗", f)
        return 1
    print("全部断言通过 ✓（A 列名 / B 后缀 / C 双射 / D 跨表键集合 / E 同名同后缀）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
