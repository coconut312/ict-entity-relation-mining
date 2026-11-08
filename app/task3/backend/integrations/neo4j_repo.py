# -*- coding: utf-8 -*-
"""任务二关系模型 · **Neo4j 实现**（《接口契约与工程整合》接入点 2 的 Neo4j 版）。

与 `repos.py::SQLiteGraphRepository` 实现**同一个契约接口**，因此平台前后端无需任何改动，
只需切换环境变量：

    set EDM_GRAPH_REPOSITORY=integrations.neo4j_repo:Neo4jGraphRepository
    set NEO4J_URI=bolt://127.0.0.1:7687
    set NEO4J_USER=neo4j
    set NEO4J_PASSWORD=<password>

图模型见 `scripts/build_neo4j_graph.py`（节点 Purchaser/Supplier/Brand/Notice，
关系 COOPERATES / CO_BIDS / PARTICIPATES / SUPPLIES / ISSUED_BY）。

> 未连接到 Neo4j 时，`get_graph_repository()` 会按契约**降级为空实现**并把原因打到日志，
> 平台不会起不来（见 `app/repositories.py`）。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from app.repositories import GraphRepository
from app.schemas import (BidderStat, CoBidPair, ProjectRecord, PurchaserStat,
                         Scenario1Out, Scenario2Out, Scenario3Out, Scenario4Out,
                         Scenario5Out, SupplierStat)

_DATE_RE = re.compile(r"(\d{4})年(\d{2})月(\d{2})日")


def _d(v: Any) -> Optional[str]:
    """把 '2026年02月02日 11:15' 归一为 'YYYY-MM-DD'（已是 ISO 则截断）。"""
    if not v:
        return None
    m = _DATE_RE.search(str(v))
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    t = str(v)[:10]
    return t if re.match(r"\d{4}-\d{2}-\d{2}", t) else None


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


class Neo4jGraphRepository(GraphRepository):
    """用 Cypher 实现任务二五大场景。

    查询与 SQLite 版**语义等价**：
      S1 合作 = (Purchaser)-[:COOPERATES]->(Supplier)
      S2 同项目共同投标 = 同一 Notice 上的两个 PARTICIPATES
      S3 共同竞标 = CO_BIDS（无向匹配）
      S4 「均有合作」 = 按采购单位聚合后成员数 == 输入主体数
      S5 共同项目 = 按公告聚合后参与主体覆盖全部输入主体
    """

    def __init__(self) -> None:
        from neo4j import GraphDatabase  # 延迟导入：未装驱动时只报错不崩服务

        uri = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
        user = os.getenv("NEO4J_USER", "neo4j")
        pwd = os.getenv("NEO4J_PASSWORD", "neo4j")
        self.database = os.getenv("NEO4J_DATABASE", "neo4j")
        self._drv = GraphDatabase.driver(uri, auth=(user, pwd))
        self._drv.verify_connectivity()          # 连不上就抛错 -> 平台按契约降级为空实现
        self.uri = uri

    # ---- 基础 ----
    def _run(self, cypher: str, **params) -> List[Dict[str, Any]]:
        with self._drv.session(database=self.database) as s:
            return [dict(r) for r in s.run(cypher, **params)]

    def _one(self, cypher: str, **params) -> Optional[Dict[str, Any]]:
        r = self._run(cypher, **params)
        return r[0] if r else None

    def has_data(self) -> bool:
        return bool(self._one("MATCH (s:Supplier) RETURN count(s) AS n")["n"])

    # ---- S1 长期合作（含产品供应商）----
    def scenario1(self, purchaser: str, min_count: int = 1, top_n: int = 20) -> Scenario1Out:
        out = Scenario1Out(purchaser=purchaser)
        rows = self._run("""
            MATCH (p:Purchaser {name: $purchaser})-[c:COOPERATES]->(s:Supplier)
            WHERE c.times >= $min_count
            RETURN s.name AS name, c.times AS cooperate_count, c.amount AS total_amount,
                   c.first_date AS first_date, c.last_date AS last_date,
                   EXISTS { (s)-[:SUPPLIES]->(:Brand) } AS is_product_supplier
            ORDER BY c.times DESC, c.amount DESC LIMIT $top_n
        """, purchaser=purchaser, min_count=max(1, min_count), top_n=top_n)
        names = {r["name"] for r in rows}
        for r in rows:
            out.suppliers.append(SupplierStat(
                name=r["name"], cooperate_count=int(r["cooperate_count"] or 0),
                total_amount=_f(r["total_amount"]), first_date=_d(r["first_date"]),
                last_date=_d(r["last_date"]), is_product_supplier=bool(r["is_product_supplier"])))
        # 产品供应商（该采购单位的中标供应商所供品牌）
        prods = self._run("""
            MATCH (p:Purchaser {name: $purchaser})-[:COOPERATES]->(:Supplier)-[:SUPPLIES]->(b:Brand)
            RETURN b.name AS name, count(*) AS notices ORDER BY notices DESC LIMIT $top_n
        """, purchaser=purchaser, top_n=top_n)
        for r in prods:
            if r["name"] in names:
                for s in out.suppliers:
                    if s.name == r["name"]:
                        s.is_product_supplier = True
                continue
            names.add(r["name"])
            out.suppliers.append(SupplierStat(name=r["name"], cooperate_count=0,
                                              total_amount=0.0, is_product_supplier=True))
        st = self._one("""
            MATCH (p:Purchaser {name: $purchaser})<-[:ISSUED_BY]-(n:Notice)
            RETURN count(n) AS c, sum(coalesce(n.project_amount,0)) AS a
        """, purchaser=purchaser) or {}
        out.summary = {
            "n_suppliers": len([s for s in out.suppliers if s.cooperate_count > 0]),
            "n_product_suppliers": len([s for s in out.suppliers if s.is_product_supplier]),
            "n_notices": int(st.get("c") or 0), "total_amount": _f(st.get("a")),
            "min_count": max(1, min_count), "engine": "neo4j",
        }
        return out

    # ---- S2 投标画像 ----
    def scenario2(self, purchaser: str, top_n: int = 5) -> Scenario2Out:
        out = Scenario2Out(purchaser=purchaser)
        for r in self._run("""
            MATCH (n:Notice)-[:ISSUED_BY]->(p:Purchaser {name: $purchaser})
            MATCH (s:Supplier)-[x:PARTICIPATES]->(n)
            RETURN s.name AS name, count(x) AS bid_count, sum(x.is_winner) AS win_count,
                   sum(CASE WHEN x.is_winner = 1 THEN coalesce(x.amount,0) ELSE 0 END) AS amount
            ORDER BY bid_count DESC, win_count DESC, name ASC LIMIT $top_n
        """, purchaser=purchaser, top_n=top_n):
            out.top_bidders.append(BidderStat(
                name=r["name"], bid_count=int(r["bid_count"] or 0),
                win_count=int(r["win_count"] or 0), cooperate_amount=_f(r["amount"])))
        for r in self._run("""
            MATCH (n:Notice)-[:ISSUED_BY]->(p:Purchaser {name: $purchaser})
            MATCH (a:Supplier)-[:PARTICIPATES]->(n)<-[:PARTICIPATES]-(b:Supplier)
            WHERE a.name < b.name
            WITH a, b, count(DISTINCT n) AS cnt,
                 collect(DISTINCT n.project_name)[0..3] AS projects
            RETURN a.name AS supplier_a, b.name AS supplier_b, cnt AS _n, projects
            ORDER BY cnt DESC, supplier_a ASC, supplier_b ASC LIMIT $top_n
        """, purchaser=purchaser, top_n=top_n):
            out.co_bid_pairs.append(CoBidPair(
                members=[r["supplier_a"], r["supplier_b"]], count=int(r["_n"] or 0),
                projects=[(x or "")[:40] for x in (r["projects"] or [])]))
        tot = self._one("""
            MATCH (n:Notice)-[:ISSUED_BY]->(p:Purchaser {name: $purchaser})
            MATCH (s:Supplier)-[:PARTICIPATES]->(n)
            RETURN count(DISTINCT s) AS n
        """, purchaser=purchaser) or {}
        out.summary = {"n_bidders": int(tot.get("n") or 0),
                       "n_co_bid_pairs": len(out.co_bid_pairs), "top_n": top_n, "engine": "neo4j"}
        return out

    # ---- S3 竞对分析 ----
    def scenario3(self, supplier: str, top_n: int = 5) -> Scenario3Out:
        out = Scenario3Out(supplier=supplier)
        rows = self._run("""
            MATCH (s:Supplier {name: $supplier})-[r:CO_BIDS]-(o:Supplier)
            WITH o, r, CASE WHEN startNode(r) = s THEN r.b_wins ELSE r.a_wins END AS opp_wins
            RETURN o.name AS name, r.times AS bid_count, opp_wins AS win_count,
                   r.project_amount AS amount
            ORDER BY bid_count DESC, name ASC
        """, supplier=supplier)
        for r in rows:
            st = BidderStat(name=r["name"], bid_count=int(r["bid_count"] or 0),
                            win_count=int(r["win_count"] or 0), cooperate_amount=_f(r["amount"]))
            out.all_bidders.append(st)
            if len(out.top_competitors) < top_n:
                out.top_competitors.append(st)
        prof = self._one("""
            MATCH (s:Supplier {name: $supplier})
            RETURN s.win_times AS w, s.bid_only_times AS b, s.participate_times AS p,
                   s.won_amount AS amt
        """, supplier=supplier) or {}
        tot = self._one("""
            MATCH (s:Supplier {name: $supplier})-[:PARTICIPATES]->(n:Notice)
            MATCH (o:Supplier)-[:PARTICIPATES]->(n)
            RETURN count(DISTINCT o) AS n
        """, supplier=supplier) or {}
        out.summary = {
            "win_notices": int(prof.get("w") or 0), "bid_notices": int(prof.get("p") or 0),
            "win_rate": (round(int(prof.get("w") or 0) / int(prof.get("p") or 1), 3)
                         if prof.get("p") else None),
            "won_amount": _f(prof.get("amt")), "n_competitors": len(out.all_bidders),
            "n_participants": int(tot.get("n") or 0), "engine": "neo4j",
        }
        return out

    # ---- S4 共同客户 ----
    def scenario4(self, suppliers: List[str], min_count: int = 1,
                  top_n: int = 20) -> Scenario4Out:
        out = Scenario4Out(suppliers=list(suppliers))
        if len(suppliers) < 2:
            return out
        for r in self._run("""
            MATCH (p:Purchaser)-[c:COOPERATES]->(s:Supplier)
            WHERE s.name IN $suppliers
            WITH p, collect(DISTINCT s.name) AS names,
                 sum(c.times) AS times, sum(coalesce(c.amount,0)) AS amount
            WHERE size(names) = size($suppliers) AND times >= $min_count
            OPTIONAL MATCH (n:Notice)-[:ISSUED_BY]->(p)
            WHERE EXISTS { (w:Supplier)-[:PARTICIPATES {is_winner:1}]->(n) WHERE w.name IN $suppliers }
            WITH p, names, times, amount, collect(DISTINCT n.project_name)[0..3] AS projects
            RETURN p.name AS name, size(names) AS n_suppliers, times AS cooperate_count,
                   amount AS cooperate_amount, projects
            ORDER BY times DESC, amount DESC LIMIT $top_n
        """, suppliers=list(suppliers), min_count=max(1, min_count), top_n=top_n):
            out.purchasers.append(PurchaserStat(
                name=r["name"], cooperate_count=int(r["cooperate_count"] or 0),
                cooperate_amount=_f(r["cooperate_amount"]),
                projects=[(x or "")[:40] for x in (r["projects"] or [])]))
        out.summary = {"n_purchasers": len(out.purchasers), "suppliers": list(suppliers),
                       "min_count": max(1, min_count), "engine": "neo4j"}
        return out

    # ---- S5 共同项目 ----
    def scenario5(self, suppliers: List[str], top_n: int = 50) -> Scenario5Out:
        out = Scenario5Out(suppliers=list(suppliers))
        if len(suppliers) < 2:
            return out
        rows = self._run("""
            MATCH (s:Supplier)-[:PARTICIPATES]->(n:Notice)
            WHERE s.name IN $suppliers
            WITH n, collect(DISTINCT s.name) AS targets
            WHERE size(targets) = size($suppliers)
            MATCH (n)-[:ISSUED_BY]->(p:Purchaser)
            OPTIONAL MATCH (o:Supplier)-[x:PARTICIPATES]->(n)
            WITH n, p, collect(DISTINCT o.name) AS participants,
                 collect(DISTINCT CASE WHEN x.is_winner = 1 THEN o.name END) AS winners
            RETURN n.notice_id AS doc_id, n.project_name AS project_name,
                   n.publish_time AS publish_time, coalesce(n.project_amount,0) AS amount,
                   p.name AS purchaser, participants,
                   [w IN winners WHERE w IS NOT NULL] AS winners
            ORDER BY n.publish_time, n.notice_id LIMIT $top_n
        """, suppliers=list(suppliers), top_n=top_n)
        total = 0.0
        for r in rows:
            amt = _f(r["amount"])
            total += amt
            wins = [w for w in (r["winners"] or []) if w]
            out.projects.append(ProjectRecord(
                project_name=r["project_name"] or "", doc_id=r["doc_id"],
                announce_date=_d(r["publish_time"]), amount=amt,
                purchaser=r["purchaser"] or "", winner="、".join(wins[:3]),
                participants=[x for x in (r["participants"] or []) if x],
                winner_is_target=any(w in set(suppliers) for w in wins)))
        out.summary = {"n_projects": len(out.projects), "total_amount": round(total, 2),
                       "suppliers": list(suppliers), "engine": "neo4j"}
        return out
