"""任务二 · 五大业务场景查询（基于 out/task2/graph.db 的图检索）。

赛题五（二）任务二要求实现的 5 个场景：
  S1 指定采购单位 -> 长期/大量合作关系的中标供应商、**产品供应商**（合作次数、交易总金额）
  S2 指定采购单位 -> 高频(TOP5)参与投标主体 + 高频**协同投标主体组合**
  S3 指定中标供应商 -> 高频(TOP5)共同竞标主体 + 全部参与投标主体信息（频次、总金额）
  S4 指定 2 个及以上中标供应商 -> 与其**均有合作**关系的采购单位（合作频次、金额）
  S5 指定 2 个及以上中标供应商 -> 历史**共同参与竞标**的项目信息与竞标结果（项目数、总金额）

用法:
    uv run python scripts/task2_scenarios.py --demo            # 自动选题跑全 5 个场景并出报告
    uv run python scripts/task2_scenarios.py -s s1 -a "东莞职业技术学院"
    uv run python scripts/task2_scenarios.py -s s4 -a "A公司" -a "B公司"
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

DB = "out/task2/graph.db"
REPORT = "deliverables/task2_scenario_report.md"


def _con() -> sqlite3.Connection:
    if not os.path.exists(DB):
        raise SystemExit(f"缺少 {DB}，请先运行 scripts/build_task2_graph.py")
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


# ---------------- S1 ----------------
def s1_purchaser_suppliers(con, purchaser: str, top: int = 20) -> dict:
    """采购单位：长期/大量合作的中标供应商 + 产品供应商（品牌）。"""
    winners = con.execute("""
        SELECT supplier, coop_times, coop_amount, first_time, last_time
        FROM v_cooperation WHERE purchaser = ? ORDER BY coop_times DESC, coop_amount DESC LIMIT ?""",
        (purchaser, top)).fetchall()
    brands = con.execute("""
        SELECT i.brand AS product_supplier, COUNT(*) AS item_times,
               ROUND(SUM(i.total_price), 2) AS item_amount,
               COUNT(DISTINCT i.supplier) AS n_winners
        FROM item i JOIN notice n ON n.notice_id = i.notice_id
        WHERE n.purchaser = ? AND i.brand IS NOT NULL AND i.brand <> ''
        GROUP BY i.brand ORDER BY item_times DESC LIMIT ?""", (purchaser, top)).fetchall()
    summary = con.execute("""
        SELECT COUNT(*) n_notices, ROUND(SUM(project_amount),2) total_amount
        FROM notice WHERE purchaser = ?""", (purchaser,)).fetchone()
    return {"purchaser": purchaser, "summary": dict(summary),
            "winners": [dict(r) for r in winners], "product_suppliers": [dict(r) for r in brands]}


# ---------------- S2 ----------------
def s2_purchaser_top_bidders(con, purchaser: str, top: int = 5) -> dict:
    """采购单位：高频参与投标主体 TOP5 + 高频协同投标组合 TOP5。"""
    bidders = con.execute("""
        SELECT b.supplier,
               COUNT(*) AS participate_times,
               SUM(b.is_winner) AS win_times,
               SUM(1 - b.is_winner) AS bid_only_times,
               ROUND(SUM(CASE WHEN b.is_winner = 1 THEN b.amount ELSE 0 END), 2) AS won_amount
        FROM bid b JOIN notice n ON n.notice_id = b.notice_id
        WHERE n.purchaser = ? GROUP BY b.supplier
        ORDER BY participate_times DESC, win_times DESC LIMIT ?""", (purchaser, top)).fetchall()
    pairs = con.execute("""
        SELECT a.supplier AS supplier_a, c.supplier AS supplier_b,
               COUNT(DISTINCT a.notice_id) AS co_times
        FROM bid a JOIN bid c ON a.notice_id = c.notice_id AND a.supplier < c.supplier
        JOIN notice n ON n.notice_id = a.notice_id
        WHERE n.purchaser = ? GROUP BY a.supplier, c.supplier
        ORDER BY co_times DESC LIMIT ?""", (purchaser, top)).fetchall()
    return {"purchaser": purchaser, "top_bidders": [dict(r) for r in bidders],
            "top_pairs": [{k: r[k] for k in ("supplier_a", "supplier_b", "co_times")} for r in pairs]}


# ---------------- S3 ----------------
def s3_supplier_co_bidders(con, supplier: str, top: int = 5) -> dict:
    """中标供应商：高频共同竞标主体 TOP5 + 全部参与投标主体信息。"""
    co = con.execute("""
        SELECT c.supplier AS co_bidder,
               COUNT(DISTINCT a.notice_id) AS co_times,
               SUM(CASE WHEN c.is_winner = 1 THEN 1 ELSE 0 END) AS co_wins,
               ROUND(SUM(CASE WHEN c.is_winner = 1 THEN c.amount ELSE 0 END), 2) AS co_won_amount
        FROM bid a JOIN bid c ON a.notice_id = c.notice_id AND a.supplier <> c.supplier
        WHERE a.supplier = ? GROUP BY c.supplier
        ORDER BY co_times DESC, co_wins DESC LIMIT ?""", (supplier, top)).fetchall()
    allco = con.execute("""
        SELECT c.supplier AS co_bidder, COUNT(DISTINCT a.notice_id) AS co_times,
               SUM(CASE WHEN c.is_winner = 1 THEN 1 ELSE 0 END) AS co_wins
        FROM bid a JOIN bid c ON a.notice_id = c.notice_id AND a.supplier <> c.supplier
        WHERE a.supplier = ? GROUP BY c.supplier ORDER BY co_times DESC""", (supplier,)).fetchall()
    me = con.execute("SELECT * FROM v_supplier_profile WHERE supplier = ?", (supplier,)).fetchone()
    return {"supplier": supplier, "profile": dict(me) if me else {},
            "top_co_bidders": [dict(r) for r in co], "all_co_bidders": [dict(r) for r in allco]}


# ---------------- S4 ----------------
def s4_common_purchasers(con, suppliers: list[str], top: int = 20) -> dict:
    """多个中标供应商：与其**均有**合作关系的采购单位（合作频次、金额）。"""
    ph = ",".join("?" * len(suppliers))
    rows = con.execute(f"""
        SELECT n.purchaser,
               COUNT(DISTINCT b.supplier) AS n_suppliers,
               COUNT(DISTINCT n.notice_id) AS coop_times,
               ROUND(SUM(b.amount), 2) AS coop_amount
        FROM bid b JOIN notice n ON n.notice_id = b.notice_id
        WHERE b.is_winner = 1 AND b.supplier IN ({ph})
          AND n.purchaser IS NOT NULL AND n.purchaser <> ''
        GROUP BY n.purchaser
        HAVING COUNT(DISTINCT b.supplier) = ?
        ORDER BY coop_times DESC, coop_amount DESC LIMIT ?""",
        (*suppliers, len(suppliers), top)).fetchall()
    return {"suppliers": suppliers, "purchasers": [dict(r) for r in rows]}


# ---------------- S5 ----------------
def s5_common_bid_projects(con, suppliers: list[str]) -> dict:
    """多个中标供应商：历史共同参与竞标的项目与竞标结果（项目数、项目总金额）。"""
    ph = ",".join("?" * len(suppliers))
    sub = " AND ".join([f"notice_id IN (SELECT notice_id FROM bid WHERE supplier = ?)"
                        for _ in suppliers])
    rows = con.execute(f"""
        SELECT n.notice_id, n.project_name, n.purchaser, n.region, n.project_amount,
               n.publish_time, n.n_winners, n.n_bidders
        FROM notice n WHERE {sub} ORDER BY n.publish_time""", tuple(suppliers)).fetchall()
    detail = []
    for r in rows:
        d = dict(r)
        d["results"] = [dict(x) for x in con.execute(
            f"SELECT supplier, is_winner, amount FROM bid WHERE notice_id = ? AND supplier IN ({ph})",
            (r["notice_id"], *suppliers)).fetchall()]
        detail.append(d)
    total = sum(float(r["project_amount"] or 0) for r in rows)
    return {"suppliers": suppliers, "n_projects": len(rows),
            "total_project_amount": round(total, 2), "projects": detail}


# ---------------- demo ----------------
def _md_table(rows: list[dict], cols: list[str], headers: list[str] | None = None) -> str:
    if not rows:
        return "_（无结果）_\n"
    hs = headers or cols
    out = ["| " + " | ".join(hs) + " |", "|" + "|".join(["---"] * len(hs)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join(out) + "\n"


def demo() -> int:
    con = _con()
    # S1/S2 选「重复合作最多」的采购单位：按项目数第一的可能每家只合作一次，
    # 体现不出「长期/大量合作关系」这一场景语义。
    row = con.execute("""
        SELECT purchaser, MAX(coop_times) AS mx, SUM(coop_times) AS tot, COUNT(*) AS n_sup
        FROM v_cooperation GROUP BY purchaser ORDER BY mx DESC, tot DESC LIMIT 1""").fetchone()
    top_purchaser = row["purchaser"] if row else con.execute(
        "SELECT purchaser FROM v_purchaser_profile ORDER BY n_notices DESC LIMIT 1").fetchone()[0]
    top_supplier = con.execute(
        "SELECT supplier FROM v_supplier_profile ORDER BY win_times DESC LIMIT 1").fetchone()[0]
    # 选一对「均有合作的采购单位最多」的中标供应商，作为 S4/S5 输入
    pair = con.execute("""
        SELECT a.supplier AS s1, c.supplier AS s2, COUNT(DISTINCT a.notice_id) AS co
        FROM bid a JOIN bid c ON a.notice_id=c.notice_id AND a.supplier<c.supplier
        WHERE a.is_winner=1 AND c.is_winner=1
        GROUP BY a.supplier, c.supplier ORDER BY co DESC LIMIT 1""").fetchone()
    pair = (pair[0], pair[1]) if pair else (top_supplier, top_supplier)

    L: list[str] = []
    L.append("# 任务二 · 五大业务场景查询结果（实跑报告）\n")
    L.append(f"> 数据底座：`out/task2/graph.db`（1038 篇公告 / "
             f"{con.execute('SELECT COUNT(*) FROM bid').fetchone()[0]} 条参与投标关系 / "
             f"{con.execute('SELECT COUNT(*) FROM v_purchaser_profile').fetchone()[0]} 个采购单位 / "
             f"{con.execute('SELECT COUNT(*) FROM v_supplier_profile').fetchone()[0]} 个供应商）\n")

    r1 = s1_purchaser_suppliers(con, top_purchaser)
    L.append(f"## S1 指定采购单位 → 长期合作的中标供应商与产品供应商\n\n"
             f"**输入采购单位**：`{top_purchaser}`（历史项目 {r1['summary'].get('n_notices')} 个，"
             f"总金额 {r1['summary'].get('total_amount')} 元）\n\n"
             f"### 中标供应商（按合作次数）\n\n"
             + _md_table(r1["winners"][:10], ["supplier", "coop_times", "coop_amount", "first_time", "last_time"],
                         ["中标供应商", "合作次数", "合作金额(元)", "首次合作", "最近合作"])
             + "\n### 产品供应商（品牌，按条目数）\n\n"
             + _md_table(r1["product_suppliers"][:10],
                         ["product_supplier", "item_times", "item_amount", "n_winners"],
                         ["产品供应商(品牌)", "供货条目", "供货金额(元)", "关联中标供应商数"]))

    r2 = s2_purchaser_top_bidders(con, top_purchaser)
    L.append(f"\n## S2 指定采购单位 → 高频参与投标主体 TOP5 与高频协同投标组合\n\n"
             f"**输入采购单位**：`{top_purchaser}`\n\n### TOP5 参与投标主体\n\n"
             + _md_table(r2["top_bidders"],
                         ["supplier", "participate_times", "win_times", "bid_only_times", "won_amount"],
                         ["主体", "参与投标次数", "中标次数", "仅投标(未中标)", "中标金额(元)"])
             + "\n### 高频协同投标组合 TOP5\n\n"
             + _md_table(r2["top_pairs"], ["supplier_a", "supplier_b", "co_times"],
                         ["主体A", "主体B", "共同投标次数"]))

    r3 = s3_supplier_co_bidders(con, top_supplier)
    L.append(f"\n## S3 指定中标供应商 → 高频共同竞标主体与全部参与投标主体\n\n"
             f"**输入中标供应商**：`{top_supplier}`"
             f"（中标 {r3['profile'].get('win_times')} 次 / 仅投标 {r3['profile'].get('bid_only_times')} 次）\n\n"
             f"### 高频共同竞标主体 TOP5\n\n"
             + _md_table(r3["top_co_bidders"], ["co_bidder", "co_times", "co_wins", "co_won_amount"],
                         ["共同竞标主体", "共同投标次数", "其中中标次数", "中标金额(元)"])
             + f"\n### 全部共同竞标主体（共 {len(r3['all_co_bidders'])} 家，列出前 20）\n\n"
             + _md_table(r3["all_co_bidders"][:20], ["co_bidder", "co_times", "co_wins"],
                         ["共同竞标主体", "共同投标次数", "其中中标次数"]))

    r4 = s4_common_purchasers(con, list(pair))
    L.append(f"\n## S4 指定两个中标供应商 → 与其均有合作关系的采购单位\n\n"
             f"**输入**：`{pair[0]}` 与 `{pair[1]}`\n\n"
             + _md_table(r4["purchasers"][:15], ["purchaser", "coop_times", "coop_amount", "n_suppliers"],
                         ["采购单位", "合作次数", "合作金额(元)", "涉及我方主体数"]))

    r5 = s5_common_bid_projects(con, list(pair))
    L.append(f"\n## S5 指定两个中标供应商 → 历史共同参与竞标的项目与竞标结果\n\n"
             f"**输入**：`{pair[0]}` 与 `{pair[1]}`\n\n"
             f"**汇总**：共同参与项目 **{r5['n_projects']}** 个，项目总金额 **{r5['total_project_amount']}** 元\n\n"
             + _md_table([{**p, "w1": next((x["is_winner"] for x in p["results"] if x["supplier"] == pair[0]), None),
                           "w2": next((x["is_winner"] for x in p["results"] if x["supplier"] == pair[1]), None)}
                          for p in r5["projects"][:15]],
                         ["notice_id", "project_name", "purchaser", "project_amount", "publish_time", "w1", "w2"],
                         ["公告ID", "项目名称", "采购单位", "项目金额(元)", "公告时间", "A是否中标", "B是否中标"]))

    os.makedirs("deliverables", exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"5 个场景均已跑通 -> {REPORT}")
    for name, r in (("S1", r1), ("S2", r2), ("S3", r3), ("S4", r4), ("S5", r5)):
        n = (len(r.get("winners") or []) or len(r.get("top_bidders") or [])
             or len(r.get("top_co_bidders") or []) or len(r.get("purchasers") or [])
             or r.get("n_projects", 0))
        print(f"   {name}: {n} 条结果")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--scenario", choices=["s1", "s2", "s3", "s4", "s5"], default=None)
    ap.add_argument("-a", "--arg", action="append", default=[])
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    if args.demo or not args.scenario:
        return demo()
    con = _con()
    s = args.scenario
    if s in ("s1", "s2"):
        r = s1_purchaser_suppliers(con, args.arg[0]) if s == "s1" else s2_purchaser_top_bidders(con, args.arg[0])
    elif s == "s3":
        r = s3_supplier_co_bidders(con, args.arg[0])
    elif s == "s4":
        r = s4_common_purchasers(con, args.arg)
    else:
        r = s5_common_bid_projects(con, args.arg)
    import json
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
