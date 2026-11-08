"""任务二 · 关系模型构建：采购单位 — 中标供应商 — 投标参与方（含产品供应商）。

数据来源（全部为任务一产出，无需再调模型）：
- `out/full/<id>/items_llm.json` 的 `announcement`：
  采购单位、中标供应商、参与投标人、项目金额、`supplier_uscc`（名称→统一社会信用代码）
- 同文件的 `items`：标的、**品牌（= 产品供应商）**、单价/总价

**实体归一（本版新增）**：
- 优先用**统一社会信用代码**作唯一键（源里公布率低，仅 ~40 篇有）；
- 否则用 `entity_key()` 归一化主键：NFKC + 去 CJK 部首补充区字符（`⻰`→`龙` 这类 NFKC 不映射的变体）
  + 去空白/各类括号标点。于是同一法人的不同写法会归到同一 `entity_id`，
  而母公司与子公司（不同信用代码、不同主键）**不会**被误合并。

产出：
- `out/task2/graph.db`（SQLite：entity + notice + bid + item + 关系视图）
- `deliverables/task2_*.csv`（可直接提交的关系表）

用法: uv run python scripts/build_task2_graph.py
"""
from __future__ import annotations

import collections
import csv
import json
import os
import re
import sqlite3
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ict_parse.clean import clean_brand, clean_purchaser  # noqa: E402
from ict_parse.noise import (NoiseReport, filter_items,  # noqa: E402
                             format_report)

ROOT = "out/full"
OUTDIR = "deliverables"
DB_DIR = "out/task2"

SCHEMA = """
DROP TABLE IF EXISTS entity; DROP TABLE IF EXISTS entity_alias;
DROP TABLE IF EXISTS notice; DROP TABLE IF EXISTS bid; DROP TABLE IF EXISTS item;
CREATE TABLE entity(
  entity_id TEXT PRIMARY KEY, canonical_name TEXT, uscc TEXT, n_aliases INTEGER, kind TEXT);
CREATE TABLE entity_alias(entity_id TEXT, name TEXT, n INTEGER);
CREATE TABLE notice(
  notice_id TEXT PRIMARY KEY, project_name TEXT, purchaser_id TEXT, purchaser TEXT,
  region TEXT, publish_time TEXT, project_amount REAL, notice_type TEXT, pricing_mode TEXT,
  procurement_mode TEXT, n_winners INTEGER, n_bidders INTEGER);
CREATE TABLE bid(
  notice_id TEXT, entity_id TEXT, supplier TEXT, is_winner INTEGER, amount REAL,
  amount_src TEXT, PRIMARY KEY(notice_id, entity_id));
CREATE TABLE item(
  notice_id TEXT, entity_id TEXT, supplier TEXT, item_name TEXT, category TEXT, brand TEXT,
  quantity REAL, unit_price REAL, total_price REAL);
CREATE TABLE manufacturer(notice_id TEXT, name TEXT, entity_id TEXT);
CREATE INDEX idx_bid_ent ON bid(entity_id);
CREATE INDEX idx_bid_notice ON bid(notice_id);
CREATE INDEX idx_notice_pur ON notice(purchaser_id);
CREATE INDEX idx_item_notice ON item(notice_id);
"""

