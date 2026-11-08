# -*- coding: utf-8 -*-
"""导出**官方标准格式**提取结果（《输出标准样例表.xlsx》口径）—— **提交件**生成工具。

产出：`deliverables/official/官方数据集提取结果.xlsx`（3 个 sheet：总表/标的表/投标人表）
      + 同名 3 个 CSV，供自动化脚本评估。

定位（与任务三的下载功能区分）
------------------------------
  * 本脚本从**仓库内的交付物**（`out/task2/graph.db` + `deliverables/items_task1.csv`
    + `out/full/<公告号>/`）生成**提交件**，与某次用户上传无关。
  * 任务三平台「下载提取结果」端点**不走本脚本** —— 它必须下载「用户上传后解析出来的
    提取结果」，因此改为现场读 `out/platform/platform.db`（见
    `task3/backend/app/export_api.py`）。

**两张路径共用同一份生成逻辑**：三张表一律由 `ict_parse/official_tables.py::build_tables()`
产出（本脚本只负责「取数 + 落盘」），因此提交件与平台下载的口径（列名、单位、分包、
`类别` / `通过审查` / `未中标原因` 判定）逐字一致，不会漂移。

规范要点（来自《关于任务数据提取结果格式的要求及补充说明.pdf》+ 样例表）
----------------------------------------------------------------------
  * 蓝色字段 = 最小必填集；**总表 B 列 URL 已变更为灰色字段（可选，不参与评估）**
  * 总表 `中标金额` / 投标人表 `报价（万元）`：单位 **万元**，保留两位小数、四舍五入
  * 标的表 `单价` / `总价`：**保留全部有效小数**（官方答疑 Q3：单价过低时四舍五入会变成 0，
    故用 `Decimal(str(v))/10000` 精确换算，只去尾随零，绝不丢精度）
  * `标段/合同包`：未分包填「未分包」，多标段逐行列出；识别规则见 `ict_parse/packages.py`
    （官方答疑 Q2：UUID 后缀 `主ID_01/_02…` 顺序不做要求，但要**正确分包**且三表自洽）
  * 数量：正整数照录；原文未给出则留空（品牌/规格/单价/总价同理）
  * 标的表 `类别`：结果公示提取 → 「需求」；招标类提取 → 「中标」
  * 投标人表 `类别`（中标人/参与人）：**三来源取并集** —— `bid.is_winner` 真值 ∪
    交付表/标的行 `is_winner` ∪ 公告级 `announcement.winners`；名称归一后必须能定位到
    **本包**的标的行才写「中标人」（定位不到不写，保证与标的行自洽）
  * 投标人表 `通过审查`：中标人一律「通过」；参与人只在有原文证据
    （该篇「未中标原因」表 / 「资格审查」表名单）时才填「通过」，否则留空
  * 投标人表 `未中标原因`：原文「未中标原因」表按 (公告, 供应商) 对上才回填（仅参与人）
  * **列必须逐字对齐样例表：总表 8 列 / 标的表 11 列 / 投标人表 9 列，没有「备注」列**
  * UUID：与源文件前缀一致（= 公告号），不得编造；分包时加 `_01/_02…` 后缀

用法：
    cd app && .venv\\Scripts\\python.exe scripts\\export_official_format.py
"""
from __future__ import annotations

import csv
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)                       # 使 ict_parse 可导入

from ict_parse import official_tables as ot                              # noqa: E402

DB = os.path.join(APP, "out", "task2", "graph.db")
FULL = os.path.join(APP, "out", "full")
OUTDIR = os.path.join(APP, "deliverables", "official")
# 投标人名单（参与人）交付表：**全量交付集默认没有这份文件**（参与人来自 bid 关系表），
# 存在时自动纳入（与平台下载路径同源：都走 `build_tables(bidder_roster=…)`）。
ROSTER = os.path.join(APP, "deliverables", "bidder_roster.csv")
# 原文 HTML 正本（仓库根目录）—— 用于「投标人 -> 包」的**原文证据**补全
HTML_DIR = os.path.join(os.path.dirname(APP), "赛题五.基准测试数据_html")

TOTAL_COLS = ot.TOTAL_COLS
ITEM_COLS = ot.ITEM_COLS
BID_COLS = ot.BIDDER_COLS

NO_PACKAGE = ot.NO_PACKAGE
LABEL_WINNER = ot.LABEL_WINNER
LABEL_BIDDER = ot.LABEL_BIDDER


def _load_sources():
    """取数：graph.db（公告/投标关系/实体别名）+ 交付表（标的行）。"""
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    cols = {r[1] for r in c.execute("PRAGMA table_info(notice)")}
    has_type = "notice_type" in cols
    print("notice 列:", sorted(cols))

    notices = [{
        "notice_id": n["notice_id"] if "notice_id" in n.keys() else n["announcement_id"],
        "project_name": n["project_name"],
        "purchaser": n["purchaser"],
        "project_amount": n["project_amount"],
        "notice_type": (n["notice_type"] if has_type else "") or "",
    } for n in c.execute("SELECT * FROM notice")]

    bids = [{
        "notice_id": b["notice_id"],
        "entity_id": b["entity_id"],
        "supplier": b["supplier"],
        "is_winner": b["is_winner"],
        "amount": b["amount"],
    } for b in c.execute("SELECT * FROM bid")]

    entities = {r["entity_id"]: r["canonical_name"] for r in c.execute("SELECT * FROM entity")}
    aliases: dict = {}
    for r in c.execute("SELECT entity_id, name FROM entity_alias"):
        if r["name"]:
            aliases.setdefault(r["entity_id"], set()).add(r["name"])
    c.close()

    # 标的明细取自交付表：graph.db 的 item 表没有 spec / is_winner 列
    with open(os.path.join(APP, "deliverables", "items_task1.csv"),
              encoding="utf-8-sig", newline="") as f:
        items = list(csv.DictReader(f))
    return notices, items, bids, entities, aliases


