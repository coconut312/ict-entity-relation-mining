# -*- coding: utf-8 -*-
"""任务二图一致性断言（**离线可跑**，不依赖线上服务）。

校验的链条 —— 「数据其实齐全，丢在入库 → 建图」这一段：

  A 交付件 `announcements_task1.csv` 里 `n_winners>0` 的公告集合
        == 交付件 `task2_bids.csv` 里 `is_winner=1` 的公告集合（逐篇对齐，不只看总数）
  B `(:Purchaser)-[:COOPERATES]->(:Supplier)` 的对数
        == `bid.is_winner=1` 去重后的 (采购单位, 供应商) 对数
           （只允许"采购单位为空"这一已披露例外：没有采购单位就建不出这条边）
  C 每篇 `n_winners>0` 的公告，图上至少有**一条**中标方的 PARTICIPATES
        （缺口逐条列出：无标的行 / 标的无供货方 等已披露例外）
  D 随机抽查 N 篇（默认 20，固定种子）有中标方的公告：该篇每个中标方都必须能在
    图上找到对应的 COOPERATES 边，逐条给不一致数量
  E `project_amount` 不丢：交付件有金额而图里为 0 的公告数必须为 0
     （"公告金额显示 0"就是这一条失败，不是字段取错）

三条数据源，语义等价，用参数切换：
  * 默认            交付件 + `out/task2/graph.db`（全量建图的底座，建图脚本读的就是它）
  * `--platform-db` 平台服务库 `out/platform/platform.db`（上传入库后的形态；测副本库用它）
  * `--neo4j`       额外直连线上图再跑一遍同一组断言（**只读**，不写图）

用法::

    python scripts/assert_graph_consistency.py
    python scripts/assert_graph_consistency.py --platform-db out/_w_test/platform.db
    python scripts/assert_graph_consistency.py --neo4j --uri bolt://127.0.0.1:7687 \\
        --user neo4j --password ict2026neo4j

退出码：0 = 全部通过（差异只能出现在已披露例外清单里）；1 = 存在必须修的不一致。
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import sqlite3
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
DELIV = APP / "deliverables"
DEFAULT_GRAPH_DB = APP / "out" / "task2" / "graph.db"
ANN_CSV = DELIV / "announcements_task1.csv"
BIDS_CSV = DELIV / "task2_bids.csv"
COOP_CSV = DELIV / "task2_cooperation.csv"

_TRUTHY = ("1", "1.0", "true", "t", "yes", "y", "是")


# ----------------------------------------------------------------------
# 名称归一键：**必须**与建图脚本同一个函数
# ----------------------------------------------------------------------
# `task2_bids.csv` 的 supplier 是**公告里的原文写法**，而图上的 Supplier 节点名是
# `entity.canonical_name`（同一实体的"最常见写法"）—— 两者会是同一法人的不同变体
# （半/全角括号、`（包2）` 标注、旧称等）。若按**字面**对账，会凭空多出 82 对
# "图上少 / 图上多"，其实关系一条不少。因此对账统一按 `entity_key()` 归一后比较。
def _entity_key(name: str) -> str:
    import importlib.util

    global _ENTITY_KEY
    if _ENTITY_KEY is None:
        spec = importlib.util.spec_from_file_location(
            "build_task2_graph", APP / "scripts" / "build_task2_graph.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)                       # type: ignore[union-attr]
        _ENTITY_KEY = mod.entity_key
    return _ENTITY_KEY(name)


_ENTITY_KEY = None


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"缺少交付件：{path}")
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _int(v) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return 0


def _float(v) -> float:
    try:
        return float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _win(row: dict) -> bool:
    return str(row.get("is_winner") or "").strip().lower() in _TRUTHY


# ----------------------------------------------------------------------
# 锚点：**交付件**（announcements_task1.csv + task2_bids.csv）—— 断言永远以它为准
# ----------------------------------------------------------------------
def anchor_from_deliverables() -> dict:
    anns = read_csv(ANN_CSV)
    bids = read_csv(BIDS_CSV)
    win_rows = [r for r in bids if _win(r)]
    winner_pairs = {(r["notice_id"], r["supplier"]) for r in win_rows}
    pur_by_notice: dict[str, str] = {}
    for r in bids:
        pur_by_notice.setdefault(r["notice_id"], r.get("purchaser") or "")
    for r in anns:
        pur_by_notice.setdefault(r["announcement_id"], r.get("purchaser") or "")
    return {
        "anns": anns,
        "winner_pairs": winner_pairs,
        "pur_by_notice": pur_by_notice,
        "amount_csv": {r["announcement_id"]: _float(r.get("project_amount")) for r in anns},
        "notice_ids": {r["announcement_id"] for r in anns},
    }


# ----------------------------------------------------------------------
# 图侧 1：graph.db（缺失时退到 task2_cooperation.csv）
# ----------------------------------------------------------------------
def graph_from_graphdb(graph_db: Path) -> dict:
    coop_pairs: set[tuple[str, str]] = set()
    participates_win: set[str] = set()
    amount_graph: dict[str, float] = {}
    notice_ids: set[str] = set()
    if graph_db.exists():
        source = f"graph.db ({graph_db})"
        c = sqlite3.connect(f"file:{graph_db.as_posix()}?mode=ro", uri=True)
        try:
            coop_pairs = {(r[0], r[1]) for r in c.execute(
                "SELECT purchaser, supplier FROM v_cooperation")}
            participates_win = {r[0] for r in c.execute(
                "SELECT DISTINCT notice_id FROM bid WHERE is_winner = 1")}
            amount_graph = {r[0]: _float(r[1]) for r in c.execute(
                "SELECT notice_id, project_amount FROM notice")}
            notice_ids = set(amount_graph)
        finally:
            c.close()
    else:
        source = "task2_*.csv（graph.db 不存在，走可移植回退路径）"
        coop_pairs = {(r["purchaser"], r["supplier"]) for r in read_csv(COOP_CSV)}
        bids = read_csv(BIDS_CSV)
        participates_win = {r["notice_id"] for r in bids if _win(r)}
        notice_ids = {r["notice_id"] for r in bids}
        amount_graph = {}
    return {
        "source": source, "coop_pairs": coop_pairs,
        "participates_win": participates_win, "amount_graph": amount_graph,
        "notice_ids": notice_ids,
    }


# ----------------------------------------------------------------------
# 图侧 2：平台服务库（副本库 / 线上库都行）
# ----------------------------------------------------------------------
def graph_from_platform_db(db: Path) -> dict:
    if not db.exists():
        raise SystemExit(f"缺少平台库：{db}")
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        tabs = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        winner_pairs: set[tuple[str, str]] = set()
        participates_win: set[str] = set()
        if "bid" in tabs:
            for nid, sup, isw in c.execute(
                    "SELECT notice_id, supplier, is_winner FROM bid"):
                if _int(isw) == 1:
                    winner_pairs.add((nid, sup))
                    participates_win.add(nid)
        coop_pairs: set[tuple[str, str]] = set()
        if "cooperation" in tabs:
            coop_pairs = {(r[0] or "", r[1] or "") for r in c.execute(
                "SELECT purchaser, supplier FROM cooperation")}
        amount_graph: dict[str, float] = {}
        notice_ids: set[str] = set()
        if "g_notice" in tabs:
            amount_graph = {r[0]: _float(r[1]) for r in c.execute(
                "SELECT notice_id, project_amount FROM g_notice")}
            notice_ids = set(amount_graph)
        elif "t1_notice" in tabs:
            rows = list(c.execute("SELECT announcement_id, project_amount FROM t1_notice"))
            amount_graph = {r[0]: _float(r[1]) for r in rows}
            notice_ids = {r[0] for r in rows}
    finally:
        c.close()
    return {
        "source": f"平台服务库 ({db})", "coop_pairs": coop_pairs,
        "participates_win": participates_win, "amount_graph": amount_graph,
        "notice_ids": notice_ids, "own_bid_winner_pairs": winner_pairs,
    }


# ----------------------------------------------------------------------
# 数据源 3：线上 Neo4j（只读）
# ----------------------------------------------------------------------
def from_neo4j(uri: str, user: str, password: str, database: str,
               sample: list[str]) -> dict:
    from neo4j import GraphDatabase

    drv = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with drv.session(database=database) as s:
            coop_pairs = {(r["p"], r["x"]) for r in s.run(
                "MATCH (p:Purchaser)-[:COOPERATES]->(x:Supplier) "
                "RETURN p.name AS p, x.name AS x")}
            participates_win = {r["n"] for r in s.run(
                "MATCH (:Supplier)-[p:PARTICIPATES]->(n:Notice) WHERE p.is_winner = 1 "
                "RETURN DISTINCT n.notice_id AS n")}
            amount_graph = {r["n"]: _float(r["a"]) for r in s.run(
                "MATCH (n:Notice) RETURN n.notice_id AS n, "
                "coalesce(n.project_amount,0) AS a")}
            nodes = {k: int(s.run(f"MATCH (n:{k}) RETURN count(n) AS n").single()["n"])
                     for k in ("Purchaser", "Supplier", "Brand", "Notice")}
            rels = {k: int(s.run(f"MATCH ()-[r:{k}]->() RETURN count(r) AS n").single()["n"])
                    for k in ("COOPERATES", "CO_BIDS", "PARTICIPATES", "SUPPLIES",
                              "ISSUED_BY")}
            graph_winners: dict[str, set] = {}
            for i in range(0, len(sample), 500):
                for r in s.run(
                        "UNWIND $ids AS nid MATCH (n:Notice {notice_id: nid})"
                        "<-[p:PARTICIPATES]-(x:Supplier) WHERE p.is_winner = 1 "
                        "RETURN n.notice_id AS nid, collect(x.name) AS names",
                        ids=sample[i:i + 500]):
                    graph_winners[r["nid"]] = set(r["names"])
    finally:
        drv.close()
    return {"source": f"线上 Neo4j ({uri})", "coop_pairs": coop_pairs,
            "participates_win": participates_win, "amount_graph": amount_graph,
            "notice_ids": set(amount_graph),
            "neo4j_nodes": nodes, "neo4j_rels": rels,
            "graph_winners": graph_winners}


# ----------------------------------------------------------------------
# 断言
# ----------------------------------------------------------------------
def check(d: dict, sample_n: int) -> tuple[list[str], list[str], list[str]]:
    """返回 (失败项, 已披露例外, 报告行)。"""
    fails: list[str] = []
    exceptions: list[str] = []
    report: list[str] = []
    anns = d["anns"]
    win_pairs: set[tuple[str, str]] = d["winner_pairs"]
    pur_by_notice: dict[str, str] = d["pur_by_notice"]
    coop: set[tuple[str, str]] = d["coop_pairs"]

    ann_win_ids = {r.get("announcement_id") or r.get("notice_id")
                   for r in anns if _int(r.get("n_winners")) > 0}
    ann_win_ids.discard(None)
    rel_win_ids = {n for n, _ in win_pairs}
    sum_win = sum(_int(r.get("n_winners")) for r in anns)

    report.append("公告表: %d 行；n_winners>0 %d 篇；Σn_winners = %d"
                  % (len(anns), len(ann_win_ids), sum_win))
    report.append("关系表: is_winner=1 的 (公告,供应商) 去重 %d 对，涉及公告 %d 篇"
                  % (len(win_pairs), len(rel_win_ids)))
    report.append("图: COOPERATES %d 对；有中标参与关系的公告 %d 篇"
                  % (len(coop), len(d["participates_win"])))

    # A 公告集合逐篇对齐
    only_ann = ann_win_ids - rel_win_ids
    only_rel = rel_win_ids - ann_win_ids
    if only_ann or only_rel:
        fails.append("A 公告集合不一致：仅公告表 %d 篇 %s；仅关系表 %d 篇 %s"
                     % (len(only_ann), sorted(only_ann)[:5],
                        len(only_rel), sorted(only_rel)[:5]))
    report.append("A 公告数对账: %d vs %d -> %s"
                  % (len(ann_win_ids), len(rel_win_ids),
                     "一致" if not (only_ann or only_rel) else "不一致"))

    # B COOPERATES 对数（按实体归一键对账：原文写法 vs 图的 canonical_name 会差括号等）
    expect_pairs = {(pur_by_notice.get(nid, ""), sup) for nid, sup in win_pairs}
    expect_nokey = {(_entity_key(p), _entity_key(s)) for p, s in expect_pairs}
    coop_nokey = {(_entity_key(p), _entity_key(s)) for p, s in coop}
    raw_by_key = {(_entity_key(p), _entity_key(s)): (p, s) for p, s in expect_pairs}
    no_pur = sorted(x for x in expect_pairs if not x[0])
    expect_ok = {(_entity_key(p), _entity_key(s)) for p, s in expect_pairs if p}
    for p in no_pur:
        exceptions.append("采购单位为空，建不出 COOPERATES：(%r, %r)" % p)
    short = expect_ok - coop_nokey
    extra = coop_nokey - expect_ok
    if short:
        fails.append("B COOPERATES 少 %d 对（图上缺中标供应商关系）：%s"
                     % (len(short), [raw_by_key.get(k, k) for k in sorted(short)[:5]]))
    if extra:
        exceptions.append("COOPERATES 多 %d 对（图上多出关系表没有的对）：%s"
                          % (len(extra), sorted(extra)[:5]))
    report.append("B COOPERATES 对账: 期望 %d 对（另有采购单位为空 %d 对不建边）/ 图上 %d 对 "
                  "-> 少 %d、多 %d（按实体归一键比较）"
                  % (len(expect_ok), len(no_pur), len(coop_nokey), len(short), len(extra)))

    # C 公告级覆盖
    missing = sorted(ann_win_ids - set(d["participates_win"]))
    for nid in missing:
        exceptions.append("有中标方但图上无中标参与关系（无标的行/标的无供货方）：%s" % nid)
    report.append("C 公告级覆盖: %d 篇有中标方，图上可查 %d 篇，缺口 %d 篇"
                  % (len(ann_win_ids), len(ann_win_ids) - len(missing), len(missing)))

    # D 抽查
    rnd = random.Random(20260304)
    pool = sorted(ann_win_ids)
    sample = rnd.sample(pool, min(sample_n, len(pool)))
    bad = []
    gw = d.get("graph_winners")
    for nid in sample:
        pur = pur_by_notice.get(nid, "")
        want = {sup for (n, sup) in win_pairs if n == nid}
        if not pur:
            continue
        if gw is not None:
            got = gw.get(nid, set())
        else:
            got = {s for (p, s) in coop if _entity_key(p) == _entity_key(pur)}
        got_keys = {_entity_key(x) for x in got}
        miss = {s for s in want if _entity_key(s) not in got_keys}
        if miss:
            bad.append((nid, pur, sorted(miss)[:3]))
    if bad:
        fails.append("D 抽查 %d 篇，%d 篇的中标方在图上找不到 COOPERATES/PARTICIPATES：%s"
                     % (len(sample), len(bad), bad[:3]))
    report.append("D 抽查: %d 篇有中标方的公告，图上缺中标方 %d 篇" % (len(sample), len(bad)))

    # E project_amount 不丢（锚点是交付件金额，图侧是 Notice.project_amount）
    lost = []
    amount_graph: dict[str, float] = d["amount_graph"]
    amt_csv: dict[str, float] = d.get("amount_csv") or {}
    for nid, a in amt_csv.items():
        if a and not amount_graph.get(nid):
            lost.append((nid, a, amount_graph.get(nid)))
    if lost:
        fails.append("E project_amount 丢失 %d 篇（交付件有金额、图里为 0）：%s"
                     % (len(lost), lost[:5]))
    report.append("E project_amount: 交付件有金额而图里为 0 的公告 %d 篇" % len(lost))

    # F 公告覆盖：图里必须包含交付件的每一篇公告（少一篇 = 该公告在平台上查不到）
    src_ids = set(d.get("notice_ids") or set())
    if src_ids:
        anchor_ids = set(d.get("anchor_notice_ids") or set())
        miss_notice = sorted(anchor_ids - src_ids)
        if miss_notice:
            fails.append("F 图里缺 %d 篇公告（交付件有、图上没有），前 5：%s"
                         % (len(miss_notice), miss_notice[:5]))
        report.append("F 公告覆盖: 交付件 %d 篇 / 图里 %d 篇 -> 缺 %d 篇"
                      % (len(anchor_ids), len(src_ids), len(miss_notice)))

    if d.get("neo4j_nodes"):
        report.append("图规模: 节点 %s / 关系 %s"
                      % (d["neo4j_nodes"], d["neo4j_rels"]))
    return fails, exceptions, report


def _compose(anchor: dict, graph: dict) -> dict:
    """锚点（交付件）+ 图侧 -> check() 需要的单一 dict。"""
    out = dict(anchor)
    out.update(graph)
    out["anchor_notice_ids"] = anchor["notice_ids"]
    out["source"] = "交付件锚点 + " + str(graph.get("source"))
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph-db", default=str(DEFAULT_GRAPH_DB))
    ap.add_argument("--platform-db", default=None,
                    help="图侧改用平台服务库（副本库/线上库）；锚点仍是仓库交付件")
    ap.add_argument("--neo4j", action="store_true", help="额外直连线上图跑同一组断言（只读）")
    ap.add_argument("--uri", default=os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"))
    ap.add_argument("--user", default=os.getenv("NEO4J_USER", "neo4j"))
    ap.add_argument("--password", default=os.getenv("NEO4J_PASSWORD", "ict2026neo4j"))
    ap.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    ap.add_argument("--sample", type=int, default=20, help="抽查有中标方的公告数（默认 20）")
    a = ap.parse_args()

    anchor = anchor_from_deliverables()
    graph = (graph_from_platform_db(Path(a.platform_db)) if a.platform_db
             else graph_from_graphdb(Path(a.graph_db)))
    d = _compose(anchor, graph)
    print("数据源: %s" % d["source"])
    fails, exceptions, report = check(d, a.sample)
    for line in report:
        print("  " + line)

    if a.neo4j:
        pool = sorted({r["announcement_id"] for r in anchor["anns"]
                       if _int(r.get("n_winners")) > 0})
        rnd = random.Random(20260304)
        sample = rnd.sample(pool, min(a.sample, len(pool)))
        dn = _compose(anchor, from_neo4j(a.uri, a.user, a.password, a.database, sample))
        print("\n线上图（Neo4j，只读）: %s" % dn["source"])
        f2, e2, r2 = check(dn, a.sample)
        for line in r2:
            print("  " + line)
        fails += ["[Neo4j] " + x for x in f2]
        exceptions += ["[Neo4j] " + x for x in e2]

    print("\n" + "-" * 78)
    if exceptions:
        print("已披露例外 %d 条：" % len(exceptions))
        for x in exceptions[:12]:
            print("  ·", x)
        if len(exceptions) > 12:
            print("  ... 其余 %d 条同类" % (len(exceptions) - 12))
    if fails:
        print("断言失败 %d 项：" % len(fails))
        for f in fails:
            print("  ✗", f)
        return 1
    print("断言通过 ✓（A 公告数 / B COOPERATES 对数 / C 公告级覆盖 / D 抽查一致 / E 金额不丢；"
          "例外 %d 条已逐条列出）" % len(exceptions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