VIEWS = """
DROP VIEW IF EXISTS v_cooperation;
DROP VIEW IF EXISTS v_cobidding;
DROP VIEW IF EXISTS v_product_supply;
DROP VIEW IF EXISTS v_purchaser_profile;
DROP VIEW IF EXISTS v_supplier_profile;

CREATE VIEW v_cooperation AS
SELECT n.purchaser AS purchaser, e.canonical_name AS supplier,
       COUNT(DISTINCT n.notice_id) AS coop_times,
       ROUND(SUM(b.amount), 2) AS coop_amount,
       MIN(n.publish_time) AS first_time, MAX(n.publish_time) AS last_time
FROM notice n JOIN bid b ON b.notice_id = n.notice_id
JOIN entity e ON e.entity_id = b.entity_id
WHERE b.is_winner = 1 AND n.purchaser IS NOT NULL AND n.purchaser <> ''
GROUP BY n.purchaser_id, b.entity_id;

CREATE VIEW v_cobidding AS
SELECT ea.canonical_name AS supplier_a, eb.canonical_name AS supplier_b,
       COUNT(DISTINCT a.notice_id) AS co_times,
       ROUND(SUM(n.project_amount), 2) AS co_project_amount,
       SUM(a.is_winner) AS a_wins, SUM(b.is_winner) AS b_wins
FROM bid a
JOIN bid b ON a.notice_id = b.notice_id AND a.entity_id < b.entity_id
JOIN notice n ON n.notice_id = a.notice_id
JOIN entity ea ON ea.entity_id = a.entity_id
JOIN entity eb ON eb.entity_id = b.entity_id
GROUP BY a.entity_id, b.entity_id;

CREATE VIEW v_product_supply AS
SELECT i.brand AS product_supplier, e.canonical_name AS winner,
       COUNT(*) AS item_times, ROUND(SUM(i.total_price), 2) AS item_amount
FROM item i JOIN entity e ON e.entity_id = i.entity_id
WHERE i.brand IS NOT NULL AND i.brand <> ''
GROUP BY i.brand, i.entity_id;

CREATE VIEW v_purchaser_profile AS
SELECT purchaser, COUNT(*) AS n_notices, ROUND(SUM(project_amount), 2) AS total_amount,
       COUNT(DISTINCT region) AS n_regions
FROM notice WHERE purchaser IS NOT NULL AND purchaser <> '' GROUP BY purchaser;

CREATE VIEW v_supplier_profile AS
SELECT e.canonical_name AS supplier, e.entity_id, e.uscc, e.n_aliases,
       SUM(b.is_winner) AS win_times,
       SUM(1 - b.is_winner) AS bid_only_times,
       COUNT(*) AS participate_times,
       ROUND(SUM(CASE WHEN b.is_winner = 1 THEN b.amount ELSE 0 END), 2) AS won_amount
FROM bid b JOIN entity e ON e.entity_id = b.entity_id
GROUP BY b.entity_id;

-- ===== 用户画像（赛题「用户画像建模」）=====
DROP VIEW IF EXISTS v_supplier_portrait;
DROP VIEW IF EXISTS v_purchaser_portrait;
DROP VIEW IF EXISTS v_supplier_purchaser;
DROP VIEW IF EXISTS v_purchaser_supplier;
DROP VIEW IF EXISTS v_supplier_category;
DROP VIEW IF EXISTS v_purchaser_category;

CREATE VIEW v_supplier_portrait AS
SELECT e.canonical_name AS supplier, e.entity_id, e.uscc,
       COUNT(DISTINCT b.notice_id) AS bid_notices,
       COUNT(DISTINCT CASE WHEN b.is_winner = 1 THEN b.notice_id END) AS win_notices,
       ROUND(1.0 * COUNT(DISTINCT CASE WHEN b.is_winner = 1 THEN b.notice_id END)
             / COUNT(DISTINCT b.notice_id), 3) AS win_rate,
       ROUND(SUM(CASE WHEN b.is_winner = 1 THEN COALESCE(b.amount, 0) ELSE 0 END), 2) AS won_amount,
       COUNT(DISTINCT n.purchaser) AS n_purchasers,
       COUNT(DISTINCT n.region) AS n_regions,
       MIN(CASE WHEN b.is_winner = 1 THEN n.publish_time END) AS first_win,
       MAX(CASE WHEN b.is_winner = 1 THEN n.publish_time END) AS last_win
FROM bid b JOIN notice n ON n.notice_id = b.notice_id
JOIN entity e ON e.entity_id = b.entity_id
GROUP BY b.entity_id;

CREATE VIEW v_purchaser_portrait AS
SELECT n.purchaser, COUNT(DISTINCT n.notice_id) AS n_notices,
       ROUND(SUM(DISTINCT 0) + SUM(n.project_amount), 2) AS total_amount,
       COUNT(DISTINCT b.entity_id) AS n_suppliers,
       SUM(b.is_winner) AS n_win_relations,
       COUNT(DISTINCT n.region) AS n_regions,
       MIN(n.publish_time) AS first_time, MAX(n.publish_time) AS last_time
FROM notice n LEFT JOIN bid b ON b.notice_id = n.notice_id
WHERE n.purchaser IS NOT NULL AND n.purchaser <> ''
GROUP BY n.purchaser;

CREATE VIEW v_supplier_purchaser AS
SELECT e.canonical_name AS supplier, n.purchaser,
       COUNT(DISTINCT n.notice_id) AS coop_times,
       ROUND(SUM(b.amount), 2) AS coop_amount
FROM bid b JOIN notice n ON n.notice_id = b.notice_id
JOIN entity e ON e.entity_id = b.entity_id
WHERE b.is_winner = 1
GROUP BY b.entity_id, n.purchaser;

CREATE VIEW v_purchaser_supplier AS
SELECT n.purchaser, e.canonical_name AS supplier,
       COUNT(DISTINCT n.notice_id) AS coop_times,
       ROUND(SUM(b.amount), 2) AS coop_amount
FROM bid b JOIN notice n ON n.notice_id = b.notice_id
JOIN entity e ON e.entity_id = b.entity_id
WHERE b.is_winner = 1
GROUP BY n.purchaser, b.entity_id;

CREATE VIEW v_purchaser_product_supplier AS
SELECT n.purchaser, t.src AS product_supplier, t.src_kind,
       COUNT(DISTINCT n.notice_id) AS notices
FROM (
  SELECT i.notice_id, i.brand AS src, 'brand' AS src_kind FROM item i
   WHERE i.brand IS NOT NULL AND i.brand <> ''
  UNION ALL
  SELECT m.notice_id, m.name AS src, 'declaration' AS src_kind FROM manufacturer m
   WHERE m.name IS NOT NULL AND m.name <> ''
) t JOIN notice n ON n.notice_id = t.notice_id
WHERE n.purchaser IS NOT NULL AND n.purchaser <> ''
GROUP BY n.purchaser, t.src, t.src_kind;

CREATE VIEW v_supplier_category AS
SELECT e.canonical_name AS supplier, i.category, COUNT(*) AS item_times,
       ROUND(SUM(i.total_price), 2) AS item_amount
FROM item i JOIN entity e ON e.entity_id = i.entity_id
WHERE i.category IS NOT NULL AND i.category <> ''
GROUP BY i.entity_id, i.category;

CREATE VIEW v_purchaser_category AS
SELECT n.purchaser, i.category, COUNT(*) AS item_times,
       ROUND(SUM(i.total_price), 2) AS item_amount
FROM item i JOIN notice n ON n.notice_id = i.notice_id
WHERE i.category IS NOT NULL AND i.category <> ''
GROUP BY n.purchaser, i.category;
"""

