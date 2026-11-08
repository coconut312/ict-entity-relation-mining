"""任务二 · Neo4j 图库构建与导出。

把任务二的关系模型（采购单位 — 中标供应商 — 投标参与方 — 产品供应商）装载到 Neo4j，
或导出为**可移植的 Cypher + CSV**，供任何 Neo4j 实例（本地/容器/Aura）加载。

图模型（节点 / 关系）
--------------------
    (:Purchaser  {name, n_notices, total_amount, n_regions})
    (:Supplier   {name, entity_id, uscc, win_times, bid_only_times, participate_times, won_amount})
    (:Brand      {name})                       # 产品供应商（品牌）
    (:Notice     {notice_id, project_name, purchaser, region, publish_time,
                  project_amount, procurement_mode, n_winners, n_bidders})

    (:Purchaser)-[:COOPERATES {times, amount, first_date, last_date}]->(:Supplier)
    (:Supplier)-[:CO_BIDS    {times, project_amount, a_wins, b_wins}]->(:Supplier)
    (:Supplier)-[:PARTICIPATES {is_winner, amount}]->(:Notice)
    (:Supplier)-[:SUPPLIES   {item_times, item_amount}]->(:Brand)

用法
----
    # 1) 只导出可移植文件（无需 Neo4j，评审/离线可用）
    uv run python scripts/build_neo4j_graph.py --export

    # 2) 直接装载到 Neo4j（需先起 Neo4j 5.x）
    set NEO4J_URI=bolt://127.0.0.1:7687
    set NEO4J_USER=neo4j
    set NEO4J_PASSWORD=yourpassword
    uv run python scripts/build_neo4j_graph.py --load

    # 3) 导出 + 装载
    uv run python scripts/build_neo4j_graph.py --load --export
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
GRAPH_DB = APP / "out" / "task2" / "graph.db"
DELIV = APP / "deliverables"
EXPORT_DIR = DELIV / "neo4j"

BATCH = 5000

CONSTRAINTS = [
    "CREATE CONSTRAINT purchaser_name IF NOT EXISTS FOR (p:Purchaser) REQUIRE p.name IS UNIQUE",
    "CREATE CONSTRAINT supplier_name  IF NOT EXISTS FOR (s:Supplier)  REQUIRE s.name IS UNIQUE",
    "CREATE CONSTRAINT brand_name     IF NOT EXISTS FOR (b:Brand)     REQUIRE b.name IS UNIQUE",
    "CREATE CONSTRAINT notice_id      IF NOT EXISTS FOR (n:Notice)    REQUIRE n.notice_id IS UNIQUE",
]
INDEXES = [
    "CREATE INDEX supplier_entity IF NOT EXISTS FOR (s:Supplier) ON (s.entity_id)",
    "CREATE INDEX notice_purchaser IF NOT EXISTS FOR (n:Notice) ON (n.purchaser)",
]


# ----------------------------------------------------------------------
# 读源数据（优先 graph.db；缺失时回退到交付表 CSV）
# ----------------------------------------------------------------------
def _rows_sqlite(db: Path, sql: str) -> list[tuple]:
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def _rows_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_source() -> dict:
    """返回 {节点表: [dict], 关系表: [dict]}。"""
    src: dict[str, list[dict]] = {}
    ent_map: dict[str, str] = {}
    if GRAPH_DB.exists():
        c = sqlite3.connect(f"file:{GRAPH_DB.as_posix()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row

        def q(sql):
            return [dict(r) for r in c.execute(sql).fetchall()]

        src["purchaser"] = q("SELECT * FROM v_purchaser_portrait")
        src["supplier"] = q("SELECT * FROM v_supplier_portrait")
        src["brand"] = q("""SELECT DISTINCT product_supplier AS name FROM v_product_supply
                            WHERE IFNULL(product_supplier,'')<>''""")
        src["notice"] = q("SELECT * FROM notice")
        src["cooperates"] = q("SELECT * FROM v_cooperation")
        src["cobids"] = q("SELECT * FROM v_cobidding")
        src["participates"] = q("SELECT * FROM bid")
        src["supplies"] = q("SELECT * FROM v_product_supply")
        # entity_id -> 规范名（用于把 bid 里的别名对齐到节点名，必须在关闭连接前取）
        ent_map = {r["entity_id"]: r["canonical_name"]
                   for r in q("SELECT entity_id, canonical_name FROM entity")}
        c.close()
    else:                                   # 回退：交付表 CSV（全新 clone）
        src["purchaser"] = _rows_csv(DELIV / "task2_purchaser_portrait.csv")
        src["supplier"] = _rows_csv(DELIV / "task2_suppliers.csv")
        src["brand"] = [{"name": r["product_supplier"]}
                        for r in _rows_csv(DELIV / "task2_product_supply.csv") if r.get("product_supplier")]
        src["notice"] = _rows_csv(DELIV / "announcements_task1.csv")
        src["cooperates"] = _rows_csv(DELIV / "task2_cooperation.csv")
        src["cobids"] = _rows_csv(DELIV / "task2_cobidding.csv")
        src["participates"] = _rows_csv(DELIV / "task2_bids.csv")
        src["supplies"] = _rows_csv(DELIV / "task2_product_supply.csv")
    # 关系按 entity 身份对齐显示名：bid 表里 supplier 是**公告里的原始写法**（可能是别名，
    # 如 `联通(广东)产业互联网有限公司` vs 规范名 `联通（广东）产业互联网有限公司`）。
    # 若直接按该名 MATCH 节点会匹配不到，导致关系被**静默丢弃**（实测丢 97 条 PARTICIPATES）。
    if ent_map:
        fixed = 0
        for r in src["participates"]:
            eid = r.get("entity_id")
            canon = ent_map.get(eid) if eid else None
            if canon and canon != r.get("supplier"):
                r["supplier"] = canon
                fixed += 1
        if fixed:
            print("  参与投标关系：按 entity 规范化显示名 %d 条（避免按别名匹配失败）" % fixed)
    # 过滤掉品牌或供货方为空的行：Brand 节点集本就跳过空名（LOAD 里也 MERGE 不了空名），
    # 留着会让 `MATCH (b:Brand {name:''})` 失败、关系被静默丢弃（实测 27 条 SUPPLIES）。
    _before = len(src["supplies"])
    src["supplies"] = [r for r in src["supplies"]
                       if (r.get("product_supplier") or "").strip()
                       and (r.get("winner") or "").strip()]
    if len(src["supplies"]) != _before:
        print("  过滤品牌/供货方为空的关系行 %d 条（无法建立有效 SUPPLIES）"
              % (_before - len(src["supplies"])))
    # 补齐品牌供货方里"不在 bid 表"的供应商（v_supplier_portrait 只覆盖 bid 出现过的实体）：
    # 注意必须放在**名称归一之后**，否则 src["supplier"] 里还没有 name 键。
    # 统一出 name 字段：graph.db 的画像视图列名是 purchaser / supplier（不是 name），交付表 CSV 同理。
    # 不归一就会出现「行数对但 name 全空」，MERGE 时直接报 null 属性错误。
    for key, alt in (("purchaser", "purchaser"), ("supplier", "supplier")):
        for r in src[key]:
            r["name"] = (r.get("name") or r.get(alt) or "").strip()
    # 统一供应商画像字段名：graph.db 的 v_supplier_portrait 用 win_notices / bid_notices，
    # 而交付表 task2_suppliers.csv（= v_supplier_profile）用 win_times / participate_times。
    # 不归一会导致 win_times 等属性为 null（写入被跳过），场景 S3 的"中标次数"读成 0。
    for r in src["supplier"]:
        w = r.get("win_times")
        if w in (None, ""):
            w = r.get("win_notices")
        p = r.get("participate_times")
        if p in (None, ""):
            p = r.get("bid_notices")
        try:
            wi, pi = int(float(w or 0)), int(float(p or 0))
        except (TypeError, ValueError):
            wi, pi = 0, 0
        r["win_times"] = wi
        r["participate_times"] = pi
        bo = r.get("bid_only_times")
        r["bid_only_times"] = int(float(bo)) if bo not in (None, "") else max(0, pi - wi)
    # 补齐品牌供货方里"不在 bid 表"的供应商节点（v_supplier_portrait 只覆盖 bid 出现过的实体）。
    # 放在归一之后：此时 src["supplier"] 已含 name 键。
    _have = {r["name"] for r in src["supplier"]}
    _added = 0
    for r in src["supplies"]:
        w = (r.get("winner") or "").strip()
        if w and w not in _have:
            _have.add(w)
            src["supplier"].append({"name": w, "win_times": 0, "participate_times": 0,
                                    "bid_only_times": 0, "won_amount": 0.0})
            _added += 1
    if _added:
        print("  补齐仅出现在品牌供货关系里的供应商节点 %d 个" % _added)
    _repair_missing_winners(src, ent_map)
    return src


# ----------------------------------------------------------------------
# 「有中标方却没进图」的结构性兜底：公告级 winners 补齐
# ----------------------------------------------------------------------
# 不变量（本脚本与 task3 `neo4j_sync.py` 必须同时满足）：
#   每条 `notice.n_winners > 0` 的公告，图上至少有一条 `(:Supplier)-[:PARTICIPATES
#   {is_winner:1}]->(:Notice)`；且采购单位非空时至少有一条 `(:Purchaser)
#   -[:COOPERATES]->(:Supplier)`。
#
# 关系本应来自 `bid`（`bid.is_winner = 1`）—— 全量 graph.db 就是如此（实测 995/995 覆盖、
# 0 例外）。但**图库比抽取结果旧**时（先建了 graph.db、之后又补跑了 winners；或 graph.db
# 由旧版构建脚本产出，只写了参与人没写中标标记），`v_cooperation`/`bid` 里那几篇会没有任何
# 中标行，图上就"查不到中标供应商"。这类缺口不能靠人眼看，这里按**公告级 winners**兜底：
#   * 仅对「`n_winners > 0` 且图上没有中标参与关系」的公告生效（有 bid 中标行的公告**绝不改动**）；
#   * winners 名字来源优先级：`out/full/<id>/items_llm.json::announcement.winners`
#     -> graph.db 的 `item.supplier`（同一公告的标的供货方）；
#   * 名字按 `entity_id -> canonical_name` 归一（否则 MATCH 不上 Supplier 节点，关系会被静默丢弃）；
#   * 已是参与人的供应商**只把 is_winner 提升为 1**，绝不新增重复关系、绝不降级、绝不自配对。
def _winner_names_from_llm(app_dir: Path, notice_id: str) -> list[str]:
    p = app_dir / "out" / "full" / notice_id / "items_llm.json"
    if not p.exists():
        return []
    try:
        ext = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ann = ext.get("announcement") or {}
    w = ann.get("winners")
    if isinstance(w, list):
        return [str(x).strip() for x in w if str(x or "").strip()]
    if isinstance(w, str) and w.strip():
        return [w.strip()]
    return []


def _item_suppliers_from_sqlite(db: Path, notice_id: str) -> list[str]:
    if not db.exists():
        return []
    try:
        return [r[0] for r in _rows_sqlite(
            db, "SELECT DISTINCT supplier FROM item WHERE notice_id=? AND IFNULL(supplier,'')<>''")
            if r and r[0]]
    except sqlite3.Error:
        return []


def _repair_missing_winners(src: dict, ent_map: dict[str, str]) -> None:
    """把「有 winners 但图上没有中标参与关系」的公告补成合法关系（官方数据是 0 篇）。"""
    notice_by_id = {r.get("notice_id"): r for r in src["notice"]}
    have = {(r.get("notice_id"), (r.get("supplier") or "").strip())
            for r in src["participates"] if int(r.get("is_winner") or 0) == 1}
    noticed = {n for n, _ in have}
    need = [nid for nid, r in notice_by_id.items()
            if int(r.get("n_winners") or 0) > 0 and nid not in noticed]
    if not need:
        return
    # 名称（原文写法）-> entity 规范名：bid 的 supplier 是原文，节点名是 canonical_name
    disp_by_name = {nm: nm for nm in ent_map.values() if nm}
    amt_by_notice_supplier: dict[tuple, float] = {}
    if GRAPH_DB.exists():
        try:
            for nid, sup, tot in _rows_sqlite(
                    GRAPH_DB, "SELECT notice_id, supplier, SUM(total_price) FROM item "
                              "WHERE notice_id IS NOT NULL AND IFNULL(supplier,'')<>'' "
                              "GROUP BY notice_id, supplier"):
                amt_by_notice_supplier[(nid, sup)] = float(tot) if tot is not None else 0.0
        except sqlite3.Error:
            pass
    repaired = []
    for nid in need:
        notice = notice_by_id[nid]
        names = _winner_names_from_llm(APP, nid) or _item_suppliers_from_sqlite(GRAPH_DB, nid)
        names = [disp_by_name.get(n, n) for n in names]
        names = [n for n in dict.fromkeys(names) if n]
        if not names:
            continue
        purchaser = (notice.get("purchaser") or "").strip()
        pub = notice.get("publish_time")
        added = 0
        for disp in names:
            if (nid, disp) in have:
                continue                      # 已是中标参与人 -> 不重复、不改写
            amt = amt_by_notice_supplier.get((nid, disp)) or None
            src["participates"].append({"notice_id": nid, "supplier": disp,
                                        "is_winner": 1, "amount": amt})
            have.add((nid, disp))
            added += 1
            if purchaser:
                src["cooperates"].append({
                    "purchaser": purchaser, "supplier": disp, "coop_times": 1,
                    "coop_amount": amt, "first_time": pub, "last_time": pub})
        repaired.append((nid, added))
    if repaired:
        print("  中标方兜底：%d 篇公告的 winners 不在 bid 中标行里，已按公告级 winners 补关系"
              "（前 5：%s）" % (len(repaired), repaired[:5]))



# ----------------------------------------------------------------------
# 可移植导出（Cypher + CSV）
# ----------------------------------------------------------------------
CYPHER_HEADER = """// 任务二 · 关系模型图（采购单位 — 中标供应商 — 投标参与方 — 产品供应商）
// 由 app/scripts/build_neo4j_graph.py 自动生成，可用 cypher-shell 直接执行：
//   cypher-shell -u neo4j -p <password> -f task2_graph.cypher
// 或分步：先执行本文件建约束，再用第 6 节的 LOAD CSV 装载数据。

