"""Neo4j 交叉验证：装载图库 → 五场景在 Neo4j 与 SQLite 上各跑一遍 → 逐项比对。

用法（拿到可达的 Neo4j 后一条命令跑完）：
    set NEO4J_URI=bolt://127.0.0.1:7687          # Aura 用 neo4j+s://xxxx.databases.neo4j.io
    set NEO4J_USER=neo4j
    set NEO4J_PASSWORD=xxxx
    uv run python scripts/verify_neo4j.py

或直接传参：
    uv run python scripts/verify_neo4j.py --uri neo4j+s://xxx --user neo4j --password xxx

退出码：0 = 可比项全部一致；1 = 存在差异（会打印差异明细）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "task3" / "backend"))

PURCHASER = "济宁学院"
SUPPLIER = "中国移动通信集团广东有限公司"
PAIR = ["山东世纪读秀信息科技有限公司", "山西同方知网数字出版技术有限公司"]


def _brief(txt: str, n: int = 46) -> str:
    txt = str(txt)
    return txt if len(txt) <= n else txt[:n - 1] + "…"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default=os.getenv("NEO4J_URI", ""))
    ap.add_argument("--user", default=os.getenv("NEO4J_USER", "neo4j"))
    ap.add_argument("--password", default=os.getenv("NEO4J_PASSWORD", ""))
    ap.add_argument("--database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    ap.add_argument("--no-load", action="store_true", help="跳过装载（图上已有数据时）")
    a = ap.parse_args()
    if not a.uri or not a.password:
        print("缺少连接信息：请设置 NEO4J_URI 与 NEO4J_PASSWORD（或用 --uri/--password）")
        return 1

    # ---- 1) 装载 ----
    if not a.no_load:
        import importlib.util
        spec = importlib.util.spec_from_file_location("bng", APP / "scripts" / "build_neo4j_graph.py")
        bng = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bng)                      # type: ignore[union-attr]
        src = bng.load_source()
        print("源数据：", {k: len(v) for k, v in src.items()})
        print("清空旧图：", bng.wipe(a.uri, a.user, a.password, a.database), "个节点/关系")
        counts = bng.load(src, a.uri, a.user, a.password, a.database)
        node_rel = {k: v for k, v in counts.items()
                    if k != "_elapsed_sec" and not k.endswith("_verify")
                    and k not in ("_shortfall",)}
        print("装载完成：", node_rel, "| 耗时 %ss" % counts.get("_elapsed_sec"))
        short = counts.get("_shortfall") or {}
        if short:
            print("⚠ 装载后校验发现关系短缺（有 MATCH 未命中，关系被静默丢弃）：", short)
        else:
            print("装载后校验：5 类关系的实际数量与去重后期望值完全一致 ✓")

    # ---- 2) 两套实现 ----
    os.environ["NEO4J_URI"] = a.uri
    os.environ["NEO4J_USER"] = a.user
    os.environ["NEO4J_PASSWORD"] = a.password
    os.environ["NEO4J_DATABASE"] = a.database

    from integrations.neo4j_repo import Neo4jGraphRepository
    from integrations.repos import SQLiteGraphRepository

    try:
        ng = Neo4jGraphRepository()
    except Exception as e:
        print("连接 Neo4j 失败：%s: %s" % (type(e).__name__, e))
        return 1
    sg = SQLiteGraphRepository()
    print("Neo4j 版：", type(ng).__name__, "| SQLite 版：", type(sg).__name__)
    print()

    rows: list[tuple[str, str, str, str, bool]] = []

    def cmp(sc: str, what: str, nv, sv, ok: bool | None = None) -> None:
        good = (nv == sv) if ok is None else ok
        rows.append((sc, what, _brief(nv), _brief(sv), good))

    # S1 长期合作（可比项：有中标合作的供应商集合与次数；产品供应商为信息项）
    n1, s1 = ng.scenario1(PURCHASER, 1, 50), sg.scenario1(PURCHASER, 1, 50)
    nw = sorted((x.name, x.cooperate_count) for x in n1.suppliers if x.cooperate_count > 0)
    sw = sorted((x.name, x.cooperate_count) for x in s1.suppliers if x.cooperate_count > 0)
    cmp("S1", "中标合作供应商(名+次数)", nw, sw)
    cmp("S1", "合作总金额", n1.summary.get("total_amount"), s1.summary.get("total_amount"))
    cmp("S1", "产品供应商数(信息项,口径不同)", n1.summary.get("n_product_suppliers"),
        s1.summary.get("n_product_suppliers"), ok=True)

    # S2 投标画像
    n2, s2 = ng.scenario2(PURCHASER, 5), sg.scenario2(PURCHASER, 5)
    cmp("S2", "TOP5 主体(名+参与+中标)",
        sorted((x.name, x.bid_count, x.win_count) for x in n2.top_bidders),
        sorted((x.name, x.bid_count, x.win_count) for x in s2.top_bidders))
    cmp("S2", "协同组合(成员+次数)",
        sorted((tuple(x.members), x.count) for x in n2.co_bid_pairs),
        sorted((tuple(x.members), x.count) for x in s2.co_bid_pairs))

    # S3 竞对分析
    n3, s3 = ng.scenario3(SUPPLIER, 5), sg.scenario3(SUPPLIER, 5)
    cmp("S3", "全部共同竞标主体数", len(n3.all_bidders), len(s3.all_bidders))
    cmp("S3", "TOP5 竞对名", sorted(x.name for x in n3.top_competitors),
        sorted(x.name for x in s3.top_competitors))
    cmp("S3", "中标次数", n3.summary.get("win_notices"), s3.summary.get("win_notices"))

    # S4 共同客户
    n4, s4 = ng.scenario4(PAIR, 1, 20), sg.scenario4(PAIR, 1, 20)
    cmp("S4", "均有合作的采购单位(名+次数)",
        sorted((x.name, x.cooperate_count) for x in n4.purchasers),
        sorted((x.name, x.cooperate_count) for x in s4.purchasers))

    # S5 共同项目
    n5, s5 = ng.scenario5(PAIR, 50), sg.scenario5(PAIR, 50)
    cmp("S5", "共同项目 doc_id 集合", sorted(x.doc_id for x in n5.projects),
        sorted(x.doc_id for x in s5.projects))
    cmp("S5", "项目总金额", n5.summary.get("total_amount"), s5.summary.get("total_amount"))

    print("%-4s %-30s %-34s %-34s %s" % ("场景", "比对项", "Neo4j", "SQLite", "判定"))
    print("-" * 118)
    bad = 0
    for sc, what, nv, sv, good in rows:
        if not good:
            bad += 1
        print("%-4s %-30s %-34s %-34s %s" % (sc, what, nv, sv, "一致" if good else "**不一致**"))
    print("-" * 118)
    print("可比项 %d 项，一致 %d 项，不一致 %d 项" % (len(rows), len(rows) - bad, bad))
    if bad:
        print("存在差异：请检查上面标记的行（S1 产品供应商为口径差异，属预期；其余不一致需修复）")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