TG = {
    "entities": ("entity", ["entity_id", "canonical_name", "uscc", "n_aliases", "kind"]),
    "purchasers": ("v_purchaser_profile", ["purchaser", "n_notices", "total_amount", "n_regions"]),
    "suppliers": ("v_supplier_profile", ["supplier", "entity_id", "uscc", "n_aliases",
                                         "win_times", "bid_only_times", "participate_times", "won_amount"]),
    "cooperation": ("v_cooperation", ["purchaser", "supplier", "coop_times", "coop_amount",
                                      "first_time", "last_time"]),
    "cobidding": ("v_cobidding", ["supplier_a", "supplier_b", "co_times", "co_project_amount",
                                  "a_wins", "b_wins"]),
    "product_supply": ("v_product_supply", ["product_supplier", "winner", "item_times", "item_amount"]),
    "supplier_portrait": ("v_supplier_portrait", ["supplier", "entity_id", "uscc", "bid_notices",
                                                  "win_notices", "win_rate", "won_amount",
                                                  "n_purchasers", "n_regions", "first_win", "last_win"]),
    "purchaser_portrait": ("v_purchaser_portrait", ["purchaser", "n_notices", "total_amount",
                                                    "n_suppliers", "n_win_relations", "n_regions",
                                                    "first_time", "last_time"]),
    "supplier_purchaser": ("v_supplier_purchaser", ["supplier", "purchaser", "coop_times", "coop_amount"]),
    "purchaser_supplier": ("v_purchaser_supplier", ["purchaser", "supplier", "coop_times", "coop_amount"]),
    "supplier_category": ("v_supplier_category", ["supplier", "category", "item_times", "item_amount"]),
    "purchaser_product_supplier": ("v_purchaser_product_supplier",
                                   ["purchaser", "product_supplier", "src_kind", "notices"]),
    "purchaser_category": ("v_purchaser_category", ["purchaser", "category", "item_times", "item_amount"]),
}

_STRIP = re.compile(r"[\s（）()【】\[\]{}、,，.。·:：;；\-—_/\\|'\"]+")


def entity_key(name: str) -> str:
    """实体归一化主键：NFKC + 去 CJK 部首补充/康熙部首区字符 + 去空白与标点。

    实测 `⻰`（CJK Radicals Supplement，U+2E80–U+2EFF）**NFKC 不映射**，
    不处理会把「黑⻰江…」与「黑龙江…」算成两家。
    """
    s = unicodedata.normalize("NFKC", str(name or ""))
    s = re.sub(r"[\u2e80-\u2fdf]", "", s)
    return _STRIP.sub("", s).strip()