def _load_roster() -> list:
    """投标人名单（参与人）交付表 -> `build_tables(bidder_roster=…)` 的入参。

    文件不存在 -> 空列表（全量库不传名单，三表规模/类别与改造前**逐字一致**）。
    存在时按平台库 `t1_bidder` 的同一套字段名映射，两条路径共用同一份判据与合并逻辑。
    """
    if not os.path.exists(ROSTER):
        return []
    with open(ROSTER, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        if not (r.get("name") or "").strip():
            continue
        out.append({
            "notice_id": (r.get("announcement_id") or "").strip(),
            "package_label": r.get("package_label") or "",
            "name": r.get("name") or "",
            "quote_yuan": r.get("quote_yuan"),
            "score": r.get("score"),
            "review": r.get("review") or "",
            "review_reason": r.get("review_reason") or "",
            "lose_reason": r.get("lose_reason") or "",
            "source": r.get("source") or "",
        })
    print("投标人名单: %s（%d 行）" % (os.path.basename(ROSTER), len(out)))
    return out


def main() -> int:
    if not os.path.exists(DB):
        print("缺少图库:", DB)
        return 1
    notices, items, bids, entities, aliases = _load_sources()
    roster = _load_roster()

    stats: dict = {}
    tables = ot.build_tables(notices, items, bids, entities, aliases,
                             bidder_roster=roster,
                             full_dir=FULL, html_dir=HTML_DIR, stats=stats)
    total_rows = tables[ot.SHEET_TOTAL]
    item_rows = tables[ot.SHEET_ITEM]
    bid_rows = tables[ot.SHEET_BIDDER]

    os.makedirs(OUTDIR, exist_ok=True)
    # CSV 落盘**复用共享核心** `official_tables.write_csvs()`（= 平台下载端点 `csv_bytes()`
    # 的同一份实现）：列顺序、Decimal 定点写法、以及「总表中标金额 / 投标人表报价（万元）
    # 两位小数」（`fmt_money2`）都只有一处代码，提交件与下载件不会漂移。
    ot.write_csvs(OUTDIR, tables)
    for name, rows in ((ot.SHEET_TOTAL, total_rows),
                       (ot.SHEET_ITEM, item_rows),
                       (ot.SHEET_BIDDER, bid_rows)):
        print("  %s -> %s.csv（%d 行）" % (name, name, len(rows)))

    # 工作簿落盘：**必须复用共享核心的 write_xlsx**（与任务三下载端点同一份实现），
    # 否则「得分」列的 float / 两位小数格式会与下载件漂移。
    try:
        xp = os.path.join(OUTDIR, "官方数据集提取结果.xlsx")
        ot.write_xlsx(xp, tables)
        print("  工作簿 -> %s（3 sheet）" % os.path.basename(xp))
    except ImportError:
        print("  未安装 openpyxl，仅输出 CSV")

    print("\n总表 %d 行 | 标的表 %d 行 | 投标人表 %d 行" % (len(total_rows), len(item_rows), len(bid_rows)))
    # 标的行噪音过滤（唯一实现 = ict_parse/noise.py，在 build_tables 内部统一调用）
    print("标的行噪音过滤: 输入 %d / 丢弃 %d（%s）"
          % (stats.get("noise_total", 0), stats.get("noise_dropped", 0),
             "、".join("%s=%d" % (k.replace("noise_", "").upper(), v)
                      for k, v in sorted(stats.items())
                      if k.startswith("noise_") and k not in ("noise_total", "noise_dropped")
                      and v) or "无命中"))
    print("分包: %d 篇 / %d 个包（判据=包组数≥2；其中 ≥2 个真包 %d 篇）"
          "| 投标人: 入表 %d / 共 %d，未定位到包而丢弃 %d"
          % (stats["multi"], stats["pkg"], stats["true_multi"],
             stats["bids_placed"], stats["bids_total"], stats["bids_dropped"]))
    print("投标人名单（参与人来源）: 名单 %d 行 / 定位到包 %d / 丢弃 %d / 新增入表 %d"
          "（名单为空 = 本次数据集没有名单来源，三表仍为旧口径）"
          % (stats.get("roster_total", 0), stats.get("roster_placed", 0),
             stats.get("roster_dropped", 0), stats.get("roster_added", 0)))
    rec = (stats["recover_p1_name_token"] + stats["recover_p2_norm_name"]
           + stats["recover_p3_html_rank"] + stats["recover_p4_alias"])
    print("投标人->包 补全（有据可依）: 补全 %d 条 —— P1 名称内显式包标识 %d / "
          "P2 名称归一化命中标的行 %d / P3 原文排名分节(标段名称) %d / P4 实体别名 %d；"
          "仍丢弃 %d 条（无上述证据，不猜）"
          % (rec, stats["recover_p1_name_token"], stats["recover_p2_norm_name"],
             stats["recover_p3_html_rank"], stats["recover_p4_alias"], stats["still_dropped"]))
    print("抽样（总表）:", {k: v for k, v in list(total_rows[0].items()) if k in ("UUID", "中标供应商", "中标金额")})
    print("抽样（标的表）:", {k: v for k, v in list(item_rows[0].items())
                              if k in ("产品服务名称", "数量", "单价", "总价", "类别")})
    print("抽样（投标人表）:", {k: v for k, v in list(bid_rows[0].items())
                                if k in ("名称", "类别", "通过审查", "未中标原因")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
