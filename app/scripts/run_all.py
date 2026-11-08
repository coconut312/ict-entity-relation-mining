"""全量跑：解析 + 规则抽取，统计覆盖率/字段率/速率。可断点续跑。

用法: uv run python scripts/run_all.py [--out out/full] [--force]
"""
import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ict_parse.pipeline import process_one  # noqa: E402

WS = os.environ.get("ICT_WS") or str(Path(__file__).resolve().parents[2])
HTML_DIR = os.path.join(WS, "赛题五.基准测试数据_html")
ZIP_DIR = os.path.join(WS, "赛题五.基准测试数据_file")
F7 = ["item_name", "category", "brand", "spec", "unit_price", "quantity", "total_price"]
NF = F7 + ["supplier_name"]


def rate_score(k):
    return 10 if k == 7 else 5 if k >= 5 else 3 if k >= 3 else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(WS, "app", "out", "full"))
    ap.add_argument("--force", action="store_true", help="忽略已有结果重跑")
    args = ap.parse_args()

    ids = sorted(os.path.splitext(f)[0] for f in os.listdir(HTML_DIR) if f.endswith(".html"))
    os.makedirs(args.out, exist_ok=True)

    stats = {"ok": 0, "fail": 0, "skip_resume": 0, "cov_html": 0, "cov_att": 0,
             "f7": {}, "nonnull": {f: 0 for f in NF}, "items": 0, "times": [], "fail_ids": []}
    t_all = time.time()
    n = len(ids)
    for i, aid in enumerate(ids, 1):
        html = os.path.join(HTML_DIR, aid + ".html")
        zp = os.path.join(ZIP_DIR, aid + ".zip")
        items_json = os.path.join(args.out, aid, "items.json")
        try:
            if os.path.exists(items_json) and not args.force:
                ext = json.load(open(items_json, encoding="utf-8"))
                stats["skip_resume"] += 1
            else:
                t0 = time.time()
                rec, _ = process_one(aid, html, zp if os.path.exists(zp) else None, args.out)
                stats["times"].append(time.time() - t0)
                ext = rec["extraction"]
            items = ext["items"]
            stats["ok"] += 1
            stats["items"] += len(items)
            # 口径与 scripts/selfcheck.py 对齐：附件类来源含 ATTACHMENT / VLTEXT(视觉) / ANNTEXT(正文块)
            if any(it.get("source") == "MAIN_ITEM_TABLE" for it in items):
                stats["cov_html"] += 1
            if any((it.get("source") or "").startswith(("ATTACHMENT", "VLTEXT", "ANNTEXT")) for it in items):
                stats["cov_att"] += 1
            present = {f for f in F7 if any(it.get(f) not in (None, "") for it in items)}
            k = len(present)
            stats["f7"][k] = stats["f7"].get(k, 0) + 1
            for f in NF:
                stats["nonnull"][f] += sum(1 for it in items if it.get(f) not in (None, ""))
        except Exception as e:  # noqa: BLE001
            stats["fail"] += 1
            stats["fail_ids"].append(aid)
            print(f"[{i}/{n}] FAIL {aid}: {e}")
            continue
        if i % 25 == 0 or i == n:
            el = time.time() - t_all
            print(f"[{i}/{n}] ok={stats['ok']} fail={stats['fail']} items={stats['items']} elapsed={el:.0f}s")

    el = time.time() - t_all
    done = stats["ok"]
    times = stats["times"]
    summary = {
        # 便于识别本文件是否过期：生成时间 + 本次是「重解析」还是「复用已有 items.json」
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "run_mode": "force(全量重解析)" if args.force else "resume(复用已有 items.json)",
        "pages": n, "ok": stats["ok"], "fail": stats["fail"], "resumed_skipped": stats["skip_resume"],
        "items": stats["items"],
        "coverage_from_html": stats["cov_html"], "coverage_from_attachment": stats["cov_att"],
        "field7_dist": {str(k): v for k, v in sorted(stats["f7"].items())},
        "field7_avg_score": round(sum(rate_score(k) * v for k, v in stats["f7"].items()) / done, 3) if done else 0,
        "nonnull_rate": {f: (round(v / stats["items"], 4) if stats["items"] else 0) for f, v in stats["nonnull"].items()},
        "elapsed_sec": round(el, 1),
        "sec_per_doc_all": round(el / n, 3),
        "sec_per_doc_measured": round(statistics.mean(times), 3) if times else None,
        "sec_per_doc_median": round(statistics.median(times), 3) if times else None,
        "fail_ids": stats["fail_ids"],
    }
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("\n=== 全量汇总 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
