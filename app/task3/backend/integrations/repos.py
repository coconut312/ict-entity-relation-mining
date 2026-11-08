# -*- coding: utf-8 -*-
"""任务一 / 任务二 数据接入实现（《接口契约与工程整合》接入点 1 与 2）。

**只新增文件，不改动 `app/` 下任何既有代码** —— 遵守契约里的「只加不改」约定。

数据来源：任务一 / 任务二的交付服务库 `app/out/platform/platform.db`
（由 `app/platform/ingest.py` 生成，含 6206 条标的物明细、1038 篇公告、
  3402 个实体、3239 条参与投标事实，以及合作关系/共同竞标/画像等派生表）。

启用方式（见 `backend/run.py`，或自行设置环境变量）：
    set EDM_DATA_BACKEND=real
    set EDM_ITEM_REPOSITORY=integrations.repos:SQLiteItemRepository
    set EDM_GRAPH_REPOSITORY=integrations.repos:SQLiteGraphRepository
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.repositories import GraphRepository, ItemRepository
from app.schemas import (BidderStat, CoBidPair, DatasetStats, EntitySuggestItem,
                         FacetValue, ItemFacets, ItemFilter, ProjectRecord,
                         PurchaserStat, Scenario1Out, Scenario2Out, Scenario3Out,
                         Scenario4Out, Scenario5Out, SupplierStat)

# 交付库默认位置：app/out/platform/platform.db
# 本文件位于 app/task3/backend/integrations/repos.py -> parents[3] == app/
_DEFAULT_DB = Path(__file__).resolve().parents[3] / 'out' / 'platform' / 'platform.db'
DB_PATH = Path(os.getenv('EDM_SQLITE_PATH', str(_DEFAULT_DB)))

# 官方 7 字段（用于字段填充率统计）
CORE_FIELDS = ('product_name', 'category', 'brand', 'spec', 'unit_price',
               'quantity', 'total_price')
_EXTRA_FILL = ('supplier', 'purchaser', 'project_name')

# 公告时间形如 '2026年02月02日 11:15' -> '2026-02-02'
_DATE_EXPR = ("CASE WHEN {c} LIKE '____年__月__日%' "
              "THEN printf('%s-%s-%s', substr({c},1,4), substr({c},6,2), substr({c},9,2)) "
              "ELSE substr({c},1,10) END")
_DATE_RE = re.compile(r'(\d{4})年(\d{2})月(\d{2})日')


def _to_date(s: Any) -> Optional[str]:
    if not s:
        return None
    m = _DATE_RE.search(str(s))
    if m:
        return f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
    t = str(s)[:10]
    return t if re.match(r'\d{4}-\d{2}-\d{2}', t) else None


def _f(v: Any) -> Optional[float]:
    """转 float；空串/None/非数字 -> None（契约要求金额单位为元、缺值用 null）。"""
    if v is None:
        return None
    s = str(v).strip().replace(',', '')
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


class _Base:
    """共用的只读连接（URI 只读模式，避免整合过程中误写交付库）。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db = Path(db_path or DB_PATH)

    def _con(self) -> sqlite3.Connection:
        c = sqlite3.connect(f'file:{self.db.as_posix()}?mode=ro', uri=True)
        c.row_factory = sqlite3.Row
        return c

    def _sql(self, sql: str, args: Tuple = ()) -> List[sqlite3.Row]:
        c = self._con()
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()

    def _one(self, sql: str, args: Tuple = ()):
        r = self._sql(sql, args)
        return r[0] if r else None

    def _ok(self) -> bool:
        if not self.db.exists():
            return False
        try:
            return bool(self._one(
                "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name='t1_item'")) \
                and self._one('SELECT COUNT(*) n FROM t1_item')['n'] > 0
        except sqlite3.Error:
            return False


# ==================================================================
# 接入点 1：标的信息（任务一）
# ==================================================================

_SELECT_ITEM = f"""
SELECT i.ROWID AS rid, i.announcement_id, i.package_no, i.item_name, i.category, i.brand,
       i.spec, i.quantity, i.unit, i.unit_price, i.total_price, i.supplier_name,
       i.source, i.engine,
       n.project_name, n.project_amount, n.purchaser, n.region, n.publish_time,
       {_DATE_EXPR.format(c='n.publish_time')} AS announce_date
FROM t1_item i
LEFT JOIN t1_notice n ON n.announcement_id = i.announcement_id
"""

