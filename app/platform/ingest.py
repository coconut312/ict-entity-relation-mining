"""任务三 · 数据入库：把任务一/任务二的交付结果装入 SQLite 服务库。

赛题「处理速率」口径为：**从数据集导入完成时刻起算，至全部提取结果写入数据库止的总耗时/篇数**。
本脚本正是那个「写入数据库」的落地环节，并输出耗时与吞吐，作为速率口径的实测依据。

产物：`out/platform/platform.db`
  t1_item / t1_notice        任务一交付表（标的物明细 / 公告级）
  t1_bidder                  投标人名单（参与人）：开标一览表/评审结果/资格审查/未中标原因表逐行
  entity / bid / g_notice    任务二关系模型（实体、参与投标事实、公告图节点）
  cooperation / cobidding / product_supply / purchaser_product_supplier
  supplier_portrait / purchaser_portrait / supplier_purchaser / purchaser_supplier
  supplier_category / purchaser_category
  _meta                      入库元信息（时间、行数、耗时、派生表来源）

**数据集派生的表每次重建都是"全新库"**：`ingest()` 永远写一个**唯一命名的临时库**，
写完后 `os.replace()` 原子替换正式库 —— 因此本次数据集没有来源的关系/画像表
在库里**就是不存在**（=空关系），「上传什么就只有什么」，不会把上一份数据的关系
结果留给场景分析。表名与列结构保持不变（平台与下载端点读的就是这些名字）。

用法: uv run python platform/ingest.py [--db out/platform/platform.db]
"""
from __future__ import annotations

import csv
import os
import sqlite3
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

# 标的行噪音过滤（**唯一实现** = `ict_parse/noise.py`）：入库是最后一道闸，
# 上传路径与全量交付路径都过这一处，平台上「标的检索」里不会出现行业分类词/表标签行。
from ict_parse.noise import (NoiseReport, filter_items,  # noqa: E402
                             enabled as noise_enabled)

DELIV = os.path.join(APP, "deliverables")
GRAPH = os.path.join(APP, "out", "task2", "graph.db")

# 任务二视图 -> 服务表（直接物化，避免服务期反复 JOIN）
GRAPH_VIEWS = {
    "cooperation": "v_cooperation",
    "cobidding": "v_cobidding",
    "product_supply": "v_product_supply",
    "purchaser_product_supplier": "v_purchaser_product_supplier",
    "supplier_portrait": "v_supplier_portrait",
    "purchaser_portrait": "v_purchaser_portrait",
    "supplier_purchaser": "v_supplier_purchaser",
    "purchaser_supplier": "v_purchaser_supplier",
    "supplier_category": "v_supplier_category",
    "purchaser_category": "v_purchaser_category",
}
CSV_TABLES = {
    "t1_item": "items_task1.csv",
    "t1_notice": "announcements_task1.csv",
    # 投标人名单（参与人）—— 官方「投标人表」里除中标方之外的行的来源。
    # 来源文件由上传管线写出（`integrations/pipeline_impl.py::_write_tables`）：
    # 从附件/正文的《开标一览表》《报价明细》《评审结果》《资格审查名单》
    # 《未中标（成交）原因表》里逐行抽出（判据见 `ict_parse/bidders.py`）。
    # 数据集里没有这份文件时 = 本次没有名单来源 -> 该表不存在（空关系），
    # 官方三表退回「只有中标方」的旧口径，不会把上一份数据的参与人留下来。
    "t1_bidder": "bidder_roster.csv",
}

