"""刷新公告级字段（不重调 LLM）：把规则层新抽的 winners/bidders 与已有结果取并集。

背景：任务二需要「采购单位—中标供应商—**投标参与方**」三方关系，而投标参与方主要来自
原文的评审结果/资格审查/未中标原因表与附件名（声明函一人一份）。
LLM 层已有的 winners/bidders 不重跑，只做**并集合并**与缺失字段补齐 —— 全本地、秒级完成。

用法: uv run python scripts/refresh_announcements.py [--out out/full] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ict_parse.extract import (build as rule_build, _valid_entity,  # noqa: E402
                               _RE_ROUND_PREFIX, _supplier_uscc, _procurement_mode,
                               _manufacturers_from_declaration)

# 只在这些字段上做「缺失才补」，不覆盖 LLM 已有值
FILL_IF_EMPTY = ("project_name", "project_code", "purchaser", "region", "publish_time",
                 "project_amount", "notice_type", "categories")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/full")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true",
                    help="重建 winners（= 规则层结果），仅 bidders 做并集。"
                         "用于修复历史污染（曾把「未中标」表的名字误判为中标方）。")
    args = ap.parse_args()

    ids = [ln.strip() for ln in open("all_ids.txt", encoding="utf-8") if ln.strip()]
    n = 0
    gained_loser = 0
    before_losers = after_losers = 0
    for aid in ids:
        ip = os.path.join(args.out, aid, "ir.json")
        ep = os.path.join(args.out, aid, "items_llm.json")
        if not (os.path.exists(ip) and os.path.exists(ep)):
            continue
        rec = json.load(open(ip, encoding="utf-8"))
        ext = json.load(open(ep, encoding="utf-8"))
        ann = ext.get("announcement") or {}
        rann = rule_build(rec)["announcement"]

        def dedup(xs, strict=False):
            seen, out = set(), []
            for x in xs:
                # 归一：剥离「第1轮-/最终报价-」等轮次前缀（存量数据里同实体被拆成两家）
                x = _RE_ROUND_PREFIX.sub("", str(x or "").strip()).strip()
                if not x or x in seen:
                    continue
                # strict=True：只保留「像市场主体名」的（清洗历史污染，
                # 如「投标人承诺函」「包1供应商评审情况表」这类附件名残留）
                if strict and not _valid_entity(x):
                    continue
                seen.add(x)
                out.append(x)
            return out

        def merge_prefix(xs):
            """同篇内 A 是 B 的严格前缀 -> A 视为截断名丢弃。"""
            xs = list(xs)
            return [a for i, a in enumerate(xs)
                    if not any(a != b and b.startswith(a) and len(b) > len(a)
                               for j, b in enumerate(xs) if j != i)]

        w0 = ann.get("winners") or []
        b0 = ann.get("bidders") or []
        if args.rebuild:
            # 重建：中标方以规则层为准（历史污染无法靠并集消除）；
            # 参与投标人 = 规则层结果 ∪ 历史中仍像主体名的（清洗残留）。
            w = merge_prefix(dedup(rann.get("winners") or [], strict=True))
            b = merge_prefix(dedup(list(rann.get("bidders") or []) + list(b0), strict=True))
        else:
            w = dedup(list(w0) + list(rann.get("winners") or []))
            b = dedup(list(b0) + list(rann.get("bidders") or []))
        before_losers += len([x for x in b0 if x not in set(w0)])
        after_losers += len([x for x in b if x not in set(w)])
        if len([x for x in b if x not in set(w)]) > len([x for x in b0 if x not in set(w0)]):
            gained_loser += 1
        ann["winners"], ann["bidders"] = w, b
        # 供应商 -> 统一社会信用代码（实体唯一键；源里公布率不高，故仅作 identity 提示）
        us = _supplier_uscc(rec)
        if us:
            ann["supplier_uscc"] = us
        pm = _procurement_mode(rec)
        if pm:
            ann["procurement_mode"] = pm
        # 产品供应商（制造商）：来自中小企业声明函正文。
        # 注意**不排除中标供应商** —— 货物类里「中标供应商本身就是制造商」是合法情形。
        mfr = _manufacturers_from_declaration(
            rec, exclude=(ann.get("purchaser"), ann.get("project_name")))
        if mfr:
            seen_m, mlist = set(), []
            for x in mfr:
                if x not in seen_m:
                    seen_m.add(x)
                    mlist.append(x)
            ann["manufacturers"] = mlist
        for k in FILL_IF_EMPTY:
            if not ann.get(k) and rann.get(k):
                ann[k] = rann[k]
        ext["announcement"] = ann
        n += 1
        if not args.dry_run:
            with open(ep, "w", encoding="utf-8") as f:
                json.dump(ext, f, ensure_ascii=False, indent=2, default=str)

    print(f"{'[dry-run] ' if args.dry_run else ''}刷新公告级 {n} 篇")
    print(f"参与投标方(bidders - winners) 总数: {before_losers} -> {after_losers}"
          f"（+{after_losers - before_losers}）")
    print(f"新增识别出未中标方的篇数: {gained_loser}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
