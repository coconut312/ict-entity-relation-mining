# -*- coding: utf-8 -*-
"""全量「归位式回填」：对 `out/full/<公告号>/` 全量跑一遍 `ict_parse.backfill`。

只补空值、不覆盖、逐条留凭证（侧车 `deliverables/backfill_provenance.csv`）。
默认 **dry-run**：只打印「每个字段 / 每个来源标签能补多少条」与样例，不改任何文件。

用法（在 `app/` 目录下）::

    .venv\\Scripts\\python.exe scripts\\backfill_deliverables.py                # = --dry-run
    .venv\\Scripts\\python.exe scripts\\backfill_deliverables.py --dry-run --limit 50
    .venv\\Scripts\\python.exe scripts\\backfill_deliverables.py --apply        # 落地 + 重生成交付件

`--apply` 做的事（顺序即依赖）：
  1. 把回填结果写回 `out/full/<id>/items_llm.json` 的 `announcement`（**只覆盖空字段**，幂等）；
  2. 重生成任务一交付表 `deliverables/announcements_task1.csv` + `items_task1.csv`；
  3. 重建图库 `out/task2/graph.db`（含 `deliverables/task2_*.csv`）；
  4. 重生成官方三表 `deliverables/official/*`；
  5. 重灌平台库 `out/platform/platform.db`；
  6. 重建 Neo4j（`--load --wipe --export`，需 Neo4j 在跑；`--no-neo4j` 可跳过）。

`--apply` 每一步都会打印返回码并在任一失败时立即停止（**绝不带着半套交付件往下走**）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]               # .../app
sys.path.insert(0, str(APP))

# Neo4j 连接默认值：与 `task3/backend/run.py` 的 `_set_env_defaults()` 保持一致
# （本机开发实例的口令；显式设了环境变量则以环境变量为准）。
# 不设这个默认值时 `--apply` 的最后一步会以 AuthError 失败（口径是对的，但一步就跑不通）。
for _k, _v in (("NEO4J_URI", "bolt://127.0.0.1:7687"), ("NEO4J_USER", "neo4j"),
               ("NEO4J_PASSWORD", "ict2026neo4j")):
    os.environ.setdefault(_k, _v)

from ict_parse import backfill                          # noqa: E402

FULL = APP / "out" / "full"
DELIVERABLES = APP / "deliverables"
OFFICIAL = DELIVERABLES / "official"
T1_NOTICE = DELIVERABLES / "announcements_task1.csv"
T1_ITEM = DELIVERABLES / "items_task1.csv"
TOTAL = OFFICIAL / "总表.csv"
ITEM = OFFICIAL / "标的表.csv"
BIDDER = OFFICIAL / "投标人表.csv"


def _ids(limit: int = 0, ids_file: Path | None = None) -> list[str]:
    """公告号清单：优先 `all_ids.txt`（与抽取/导出同一份），否则扫 `out/full/` 目录。"""
    p = ids_file or (APP / "all_ids.txt")
    out: list[str] = []
    if p.exists():
        out = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    else:
        out = sorted(d.name for d in FULL.iterdir() if d.is_dir())
    return out[:limit] if limit else out


def _csv_stats(path: Path, cols: list[str]) -> tuple[int, dict[str, int]]:
    """`(行数, {列: 非空行数})` —— `--apply` 前后对比用。"""
    import csv
    if not path.exists():
        return 0, {c: 0 for c in cols}
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    nonempty = {c: sum(1 for r in rows if str(r.get(c) or "").strip()
                       not in ("", "0", "0.0")) for c in cols}
    return len(rows), nonempty


def _provenance_lines() -> int:
    p = backfill.provenance_path()
    if not p.exists():
        return 0
    with open(p, encoding="utf-8-sig") as f:
        return max(0, sum(1 for _ in f) - 1)


def _run(step: str, cmd: list[str]) -> int:
    print(f"\n[--apply] {step}: {' '.join(cmd)}", flush=True)
    t0 = time.time()
    rc = subprocess.call(cmd, cwd=str(APP))
    print(f"[--apply] {step} -> exit {rc}（{round(time.time() - t0, 1)}s）", flush=True)
    return rc


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="全量归位式回填（只补空值、逐条留凭证）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="只算不写（默认）")
    g.add_argument("--apply", action="store_true", help="落地并重生成交付件")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 篇（排查用）")
    ap.add_argument("--ids", default="", help="公告号清单文件（默认 app/all_ids.txt）")
    ap.add_argument("--show", type=int, default=10, help="打印多少条样例（0=不打印）")
    ap.add_argument("--no-neo4j", action="store_true", help="--apply 时跳过 Neo4j 重建")
    ap.add_argument("--json", default="", help="把统计写成 JSON（可选）")
    a = ap.parse_args()
    apply_mode = bool(a.apply)
    if a.dry_run:
        apply_mode = False

    if not backfill.enabled():
        print("EDM_BACKFILL=0：回填已关闭，未做任何事")
        return 0

    raw_ids = _ids(a.limit, Path(a.ids) if a.ids else None)
    recs: list[dict] = []
    missing: list[str] = []
    for aid in raw_ids:
        rec = backfill.load_record(aid, FULL)
        if rec is None:
            missing.append(aid)
            continue
        rec["_origin"] = f"out/full/{aid}"
        recs.append(rec)
    print(f"回填范围：{len(recs)} 篇（清单 {len(raw_ids)} 篇，缺 items_llm.json {len(missing)} 篇）")
    if missing:
        print(f"  缺少 items_llm.json（跳过，绝不从别处凑数据）：{missing[:10]}"
              f"{' …' if len(missing) > 10 else ''}")

    # ---- 前后基线（--apply 时用于「行数/非空率」对比）----
    base_notice = _csv_stats(T1_NOTICE, ["purchaser", "publish_time", "project_amount",
                                         "n_winners", "n_bidders"])
    base_total = _csv_stats(TOTAL, ["采购单位", "中标供应商", "中标金额"])
    base_item = _csv_stats(ITEM, ["产品服务名称", "品牌", "单价", "总价"])
    base_bidder = _csv_stats(BIDDER, ["名称", "类别", "报价（万元）", "得分"])
    base_prov = _provenance_lines()

    t0 = time.time()
    rep = backfill.apply(recs, scope={r["announcement_id"] for r in recs},
                         dry_run=not apply_mode, write_provenance=apply_mode)
    elapsed = round(time.time() - t0, 1)
    print()
    for line in rep.lines(show_samples=a.show):
        print(line)
    print(f"耗时 {elapsed}s")

    if not apply_mode:
        print("\n[dry-run] 未改动任何文件（items_llm.json / 交付表 / 侧车凭证都没写）。"
              "\n要落地请加 --apply。")
        if a.json:
            Path(a.json).write_text(json.dumps({
                "mode": "dry-run",
                "n_records": rep.n_records,
                "n_scope": rep.n_scope,
                "field_counts": rep.field_counts(),
                "tag_counts": {f"{k[0]}|{k[1]}": v for k, v in rep.tag_counts().items()},
                "skipped_has_value": rep.skipped_has_value,
                "skipped_no_evidence": rep.skipped_no_evidence,
                "rejected": rep.rejected,
                "patches": [p.as_row() for p in rep.patches],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    # ---- 落地：写回 items_llm.json ----
    changed = 0
    for rec in recs:
        try:
            changed += 1 if backfill.persist_record(rec, FULL) else 0
        except Exception as e:                          # noqa: BLE001
            print(f"[警告] 写回失败 {rec.get('announcement_id')}：{type(e).__name__}: {e}")
    print(f"\n写回 items_llm.json：{changed} 篇变更（只填空字段）")
    print(f"侧车凭证 {backfill.provenance_path()}：{base_prov} -> {_provenance_lines()} 行")

    # ---- 重生成交付件（每步都查返回码）----
    py = sys.executable
    steps: list[tuple[str, list[str]]] = [
        ("任务一交付表", [py, str(APP / "scripts" / "export_task1_tables.py")]),
        ("图库 graph.db", [py, str(APP / "scripts" / "build_task2_graph.py")]),
        ("官方三表", [py, str(APP / "scripts" / "export_official_format.py")]),
        ("平台库 platform.db", [py, str(APP / "platform" / "ingest.py"),
                                "--db", str(APP / "out" / "platform" / "platform.db")]),
    ]
    if not a.no_neo4j:
        steps.append(("Neo4j 重建", [py, str(APP / "scripts" / "build_neo4j_graph.py"),
                                    "--load", "--wipe", "--export"]))
    for name, cmd in steps:
        rc = _run(name, cmd)
        if rc != 0:
            print(f"\n[失败] {name} 返回 {rc}：已停止，不再继续后续步骤（避免半套交付件）。")
            return rc

    # ---- 前后对比 ----
    new_notice = _csv_stats(T1_NOTICE, ["purchaser", "publish_time", "project_amount",
                                        "n_winners", "n_bidders"])
    new_total = _csv_stats(TOTAL, ["采购单位", "中标供应商", "中标金额"])
    new_item = _csv_stats(ITEM, ["产品服务名称", "品牌", "单价", "总价"])
    new_bidder = _csv_stats(BIDDER, ["名称", "类别", "报价（万元）", "得分"])

    def _diff(label: str, before, after) -> None:
        (nb, cb), (na, ca) = before, after
        print(f"\n{label}：行数 {nb} -> {na}")
        for c in cb:
            pct_b = (cb[c] / nb) if nb else 0
            pct_a = (ca[c] / na) if na else 0
            print(f"  {c:<8} 非空 {cb[c]}({pct_b:.1%}) -> {ca[c]}({pct_a:.1%})"
                  f"（+{ca[c] - cb[c]}）")

    _diff("公告表 announcements_task1.csv", base_notice, new_notice)
    _diff("官方总表 总表.csv", base_total, new_total)
    _diff("官方标的表 标的表.csv", base_item, new_item)
    _diff("官方投标人表 投标人表.csv", base_bidder, new_bidder)
    print(f"\n侧车凭证行数：{base_prov} -> {_provenance_lines()}"
          f"（{backfill.provenance_path()}）")
    print(rep.message())

    # ---- 一致性校验（都是只读脚本）----
    print("\n==== 一致性校验 ====")
    checks = [
        ("assert_official_consistency", [py, str(APP / "scripts" / "assert_official_consistency.py")]),
        ("assert_graph_consistency", [py, str(APP / "scripts" / "assert_graph_consistency.py")]),
    ]
    if not a.no_neo4j:
        checks.append(("verify_neo4j", [py, str(APP / "scripts" / "verify_neo4j.py")]))
    for name, cmd in checks:
        rc = _run(name, cmd)
        if rc != 0:
            print(f"[失败] {name} 返回 {rc}")
            return rc
    if a.json:
        Path(a.json).write_text(json.dumps({
            "mode": "apply",
            "n_records": rep.n_records,
            "n_scope": rep.n_scope,
            "changed_files": changed,
            "field_counts": rep.field_counts(),
            "tag_counts": {f"{k[0]}|{k[1]}": v for k, v in rep.tag_counts().items()},
            "provenance_rows": _provenance_lines(),
            "before": {"notice": base_notice, "total": base_total,
                       "item": base_item, "bidder": base_bidder},
            "after": {"notice": new_notice, "total": new_total,
                      "item": new_item, "bidder": new_bidder},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
