# -*- coding: utf-8 -*-
"""《数据质量分析报告》数字复核脚本（只读，不改任何交付数据）。

用途：把报告里引用的每一个数字，从**源头文件**重新算一遍，输出到 stdout 并落盘
`out/recheck_dq_report.json`，供报告标注口径与来源。

数据源（全部为仓库内既有文件，只读）：
    deliverables/announcements_task1.csv   1038 篇公告级交付表
    deliverables/items_task1.csv           交付版标的表（LLM 主抽取 + 归一化）
    deliverables/items_task1_rule.csv      规则层标的表（基线对照）
    deliverables/official/*.csv            官方标准三表 + 排除评估清单
    deliverables/task2_*.csv               任务二关系表（实体计数）
    out/full/<公告id>/ir.json              逐篇中间表示（附件构成/解析难点）

用法：
    cd app
    .\.venv\Scripts\python.exe scripts\recheck_dq_report.py
"""
from __future__ import annotations

import collections
import csv
import json
import os
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
D = os.path.join(APP, "deliverables")
FULL = os.path.join(APP, "out", "full")
F7 = ["item_name", "category", "brand", "spec", "unit_price", "quantity", "total_price"]
# 交付表 -> 官方 7 字段的列名映射（交付表用 item_name/category/... 同名）
RESULT: dict = {}


def load(name: str) -> list[dict]:
    """name 相对 app/ 解析（可写 'deliverables/x.csv' 或 'x.csv'）。"""
    p = name if os.path.isabs(name) else os.path.join(APP, name)
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def nonnull(v) -> bool:
    return v is not None and str(v).strip() not in ("", "nan", "None")


# ---------------------------------------------------------------- 1. 数据集概况
def sec_overview() -> None:
    ann = load("announcements_task1.csv")
    it = load("items_task1.csv")
    itr = load("items_task1_rule.csv")
    sup = load("task2_suppliers.csv")
    pur = load("task2_purchasers.csv")
    ent = load("task2_entities.csv")
    off_item = load("official/标的表.csv")
    off_total = load("official/总表.csv")

    proj = [float(a["project_amount"]) for a in ann if nonnull(a["project_amount"])]
    o = {
        "announcements": len(ann),
        "items_delivered": len(it),
        "items_rule": len(itr),
        "notice_type_dist": dict(collections.Counter(a["notice_type"] for a in ann)),
        "purchaser_distinct_raw": len({a["purchaser"] for a in ann}),
        "purchaser_distinct_nonempty": len({a["purchaser"].strip() for a in ann if nonnull(a["purchaser"])}),
        "purchaser_rows_task2": len(pur),
        "supplier_rows_task2": len(sup),
        "supplier_distinct_items": len({i["supplier_name"].strip() for i in it if nonnull(i["supplier_name"])}),
        "brand_distinct_delivered": len({i["brand"].strip() for i in it if nonnull(i["brand"])}),
        "brand_distinct_official_itemsheet": len({r["品牌"].strip() for r in off_item if nonnull(r["品牌"])}),
        "entity_rows_task2": len(ent),
        "project_amount_nonempty_pages": len(proj),
        "project_amount_sum_yuan": round(sum(proj), 2),
        "official_total_rows": len(off_total),
        "official_item_rows": len(off_item),
        "official_total_amount_wan_sum": round(sum(float(r["中标金额"]) for r in off_total if nonnull(r["中标金额"])), 2),
        "items_total_price_sum_yuan": round(sum(float(i["total_price"]) for i in it if nonnull(i["total_price"])), 2),
    }
    RESULT["overview"] = o
    print("=" * 78)
    print("[1] 数据集概况  (源: deliverables/announcements_task1.csv, items_task1.csv, task2_*.csv)")
    for k, v in o.items():
        print(f"   {k:<38} {v}")