# graph.db 缺失时用于重建关系表的交付表（文件名 -> 目标表名与列一一对应）
GRAPH_CSV = {
    "entity": "task2_entities.csv",
    "cooperation": "task2_cooperation.csv",
    "cobidding": "task2_cobidding.csv",
    "product_supply": "task2_product_supply.csv",
    "purchaser_product_supplier": "task2_purchaser_product_supplier.csv",
    "supplier_portrait": "task2_supplier_portrait.csv",
    "purchaser_portrait": "task2_purchaser_portrait.csv",
    "supplier_purchaser": "task2_supplier_purchaser.csv",
    "purchaser_supplier": "task2_purchaser_supplier.csv",
    "supplier_category": "task2_supplier_category.csv",
    "purchaser_category": "task2_purchaser_category.csv",
}
# 任务二「数据集派生表」全集 = 实体/参与投标/公告节点 + 全部关系与画像表。
# 用途（**只此一处**）：入库结束后核出「本次数据集没有来源的派生表」，写进
# `_meta.derived_empty` 留痕 —— 这些表就是空关系（表不存在），不是漏写。
#
# 历史（已修正的死代码）：改造前还有一个 `_clear_derived()` 在写库之前逐表
# `DROP TABLE`，注释说「必须先清空上一份数据的关系表」。但 `ingest()` 每次都在
# **全新创建的临时库**上重建（见 `ingest()` 里唯一命名的 `tmp`），临时库里本来
# 一张表都没有，`_clear_derived()` 永远返回空列表 —— `_meta.derived_dropped`
# 恒为 0 就是它从没生效过的证据。既然「是否存在上一份数据的关系表」这件事
# 在临时库方案下根本不存在，这个函数与那次调用已删除（保留会让后来人误以为
# 有一步"清理"在起作用，从而不敢改临时库方案）。
DERIVED_TABLES = tuple(dict.fromkeys(("entity", "bid", "g_notice", "t1_bidder")
                                     + tuple(GRAPH_VIEWS) + tuple(GRAPH_CSV)))
INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_item_name ON t1_item(item_name)",
    "CREATE INDEX IF NOT EXISTS ix_item_sup ON t1_item(supplier_name)",
    "CREATE INDEX IF NOT EXISTS ix_item_cat ON t1_item(category)",
    "CREATE INDEX IF NOT EXISTS ix_item_brand ON t1_item(brand)",
    "CREATE INDEX IF NOT EXISTS ix_item_spec ON t1_item(spec)",
    "CREATE INDEX IF NOT EXISTS ix_item_aid ON t1_item(announcement_id)",
    "CREATE INDEX IF NOT EXISTS ix_bid_ent ON bid(entity_id)",
    "CREATE INDEX IF NOT EXISTS ix_bid_notice ON bid(notice_id)",
    "CREATE INDEX IF NOT EXISTS ix_bidder_aid ON t1_bidder(announcement_id)",
]


def _read_csv(path: str) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        return list(rd.fieldnames or []), list(rd)


def _filter_item_rows(rows: list[dict], ann_ctx: dict[str, tuple[str, str]],
                      rep: NoiseReport) -> tuple[list[dict], dict[str, int], set[str]]:
    """入库前过滤标的行噪音 -> `(保留行, {公告号: 保留条数}, 出现过的公告号集合)`。

    逐行喂 `ict_parse/noise.filter_items`（判据/词表/开关都在那里，本函数不复制规则）。
    公告号 -> (项目名, 采购单位) 由 `t1_notice` 交付表提供（R6「与标题同名」判据用）。
    第三个返回值用于回填 `n_items`：某篇的标的行**全被判掉**时必须写 0，
    而不是沿用交付表里过滤前的旧条数。
    """
    keep: list[dict] = []
    kept_by_aid: dict[str, int] = {}
    seen_aids: set[str] = set()
    for r in rows:
        aid = (r.get("announcement_id") or "").strip()
        seen_aids.add(aid)
        proj, pur = ann_ctx.get(aid, ("", ""))
        if filter_items([r], project_name=proj, purchaser=pur, report=rep, aid=aid):
            keep.append(r)
            kept_by_aid[aid] = kept_by_aid.get(aid, 0) + 1
    return keep, kept_by_aid, seen_aids