def build(loaded, db_path: str, outdir: str, bid_source=None) -> dict:
    """由 `(公告号, 公告字段, 标的行)` 列表构建 graph.db，并导出 `task2_*.csv`。

    `loaded` 的元素形如 `(aid, ann, items)`；`ann` 的字段与
    `out/full/<id>/items_llm.json` 的 `announcement` 同构（`purchaser` / `winners` /
    `bidders` / `project_amount` / `supplier_uscc` 等）。

    `bid_source` 为 `None` 时 `task2_bids.csv` 的列与历史交付件**完全一致**；
    传入可调用对象 `(notice_id, supplier, is_winner) -> str` 时追加一列 `source`，
    供上传路径标明推导口径（`DERIVED_FROM_ITEMS` / `DERIVED_FROM_ROSTER` 等）。

    本函数是**纯计算**（不联网、不调模型），全量 CLI 与任务三上传路径共用同一套
    实体归一/建图/导出逻辑 —— 上传路径的关系表口径因此与全量交付件一致。
    """
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    os.makedirs(outdir, exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    uscc_map: dict[str, str] = {}
    for _, ann, _items in loaded:
        for n, u in (ann.get("supplier_uscc") or {}).items():
            if n and u:
                uscc_map.setdefault(n, u)

    # ---- 实体解析：先收集所有出现过的名称，再统一分配 entity_id ----
    def eid(n: str) -> str:
        return uscc_map.get(n) or entity_key(n)

    alias: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for _, ann, items in loaded:
        for n in list(ann.get("winners") or []) + list(ann.get("bidders") or []):
            if n:
                alias[eid(n)][n] += 1
        for it in items:
            n = it.get("supplier_name")
            if n:
                alias[eid(n)][n] += 1
        pu = ann.get("purchaser")
        if pu:
            alias[eid(pu)][pu] += 1

    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    n_ent = n_merged = 0
    for k, c in alias.items():
        canon = c.most_common(1)[0][0]
        con.execute("INSERT OR REPLACE INTO entity VALUES (?,?,?,?,?)",
                    (k, canon, uscc_map.get(canon), len(c), ""))
        for nm, cnt in c.items():
            con.execute("INSERT INTO entity_alias VALUES (?,?,?)", (k, nm, cnt))
        n_ent += 1
        if len(c) > 1:
            n_merged += 1

    n_notice = n_bid = n_item = 0
    for aid, ann, items in loaded:
        w = set(ann.get("winners") or [])
        seen, allb = set(), []
        for x in list(ann.get("winners") or []) + list(ann.get("bidders") or []):
            e = eid(x) if x else None
            if e and e not in seen:
                seen.add(e)
                allb.append(x)
        con.execute("INSERT OR REPLACE INTO notice VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            aid, ann.get("project_name"), eid(ann["purchaser"]) if ann.get("purchaser") else None,
            ann.get("purchaser"), ann.get("region"), str(ann.get("publish_time") or ""),
            ann.get("project_amount"), ann.get("notice_type"), ann.get("pricing_mode"),
            ann.get("procurement_mode"),
            len({eid(x) for x in w if x}), len(allb)))
        n_notice += 1
        amt: dict[str, float] = {}
        for it in items:
            s = it.get("supplier_name")
            tp = it.get("total_price")
            if s and isinstance(tp, (int, float)):
                amt[eid(s)] = amt.get(eid(s), 0.0) + float(tp)
        # 金额回退：中标金额以 item 明细分项价汇总为准；为空且本篇**仅一个中标人**时，
        # 用公告级总中标金额（无歧义）；多中标人篇不猜（留空并标注 amount_src）。
        w_ents = {eid(x) for x in w if x}
        pa = ann.get("project_amount")
        for x in allb:
            e = eid(x)
            a = amt.get(e)
            src = "item" if a is not None else None
            if a is None and x in w and len(w_ents) == 1 and isinstance(pa, (int, float)):
                a, src = float(pa), "notice"
            con.execute("INSERT OR REPLACE INTO bid VALUES (?,?,?,?,?,?)",
                        (aid, e, x, 1 if x in w else 0, a, src))
            n_bid += 1
        # 产品供应商（制造商）：来自中小企业声明函正文（模板型声明函无解）
        for mm in (ann.get("manufacturers") or []):
            con.execute("INSERT INTO manufacturer VALUES (?,?,?)", (aid, mm, eid(mm)))
        for it in items:
            s = it.get("supplier_name")
            con.execute("INSERT INTO item VALUES (?,?,?,?,?,?,?,?,?)", (
                aid, eid(s) if s else None, s, it.get("item_name"), it.get("category"),
                it.get("brand"), it.get("quantity"), it.get("unit_price"), it.get("total_price")))
            n_item += 1
    con.executescript(VIEWS)
    con.commit()

    stats = {}
    for name, (view, cols) in TG.items():
        rows = con.execute(f"SELECT {','.join(cols)} FROM {view}").fetchall()
        with open(os.path.join(outdir, f"task2_{name}.csv"), "w",
                  encoding="utf-8-sig", newline="") as f:
            wtr = csv.writer(f)
            wtr.writerow(cols)
            wtr.writerows(rows)
        stats[name] = len(rows)
    bcols = ["notice_id", "purchaser", "supplier", "is_winner", "amount",
             "project_amount", "project_name"]
    rows = con.execute("""SELECT b.notice_id, n.purchaser, b.supplier, b.is_winner, b.amount,
                                 n.project_amount, n.project_name
                          FROM bid b JOIN notice n ON n.notice_id=b.notice_id""").fetchall()
    if bid_source is not None:
        bcols = bcols + ["source"]
        rows = [tuple(r) + (bid_source(r[0], r[2], r[3]),) for r in rows]
    with open(os.path.join(outdir, "task2_bids.csv"), "w",
              encoding="utf-8-sig", newline="") as f:
        wtr = csv.writer(f)
        wtr.writerow(bcols)
        wtr.writerows(rows)
    stats["bids"] = len(rows)
    con.close()
    return {"graph_db": db_path, "stats": stats, "n_entity": n_ent, "n_merged": n_merged,
            "n_notice": n_notice, "n_bid": n_bid, "n_item": n_item,
            "n_uscc": len(set(uscc_map.values()))}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    os.makedirs(DB_DIR, exist_ok=True)
    os.makedirs(OUTDIR, exist_ok=True)
    db = os.path.join(DB_DIR, "graph.db")

    ids = [ln.strip() for ln in open("all_ids.txt", encoding="utf-8") if ln.strip()]
    loaded = []
    noise_rep = NoiseReport()          # 标的行噪音过滤统计（唯一实现 = ict_parse/noise.py）
    for aid in ids:
        p = os.path.join(ROOT, aid, "items_llm.json")
        if not os.path.exists(p):
            continue
        ext = json.load(open(p, encoding="utf-8"))
        ann, items = ext.get("announcement") or {}, ext.get("items") or []
        # 字段清洗（规则唯一定义在 ict_parse/clean.py）：品牌多值串取首个、
        # 采购单位代码式值置空 —— 否则会污染"产品供应商"关系与采购单位画像。
        if ann.get("purchaser"):
            ann["purchaser"] = clean_purchaser(ann.get("purchaser"))
        for it in items:
            if it.get("brand"):
                it["brand"] = clean_brand(it.get("brand"))
        # ---- 标的行噪音过滤（唯一实现 = ict_parse/noise.py）----
        # 图库的 `item` 表是 v_product_supply / supplier_category / purchaser_category
        # 的来源：`工业` 之类的行业分类词、`总价:` 之类的表标签一旦入库，就会变成
        # "品牌/品目关系"（`工业 × 供应商`），所以建图前在同一处判掉。
        items = filter_items(items, project_name=ann.get("project_name") or "",
                             purchaser=ann.get("purchaser") or "",
                             report=noise_rep, aid=aid)
        loaded.append((aid, ann, items))

    res = build(loaded, db, OUTDIR)
    stats = res["stats"]
    print(f"图库: {db}")
    print(f"notice={res['n_notice']}  bid={res['n_bid']}  item={res['n_item']}")
    print(format_report(noise_rep, title="标的行噪音过滤（建图库前）", show_samples=5))
    print(f"实体归一: 归一后实体 {res['n_entity']} 个；其中由多个名称写法合并而来 "
          f"{res['n_merged']} 个（含 USCC 唯一键 {res['n_uscc']} 个）")
    print("导出 CSV（deliverables/）：")
    for k, v in stats.items():
        print(f"   task2_{k}.csv   {v} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