// 1) 唯一约束与索引（幂等）
@@CONSTRAINTS@@

// 2) 节点：采购单位
LOAD CSV WITH HEADERS FROM 'file:///nodes_purchaser.csv' AS r
MERGE (p:Purchaser {name: r.name})
SET p.n_notices = toInteger(r.n_notices), p.total_amount = toFloat(r.total_amount),
    p.n_regions  = toInteger(r.n_regions);

// 3) 节点：供应商（含画像指标）
LOAD CSV WITH HEADERS FROM 'file:///nodes_supplier.csv' AS r
MERGE (s:Supplier {name: r.name})
SET s.entity_id = r.entity_id, s.uscc = r.uscc,
    s.win_times = toInteger(r.win_times), s.bid_only_times = toInteger(r.bid_only_times),
    s.participate_times = toInteger(r.participate_times), s.won_amount = toFloat(r.won_amount);

// 4) 节点：产品供应商（品牌）
LOAD CSV WITH HEADERS FROM 'file:///nodes_brand.csv' AS r
MERGE (:Brand {name: r.name});

// 5) 节点：公告
LOAD CSV WITH HEADERS FROM 'file:///nodes_notice.csv' AS r
MERGE (n:Notice {notice_id: r.notice_id})
SET n.project_name = r.project_name, n.purchaser = r.purchaser, n.region = r.region,
    n.publish_time = r.publish_time, n.project_amount = toFloat(r.project_amount),
    n.procurement_mode = r.procurement_mode,
    n.n_winners = toInteger(r.n_winners), n.n_bidders = toInteger(r.n_bidders);