_SORT_MAP = {
    'doc_id': 'i.announcement_id', 'announce_date': 'announce_date',
    'product_name': 'i.item_name', 'category': 'i.category', 'brand': 'i.brand',
    'supplier': 'i.supplier_name', 'purchaser': 'n.purchaser',
    'unit_price': 'CAST(i.unit_price AS REAL)', 'quantity': 'CAST(i.quantity AS REAL)',
    'total_price': 'CAST(i.total_price AS REAL)', 'project_amount': 'CAST(n.project_amount AS REAL)',
}


class SQLiteItemRepository(_Base, ItemRepository):
    """基于 SQLite（交付服务库）的标的信息仓储。

    全部筛选都下推到 SQL 并走索引（`t1_item` 上建有 item_name/supplier_name/category/brand/spec
    与 announcement_id 索引），**不在 Python 侧全表扫描**，满足「单次查询 <1 秒」。
    """

    # ---- 概览 ----
    def has_data(self) -> bool:
        return self._ok()

    def stats(self) -> DatasetStats:
        if not self._ok():
            return DatasetStats()
        n_items = self._one('SELECT COUNT(*) n FROM t1_item')['n']
        n_docs = self._one('SELECT COUNT(*) n FROM t1_notice')['n']
        r = self._one("""SELECT COUNT(DISTINCT supplier_name) s FROM t1_item
                         WHERE IFNULL(supplier_name,'')<>''""")
        n_sup = r['s'] if r else 0
        r = self._one("""SELECT COUNT(DISTINCT purchaser) p FROM t1_notice
                         WHERE IFNULL(purchaser,'')<>''""")
        n_pur = r['p'] if r else 0
        r = self._one("""SELECT COUNT(DISTINCT brand) b FROM t1_item
                         WHERE IFNULL(brand,'')<>''""")
        n_brand = r['b'] if r else 0
        # 项目总金额：按公告去重求和（避免多包明细重复计入）
        r = self._one("""SELECT ROUND(SUM(CAST(project_amount AS REAL)),2) a,
                                MIN(""" + _DATE_EXPR.format(c='publish_time') + """) d0,
                                MAX(""" + _DATE_EXPR.format(c='publish_time') + """) d1
                         FROM t1_notice""")
        # 字段填充率
        fill: Dict[str, float] = {}
        pairs = [('product_name', 'item_name'), ('category', 'category'), ('brand', 'brand'),
                 ('spec', 'spec'), ('unit_price', 'unit_price'), ('quantity', 'quantity'),
                 ('total_price', 'total_price'), ('supplier', 'supplier_name')]
        for label, col in pairs:
            n = self._one(f"""SELECT SUM(CASE WHEN IFNULL("{col}",'')<>'' THEN 1 ELSE 0 END) k,
                                     COUNT(*) t FROM t1_item""")
            fill[label] = round((n['k'] or 0) / n['t'], 4) if n and n['t'] else 0.0
        # 采购单位填充率（按公告）
        n = self._one("""SELECT SUM(CASE WHEN IFNULL(purchaser,'')<>'' THEN 1 ELSE 0 END) k,
                                COUNT(*) t FROM t1_notice""")
        fill['purchaser'] = round((n['k'] or 0) / n['t'], 4) if n and n['t'] else 0.0
        return DatasetStats(
            n_docs=n_docs, n_items=n_items, n_suppliers=n_sup, n_purchasers=n_pur,
            n_brands=n_brand, total_amount=float(r['a'] or 0.0),
            date_min=r['d0'], date_max=r['d1'],
            avg_process_ms=self._measured_avg_ms(),
            field_fill_rate=fill)

    def _measured_avg_ms(self) -> float:
        """「单篇耗时」用**实测值**，不再写死常量。

        优先级：① 最近一次上传 job 的实测（elapsed_ms / 已处理篇数）；
                ② LLM 调用日志的 p50 延迟（真实单篇抽取耗时）；
                ③ `_meta.per_page_sec`（入库实测，×1000）；
                ④ 环境变量常量（最后兜底）。
        这样页面上的数字与实际处理速度同步，而不是永远显示 1200。
        """
        # ① 最近一次导入 job（services 里维护的内存态）
        try:
            from app import services as _svc          # noqa: PLC0415
            # 取**最近一次**已完成导入的实测均值（不是历史最大值）：
            # 上传页那行「35.0s · 单篇 1750 ms」用的就是本次 job 的 elapsed/docs，
            # 这里必须与它同源，否则卡片会停留在历史上最慢的那次。
            best, best_key = None, None
            for job in list(getattr(_svc.dataset_service, '_jobs', {}).values()):
                el, n = getattr(job, 'elapsed_ms', None), getattr(job, 'processed_docs', None)
                if not el or not n:
                    continue
                key = str(getattr(job, 'started_at', '') or getattr(job, 'job_id', ''))
                if best_key is None or key >= best_key:
                    best_key, best = key, float(el) / float(n)
            if best:
                return round(best, 1)
        except Exception:                             # noqa: BLE001
            pass
        # ② LLM 调用日志 p50
        try:
            from ict_parse.llm_log import summarize   # noqa: PLC0415
            st = summarize()
            p50s = [v.get('latency_ms_p50') for v in (st.get('by_model') or {}).values()
                    if v.get('latency_ms_p50')]
            if p50s:
                return round(float(sum(p50s)) / len(p50s), 1)
        except Exception:                             # noqa: BLE001
            pass
        # ③ 入库实测（每页秒 → 毫秒）
        try:
            row = self._one("SELECT value FROM _meta WHERE key='per_page_sec'")
            if row and row['value']:
                v = float(row['value']) * 1000.0
                if v > 0:
                    return round(v, 1)
        except Exception:                             # noqa: BLE001
            pass
        # ④ 兜底：环境变量常量
        return float(os.getenv('EDM_AVG_PROCESS_MS', '1200'))

    # ---- 检索 ----
    def _where(self, flt: ItemFilter) -> Tuple[str, List[Any]]:
        w: List[str] = []
        a: List[Any] = []
        if flt.keyword:
            k = f"%{flt.keyword.strip()}%"
            w.append("(IFNULL(i.item_name,'') LIKE ? OR IFNULL(i.brand,'') LIKE ? "
                     "OR IFNULL(i.spec,'') LIKE ? OR IFNULL(i.category,'') LIKE ?)")
            a += [k, k, k, k]
        for col, val in (('i.brand', flt.brand), ('i.category', flt.category),
                         ('i.supplier_name', flt.supplier), ('n.purchaser', flt.purchaser)):
            if val:
                w.append(f"IFNULL({col},'') LIKE ?")
                a.append(f"%{val.strip()}%")
        d = _DATE_EXPR.format(c='n.publish_time')
        if flt.date_from:
            w.append(f'{d} >= ?')
            a.append(flt.date_from)
        if flt.date_to:
            w.append(f'{d} <= ?')
            a.append(flt.date_to)
        if flt.price_min is not None:
            w.append('CAST(i.total_price AS REAL) >= ?')
            a.append(flt.price_min)
        if flt.price_max is not None:
            w.append('CAST(i.total_price AS REAL) <= ?')
            a.append(flt.price_max)
        return (' WHERE ' + ' AND '.join(w)) if w else '', a

    def search(self, flt: ItemFilter, page: int, page_size: int
               ) -> Tuple[List[Dict[str, Any]], int]:
        if not self._ok():
            return [], 0
        where, args = self._where(flt)
        base = ("FROM t1_item i LEFT JOIN t1_notice n ON n.announcement_id = i.announcement_id"
                + where)
        total = self._one('SELECT COUNT(*) n ' + base, tuple(args))['n']
        col = _SORT_MAP.get(flt.sort_by or 'doc_id', 'i.announcement_id')
        order = 'DESC' if str(flt.sort_order).lower() == 'desc' else 'ASC'
        rows = self._sql(f'{_SELECT_ITEM}{where} ORDER BY {col} {order}, i.ROWID ASC LIMIT ? OFFSET ?',
                         tuple(args) + (page_size, (page - 1) * page_size))
        return [self._to_item(r) for r in rows], total

    @staticmethod
    def _to_item(r: sqlite3.Row) -> Dict[str, Any]:
        return {
            # ---- 官方 7 字段 ----
            'product_name': r['item_name'] or '',
            'category': r['category'] or '',
            'brand': r['brand'] or '',
            'spec': r['spec'] or '',
            'unit_price': _f(r['unit_price']),
            'quantity': _f(r['quantity']),
            'total_price': _f(r['total_price']),
            # ---- 扩展字段 ----
            'id': f"{r['announcement_id']}#{r['package_no'] or ''}#{r['rid']}",
            'doc_id': r['announcement_id'] or '',
            'project_name': r['project_name'] or '',
            'project_amount': _f(r['project_amount']),
            'purchaser': r['purchaser'] or '',
            'supplier': r['supplier_name'] or '',
            'unit': r['unit'] or '',
            'announce_date': r['announce_date'],
            'source': (r['source'] or r['engine'] or ''),
            'confidence': None,
        }

    # ---- 筛选取值域 ----
    def facets(self) -> ItemFacets:
        if not self._ok():
            return ItemFacets()

        def top(col: str, table: str = 't1_item', limit: int = 200) -> List[FacetValue]:
            rows = self._sql(f"""SELECT "{col}" v, COUNT(*) c FROM {table}
                                 WHERE IFNULL("{col}",'')<>'' GROUP BY "{col}"
                                 ORDER BY c DESC, v ASC LIMIT ?""", (limit,))
            return [FacetValue(value=x['v'], count=x['c']) for x in rows]

        return ItemFacets(brands=top('brand'), categories=top('category'),
                          suppliers=top('supplier_name'),
                          purchasers=top('purchaser', 't1_notice'))

    # ---- 实体补全（场景查询表单用）----
    def _table_exists(self, name: str) -> bool:
        """派生表可能因「上传小数据集后被 DROP」而不存在 —— 先探测，避免 500。"""
        try:
            row = self._sql("SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                            "AND name=?", (name,))
            return bool(row)
        except Exception:                    # noqa: BLE001
            return False

    def suggest_entities(self, keyword: str, entity_type: str = '',
                         limit: int = 20) -> List[EntitySuggestItem]:
        if not self._ok():
            return []
        kw = f'%{(keyword or "").strip()}%'
        out: List[EntitySuggestItem] = []
        if entity_type in ('', 'supplier'):
            if self._table_exists('supplier_portrait'):
                for r in self._sql("""SELECT supplier n, win_notices c FROM supplier_portrait
                                      WHERE supplier LIKE ? ORDER BY win_notices DESC LIMIT ?""",
                                   (kw, limit)):
                    out.append(EntitySuggestItem(id=r['n'], name=r['n'], type='supplier',
                                                 count=r['c'] or 0))
            else:
                # 派生画像表缺失（上传小数据集后被 DROP）→ 直接从当前标的表动态聚合，
                # 保证实体下拉在任何数据集下都有候选，而不是空列表。
                for r in self._sql("""SELECT supplier_name n, COUNT(*) c FROM t1_item
                                      WHERE IFNULL(supplier_name,'')<>'' AND supplier_name LIKE ?
                                      GROUP BY supplier_name ORDER BY c DESC LIMIT ?""",
                                   (kw, limit)):
                    out.append(EntitySuggestItem(id=r['n'], name=r['n'], type='supplier',
                                                 count=r['c'] or 0))
        if entity_type in ('', 'purchaser') and len(out) < limit:
            if self._table_exists('purchaser_portrait'):
                for r in self._sql("""SELECT purchaser n, n_notices c FROM purchaser_portrait
                                      WHERE purchaser LIKE ? ORDER BY n_notices DESC LIMIT ?""",
                                   (kw, limit - len(out))):
                    out.append(EntitySuggestItem(id=r['n'], name=r['n'], type='purchaser',
                                                 count=r['c'] or 0))
            else:
                for r in self._sql("""SELECT purchaser n, COUNT(*) c FROM t1_notice
                                      WHERE IFNULL(purchaser,'')<>'' AND purchaser LIKE ?
                                      GROUP BY purchaser ORDER BY c DESC LIMIT ?""",
                                   (kw, limit - len(out))):
                    out.append(EntitySuggestItem(id=r['n'], name=r['n'], type='purchaser',
                                                 count=r['c'] or 0))
        return out[:limit]


# ==================================================================
# 接入点 2：关系模型（任务二五大场景）
# ==================================================================

class SQLiteGraphRepository(_Base, GraphRepository):
    """基于交付关系表（合作 / 共同竞标 / 参与投标事实）的场景查询。

    契约要求「预计算物化表最快」：本实现直接读 `cooperation`、`cobidding`、
    `bid`、`g_notice`、`product_supply` 等已物化表，只做必要的 JOIN 与聚合。
    """

    def has_data(self) -> bool:
        return self._ok()

    def _ready(self, *tables: str) -> bool:
        """本次数据集的「关系表来源」是否齐备（缺表 -> 场景返回空结果）。

        上传小数据集时 `platform/ingest.py::_clear_derived` 会把这些**数据集派生的**
        关系/画像表整批 DROP（本次数据集没有 `task2_*.csv` 来源 -> 空关系）。
        派生表缺失时若照样查询，`no such table: cooperation` 会变成 HTTP 500；
        正确行为是「本次没有关系数据」= 空结果（与 EmptyGraphRepository 一致），
        因此这里先查 sqlite_master，缺表就直接返回空。
        """
        if not self._ok():
            return False
        for t in tables:
            if not self._one("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'",
                             (t,)):
                return False
        return True

    # ---- S1 长期合作（含产品供应商）----
    def scenario1(self, purchaser: str, min_count: int = 1, top_n: int = 20) -> Scenario1Out:
        out = Scenario1Out(purchaser=purchaser)
        if not self._ready('cooperation', 'purchaser_product_supplier'):
            return out
        rows = self._sql("""SELECT supplier, coop_times, coop_amount, first_time, last_time
                            FROM cooperation WHERE purchaser=? AND coop_times>=?
                            ORDER BY coop_times DESC, coop_amount DESC, supplier ASC LIMIT ?""",
                         (purchaser, max(1, min_count), top_n))
        names = {r['supplier'] for r in rows}
        # 是否「产品供应商（有品牌记录）」按**本采购单位**判断：
        # 全局判据会让几乎所有货物类供应商都被标记，失去区分度。
        prod_flag = set()
        for r in self._sql("""SELECT DISTINCT i.supplier_name s
                              FROM t1_item i JOIN t1_notice n ON n.announcement_id=i.announcement_id
                              WHERE n.purchaser=? AND IFNULL(i.brand,'')<>''
                                AND IFNULL(i.supplier_name,'')<>''""", (purchaser,)):
            prod_flag.add(r['s'])
        for r in rows:
            n = r['supplier']
            out.suppliers.append(SupplierStat(
                name=n, cooperate_count=r['coop_times'] or 0,
                total_amount=float(r['coop_amount'] or 0.0),
                first_date=_to_date(r['first_time']), last_date=_to_date(r['last_time']),
                is_product_supplier=(n in prod_flag)))
        # 产品供应商（品牌 或 声明函制造商）：作为「有品牌记录」的供应方补充进列表，
        # 合作次数为 0 表示其在本采购单位下没有中标合作记录（如品牌厂商通过代理供货）。
        prods = self._sql("""SELECT product_supplier p, SUM(notices) c,
                                    GROUP_CONCAT(DISTINCT src_kind) k
                             FROM purchaser_product_supplier WHERE purchaser=?
                             GROUP BY product_supplier ORDER BY c DESC LIMIT ?""",
                          (purchaser, top_n))
        for r in prods:
            n = r['p']
            if n in names:
                for s in out.suppliers:
                    if s.name == n:
                        s.is_product_supplier = True
                continue
            names.add(n)
            out.suppliers.append(SupplierStat(name=n, cooperate_count=0, total_amount=0.0,
                                              is_product_supplier=True))
        st = self._one("""SELECT COUNT(*) c, ROUND(SUM(CAST(project_amount AS REAL)),2) a
                          FROM g_notice WHERE purchaser=?""", (purchaser,))
        out.summary = {
            'n_suppliers': len([s for s in out.suppliers if s.cooperate_count > 0]),
            'n_product_suppliers': len([s for s in out.suppliers if s.is_product_supplier]),
            'n_notices': (st['c'] if st else 0),
            'total_amount': float((st['a'] if st else 0) or 0.0),
            'min_count': max(1, min_count),
        }
        return out

    # ---- S2 投标画像 ----
    def scenario2(self, purchaser: str, top_n: int = 5) -> Scenario2Out:
        out = Scenario2Out(purchaser=purchaser)
        if not self._ready('bid', 'entity'):
            return out
        for r in self._sql("""SELECT b.supplier n, COUNT(*) c, SUM(b.is_winner) w,
                                     ROUND(SUM(CASE WHEN b.is_winner=1 THEN COALESCE(b.amount,0) ELSE 0 END),2) a
                              FROM bid b JOIN g_notice n2 ON n2.notice_id=b.notice_id
                              WHERE n2.purchaser=? GROUP BY b.entity_id
                              ORDER BY c DESC, w DESC, b.supplier ASC LIMIT ?""", (purchaser, top_n)):
            out.top_bidders.append(BidderStat(name=r['n'], bid_count=r['c'] or 0,
                                              win_count=r['w'] or 0,
                                              cooperate_amount=float(r['a'] or 0.0)))
        # 注意：协同组合是**无序对**。这里用 entity_id 去重，但**输出按名称排序的成员**
        # （与 Neo4j 版 `WHERE a.name < b.name` 对齐），否则并列时两套实现会选出不同的 TOP5。
        for r in self._sql("""SELECT CASE WHEN ea.canonical_name < eb.canonical_name
                                            THEN ea.canonical_name ELSE eb.canonical_name END a,
                                     CASE WHEN ea.canonical_name < eb.canonical_name
                                            THEN eb.canonical_name ELSE ea.canonical_name END b,
                                     COUNT(DISTINCT x.notice_id) c
                              FROM bid x JOIN bid y ON x.notice_id=y.notice_id AND x.entity_id<y.entity_id
                              JOIN g_notice n ON n.notice_id=x.notice_id
                              JOIN entity ea ON ea.entity_id=x.entity_id
                              JOIN entity eb ON eb.entity_id=y.entity_id
                              WHERE n.purchaser=? GROUP BY x.entity_id, y.entity_id
                              ORDER BY c DESC, a ASC, b ASC LIMIT ?""", (purchaser, top_n)):
            projs = [z['project_name'] for z in self._sql("""
                        SELECT DISTINCT n.project_name FROM bid x
                        JOIN bid y ON x.notice_id=y.notice_id AND x.entity_id<y.entity_id
                        JOIN g_notice n ON n.notice_id=x.notice_id
                        JOIN entity ea ON ea.entity_id=x.entity_id
                        JOIN entity eb ON eb.entity_id=y.entity_id
                        WHERE n.purchaser=? AND ea.canonical_name=? AND eb.canonical_name=?
                        LIMIT 3""", (purchaser, r['a'], r['b'])) if z['project_name']]
            out.co_bid_pairs.append(CoBidPair(members=[r['a'], r['b']], count=r['c'] or 0,
                                              projects=[p[:40] for p in projs]))
        tot = self._one("""SELECT COUNT(DISTINCT b.entity_id) n FROM bid b
                           JOIN g_notice g ON g.notice_id=b.notice_id WHERE g.purchaser=?""",
                        (purchaser,))
        out.summary = {'n_bidders': (tot['n'] if tot else 0),
                       'n_co_bid_pairs': len(out.co_bid_pairs), 'top_n': top_n}
        return out

    # ---- S3 竞对分析 ----
    def scenario3(self, supplier: str, top_n: int = 5) -> Scenario3Out:
        out = Scenario3Out(supplier=supplier)
        if not self._ready('cobidding', 'bid', 'supplier_portrait'):
            return out
        rows = self._sql("""SELECT CASE WHEN supplier_a=? THEN supplier_b ELSE supplier_a END n,
                                   co_times c, co_project_amount a,
                                   CASE WHEN supplier_a=? THEN b_wins ELSE a_wins END w
                            FROM cobidding WHERE supplier_a=? OR supplier_b=?
                            ORDER BY c DESC, n ASC""", (supplier, supplier, supplier, supplier))
        for r in rows:
            st = BidderStat(name=r['n'], bid_count=r['c'] or 0, win_count=r['w'] or 0,
                            cooperate_amount=float(r['a'] or 0.0))
            out.all_bidders.append(st)
            if len(out.top_competitors) < top_n:
                out.top_competitors.append(st)
        prof = self._one('SELECT * FROM supplier_portrait WHERE supplier=?', (supplier,))
        tot = self._one("""SELECT COUNT(DISTINCT b.entity_id) n FROM bid b WHERE b.notice_id IN
                           (SELECT notice_id FROM bid WHERE supplier=?)""", (supplier,))
        out.summary = {
            'bid_notices': (prof['bid_notices'] if prof else 0),
            'win_notices': (prof['win_notices'] if prof else 0),
            'win_rate': float(prof['win_rate']) if prof and prof['win_rate'] is not None else None,
            'won_amount': float(prof['won_amount']) if prof and prof['won_amount'] is not None else 0.0,
            'n_purchasers': (prof['n_purchasers'] if prof else 0),
            'n_competitors': len(out.all_bidders),
            'n_participants': (tot['n'] if tot else 0),
        }
        return out

    # ---- S4 共同客户 ----
    def scenario4(self, suppliers: List[str], min_count: int = 1,
                  top_n: int = 20) -> Scenario4Out:
        out = Scenario4Out(suppliers=list(suppliers))
        if not self._ready('cooperation', 'bid') or len(suppliers) < 2:
            return out
        ph = ','.join('?' * len(suppliers))
        rows = self._sql(f"""SELECT purchaser p, COUNT(DISTINCT supplier) n,
                                    SUM(coop_times) c, ROUND(SUM(coop_amount),2) a
                             FROM cooperation WHERE supplier IN ({ph})
                             GROUP BY purchaser HAVING COUNT(DISTINCT supplier)=?
                             ORDER BY c DESC, a DESC, p ASC LIMIT ?""",
                         tuple(suppliers) + (len(suppliers), top_n))
        for r in rows:
            projs = [z['project_name'] for z in self._sql(f"""
                        SELECT DISTINCT g.project_name FROM bid b
                        JOIN g_notice g ON g.notice_id=b.notice_id
                        WHERE g.purchaser=? AND b.is_winner=1 AND b.supplier IN ({ph})
                        LIMIT 3""", (r['p'], *suppliers)) if z['project_name']]
            out.purchasers.append(PurchaserStat(
                name=r['p'], cooperate_count=r['c'] or 0,
                cooperate_amount=float(r['a'] or 0.0), projects=[p[:40] for p in projs]))
        out.summary = {'n_purchasers': len(out.purchasers), 'suppliers': list(suppliers),
                       'min_count': max(1, min_count)}
        return out

    # ---- S5 共同项目 ----
    def scenario5(self, suppliers: List[str], top_n: int = 50) -> Scenario5Out:
        out = Scenario5Out(suppliers=list(suppliers))
        if not self._ready('bid', 'g_notice') or len(suppliers) < 2:
            return out
        ph = ','.join('?' * len(suppliers))
        sub = ' AND '.join(['notice_id IN (SELECT notice_id FROM bid WHERE supplier=?)'
                            for _ in suppliers])
        rows = self._sql(f"""SELECT notice_id, project_name, purchaser, project_amount, publish_time
                             FROM g_notice WHERE {sub} ORDER BY publish_time, notice_id LIMIT ?""",
                         tuple(suppliers) + (top_n,))
        total = 0.0
        for r in rows:
            parts = [z['supplier'] for z in self._sql(
                'SELECT supplier FROM bid WHERE notice_id=?', (r['notice_id'],))]
            winners = [z['supplier'] for z in self._sql(
                'SELECT supplier FROM bid WHERE notice_id=? AND is_winner=1', (r['notice_id'],))]
            amt = float(r['project_amount'] or 0.0)
            total += amt
            out.projects.append(ProjectRecord(
                project_name=r['project_name'] or '', doc_id=r['notice_id'],
                announce_date=_to_date(r['publish_time']), amount=amt,
                purchaser=r['purchaser'] or '',
                winner='、'.join(winners[:3]),
                participants=parts,
                winner_is_target=any(w in set(suppliers) for w in winners)))
        out.summary = {'n_projects': len(out.projects), 'total_amount': round(total, 2),
                       'suppliers': list(suppliers)}
        return out