def _looks_numeric(col: str, rows: list[dict], limit: int = 500) -> bool:
    """该列是否**整列都是数值**（空值不算）—— 用于还原列亲和性。

    CSV 里所有单元格都是文本；若不还原数值列的类型，`coop_times >= 1` 会变成
    文本比较（`'94800' < '1'` 之类的怪序），S1–S5 的**排序与阈值**就会与
    `graph.db` 路径不一致。判据只看带样本的有限行，非空值必须全部可解析为数字。
    """
    seen = 0
    for r in rows:
        v = r.get(col)
        if v is None or str(v).strip() == "":
            continue
        seen += 1
        if seen > limit:
            break
        try:
            float(str(v).strip().replace(",", ""))
        except ValueError:
            return False
    return seen > 0


def _num_cell(v):
    """数值列单元格 -> int/float（保留全精度；空值保持空）。"""
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        f = float(s.replace(",", ""))
    except ValueError:
        return v
    return int(f) if abs(f - round(f)) < 1e-9 else f


def _graph_from_csv(con: sqlite3.Connection, deliv: str, counts: dict) -> None:
    """从交付表 CSV 重建任务二的关系表（graph.db 不可用时的回退路径）。

    交付表 `task2_*.csv` 就是图库视图的物化结果，列名与平台查询一致，
    因此可以直接装载；另需两份特殊处理：
      * `g_notice` 由 `announcements_task1.csv` 映射（announcement_id -> notice_id）
      * `bid`      由 `task2_bids.csv` + `task2_suppliers.csv` 的名称->entity_id 映射补齐

    本次数据集里没有对应 CSV 的表**在这条路径上不会被创建**（=空关系，平台查询
    按空结果处理），绝不会留下上一份数据的关系行 —— 因为整个库都是新建的临时库。
    """
    loaded: dict[str, list[str]] = {}
    missing: list[str] = []
    for table, fname in GRAPH_CSV.items():
        p = os.path.join(deliv, fname)
        if not os.path.exists(p):
            # 本次数据集没有这张表的来源 -> 表已被清空且不再重建（=空关系）
            missing.append(table)
            continue
        cols, rows = _read_csv(p)
        # 列亲和性还原：数值列声明为 REAL（与 graph.db 路径的 int/float 存储对齐），
        # 文本列保持无类型（原样存文本）—— 否则阈值/排序会走文本比较。
        num_cols = {c for c in cols if _looks_numeric(c, rows)}
        decl = ",".join('"%s"%s' % (c, " REAL" if c in num_cols else "") for c in cols)
        con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute(f"CREATE TABLE IF NOT EXISTS {table} ({decl})")
        con.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * len(cols))})",
                        [[_num_cell(r.get(c)) if c in num_cols else r.get(c) for c in cols]
                         for r in rows])
        counts[table] = len(rows)
        loaded[table] = cols

    # 供应商名 -> entity_id
    ent: dict[str, str] = {}
    p = os.path.join(deliv, "task2_suppliers.csv")
    if os.path.exists(p):
        _, rows = _read_csv(p)
        for r in rows:
            if r.get("supplier"):
                ent.setdefault(r["supplier"], r.get("entity_id") or r["supplier"])

    # 公告节点
    p = os.path.join(deliv, "announcements_task1.csv")
    if os.path.exists(p):
        _, rows = _read_csv(p)
        gcols = ["notice_id", "project_name", "purchaser_id", "purchaser", "region",
                 "publish_time", "project_amount", "notice_type", "pricing_mode",
                 "procurement_mode", "n_winners", "n_bidders"]
        con.execute("DROP TABLE IF EXISTS g_notice")
        con.execute("CREATE TABLE IF NOT EXISTS g_notice (%s)" % ",".join(
            '"%s"%s' % (c, " REAL" if c == "project_amount" else "") for c in gcols))
        con.executemany("INSERT INTO g_notice VALUES (%s)" % ",".join("?" * len(gcols)),
                        [[r.get("announcement_id"), r.get("project_name"), None, r.get("purchaser"),
                          r.get("region"), r.get("publish_time"),
                          _num_cell(r.get("project_amount")),
                          r.get("notice_type"), r.get("pricing_mode"), r.get("procurement_mode"),
                          _num_cell(r.get("n_winners")), _num_cell(r.get("n_bidders"))]
                         for r in rows])
        counts["g_notice"] = len(rows)

    # 参与投标事实
    p = os.path.join(deliv, "task2_bids.csv")
    if os.path.exists(p):
        _, rows = _read_csv(p)
        bcols = ["notice_id", "entity_id", "supplier", "is_winner", "amount", "amount_src"]
        con.execute("DROP TABLE IF EXISTS bid")
        con.execute("CREATE TABLE IF NOT EXISTS bid (%s)" % ",".join(
            '"%s"%s' % (c, " REAL" if c in ("is_winner", "amount") else "") for c in bcols))
        con.executemany("INSERT INTO bid VALUES (%s)" % ",".join("?" * len(bcols)),
                        [[r.get("notice_id"), ent.get(r.get("supplier") or "") or r.get("supplier"),
                          r.get("supplier"), _num_cell(r.get("is_winner")),
                          _num_cell(r.get("amount")), None]
                         for r in rows])
        counts["bid"] = len(rows)

    return {"from_csv": sorted(loaded), "missing": sorted(missing)}


