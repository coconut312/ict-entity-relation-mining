"""数据质量画像：为《过程性文档·数据质量分析报告》产出可引用的量化表格。

覆盖：附件构成与解析难点、脏数据形态、缺失值分布、报价方式分布。
结果同时打印并落盘 `out/data_quality_profile.json`。

用法: uv run python scripts/profile_data_quality.py
"""
from __future__ import annotations

import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ict_parse.extract import HEADER_MAP, _canon, _nh, _pick_header_row, _pricing_mode  # noqa: E402

ROOT = "out/full"
F7 = ["item_name", "category", "brand", "spec", "unit_price", "quantity", "total_price"]
MONEY_RE = re.compile(r"[¥￥]?\s*\d[\d,]*(?:\.\d+)?\s*(?:亿元|万元|亿|万|元|圆)?")
PLACEHOLDER = ("详见招标文件", "详见采购文件", "详见磋商文件", "详见附件", "详情查看", "不涉及",
               "完全响应", "满足招标文件", "见附件", "无分包", "详见包")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ids = [ln.strip() for ln in open("all_ids.txt", encoding="utf-8") if ln.strip()]

    ext = collections.Counter()
    att_flags = collections.Counter()
    money_form = collections.Counter()
    multi_val = 0
    multi_val_ex = []
    ghost_hdr = 0
    ghost_hdr_ex = []
    hdr_not_first = 0
    giant_hdr = 0
    placeholder = collections.Counter()
    modes = collections.Counter()
    f_nonnull = {f: 0 for f in F7 + ["supplier_name"]}
    n_items = 0
    kdist = collections.Counter()
    n_parse_fail = 0
    # 「一格里多值」与「率型/服务型」的篇级统计
    cell_total = 0

    for aid in ids:
        try:
            rec = json.load(open(os.path.join(ROOT, aid, "ir.json"), encoding="utf-8"))
        except Exception:  # noqa: BLE001
            n_parse_fail += 1
            continue
        for a in rec.get("extracted_files", []):
            ext[(a.get("ext") or "").lower() or "(无后缀)"] += 1
            note = a.get("note") or ""
            if "not auto-extracted" in note:
                att_flags["未解压归档(rar/7z)"] += 1
            if "text extract failed" in note:
                att_flags["文本抽取失败(含损坏PDF)"] += 1
            if "nested extract failed" in note:
                att_flags["套娃解压失败"] += 1
            if "scanned_suspect=True" in note:
                att_flags["扫描件(需视觉)"] += 1
            if "VLM" in note:
                att_flags["已跑VLM"] += 1
        modes[_pricing_mode(rec)] += 1

        for g in rec.get("main_item_tables", []):
            hdr = g.get("headers") or []
            if hdr:
                # 表头重影：NFKC+去空白后，折叠重复字符会改变它
                h0 = hdr[0] if len(hdr) == 1 else "".join(hdr)
                squeezed = re.sub(r"(.)\1+", r"\1", h0)
                if squeezed != h0:
                    ghost_hdr += 1
                    if len(ghost_hdr_ex) < 4:
                        ghost_hdr_ex.append(h0[:40])
                if len(hdr) == 1 and len(str(hdr[0])) > 200:
                    giant_hdr += 1
            rows = g.get("rows") or []
            table = [hdr] + rows
            if len(table) >= 2 and _pick_header_row(table) not in (0, None):
                hdr_not_first += 1
            for r in rows:
                for c in r:
                    s = str(c)
                    if not s.strip():
                        continue
                    cell_total += 1
                    if "；" in s or ";" in s:
                        multi_val += 1
                        if len(multi_val_ex) < 4 and len(s) < 90:
                            multi_val_ex.append(s)
                    if MONEY_RE.search(s):
                        if "万元" in s or "万" in s:
                            money_form["万元"] += 1
                        if "￥" in s or "¥" in s:
                            money_form["带￥符号"] += 1
                        if "," in s:
                            money_form["千分位逗号"] += 1
                        if "（" in s or "(" in s:
                            money_form["含括号单位"] += 1
                        if s.strip().startswith(("报价", "人民币", "大写")):
                            money_form["带前缀(报价/人民币/大写)"] += 1
                    for p in PLACEHOLDER:
                        if p in s:
                            placeholder[p] += 1

        try:
            items = json.load(open(os.path.join(ROOT, aid, "items_llm.json"), encoding="utf-8"))["items"]
        except Exception:  # noqa: BLE001
            items = []
        n_items += len(items)
        present = set()
        for it in items:
            for f in F7 + ["supplier_name"]:
                if it.get(f) not in (None, "", []):
                    f_nonnull[f] += 1
                    if f in F7:
                        present.add(f)
        kdist[len(present)] += 1

    nn = {f: round(v / n_items, 4) if n_items else 0 for f, v in f_nonnull.items()}
    out = {
        "pages": len(ids), "ir_parse_fail": n_parse_fail,
        "attachments_by_ext": dict(ext.most_common()),
        "attachment_flags": dict(att_flags),
        "cells_scanned": cell_total,
        "dirty": {
            "money_forms": dict(money_form),
            "multi_value_cells": multi_val,
            "multi_value_examples": multi_val_ex,
            "ghost_header_tables": ghost_hdr,
            "ghost_header_examples": ghost_hdr_ex,
            "header_not_first_row_tables": hdr_not_first,
            "giant_single_cell_header_tables": giant_hdr,
            "placeholder_hits": dict(placeholder.most_common()),
        },
        "pricing_modes": dict(modes.most_common()),
        "items": n_items,
        "field_nonnull": nn,
        "field7_dist": {str(k): v for k, v in sorted(kdist.items(), reverse=True)},
    }
    os.makedirs("out", exist_ok=True)
    with open("out/data_quality_profile.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"篇数 {len(ids)} | ir 解析失败 {n_parse_fail} | 明细 {n_items} | 扫描单元格 {cell_total}")
    print("\n[附件构成] (top12)")
    for k, v in ext.most_common(12):
        print(f"   {k:<10} {v}")
    print("\n[附件解析难点]")
    for k, v in att_flags.most_common():
        print(f"   {k:<22} {v}")
    print("\n[脏数据形态]")
    print(f"   金额形态: {dict(money_form)}")
    print(f"   一格里多值('；'): {multi_val} 个单元格  例: {multi_val_ex[:2]}")
    print(f"   表头重影(需折叠重复字): {ghost_hdr} 张表  例: {ghost_hdr_ex[:2]}")
    print(f"   表头不在首行: {hdr_not_first} 张表")
    print(f"   单格巨型表头(整页文本): {giant_hdr} 张表")
    print(f"   占位值命中: {dict(placeholder.most_common(8))}")
    print("\n[报价方式分布]")
    for k, v in modes.most_common():
        print(f"   {k:<10} {v}")
    print("\n[字段非空率]")
    print("  ", nn)
    print("\n[篇级 7 字段分布]", dict(sorted(kdist.items(), reverse=True)))
    print("\n-> out/data_quality_profile.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