// 6) 关系：合作（采购单位 -> 中标供应商）
LOAD CSV WITH HEADERS FROM 'file:///rels_cooperates.csv' AS r
MATCH (p:Purchaser {name: r.purchaser}), (s:Supplier {name: r.supplier})
MERGE (p)-[c:COOPERATES]->(s)
SET c.times = toInteger(r.coop_times), c.amount = toFloat(r.coop_amount),
    c.first_date = r.first_time, c.last_date = r.last_time;

// 7) 关系：共同竞标（供应商 <-> 供应商，无向语义用单向存储 + 查询双向）
LOAD CSV WITH HEADERS FROM 'file:///rels_cobids.csv' AS r
MATCH (a:Supplier {name: r.supplier_a}), (b:Supplier {name: r.supplier_b})
MERGE (a)-[x:CO_BIDS]->(b)
SET x.times = toInteger(r.co_times), x.project_amount = toFloat(r.co_project_amount),
    x.a_wins = toInteger(r.a_wins), x.b_wins = toInteger(r.b_wins);

// 8) 关系：参与投标（供应商 -> 公告）
LOAD CSV WITH HEADERS FROM 'file:///rels_participates.csv' AS r
MATCH (s:Supplier {name: r.supplier}), (n:Notice {notice_id: r.notice_id})
MERGE (s)-[x:PARTICIPATES]->(n)
SET x.is_winner = toInteger(r.is_winner), x.amount = toFloat(r.amount);

