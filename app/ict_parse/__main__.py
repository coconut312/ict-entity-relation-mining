"""CLI: 端到端跑公告解析。

用法:
    uv run python -m ict_parse                              # 默认 2 篇 demo
    uv run python -m ict_parse <id> [<id>...]               # 指定公告 id
    uv run python -m ict_parse --ids-file sample50.txt --out out/sample50 --report
    uv run python -m ict_parse --sample 50 --seed 42 --out out/sample50 --report
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

from .pipeline import process_one
from .report import _collect_flags, build_report

WS = os.environ.get("ICT_WS") or str(Path(__file__).resolve().parents[2])
HTML_DIR = os.path.join(WS, "赛题五.基准测试数据_html")
ZIP_DIR = os.path.join(WS, "赛题五.基准测试数据_file")
OUT_ROOT = os.path.join(WS, "app", "out")

DEMO = ["t20260202_26139731", "t20260905_27275031"]


def _resolve_ids(args) -> list[str]:
    if args.ids:
        return args.ids
    if args.ids_file:
        with open(args.ids_file, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]
    if args.sample:
        hids = {os.path.splitext(f)[0] for f in os.listdir(args.html_dir) if f.endswith(".html")}
        zids = {os.path.splitext(f)[0] for f in os.listdir(args.zip_dir) if f.endswith(".zip")}
        both = sorted(hids & zids)
        random.seed(args.seed)
        return random.sample(both, min(args.sample, len(both)))
    return DEMO


ITEM_COLS = ["item_name", "category", "category_code", "brand", "spec", "quantity", "unit", "unit_price",
             "total_price", "supplier_name", "is_winner", "package_no", "item_type", "source", "needs_review"]


def _write_extraction(out_root: str, ids: list[str]) -> None:
    """汇总各篇 items.json -> items.csv / announcements.csv + 字段非空率。"""
    import csv
    import json

    items_rows: list[dict] = []
    ann_rows: list[dict] = []
    for aid in ids:
        ip = os.path.join(out_root, aid, "items.json")
        if not os.path.exists(ip):
            continue
        with open(ip, encoding="utf-8") as f:
            ext = json.load(f)
        ann, items = ext["announcement"], ext["items"]
        ann_rows.append({
            "id": aid, "notice_type": ann["notice_type"], "project_name": ann["project_name"],
            "project_code": ann["project_code"], "purchaser": ann["purchaser"],
            "project_amount": ann["project_amount"], "region": ann["region"],
            "n_winners": len(ann["winners"]), "n_bidders": len(ann["bidders"]), "n_items": len(items),
        })
        for it in items:
            row = {"announcement_id": aid}
            row.update({c: it.get(c) for c in ITEM_COLS})
            items_rows.append(row)

    ic_path = os.path.join(out_root, "items.csv")
    ac_path = os.path.join(out_root, "announcements.csv")

    def _save(path, fields, data):
        try:
            f = open(path, "w", encoding="utf-8-sig", newline="")
        except PermissionError:
            path = path.replace(".csv", "_new.csv")
            f = open(path, "w", encoding="utf-8-sig", newline="")
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(data)
        f.close()
        return path

    ic = _save(ic_path, ["announcement_id"] + ITEM_COLS, items_rows)
    ac = _save(ac_path, list(ann_rows[0].keys()) if ann_rows else ["id"], ann_rows)

    n = len(items_rows)
    fields = ["item_name", "category", "brand", "spec", "quantity", "unit_price", "total_price", "supplier_name"]
    nn = {f: sum(1 for r in items_rows if r.get(f) not in (None, "")) for f in fields}
    print(f"\n明细表 -> {ic}（{n} 行）")
    print(f"公告表 -> {ac}（{len(ann_rows)} 篇）")
    print("明细字段非空率:", {f: (round(v / n, 3) if n else 0.0) for f, v in nn.items()})


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="任务一解析层：公告 html + 附件 zip -> IR JSON")
    ap.add_argument("ids", nargs="*", default=None, help="公告 id（文件名去扩展名）")
    ap.add_argument("--ids-file", default=None, help="从文件读取 id（每行一个）")
    ap.add_argument("--sample", type=int, default=0, help="从（含附件的）公告中随机抽 N 篇")
    ap.add_argument("--seed", type=int, default=42, help="随机种子")
    ap.add_argument("--html-dir", default=HTML_DIR)
    ap.add_argument("--zip-dir", default=ZIP_DIR)
    ap.add_argument("--out", default=OUT_ROOT)
    ap.add_argument("--report", action="store_true", help="生成 HTML 核验报告 + CSV 汇总")
    ap.add_argument("--extract", action="store_true", help="运行规则抽取层，汇总 items.csv / announcements.csv")
    ap.add_argument("--vlm", action="store_true", help="对扫描件 PDF/office 文档启用 Qwen-VL 视觉识别（需 DASHSCOPE_API_KEY）")
    ap.add_argument("--llm", action="store_true", help="用 LLM(Qwen/DeepSeek) 抽取（需 LLM_API_KEY / DEEPSEEK_API_KEY）")
    ap.add_argument("--verbose", action="store_true", help="逐篇打印详细结构")
    args = ap.parse_args(argv)

    ids = _resolve_ids(args)
    print(f"共 {len(ids)} 篇待处理 -> {args.out}")

    for idx, aid in enumerate(ids, 1):
        html = os.path.join(args.html_dir, aid + ".html")
        zp = os.path.join(args.zip_dir, aid + ".zip")
        if not os.path.exists(html):
            print(f"[{idx}/{len(ids)}][skip] html not found: {aid}")
            continue
        rec, out = process_one(aid, html, zp if os.path.exists(zp) else None, args.out,
                               use_vlm=args.vlm, use_llm=args.llm)
        groups = rec["main_item_tables"]
        item_rows = sum(len(g["rows"]) for g in groups)
        flags = _collect_flags(rec)
        flag_s = f" | ⚠ {len(flags)}" if flags else ""
        print(f"[{idx}/{len(ids)}] {aid} | {len(rec['sections'])}节 {len(groups)}表/{item_rows}行 "
              f"| 附件{len(rec['extracted_files'])}个{flag_s}")
        if args.verbose:
            for i, g in enumerate(groups[:5]):
                print(f"      table[{i}] headers={g['headers']} rows={len(g['rows'])}")
            for a in rec["extracted_files"]:
                print(f"    - {a['rel_path']} [{a['ext']}] text={len(a.get('text') or '')} "
                      f"tables={len(a.get('tables') or [])} note={a.get('note')}")

    if args.report:
        rp = os.path.join(args.out, "report.html")
        cp = os.path.join(args.out, "summary.csv")
        stat = build_report(ids, args.out, rp, cp)
        print(f"\n报告已生成: {rp}\n汇总已生成: {cp}")
        print(f"无异常 {stat['ok']} 篇 | 需关注 {stat['flagged']} 篇 | 缺失 {stat['missing']} 篇")
    if args.extract:
        _write_extraction(args.out, ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