# ------------------------------------------------------- 2. 脏数据类型与实例
RE_PROJCODE = re.compile(r"^[A-Za-z]{2,}\d{6,}$|^\d{8,}$")
RE_BULLET = re.compile(r"^[\s\u3000]*[●•·▪◦◆■□▶►\-\*\u25cf\u25a0]+\s*")
RE_NUM_PREFIX = re.compile(r"^[\s\u3000]*(?:第?\s*[0-9０-９一二三四五六七八九十]{1,3}\s*[、.．)）]|\([0-9]{1,2}\)|[0-9]{1,2}\s*[、.])\s*")
RE_MULTI = re.compile(r"[、,，;；/]|等\s*详见|详见")
RE_NONBRAND = ("详见", "见附件", "满足招标文件", "详见投标文件", "具体内容", "完全响应", "不涉及")


def sec_dirty() -> None:
    ann = load("announcements_task1.csv")
    it = load("items_task1.csv")
    itr = load("items_task1_rule.csv")
    excl = load("official/排除评估清单.csv")

    d: dict = {}
    # 2.1 purchaser 混入项目编号
    bad_pur = [(a["announcement_id"], a["purchaser"]) for a in ann if RE_PROJCODE.match(a["purchaser"].strip())]
    d["purchaser_looks_like_project_code_rows"] = len(bad_pur)
    d["purchaser_looks_like_project_code_examples"] = bad_pur[:5]

    # 2.2 brand 多值串
    multi_brand = [i for i in it if nonnull(i["brand"]) and RE_MULTI.search(i["brand"].strip())]
    d["brand_multivalue_rows_delivered"] = len(multi_brand)
    d["brand_multivalue_rows_rule"] = sum(1 for i in itr if nonnull(i["brand"]) and RE_MULTI.search(i["brand"].strip()))
    d["brand_multivalue_examples"] = [(i["announcement_id"], i["brand"]) for i in multi_brand[:5]]

    # 2.3 brand 非品牌文案
    nb = [i for i in it if nonnull(i["brand"]) and any(k in i["brand"] for k in RE_NONBRAND)]
    nb_r = [i for i in itr if nonnull(i["brand"]) and any(k in i["brand"] for k in RE_NONBRAND)]
    d["brand_nonbrand_rows_delivered"] = len(nb)
    d["brand_nonbrand_rows_rule"] = len(nb_r)
    d["brand_nonbrand_examples_rule"] = [(i["announcement_id"], i["brand"]) for i in nb_r[:5]]

    # 2.4 item_name 前导项目符号 / 编号前缀
    bul = [i for i in it if RE_BULLET.match(str(i["item_name"]))]
    num = [i for i in it if RE_NUM_PREFIX.match(str(i["item_name"]))]
    d["item_name_leading_bullet_rows_delivered"] = len(bul)
    d["item_name_leading_number_rows_delivered"] = len(num)
    d["item_name_bullet_rows_rule"] = sum(1 for i in itr if RE_BULLET.match(str(i["item_name"])))
    d["item_name_bullet_examples_rule"] = [(i["announcement_id"], i["item_name"]) for i in itr if RE_BULLET.match(str(i["item_name"]))][:5]

    # 2.5 category 同义 / 层级差异
    cat = [i["category"].strip() for i in it if nonnull(i["category"])]
    catr = [i["category"].strip() for i in itr if nonnull(i["category"])]
    d["category_distinct_delivered"] = len(set(cat))
    d["category_distinct_rule"] = len(set(catr))
    d["category_rows_toplevel_service_delivered"] = sum(1 for c in cat if c in ("服务", "服务类"))
    d["category_rows_toplevel_service_rule"] = sum(1 for c in catr if c in ("服务", "服务类"))
    d["category_rows_leaf_other_delivered"] = sum(1 for c in cat if "/其他" in c)
    d["category_rows_leaf_other_rule"] = sum(1 for c in catr if "/其他" in c)
    d["category_examples_rule_toplevel"] = sorted({c for c in catr if c.count("/") <= 1})[:8]

    # 2.6 金额科学计数法 / 尾随零（规则层原始输出）
    def money_stats(rows, field):
        sci = [r for r in rows if nonnull(r[field]) and re.search(r"[eE][-+]?\d+", str(r[field]))]
        tz = [r for r in rows if nonnull(r[field]) and re.fullmatch(r"-?\d+\.0+", str(r[field]).strip())]
        return sci, tz

    sci_u, tz_u = money_stats(itr, "unit_price")
    sci_t, tz_t = money_stats(itr, "total_price")
    d["money_scientific_unit_price_rule"] = len(sci_u)
    d["money_scientific_total_price_rule"] = len(sci_t)
    d["money_trailing_zero_unit_price_rule"] = len(tz_u)
    d["money_trailing_zero_total_price_rule"] = len(tz_t)
    d["money_scientific_examples"] = [r["unit_price"] for r in sci_u[:5]] + [r["total_price"] for r in sci_t[:5]]
    d["money_trailing_zero_examples"] = [r["unit_price"] for r in tz_u[:3]] + [r["total_price"] for r in tz_t[:3]]

    # 2.7 实体全/半角括号变体
    names = [i["supplier_name"].strip() for i in it if nonnull(i["supplier_name"])]
    names += [a["purchaser"].strip() for a in ann if nonnull(a["purchaser"])]
    norm = lambda s: s.replace("（", "(").replace("）", ")")
    groups: dict[str, set] = collections.defaultdict(set)
    for n in names:
        groups[norm(n)].add(n)
    variants = {k: sorted(v) for k, v in groups.items() if len(v) > 1}
    d["entity_name_paren_variant_groups"] = len(variants)
    d["entity_name_paren_variant_examples"] = list(variants.items())[:5]

    # 2.8 框架协议/入围/协议折扣类（官方排除清单）
    d["pricing_excluded_notices"] = len(excl)
    d["pricing_excluded_cols"] = list(excl[0].keys()) if excl else []
    d["pricing_excluded_reason_dist"] = dict(collections.Counter(
        (r.get("排除原因") or r.get("reason") or "")[:24] for r in excl))

    # 2.9 录入矛盾：单价×数量≠总价
    mismatch, checked = [], 0
    for i in it:
        if not (nonnull(i["unit_price"]) and nonnull(i["quantity"]) and nonnull(i["total_price"])):
            continue
        try:
            up, q, tp = float(i["unit_price"]), float(i["quantity"]), float(i["total_price"])
        except ValueError:
            continue
        checked += 1
        if q and abs(up * q - tp) > max(0.01, abs(tp) * 1e-6):
            mismatch.append((i["announcement_id"], up, q, tp))
    d["price_mismatch_rows"] = len(mismatch)
    d["price_checked_rows"] = checked
    d["price_mismatch_examples"] = mismatch[:5]
    d["needs_review_rows_delivered"] = sum(1 for i in it if nonnull(i["needs_review"])
                                           and str(i["needs_review"]).strip().lower() not in ("0", "false", "no"))
    RESULT["dirty"] = d
    print()
    print("=" * 78)
    print("[2] 脏数据类型与实例  (源: deliverables/items_task1*.csv, announcements_task1.csv)")
    for k, v in d.items():
        print(f"   {k:<42} {v}")