// 9) 关系：供货（中标供应商 -> 产品供应商/品牌）
LOAD CSV WITH HEADERS FROM 'file:///rels_supplies.csv' AS r
MATCH (s:Supplier {name: r.winner}), (b:Brand {name: r.product_supplier})
MERGE (s)-[x:SUPPLIES]->(b)
SET x.item_times = toInteger(r.item_times), x.item_amount = toFloat(r.item_amount);

// 10) 关系：公告发布方（公告 -> 采购单位）
LOAD CSV WITH HEADERS FROM 'file:///rels_issued_by.csv' AS r
MATCH (n:Notice {notice_id: r.notice_id}), (p:Purchaser {name: r.purchaser})
MERGE (n)-[:ISSUED_BY]->(p);
"""


def export(src: dict) -> dict[str, int]:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}

    def dump(name: str, rows: list[dict], header: list[str]) -> None:
        p = EXPORT_DIR / name
        with open(p, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            for r in rows:
                w.writerow([("" if r.get(c) is None else r.get(c)) for c in header])
        written[name] = len(rows)

    # 空名守卫：节点名是 MERGE 的唯一键，为空会在装载时报 null 属性错误，
    # 也可能造成「行数对但内容空」的静默错误（曾踩过）。
    for key, label in (("purchaser", "采购单位"), ("supplier", "供应商"), ("brand", "品牌")):
        empty = [r for r in src[key] if not (r.get("name") or "").strip()]
        if empty:
            raise SystemExit("%s 节点有 %d 条 name 为空，拒绝导出/装载（请检查源数据）"
                             % (label, len(empty)))

    dump("nodes_purchaser.csv", src["purchaser"],
         ["name", "n_notices", "total_amount", "n_regions"])
    dump("nodes_supplier.csv", src["supplier"],
         ["name", "entity_id", "uscc", "win_times", "bid_only_times", "participate_times", "won_amount"])
    dump("nodes_brand.csv", src["brand"], ["name"])
    dump("nodes_notice.csv", src["notice"],
         ["notice_id", "project_name", "purchaser", "region", "publish_time",
          "project_amount", "procurement_mode", "n_winners", "n_bidders"])
    dump("rels_cooperates.csv", src["cooperates"],
         ["purchaser", "supplier", "coop_times", "coop_amount", "first_time", "last_time"])
    dump("rels_cobids.csv", src["cobids"],
         ["supplier_a", "supplier_b", "co_times", "co_project_amount", "a_wins", "b_wins"])
    dump("rels_participates.csv", src["participates"], ["notice_id", "supplier", "is_winner", "amount"])
    dump("rels_supplies.csv", src["supplies"],
         ["product_supplier", "winner", "item_times", "item_amount"])
    dump("rels_issued_by.csv",
         [{"notice_id": r.get("notice_id"), "purchaser": r.get("purchaser")}
          for r in src["notice"] if (r.get("purchaser") or "").strip()],
         ["notice_id", "purchaser"])

    # 注意：Cypher 里有大量 `{name: r.name}` 字面量，不能用 str.format（会被当占位符），用 token 替换
    cypher = CYPHER_HEADER.replace("@@CONSTRAINTS@@", ";\n".join(CONSTRAINTS + INDEXES) + ";")
    (EXPORT_DIR / "task2_graph.cypher").write_text(cypher, encoding="utf-8")
    (EXPORT_DIR / "README.txt").write_text(
        "把本目录内容放到 Neo4j 的 import 目录（默认 <neo4j-home>/import），然后：\n"
        "  cypher-shell -u neo4j -p <password> -f task2_graph.cypher\n"
        "或让脚本直接装载：设置 NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD 后\n"
        "  uv run python scripts/build_neo4j_graph.py --load\n", encoding="utf-8")
    return written


# ----------------------------------------------------------------------
# 装载到 Neo4j
# ----------------------------------------------------------------------
def load(src: dict, uri: str, user: str, password: str, database: str = "neo4j") -> dict:
    try:
        from neo4j import GraphDatabase
    except ImportError:
        raise SystemExit("缺少 neo4j 驱动：uv pip install neo4j")

    drv = GraphDatabase.driver(uri, auth=(user, password))
    counts: dict[str, int] = {}
    try:
        drv.verify_connectivity()
    except Exception as e:
        raise SystemExit(f"无法连接 Neo4j（{uri}）：{type(e).__name__}: {e}\n"
                         f"请确认 Neo4j 已启动，或改用 --export 仅导出可移植文件。")
    t0 = time.time()
    with drv.session(database=database) as s:
        for stmt in CONSTRAINTS + INDEXES:
            s.run(stmt).consume()

        def push(label: str, cypher: str, rows: list[dict], keys: list[str]) -> None:
            n = 0
            for i in range(0, len(rows), BATCH):
                chunk = [{k: r.get(k) for k in keys} for r in rows[i:i + BATCH]]
                s.run(cypher, rows=chunk).consume()
                n += len(chunk)
            counts[label] = n

        push("Purchaser", "UNWIND $rows AS r MERGE (p:Purchaser {name: r.name}) "
                          "SET p.n_notices=toInteger(r.n_notices), p.total_amount=toFloat(r.total_amount), "
                          "    p.n_regions=toInteger(r.n_regions)",
             src["purchaser"], ["name", "n_notices", "total_amount", "n_regions"])
        push("Supplier", "UNWIND $rows AS r MERGE (x:Supplier {name: r.name}) "
                         "SET x.entity_id=r.entity_id, x.uscc=r.uscc, x.win_times=toInteger(r.win_times), "
                         "    x.bid_only_times=toInteger(r.bid_only_times), "
                         "    x.participate_times=toInteger(r.participate_times), "
                         "    x.won_amount=toFloat(r.won_amount)",
             src["supplier"], ["name", "entity_id", "uscc", "win_times", "bid_only_times",
                               "participate_times", "won_amount"])
        push("Brand", "UNWIND $rows AS r MERGE (:Brand {name: r.name})", src["brand"], ["name"])
        push("Notice", "UNWIND $rows AS r MERGE (n:Notice {notice_id: r.notice_id}) "
                       "SET n.project_name=r.project_name, n.purchaser=r.purchaser, n.region=r.region, "
                       "    n.publish_time=r.publish_time, n.project_amount=toFloat(r.project_amount), "
                       "    n.procurement_mode=r.procurement_mode, "
                       "    n.n_winners=toInteger(r.n_winners), n.n_bidders=toInteger(r.n_bidders)",
             src["notice"], ["notice_id", "project_name", "purchaser", "region", "publish_time",
                             "project_amount", "procurement_mode", "n_winners", "n_bidders"])
        push("COOPERATES", "UNWIND $rows AS r "
                           "MATCH (p:Purchaser {name: r.purchaser}), (x:Supplier {name: r.supplier}) "
                           "MERGE (p)-[c:COOPERATES]->(x) "
                           "SET c.times=toInteger(r.coop_times), c.amount=toFloat(r.coop_amount), "
                           "    c.first_date=r.first_time, c.last_date=r.last_time",
             src["cooperates"], ["purchaser", "supplier", "coop_times", "coop_amount",
                                 "first_time", "last_time"])
        push("CO_BIDS", "UNWIND $rows AS r "
                        "MATCH (a:Supplier {name: r.supplier_a}), (b:Supplier {name: r.supplier_b}) "
                        "MERGE (a)-[x:CO_BIDS]->(b) "
                        "SET x.times=toInteger(r.co_times), x.project_amount=toFloat(r.co_project_amount), "
                        "    x.a_wins=toInteger(r.a_wins), x.b_wins=toInteger(r.b_wins)",
             src["cobids"], ["supplier_a", "supplier_b", "co_times", "co_project_amount", "a_wins", "b_wins"])
        push("PARTICIPATES", "UNWIND $rows AS r "
                             "MATCH (x:Supplier {name: r.supplier}), (n:Notice {notice_id: r.notice_id}) "
                             "MERGE (x)-[p:PARTICIPATES]->(n) "
                             "SET p.is_winner=toInteger(r.is_winner), p.amount=toFloat(r.amount)",
             src["participates"], ["notice_id", "supplier", "is_winner", "amount"])
        push("SUPPLIES", "UNWIND $rows AS r "
                         "MATCH (x:Supplier {name: r.winner}), (b:Brand {name: r.product_supplier}) "
                         "MERGE (x)-[p:SUPPLIES]->(b) "
                         "SET p.item_times=toInteger(r.item_times), p.item_amount=toFloat(r.item_amount)",
             src["supplies"], ["product_supplier", "winner", "item_times", "item_amount"])
        push("ISSUED_BY", "UNWIND $rows AS r "
                          "MATCH (n:Notice {notice_id: r.notice_id}), (p:Purchaser {name: r.purchaser}) "
                          "MERGE (n)-[:ISSUED_BY]->(p)",
             [r for r in src["notice"] if (r.get("purchaser") or "").strip()],
             ["notice_id", "purchaser"])

        # ---- 装载后校验（把"静默丢失"变成显式失败）----
        # MERGE 语义下期望关系数 = 去重后的键组合数；任何短缺都说明有 MATCH 未命中
        # （实测曾因 bid.supplier 用别名而静默丢掉 97 条 PARTICIPATES）。
        def distinct(rows_: list[dict], keys: tuple) -> int:
            return len({tuple((r.get(k) or "") for k in keys) for r in rows_})

        expect = {
            "COOPERATES": distinct(src["cooperates"], ("purchaser", "supplier")),
            "CO_BIDS": distinct(src["cobids"], ("supplier_a", "supplier_b")),
            "PARTICIPATES": distinct(src["participates"], ("notice_id", "supplier")),
            "SUPPLIES": distinct(src["supplies"], ("product_supplier", "winner")),
            "ISSUED_BY": distinct([r for r in src["notice"] if (r.get("purchaser") or "").strip()],
                                  ("notice_id", "purchaser")),
        }
        bad = {}
        for rel, exp in expect.items():
            act = int(s.run(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS n").single()["n"])
            counts[rel + "_verify"] = (act, int(exp))
            if act != int(exp):
                bad[rel] = (act, int(exp))
        counts["_shortfall"] = bad

        # ---- 「有中标方却查不到」的结构性校验（S1 的直接死因）----
        # 每条 n_winners>0 的公告都必须至少有一条 is_winner=1 的 PARTICIPATES；
        # 缺一条 = 该采购单位在 S1 里少一个供应商（甚至 0 家）。有缺口就逐条列出，
        # 由调用方决定中止（`sync_full_graph` 会抛错），不允许"图建完了但查不到中标方"。
        win_notices = int(s.run(
            "MATCH (n:Notice) WHERE toInteger(coalesce(n.n_winners,0)) > 0 "
            "RETURN count(n) AS n").single()["n"])
        covered = int(s.run(
            "MATCH (n:Notice)<-[p:PARTICIPATES]-(:Supplier) WHERE p.is_winner = 1 "
            "RETURN count(DISTINCT n) AS n").single()["n"])
        gap = [r["nid"] for r in s.run(
            "MATCH (n:Notice) WHERE toInteger(coalesce(n.n_winners,0)) > 0 "
            "AND NOT EXISTS { (n)<-[p:PARTICIPATES]-(:Supplier) WHERE p.is_winner = 1 } "
            "RETURN n.notice_id AS nid ORDER BY nid")]
        counts["_winners_coverage"] = (covered, win_notices)
        counts["_winners_gap"] = gap
    drv.close()
    counts["_elapsed_sec"] = round(time.time() - t0, 2)
    return counts


def wipe(uri: str, user: str, password: str, database: str = "neo4j") -> int:
    """清空图（避免重复装载时残留旧节点/关系），分批删除以免大事务。"""
    from neo4j import GraphDatabase

    drv = GraphDatabase.driver(uri, auth=(user, password))
    total = 0
    try:
        drv.verify_connectivity()
        with drv.session(database=database) as s:
            while True:
                r = s.run("MATCH (x) WITH x LIMIT 50000 DETACH DELETE x RETURN count(x) AS n").single()
                c = int(r["n"]) if r else 0
                total += c
                if c == 0:
                    break
    finally:
        drv.close()
    return total


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true", help="装载到 Neo4j（需 NEO4J_URI）")
    ap.add_argument("--export", action="store_true", help="导出可移植 Cypher + CSV")
    ap.add_argument("--wipe", action="store_true", help="装载前清空图（避免残留）")
    ap.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"))
    ap.add_argument("--user", default=os.getenv("NEO4J_USER", "neo4j"))
    ap.add_argument("--password", default=os.getenv("NEO4J_PASSWORD", "neo4j"))
    ap.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    a = ap.parse_args()
    if not (a.load or a.export):
        ap.error("至少指定 --load 或 --export")

    src = load_source()
    print("源数据：", {k: len(v) for k, v in src.items()})
    if a.export:
        w = export(src)
        print("已导出 ->", EXPORT_DIR)
        for k, v in w.items():
            print("   %-26s %d 行" % (k, v))
        print("   任务二图 Cypher ->", EXPORT_DIR / "task2_graph.cypher")
    if a.load:
        if a.wipe:
            print("已清空图中节点/关系：", wipe(a.uri, a.user, a.password, a.database))
        c = load(src, a.uri, a.user, a.password, a.database)
        print("已装载到 Neo4j（%s）：" % a.uri)
        for k, v in c.items():
            print("   %-16s %s" % (k, v))
        cov = c.get("_winners_coverage") or (0, 0)
        gap = c.get("_winners_gap") or []
        if gap:
            print("⚠ 中标方覆盖缺口：%d 篇公告 n_winners>0 但图上没有中标参与关系（前 5：%s）"
                  % (len(gap), gap[:5]))
        else:
            print("中标方覆盖：有中标方的公告 %d 篇 / 图上可查 %d 篇（缺口 0）"
                  % (int(cov[1]), int(cov[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
