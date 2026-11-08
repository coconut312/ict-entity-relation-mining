# -*- coding: utf-8 -*-
"""打包「任务一/二/三 整合提交包」。

策略：保留**可运行的目录结构**（文档里的命令原样可用），只排除
  依赖(.venv/node_modules)、中间产物(out/ 大部分)、原始赛题数据(3.4GB 等)、git 历史。

out/ 下的收录白名单（`OUT_INCLUDE`，按**文件名**匹配、任意层级生效）：
  * `items_llm.json` / `items.json` —— 逐篇抽取结果（上传管线/测试用例产物同样叫这两个名字）
  * `graph.db` —— 任务二关系库（约 3.2 MB），**部署必需**：`platform/ingest.py` 用它物化
    任务二 10 张关系/画像表；`scripts/build_neo4j_graph.py` 用它建图。缺它则平台与建图
    只能退回「交付表 CSV」路径（可用但能力受限），所以必须随包。

out/ 下的排除（`_` 前缀目录）：`_up_test` / `_bench` / `_noise` / `_chunk` / `_w_test` /
  `_bidder_test` / `_r2` / `_r3` / `_bf_*` / `_judge` / `_render_*` / `_smoke_copy` /
  `_base` / `_pdf_*` 等全是**测试/临时目录**（gitignored、可再生产、共约 380 MB），
  一律不进包；磁盘上由 `--clean` 先清掉（默认开启，`--keep-out-junk` 关闭）。

用法:
    uv run python scripts/pack_delivery.py            # 清理 out/_* + 打包到 dist/
    uv run python scripts/pack_delivery.py --dry      # 只统计体量，不清理不落盘
    uv run python scripts/pack_delivery.py --keep-out-junk   # 不清理 out/_*
    uv run python scripts/pack_delivery.py --out xxx.zip
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
WS = os.environ.get("ICT_WS") or str(Path(__file__).resolve().parents[2])
APP = os.path.join(WS, "app")
ROOT_IN_ZIP = "ict_任务一二三_整合包"

# 排除规则（目录名，任意层级）
EXCLUDE_DIRS = {
    ".git", ".venv", "node_modules", "__pycache__", ".idea", ".vscode",
    "赛题五.基准测试数据_file", "赛题五.基准测试数据_html", "C类", "E类", "_file",
    "_incoming", "backup_gold", "dist", "full", ".reasonix", "uploads", "var",
    # 成品质检/提交暂存目录：内含上一次打包的旧 zip 与旧版文档，不能再次打进交付包
    "提交材料",
}
# 排除规则（文件名）
EXCLUDE_FILES = {"llm_settings.json", ".env", "auth_backup.zip", "ict - 电子杯ict.md",
                 # 团队内部评审材料（历史 diff 记录）：源文件保留在仓库，但不进交付包
                 "task3-modified-diff.md"}
# app/scripts/ 下的测试残留（`_tmp_ids.txt` / `_dq_verify_tmp.py` 等）不进包；
# `_metric.py` 是白名单：它被 `scripts/val_eval.py:57` 引用，必须随包。
EXCLUDE_SCRIPT_PREFIX = "_"
SCRIPT_KEEP = {"_metric.py"}
# out/ 下只收这些文件（按文件名，任意层级）：逐篇抽取 JSON + 任务二关系库 + 模型调用日志
OUT_INCLUDE = ("items_llm.json", "items.json", "graph.db",
               "llm_calls.jsonl", "data_quality_profile.json")
# app/out/ 下以 `_` 开头的目录 = 测试/临时目录（gitignored 可再生产物），一律不打包
OUT_JUNK_PREFIX = "_"


def human(n: float) -> str:
    return "%.1f MB" % (n / 1024 / 1024)


def _is_out_junk_dir(parts: list[str], dirname: str) -> bool:
    """`app/out/_xxx` 及更深层的 `_xxx` 目录 -> 视为测试垃圾。"""
    if len(parts) < 2 or parts[0] != "app" or parts[1] != "out":
        return False
    return dirname.startswith(OUT_JUNK_PREFIX)


def clean_out_junk(dry: bool = False) -> tuple[int, int, float]:
    """删除 `app/out/` 下的 `_*` 测试目录（逐路径确认落在 app/out 内）。

    返回 (删除目录数, 文件数, 释放字节)。`_` 前缀目录在 `.gitignore` 的 `app/out/`
    规则下本来就未入库，属可再生产物，删除不影响仓库与交付数据。
    """
    out_root = os.path.join(APP, "out")
    out_abs = os.path.abspath(out_root)
    if not os.path.isdir(out_abs):
        return 0, 0, 0.0
    n_dirs = n_files = 0
    freed = 0.0
    for name in sorted(os.listdir(out_abs)):
        target = os.path.abspath(os.path.join(out_abs, name))
        # ---- 安全检查：必须是 app/out 的直接子目录，且有 `_` 前缀，且不是符号链接 ----
        if os.path.dirname(target) != out_abs:
            raise SystemExit("拒绝删除（不在 app/out 直接子目录）: %s" % target)
        if not name.startswith(OUT_JUNK_PREFIX):
            continue
        if os.path.islink(target):
            raise SystemExit("拒绝删除（符号链接）: %s" % target)
        if not os.path.isdir(target):
            continue
        for cur, _dirs, files in os.walk(target):
            for f in files:
                try:
                    freed += os.path.getsize(os.path.join(cur, f))
                except OSError:
                    pass
                n_files += 1
        n_dirs += 1
        print("  - 删除 %s（%d 个文件）" % (target, sum(len(fs) for _, _, fs in os.walk(target))))
        if not dry:
            shutil.rmtree(target, ignore_errors=True)
    return n_dirs, n_files, freed


def collect(with_raw_json: bool = False) -> tuple[list[tuple[str, str]], list[str]]:
    """返回 ([(zip 内路径, 磁盘路径)], 说明列表)。

    `with_raw_json=True` 时不剪掉 `out/full`，此时只会有白名单文件名通过
    （`items_llm.json`/`items.json`，共 2076 个文件约 16 MB），附件副本仍被挡掉。
    """
    exclude_dirs = set(EXCLUDE_DIRS)
    if with_raw_json:
        exclude_dirs.discard("full")
    items: list[tuple[str, str]] = []
    for cur, dirs, files in os.walk(WS):
        rel_dir = os.path.relpath(cur, WS)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        dirs[:] = [d for d in dirs
                   if d not in exclude_dirs and not _is_out_junk_dir(parts, d)]
        # app/out/ 特殊处理：只收白名单文件（逐篇 JSON / 关系库），其余中间产物全丢
        for f in files:
            if f in EXCLUDE_FILES:
                continue
            # app/scripts/ 的 `_*` = 测试残留（白名单 `_metric.py` 保留，val_eval 引用它）
            if (len(parts) >= 2 and parts[0] == "app" and parts[1] == "scripts"
                    and f.startswith(EXCLUDE_SCRIPT_PREFIX) and f not in SCRIPT_KEEP):
                continue
            if parts[:2] == ["app", "out"]:
                if f not in OUT_INCLUDE:
                    continue
            arc = os.path.join(ROOT_IN_ZIP, rel_dir, f) if rel_dir != "." \
                else os.path.join(ROOT_IN_ZIP, f)
            items.append((arc, os.path.join(cur, f)))
    return items, [
        "排除：`.venv`/`node_modules`（依赖）、`app/out/_*`（测试目录）、"
        "`out/full` 里的附件副本（887 MB，只留逐篇 JSON）、"
        "`out/` 下白名单之外的中间产物、`app/scripts/_*` 测试残留（仅保留 `_metric.py`）、"
        "`docs/task3-modified-diff.md`（团队内部评审材料）、"
        "原始赛题数据（`赛题五.基准测试数据_file` 约 3.4 GB 等）、`.git` 历史、`C类`/`E类`、内部工作笔记",
        "收录：`items_llm.json`/`items.json`（逐篇抽取）+ `graph.db`（任务二关系库，部署必需）"
        "+ `llm_calls.jsonl`（模型调用日志）+ `data_quality_profile.json`",
    ]


# ---- 包内自检用的关键文件清单（后缀匹配）----
KEY_REQUIRED = [
    "app/deliverables/items_task1.csv",
    "app/deliverables/announcements_task1.csv",
    "app/deliverables/items_task1_rule.csv",
    "app/deliverables/announcements_task1_rule.csv",
    "app/deliverables/task2_scenario_report.md",
    "app/deliverables/model_manifest.json",
    "app/deliverables/official/总表.csv",
    "app/deliverables/official/标的表.csv",
    "app/deliverables/official/投标人表.csv",
    "app/deliverables/official/排除评估清单.csv",
    "app/deliverables/official/官方数据集提取结果.xlsx",
    "app/deliverables/过程性文档.docx",
    "app/deliverables/过程性文档.pdf",
    "app/deliverables/系统设计文档.docx",
    "app/deliverables/系统设计文档.pdf",
    "app/deliverables/数据质量分析报告.docx",
    "app/deliverables/数据质量分析报告.pdf",
    "app/deliverables/platform_snapshot_1038.db",
    "app/deliverables/backfill_provenance.csv",
    "app/deliverables/neo4j/task2_graph.cypher",
    "app/deliverables/neo4j/README.txt",
    "app/out/task2/graph.db",
    "app/pyproject.toml",
    "app/uv.lock",
    "app/task3/backend/requirements.txt",
    "app/task3/backend/run.py",
    "app/task3/backend/static/index.html",
    "app/task3/docs/评审方操作说明.md",
    "app/scripts/build_neo4j_graph.py",
    "app/scripts/export_official_format.py",
    "app/platform/ingest.py",
    "docs/process-document.md",
    "docs/system-design-document.md",
    "docs/data-quality-report.md",
    "提交说明与运行指南.md",
    "部署说明.md",
    "软件版本与运行环境.md",
]


def selfcheck(zip_path: str, total_raw: float) -> None:
    print("\n=== 包内自检 ===")
    size = os.path.getsize(zip_path)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        print("  体积: %s（原始 %s，压缩率 %.0f%%）"
              % (human(size), human(total_raw), 100 * size / max(total_raw, 1)))
        print("  条目数:", len(names))
        top = sorted({n.split("/")[1] for n in names if n.count("/") >= 1 and n.split("/")[1]})
        print("  顶层:", top[:12])
        bad = [n for n in names if re.search(r"(llm_settings\.json|\.env$|id_rsa|\.pem$)", n)]
        print("  敏感文件命中:", bad or "无")
        junk = [n for n in names if re.search(r"/app/out/_[^/]*/", n)]
        print("  out/_* 测试垃圾:", junk[:3] or "无（0 条）")
        script_junk = [n for n in names if re.search(r"/app/scripts/_", n)]
        print("  scripts/_* 残留:", script_junk[:5] or "无（0 条，_metric.py 白名单）")
        if not any(n.endswith("/app/scripts/_metric.py") for n in names):
            print("  [WARN] 缺少 app/scripts/_metric.py（val_eval.py 引用它）")
        diff_doc = [n for n in names if n.endswith("docs/task3-modified-diff.md")]
        print("  内部评审材料 task3-modified-diff.md:", diff_doc or "无（已排除）")
        missing = [t for t in KEY_REQUIRED if not any(n.endswith(t) for n in names)]
        print("  关键文件缺失:", missing or "无")

        def find(suffix: str) -> str | None:
            return next((n for n in names if n.endswith(suffix)), None)

        for suffix, label in (("app/deliverables/items_task1.csv", "items_task1.csv"),
                              ("app/deliverables/announcements_task1.csv", "announcements_task1.csv")):
            hit = find(suffix)
            if hit:
                data = z.read(hit).decode("utf-8-sig")
                rows = sum(1 for _ in csv.reader(io.StringIO(data))) - 1
                print("  %-34s %d 行" % (label, rows))
        t2 = [n for n in names if re.search(r"app/deliverables/task2_[^/]*\.csv$", n)]
        neo = [n for n in names if re.search(r"app/deliverables/neo4j/[^/]*\.csv$", n)]
        per = [n for n in names if re.search(r"app/out/full/[^/]+/items_llm\.json$", n)]
        offi = [n for n in names if re.search(r"app/deliverables/official/.*\.(csv|xlsx)$", n)]
        for suffix in ("app/deliverables/official/总表.csv", "app/deliverables/official/标的表.csv",
                       "app/deliverables/official/投标人表.csv"):
            hit = find(suffix)
            if hit:
                data = z.read(hit).decode("utf-8-sig")
                rd = csv.reader(io.StringIO(data))
                hdr = next(rd)
                n = sum(1 for _ in rd)
                print("  official/%-24s %d 列 / %d 行" % (os.path.basename(suffix), len(hdr), n))
        g = find("app/out/task2/graph.db")
        if g:
            print("  %-34s %s" % ("out/task2/graph.db", human(z.getinfo(g).file_size)))
        print("  task2_*.csv 个数: %d ｜ neo4j/*.csv: %d ｜ official 表: %d ｜ out/full 逐篇 JSON: %d"
              % (len(t2), len(neo), len(offi), len(per)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        WS, "dist", "ict_任务一二三整合包_%s.zip" % time.strftime("%Y%m%d")))
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--keep-out-junk", action="store_true",
                    help="不清理 app/out/_*（默认清理）")
    ap.add_argument("--with-raw-json", action="store_true",
                    help="额外收录 out/full 下的逐篇 items_llm.json/items.json（约 16 MB 原始）")
    a = ap.parse_args()

    if not a.keep_out_junk:
        print("=== 清理 app/out/_* 测试目录 ===")
        d, f, freed = clean_out_junk(dry=a.dry)
        print("  共 %d 个目录 / %d 个文件，释放 %s%s"
              % (d, f, human(freed), "（dry-run 未删）" if a.dry else ""))
    else:
        print("=== 按要求跳过 out/_* 清理 ===")

    items, notes = collect(with_raw_json=a.with_raw_json)
    total = sum(os.path.getsize(p) for _, p in items if os.path.exists(p))
    print("\n将打包 %d 个文件，合计 %s" % (len(items), human(total)))
    for n in notes:
        print("  " + n)
    if a.dry:
        return 0

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for arc, p in items:
            if os.path.exists(p):
                z.write(p, arc.replace("\\", "/"))
    print("\n已生成: %s" % a.out)
    selfcheck(a.out, total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