# ------------------------------------------------------------- 3. 缺失值分布
def sec_missing() -> None:
    it = load("deliverables/items_task1.csv")
    itr = load("deliverables/items_task1_rule.csv")

    def stats(rows):
        nn = {f: sum(1 for r in rows if nonnull(r[f])) for f in F7}
        rate = {f: round(nn[f] / len(rows), 4) for f in F7}
        kdist = collections.Counter()
        for r in rows:
            kdist[sum(1 for f in F7 if nonnull(r[f]))] += 1
        # 篇级 7 字段达成（任一条目有值即该篇有值）
        per: dict[str, set] = collections.defaultdict(set)
        for r in rows:
            for f in F7:
                if nonnull(r[f]):
                    per[r["announcement_id"]].add(f)
        pk = collections.Counter(len(v) for v in per.values())
        ids = {r["announcement_id"] for r in rows}
        for aid in ids:
            pk.setdefault(0, 0)
        pk[0] = len(ids) - sum(1 for v in per.values() if v)
        return nn, rate, kdist, pk

    nn, rate, kdist, pk = stats(it)
    nnr, rater, kdistr, pkr = stats(itr)
    o = {
        "rows": len(it),
        "field_nonnull_counts": nn,
        "field_nonnull_rate": rate,
        "item_level_k_dist": {str(k): v for k, v in sorted(kdist.items(), reverse=True)},
        "notice_level_k_dist": {str(k): v for k, v in sorted(pk.items(), reverse=True)},
        "rule_field_nonnull_rate": rater,
        "rule_notice_level_k_dist": {str(k): v for k, v in sorted(pkr.items(), reverse=True)},
        "notice_count": len({r["announcement_id"] for r in it}),
        "rule_item_rows": len(itr),
    }
    RESULT["missing"] = o
    print()
    print("=" * 78)
    print("[3] 缺失值分布  (源: deliverables/items_task1.csv)")
    print("   字段非空率(条数):")
    for f in F7:
        print(f"      {f:<14} {rate[f]:.3f}  ({nn[f]}/{len(it)})")
    print("   条目级 7 字段分档:", o["item_level_k_dist"])
    print("   篇级 7 字段分档  :", o["notice_level_k_dist"])
    print("   规则层字段非空率 :", rater)
    print("   规则层篇级分档   :", o["rule_notice_level_k_dist"])