def ingest(db_path: str, deliv_dir: str | None = None, graph_db: str | None = None) -> dict:
    """把交付表与关系图库写入服务库。

    `deliv_dir` / `graph_db` 可覆盖默认来源 —— 供任务三平台的**上传导入**
    （`app/task3/backend/integrations/pipeline_impl.py`）把用户上传的数据集入库使用。

    `graph_db` 的三种取值（**关系表来源**的开关，必须在注释里说清楚）：
      * `None`           未指定 -> 用仓库默认的 `out/task2/graph.db`（CLI 行为不变）；
      * 非空路径          用该图库重建关系表；
      * **空字符串 `""`**  明确表示「本次数据集没有关系表来源」-> 不走 graph.db，
        关系/画像表按"本次无来源"处理（保持空关系），不沿用上一份数据的关系结果。
    """
    deliv = deliv_dir or DELIV
    use_graph_db = True
    if graph_db is None:
        graph = GRAPH
    elif str(graph_db).strip() == "":
        graph, use_graph_db = "", False
    else:
        graph = graph_db
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # ---- 临时库名必须**每次唯一**（P1-1 的根因之一）----
    # 改造前固定用 `db_path + '.tmp'`：两个 ingest 并发时共用一个临时库，
    #   A: DROP/CREATE t1_item → B: DROP t1_item → A: CREATE t1_item（已存在 -> 报错）
    #   → 一方 `os.replace` 把另一方正在写的文件换走（PermissionError）
    #   → 最终落地的库缺表（实测 `no such table: t1_notice`，整库报废）。
    # 唯一后缀（pid + uuid）让并发写各自写各自的文件；真正落地仍是**原子** os.replace。
    tmp = f'{db_path}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}'
    t0 = time.time()
    counts: dict[str, int] = {}
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(tmp)
        con.execute("PRAGMA journal_mode=OFF")

        # 1) 任务一交付表 -> t1_item / t1_notice（全字段检索的数据底座）
        #    先 DROP 再 CREATE：本函数永远在**空的新库**上跑，DROP 只是让
        #    "同一函数内重复装载" 也安全（幂等）；CREATE 再加 IF NOT EXISTS 兜底。
        #
        #    t1_item 在**写入前**过一遍标的行噪音过滤（`ict_parse/noise.py`，唯一实现）：
        #    上传路径（`pipeline_impl._write_tables` 写 items_task1.csv -> 本函数）同样会
        #    带上《中小企业声明函》的行业分类词（`工业`/`建筑业`/`农林牧渔`）与表格行标签
        #    （`总价:`/`投标人名称`/`总下浮率：（…）`/`……`）——这些行在平台上会被当成
        #    产品搜出来（用户实测截图）。过滤后**回填 t1_notice.n_items**，让「公告的标的
        #    条数」与库里的真实标的行一致；对已过滤过的交付表是幂等的。
        ann_ctx: dict[str, tuple[str, str]] = {}
        _ann_csv = os.path.join(deliv, CSV_TABLES.get("t1_notice", ""))
        if os.path.exists(_ann_csv):
            with open(_ann_csv, encoding="utf-8-sig", newline="") as f:
                for _r in csv.DictReader(f):
                    ann_ctx[(_r.get("announcement_id") or "").strip()] = (
                        _r.get("project_name") or "", _r.get("purchaser") or "")
        noise_rep = NoiseReport()
        kept_by_aid: dict[str, int] = {}
        item_aids: set[str] = set()
        for table, fname in CSV_TABLES.items():
            p = os.path.join(deliv, fname)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8-sig", newline="") as f:
                rd = csv.DictReader(f)
                cols = rd.fieldnames or []
                rows = list(rd)
            if table == "t1_item":
                rows, kept_by_aid, item_aids = _filter_item_rows(rows, ann_ctx, noise_rep)
            # 公告级 n_items 回填（**必须在 INSERT 之前**改行）：只在本轮真的过滤过
            # t1_item、且表里有 n_items 列时才改。逐篇写「保留条数」（全被判掉写 0），
            # 否则会留下"标的行 0 条但 n_items=13"的自相矛盾（交付表的 n_items 是过滤前写的）。
            if table == "t1_notice" and item_aids and "n_items" in cols:
                _ann = {(r.get("announcement_id") or "").strip(): r for r in rows}
                for _aid in item_aids:
                    if _aid in _ann:
                        _ann[_aid]["n_items"] = str(kept_by_aid.get(_aid, 0))
            con.execute(f"DROP TABLE IF EXISTS {table}")
            con.execute(
                f"CREATE TABLE IF NOT EXISTS {table} "
                f"({','.join(chr(34) + c + chr(34) + ' TEXT' for c in cols)})")
            ph = ",".join("?" * len(cols))
            con.executemany(f"INSERT INTO {table} VALUES ({ph})",
                            [[r.get(c) for c in cols] for r in rows])
            counts[table] = len(rows)

        # 2) 任务二关系模型：实体 / 参与投标 / 公告节点 + 关系视图物化
        derived_src = {"mode": "", "from_csv": [], "missing": []}
        if use_graph_db and os.path.exists(graph):
            # 用第二个连接读 graph.db 再写入（ATTACH 在部分 sqlite3 版本上会静默失败）
            derived_src["mode"] = "graph.db"
            g = sqlite3.connect(graph)
            for t in ("entity", "bid", "notice"):
                dest = "g_notice" if t == "notice" else t
                cur = g.execute(f"SELECT * FROM {t}")
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
                con.execute(f"DROP TABLE IF EXISTS {dest}")
                con.execute(f"CREATE TABLE IF NOT EXISTS {dest} "
                            f"({','.join(chr(34) + c + chr(34) for c in cols)})")
                con.executemany(
                    f"INSERT INTO {dest} VALUES ({','.join('?' * len(cols))})", rows)
                counts[dest] = len(rows)
            for name, view in GRAPH_VIEWS.items():
                cur = g.execute(f"SELECT * FROM {view}")
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
                con.execute(f"DROP TABLE IF EXISTS {name}")
                con.execute(f"CREATE TABLE IF NOT EXISTS {name} "
                            f"({','.join(chr(34) + c + chr(34) for c in cols)})")
                con.executemany(
                    f"INSERT INTO {name} VALUES ({','.join('?' * len(cols))})", rows)
                counts[name] = len(rows)
            g.close()
        else:
            # graph.db 不存在（例如全新 clone：out/ 未入库）时，从**已入库的交付表 CSV**
            # 重建关系表，使平台仅依赖 deliverables/ 即可跑通五大场景 ——
            # 既不需要赛方原始数据，也不需要重新调用大模型。
            derived_src["mode"] = "deliverables-csv"
            info = _graph_from_csv(con, deliv, counts)
            derived_src["from_csv"] = info["from_csv"]
            derived_src["missing"] = info["missing"]

        for sql in INDEXES:
            try:
                con.execute(sql)
            except sqlite3.OperationalError:
                pass
        con.execute("DROP TABLE IF EXISTS _meta")
        con.execute("CREATE TABLE _meta(k TEXT PRIMARY KEY, v TEXT)")
        con.commit()
        el = time.time() - t0
        # 本次数据集**没有来源**的派生表：明确留痕（这些表就是空关系，不是漏写）
        no_source = [t for t in DERIVED_TABLES
                     if not con.execute(
                         "SELECT 1 FROM sqlite_master WHERE name=? AND type='table'",
                         (t,)).fetchone()]
        con.executemany("INSERT INTO _meta VALUES (?,?)", [
            ("built_at", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("elapsed_sec", f"{el:.2f}"),
            ("pages", str(counts.get("t1_notice", 0))),
            ("total_rows", str(sum(counts.values()))),
            ("per_page_sec", f"{el / max(1, counts.get('t1_notice', 1)):.4f}"),
            ("derived_source", derived_src["mode"]),
            ("derived_empty", ",".join(no_source)),
            # 标的行噪音过滤（唯一实现 = ict_parse/noise.py）：开关状态 + 丢了什么、丢了多少
            ("noise_filter", "on" if noise_enabled() else "off"),
            ("noise_input", str(noise_rep.total_in)),
            ("noise_dropped", str(noise_rep.dropped)),
            ("noise_rules", ",".join("%s=%d" % (k, v) for k, v in noise_rep.hits.items() if v)
             or ""),
        ])
        con.commit()
        con.close()
        con = None
    except BaseException:
        # 任何一步失败都**不能**留下半成品临时库（否则下次排查会被它误导），
        # 也不能改动正式库 —— 正式库只在下面 `os.replace()` 那一刻才被替换。
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass
        for leftover in (tmp, f'{tmp}-journal', f'{tmp}-wal', f'{tmp}-shm'):
            try:
                os.remove(leftover)
            except OSError:
                pass
        raise

    # ---- 原子落地（目标是正式库）----
    # 改造前这里在「删掉正式库之后、第二次 os.replace 仍然失败」时**不清理临时库**，
    # 于是 `platform.db.tmp-<pid>-<rand>` 会一直躺在 out/platform/ 里（P2）：
    # 下次排查会被它误导成「上次的库」，逐步累积还会把磁盘占满。
    # 现在两种情况都清理：最终成功 -> 临时库已被 replace 走（不存在，unlink 无副作用）；
    # 最终失败 -> 显式删掉临时库及其 journal/wal/shm，正式库保持原样。
    def _drop_tmp() -> None:
        for leftover in (tmp, f'{tmp}-journal', f'{tmp}-wal', f'{tmp}-shm'):
            try:
                os.remove(leftover)
            except OSError:
                pass

    try:
        os.replace(tmp, db_path)
    except OSError:
        # Windows：目标文件可能仍被另一个连接占用。与改造前一致，先删再换；
        # 仍失败则抛错（正式库保持原样，临时库在这里被清理掉）。
        try:
            if os.path.exists(db_path):
                os.remove(db_path)
            os.replace(tmp, db_path)
        except BaseException:
            _drop_tmp()
            raise
    return {"counts": counts, "elapsed_sec": round(el, 2),
            "per_page_sec": round(el / max(1, counts.get("t1_notice", 1)), 4),
            "derived": {"source": derived_src["mode"], "empty_after": no_source},
            # 标的行噪音过滤结果（入库前判掉的行；`dropped=0` = 本数据集本来就干净）
            "noise": {"enabled": noise_enabled(), "input": noise_rep.total_in,
                      "dropped": noise_rep.dropped,
                      "hits": {k: v for k, v in noise_rep.hits.items() if v}}}


if __name__ == "__main__":
    import argparse
    import json

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(APP, "out", "platform", "platform.db"))
    a = ap.parse_args()
    r = ingest(a.db)
    print(json.dumps({"db": a.db, **r}, ensure_ascii=False, indent=2))