# ---------------------------------------------------------- 4. 附件解析难点
def sec_attach() -> None:
    ids = [ln.strip() for ln in open(os.path.join(APP, "all_ids.txt"), encoding="utf-8") if ln.strip()]
    ext = collections.Counter()
    flags = collections.Counter()
    pages_with_flag: dict[str, set] = collections.defaultdict(set)
    nested_saved: list[tuple[str, int]] = []
    for aid in ids:
        p = os.path.join(FULL, aid, "ir.json")
        if not os.path.exists(p):
            continue
        try:
            rec = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for a in rec.get("extracted_files", []):
            ext[(a.get("ext") or "").lower() or "(无后缀)"] += 1
            note = a.get("note") or ""
            for key, tag in (("not auto-extracted", "未解压归档(rar/7z)"),
                             ("text extract failed", "文本抽取失败"),
                             ("nested extract failed", "套娃解压失败"),
                             ("scanned_suspect=True", "扫描件(需视觉)"),
                             ("VLM", "已跑VLM")):
                if key in note:
                    flags[tag] += 1
                    pages_with_flag[tag].add(aid)
    o = {
        "attachments_by_ext": dict(ext.most_common(14)),
        "attachment_flags": dict(flags),
        "attachment_flag_pages": {k: len(v) for k, v in pages_with_flag.items()},
        "total_attachment_entries": sum(ext.values()),
    }
    # 嵌套包解压后的代表篇条目数（对比用）
    for aid in ("t20260309_26250201",):
        p = os.path.join(FULL, aid, "items_llm.json")
        n = 0
        if os.path.exists(p):
            try:
                n = len(json.load(open(p, encoding="utf-8")).get("items", []))
            except Exception:
                n = -1
        nested_saved.append((aid, n))
    o["nested_example_items"] = nested_saved
    RESULT["attach"] = o
    print()
    print("=" * 78)
    print("[4] 附件解析难点  (源: out/full/<id>/ir.json, 全 1038 篇)")
    print("   附件条目总数:", o["total_attachment_entries"])
    for k, v in list(o["attachments_by_ext"].items())[:14]:
        print(f"      {k:<10} {v}")
    print("   难点标记:", o["attachment_flags"])
    print("   涉及篇数:", o["attachment_flag_pages"])
    print("   嵌套包代表篇条目:", nested_saved)


def main() -> int:
    sec_overview()
    sec_dirty()
    sec_missing()
    sec_attach()
    out = os.path.join(APP, "out", "recheck_dq_report.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(RESULT, f, ensure_ascii=False, indent=2)
    print()
    print("-> 已落盘", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
