# -*- coding: utf-8 -*-
r"""把「当前 platform.db」同步到 Neo4j 图库（上传入库后的第 5 步，接入点 2 的补齐）。

为什么需要这个模块
------------------
平台默认图库后端是 **Neo4j**（`run.py` 设 `EDM_GRAPH_REPOSITORY=integrations.neo4j_repo:
Neo4jGraphRepository`）。但上传数据集走的是 `integrations/pipeline_impl.py` →
`platform/ingest.py`，它只重建 `platform.db`；而 Neo4j 里的图是
`scripts/build_neo4j_graph.py` 从**全量语料**（`out/task2/graph.db` / 交付表）一次性装载的。
两者互不相干，于是上传小样本后：`/dataset/stats` 已经是小样本，而五个场景仍然读 Neo4j
里的旧全量图 —— 现象就是「上传数据集后场景分析不更新」。

本模块把「本次入库后的 platform.db」按与 `build_neo4j_graph.py` **同一套图模型**重新装载到
Neo4j，使图与库永远同源。属性键/类型严格对齐 `integrations/neo4j_repo.py` 里五个 Cypher
查询所读的字段（`COOPERATES.times/amount/first_date/last_date`、
`CO_BIDS.times/project_amount/a_wins/b_wins`、`PARTICIPATES.is_winner/amount`、
`Supplier.win_times/bid_only_times/participate_times/won_amount`、
`Notice.notice_id/project_name/publish_time/project_amount`…），否则场景会查空。

图模型（与 `scripts/build_neo4j_graph.py` 一致）
------------------------------------------------
    (:Purchaser {name, n_notices, total_amount, n_regions})
    (:Supplier  {name, entity_id, uscc, win_times, bid_only_times,
                 participate_times, won_amount})
    (:Brand     {name})
    (:Notice    {notice_id, project_name, purchaser, region, publish_time,
                 project_amount, procurement_mode, n_winners, n_bidders})

    (:Purchaser)-[:COOPERATES  {times, amount, first_date, last_date}]->(:Supplier)
    (:Supplier) -[:CO_BIDS     {times, project_amount, a_wins, b_wins}]->(:Supplier)
    (:Supplier) -[:PARTICIPATES{is_winner, amount}]->(:Notice)
    (:Supplier) -[:SUPPLIES    {item_times, item_amount}]->(:Brand)
    (:Notice)   -[:ISSUED_BY]->(:Purchaser)

来源表（都从 **platform.db** 读，缺失的表按空处理）
---------------------------------------------------
    t1_notice  -> Notice 节点 / Purchaser 节点 / ISSUED_BY
    t1_item    -> 标的明细：中标事实（supplier_name + is_winner + total_price）、
                  SUPPLIES（brand）、COOPERATES / CO_BIDS / 画像指标的来源
                  （`is_winner` 是死列且没有 bid 表时，按「标的表兜底」把本行
                  supplier_name 视为中标人；判据见下方 `_items_are_unlabelled`）
    bid        -> 有这张表（数据集自带 task2_*.csv 时才有）就以它为准：它带
                  **非中标参与方**与金额，比 t1_item 更完整；没有就从 t1_item 推
    entity     -> entity_id -> canonical_name / uscc（名称归一；没有就用原始名）

幂等：写入前先分批 `DETACH DELETE` 清空整张图，再按批 `UNWIND` 写入；
写完后按「platform.db 推导值」逐个校验节点/关系数量（`shortfall` 为空才算一致）。

用法（一般不直接调，由 `pipeline_impl._load_deliverables` 在入库成功后调用）::

    from integrations.neo4j_sync import sync_from_platform
    r = sync_from_platform('app/out/platform/platform.db',
                           'bolt://127.0.0.1:7687', 'neo4j', 'ict2026neo4j')
"""
from __future__ import annotations

import importlib.util
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

BATCH = 5000

# app/ 目录（本文件位于 app/task3/backend/integrations/neo4j_sync.py）
_APP_DIR = Path(__file__).resolve().parents[3]

# 与 build_neo4j_graph.py 完全相同的约束/索引（IF NOT EXISTS，幂等）
CONSTRAINTS = [
    "CREATE CONSTRAINT purchaser_name IF NOT EXISTS "
    "FOR (p:Purchaser) REQUIRE p.name IS UNIQUE",
    "CREATE CONSTRAINT supplier_name IF NOT EXISTS "
    "FOR (s:Supplier) REQUIRE s.name IS UNIQUE",
    "CREATE CONSTRAINT brand_name IF NOT EXISTS "
    "FOR (b:Brand) REQUIRE b.name IS UNIQUE",
    "CREATE CONSTRAINT notice_id IF NOT EXISTS "
    "FOR (n:Notice) REQUIRE n.notice_id IS UNIQUE",
]
INDEXES = [
    "CREATE INDEX supplier_entity IF NOT EXISTS FOR (s:Supplier) ON (s.entity_id)",
    "CREATE INDEX notice_purchaser IF NOT EXISTS FOR (n:Notice) ON (n.purchaser)",
]

_TRUTHY = {'1', 'true', 't', 'yes', 'y', '是', 'win', 'winner'}


# ==================================================================
# 读 platform.db（只读；表缺失 = 空）
# ==================================================================

def _connect_ro(db: Path) -> sqlite3.Connection:
    p = Path(db)
    if not p.exists():
        raise FileNotFoundError(f'platform.db 不存在：{p}')
    return sqlite3.connect(f'file:{p.as_posix()}?mode=ro', uri=True)


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? AND type IN ('table','view')",
        (name,)).fetchone())


def _read_table(con: sqlite3.Connection, name: str) -> List[Dict[str, Any]]:
    """读整张表为 dict 列表；表不存在返回 []（不抛错）。"""
    if not _has_table(con, name):
        return []
    cur = con.execute(f'SELECT * FROM "{name}"')
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ==================================================================
# 取值工具（与 ingest.py / repos.py 的口径保持一致）
# ==================================================================

def _s(v: Any) -> str:
    return '' if v is None else str(v).strip()


def _bool_int(v: Any) -> int:
    """是否为真 -> 1/0（`is_winner` 在 Neo4j 里必须是整数：S2 用 sum()，
    S4 用行内属性匹配 `{is_winner:1}`）。"""
    if v is None:
        return 0
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if v else 0
    t = str(v).strip().lower()
    if not t:
        return 0
    if t in _TRUTHY:
        return 1
    try:
        return 1 if float(t) else 0
    except ValueError:
        return 0


def _f(v: Any) -> float:
    """金额/数值 -> float（空值、非数字一律 0.0，不抛错）。"""
    if v is None or isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(',', '')
    if not t or t.lower() in ('none', 'nan', 'null', '-'):
        return 0.0
    try:
        return float(t)
    except ValueError:
        return 0.0


def _i(v: Any) -> int:
    return int(_f(v))


# ------------------------------------------------------------------
# 「标的表兜底」：is_winner 死列 + 无 bid 表时的中标人判据
# ------------------------------------------------------------------
#
# 背景（真 bug）：交付表 `deliverables/items_task1.csv` 的 `is_winner` 是**死列**
# （全空；上传走 LLM 抽取的数据集同样不落这一列）。全量库之所以有中标方，是因为
# 数据集自带 `task2_*.csv` → 平台库有 `bid` 表（带 `is_winner` 真值 + 非中标参与方）。
# **小样本数据集没有 bid 表**，于是从 t1_item 反推时每个 (公告, 供应商) 的
# `is_winner` 都是 0 → 图里没有 `COOPERATES`（采购方 → 中标供应商）、
# `PARTICIPATES.is_winner` 全 0、`Supplier.win_times/won_amount` 全 0
# → S1「供应商数 0」、S3「中标次数 0」。
#
# 兜底规则（**逐条判据，缺一不可**）：
#   1. 整库**没有任何 bid 表行**（`bids == []`，即数据集不带任务二关系表）——
#      有 bid 表就以它为准，兜底**永不生效**（保证全量库结果逐字不变）；
#   2. 该篇公告的 t1_item 行 **`is_winner` 逐行为空**（既没有真值、也没有显式假值）——
#      任何一行写了 `True/1/是` 或 `False/0/否` 都算「有标记」，本篇不启用兜底；
#   3. 该行 `supplier_name` 非空（空供货方建不出关系）。
# 满足时把这些 supplier_name 视为**中标人**。
#
# 「有据可依」的依据：t1_item 的语义就是《输出标准样例表》里的**标的表** ——
# 它记录的是**结果公示里"这个包由谁供什么"的成交标的**（招标/需求类公告没有标的行，
# 有标的行的都是中标/成交结果）。所以「标的供货方」在本就缺 `is_winner` 标记时，
# 就是该行的中标人，而不是"参与投标但未中标"的陪标方 —— 陪标方只出现在
# `bid` 表/投标人表里，从不写进标的表。这也是平台「单一来源=中标方」既有兜底
# （`ict_parse/official_tables.py`）的同一条口径。

def _items_are_unlabelled(rows: List[Dict[str, Any]]) -> bool:
    """某篇公告的 t1_item 行里 `is_winner` 是否**逐行为空**（判据 2）。

    * 空 / None / 全空白字符串 -> 这一行算"未标注"；
    * `True` / `False` / `1` / `0` / `'是'` / `'否'` -> 这行**有标记**，整篇不启用兜底
      （显式 `False` 明确表示"不是中标方"，绝不能被兜底翻成中标人）；
    * 没有任何行 -> 不启用（没有标的就没有中标标的）。
    """
    if not rows:
        return False
    for r in rows:
        v = r.get('is_winner')
        if isinstance(v, bool):      # 显式 True/False 都算"有标记"
            return False
        if _s(v):                    # 任何非空取值都算"有标记"
            return False
    return True


# ==================================================================
# 派生：platform.db -> 与 build_neo4j_graph.py 同构的节点/关系行
# ==================================================================

# ==================================================================
# 名称归一：同一实体的多种写法 -> 同一个键（P2）
# ==================================================================
#
# 已确认的缺陷：交付表/上传数据里同一个供应商会有两种写法 ——
#   `甲科技有限公司`（标的行）与 `甲科技有限公司（包2）`（关系表/投标人表，原文自带包标注）。
# 改造前 `facts` 的键是**显示名**，于是：
#   * 同一实体在图上变成 **两个 Supplier 节点**（画像被劈成两半）；
#   * 同一篇公告上出现 `甲 -> 甲` 的 **CO_BIDS 自配对**（自己跟自己竞标，S3/S4 直接错）。
#
# 修法：`facts` / `coop` / `cobids` / `supplies` / `supplier` 全部按
# `official_tables.norm_base_name`（剥掉「（包2）」这类包标注后归一）聚合；
# 显示名在登记簿里择优 —— **没有包标注的写法优先**，所以中标方的原始写法不会被
# 降级成带包标注的形式，is_winner 也不会因为合并而丢失（见 facts 的 max()）。

def _norm_key(name: Any) -> str:
    """实体名 -> 归一键（`official_tables.norm_base_name` 口径 + 剥包标注）。

    ⚠️ 这里**先** `strip_pkg_affix()` 再 `norm_base_name()`，不是多余的：
    `official_tables.norm_base_name()` 实现是 `_PKG_AFFIX_RE.sub("", norm_name(s))`，
    而 `norm_name()` 会先把括号从字符串里**删掉**（`_NORM_STRIP` 含 `（）()【】`），
    于是后面那个「括号里的包标注」正则永远匹配不到 —— 实测
    `norm_base_name('甲科技有限公司（包2）') == '甲科技有限公司包2'`（没剥掉）。
    包标注在**开头/结尾**时，`strip_pkg_affix()` 用的是未归一的原文，能正确剥掉。
    （`official_tables.py` 不归本任务改，已在报告里给出建议。）
    """
    s = _s(name)
    if not s:
        return ''
    try:
        from ict_parse.official_tables import norm_base_name, strip_pkg_affix  # noqa: PLC0415
        return norm_base_name(strip_pkg_affix(s)) or norm_base_name(s)
    except Exception:                                          # noqa: BLE001
        return s


def _is_clean_name(name: str) -> bool:
    """该写法是否**不带包标注**（`甲科技有限公司` -> True / `甲科技有限公司（包2）` -> False）。"""
    try:
        from ict_parse.official_tables import strip_pkg_affix  # noqa: PLC0415
        return strip_pkg_affix(name) == _s(name)
    except Exception:                                          # noqa: BLE001
        return True


class _SupplierNames:
    """供应商显示名登记簿：内部一律用归一键，显示名择优（不带包标注者优先）。"""

    __slots__ = ('_by_key',)

    def __init__(self) -> None:
        self._by_key: Dict[str, str] = {}

    @staticmethod
    def key(name: Any) -> str:
        nm = _s(name)
        return _norm_key(nm) if nm else ''

    def put(self, name: Any) -> str:
        """登记一种写法，返回该实体的归一键（空名返回 ''）。"""
        nm = _s(name)
        if not nm:
            return ''
        k = _norm_key(nm)
        cur = self._by_key.get(k)
        if cur is None or (_is_clean_name(nm) and not _is_clean_name(cur)):
            self._by_key[k] = nm          # 干净写法优先；否则保留先出现的写法
        return k

    def display(self, key: str) -> str:
        return self._by_key.get(key, key)


def derive(platform_db: Path) -> Dict[str, List[Dict[str, Any]]]:
    """从 platform.db 派生图数据（纯计算，不碰 Neo4j）。

    返回 `{'purchaser','supplier','brand','notice','cooperates','cobids',
    'participates','supplies','issued_by'}`，键名与
    `scripts/build_neo4j_graph.py::load_source()` 一致，便于对照。
    """
    con = _connect_ro(platform_db)
    try:
        notices = _read_table(con, 't1_notice')
        items = _read_table(con, 't1_item')
        bids = _read_table(con, 'bid')
        # 投标人名单（参与人）：附件/正文《开标一览表》《报价明细》《评审结果》
        # 《资格审查名单》《未中标（成交）原因表》逐行抽出的**全部投标人**
        # （上传管线写 `bidder_roster.csv` -> 平台库 `t1_bidder`）。
        # 表不存在 = 没有名单来源 -> 空列表（旧库行为不变）。
        roster = _read_table(con, 't1_bidder')
        ents = _read_table(con, 'entity')
        aliases = _read_table(con, 'entity_alias')
    finally:
        con.close()

    # ---- 名称归一：entity/entity_alias 在库时才做（大数据集自带）----
    canon_by_id: Dict[str, str] = {}
    uscc_by_id: Dict[str, str] = {}
    for r in ents:
        eid = _s(r.get('entity_id'))
        if not eid:
            continue
        canon_by_id[eid] = _s(r.get('canonical_name')) or eid
        uscc_by_id[eid] = _s(r.get('uscc'))
    id_by_name: Dict[str, str] = {}
    for r in aliases:
        nm, eid = _s(r.get('name')), _s(r.get('entity_id'))
        if nm and eid:
            id_by_name.setdefault(nm, eid)
    for eid, nm in canon_by_id.items():
        id_by_name.setdefault(nm, eid)

    def canon(name: Any, entity_id: Any = None) -> str:
        """显示名 -> 规范名（没有 entity 数据时就是原名本身）。"""
        eid = _s(entity_id)
        if eid and eid in canon_by_id:
            return canon_by_id[eid]
        nm = _s(name)
        e2 = id_by_name.get(nm)
        return canon_by_id.get(e2, nm) if e2 else nm

    def entity_of(name: str) -> Tuple[str, str]:
        eid = id_by_name.get(name, '')
        return eid, uscc_by_id.get(eid, '')

    # 供应商显示名登记簿：`facts` 等全部结构内部只存**归一键**，
    # 写出去时才用 `names.display(key)` 换成显示名（见文件上方 `_SupplierNames`）。
    names = _SupplierNames()

    # ---- Notice / Purchaser ----
    notice_by_id: Dict[str, Dict[str, Any]] = {}
    for r in notices:
        nid = _s(r.get('announcement_id'))
        if not nid:
            continue
        notice_by_id[nid] = {
            'notice_id': nid,
            'project_name': _s(r.get('project_name')),
            'purchaser': _s(r.get('purchaser')),
            'region': _s(r.get('region')),
            'publish_time': _s(r.get('publish_time')),
            'project_amount': _f(r.get('project_amount')),
            'procurement_mode': _s(r.get('procurement_mode')),
            'n_winners': _i(r.get('n_winners')),
            'n_bidders': _i(r.get('n_bidders')),
        }

    purchasers: Dict[str, Dict[str, Any]] = {}
    for n in notice_by_id.values():
        p = n['purchaser']
        if not p:
            continue
        d = purchasers.setdefault(p, {'name': p, 'n_notices': 0,
                                      'total_amount': 0.0, '_regions': set()})
        d['n_notices'] += 1
        d['total_amount'] += n['project_amount']
        if n['region']:
            d['_regions'].add(n['region'])

    # ---- 参与投标事实 (notice, supplier) -> is_winner / amount ----
    # 有 bid 表就用它（含非中标参与方与金额，语义与 graph.db 的 bid 一致）；
    # 否则从 t1_item 推（只有中标方，小样本上传后就是这种情况）。
    facts: Dict[Tuple[str, str], Dict[str, Any]] = {}
    by_notice_items: Dict[str, List[Dict[str, Any]]] = {}
    for r in items:
        nid = _s(r.get('announcement_id'))
        if nid in notice_by_id:
            by_notice_items.setdefault(nid, []).append(r)

    def _item_derived_winner_facts(nid: str) -> int:
        """用该篇标的行的供货方补「中标事实」；返回新增/提升的事实条数。

        **只在本篇 `is_winner` 逐行为空时**生效（判据见 `_items_are_unlabelled`）：
        标的表记录的是「结果公示里这个包由谁供什么」，缺中标标记时供货方即中标人。
        已被别处标成参与人（is_winner=0）的同一实体在此**只提升为中标人，不删除、不改名**。
        """
        rows = by_notice_items.get(nid) or []
        if not _items_are_unlabelled(rows):
            return 0
        n = 0
        for r in rows:
            skey = names.put(canon(r.get('supplier_name')))
            if not skey:                               # 空供货方建不出关系
                continue
            d = facts.setdefault((nid, skey), {'notice_id': nid, 'supplier': skey,
                                               'is_winner': 0, 'amount': 0.0})
            if not d['is_winner']:
                d['is_winner'] = 1
                n += 1
            d['amount'] += _f(r.get('total_price'))
        return n

    if bids:
        for r in bids:
            nid = _s(r.get('notice_id'))
            if nid not in notice_by_id:
                continue
            # 归一键（同一实体的 `（包2）` 写法与干净写法合并成同一条事实）
            skey = names.put(canon(r.get('supplier'), r.get('entity_id')))
            if not skey:
                continue
            d = facts.setdefault((nid, skey), {'notice_id': nid, 'supplier': skey,
                                               'is_winner': 0, 'amount': 0.0})
            d['is_winner'] = max(d['is_winner'], _bool_int(r.get('is_winner')))
            d['amount'] += _f(r.get('amount'))
    else:
        # 无 bid 表（小样本）→ 从标的表反推；**本篇 is_winner 逐行为空**时启用
        # 「标的表兜底」（判据全文见文件上方 `_items_are_unlabelled` 的注释块）：
        # 该行 supplier_name 就是中标人，amount 取该行总价。
        for nid in by_notice_items:
            _item_derived_winner_facts(nid)

    # ---- 逐篇补齐：bid 表存在，但某篇「有中标方」却没有任何中标事实 ----
    # 这是「S1 查不到中标供应商」的第二条成因：`task2_bids.csv`/`bid` 表**只覆盖了部分公告**
    # （数据集自带的关系表是上一版跑的，后来又补抽了 winners；或上传时只带了一份残缺的
    # bids 表）。改造前 `if bids:` 一旦成立就**整库**不信标的表，于是这些公告
    # 在图上 n_winners>0 却连一条 `PARTICIPATES{is_winner:1}` 都没有 ->
    # `(:Purchaser)-[:COOPERATES]->(:Supplier)` 缺边 -> S1 供应商数偏少/为 0。
    #
    # 兜底口径与 `scripts/build_neo4j_graph.py::_repair_missing_winners` 完全一致：
    #   * 只处理「`t1_notice.n_winners > 0` 且当前没有任何中标事实」的公告（其余一律不动）；
    #   * 中标人取该篇标的行的供货方（`_items_are_unlabelled` 判据不满足则不猜）；
    #   * 不新增参与人、不删事实、不把中标方降级、不自配对。
    repaired: List[str] = []
    if bids:
        for nid, n in notice_by_id.items():
            if _i(n.get('n_winners')) <= 0:
                continue
            if any(f['notice_id'] == nid and f['is_winner'] for f in facts.values()):
                continue
            if _item_derived_winner_facts(nid):
                repaired.append(nid)

    # ---- 投标人名单里的人也要进图（P2 缺陷：42/202 参与人不在图里）----
    # 症状：上传小样本（无 bid 表）时，`facts` 只有「标的供货方 = 中标方」，
    # 于是「参与但未中标」的公司在图里**完全没有节点/关系**，S2（投标画像）、
    # S3（竞对分析）都看不到它们；实测 202 个参与人里有 42 个不在图里。
    #
    # 修法：把 `t1_bidder` 里出现的人（**仅参与人口径，is_winner=0**）补成一条
    # 参与事实。三条安全规则：
    #   1. 已存在的事实**绝不改写**（特别是不能把中标方降级成参与人）；
    #   2. 金额只在**新建**事实时取该行的报价（`quote_yuan`），
    #      已存在的事实金额保持原值（避免把"中标金额"与"报价"相加）；
    #   3. 公告号必须在本库 `t1_notice` 里（否则建不出 Notice 关系，写了也会被
    #      MATCH 静默丢掉）。
    for r in roster:
        nid = _s(r.get('announcement_id'))
        if nid not in notice_by_id:
            continue
        skey = names.put(canon(r.get('name')))
        if not skey:
            continue
        if (nid, skey) in facts:
            continue                      # 规则 1：已有事实（含中标标记）原样保留
        facts[(nid, skey)] = {'notice_id': nid, 'supplier': skey,
                              'is_winner': 0, 'amount': _f(r.get('quote_yuan'))}

    # ---- Supplier 画像（与 v_supplier_portrait 同口径：
    #      bid_notices=参与公告数、win_notices=中标公告数、won_amount=中标金额和）----
    sup: Dict[str, Dict[str, Any]] = {}
    for f in facts.values():
        d = sup.setdefault(f['supplier'], {'name': f['supplier'], '_part': set(),
                                           '_win': set(), 'won_amount': 0.0})
        d['_part'].add(f['notice_id'])
        if f['is_winner']:
            d['_win'].add(f['notice_id'])
            d['won_amount'] += f['amount']

    # ---- COOPERATES（采购单位 -> 中标供应商）----
    coop: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for f in facts.values():
        if not f['is_winner']:
            continue
        n = notice_by_id[f['notice_id']]
        p = n['purchaser']
        if not p:
            continue
        d = coop.setdefault((p, f['supplier']),
                            {'purchaser': p, 'supplier': f['supplier'], 'times': 0,
                             'amount': 0.0, 'first_date': None, 'last_date': None,
                             '_seen': set()})
        if f['notice_id'] not in d['_seen']:
            d['_seen'].add(f['notice_id'])
            d['times'] += 1
        d['amount'] += f['amount']
        pt = n['publish_time'] or None
        if pt:
            if d['first_date'] is None or pt < d['first_date']:
                d['first_date'] = pt
            if d['last_date'] is None or pt > d['last_date']:
                d['last_date'] = pt

    # ---- CO_BIDS（同一公告上的两个参与方，无向语义存单向）----
    by_notice: Dict[str, Dict[str, int]] = {}
    for f in facts.values():
        by_notice.setdefault(f['notice_id'], {})[f['supplier']] = f['is_winner']
    cob: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for nid, parts in by_notice.items():
        names_here = sorted(parts)                # 归一键（同一实体只会出现一次）
        if len(names_here) < 2:
            continue                              # 只有一个参与方 -> 没有「共同投标」
        amt = notice_by_id[nid]['project_amount']
        for i in range(len(names_here)):
            for j in range(i + 1, len(names_here)):
                a, b = names_here[i], names_here[j]
                if a == b:                        # 双保险：绝不自配对（P2 回归点）
                    continue
                d = cob.setdefault((a, b), {'supplier_a': a, 'supplier_b': b,
                                            'times': 0, 'project_amount': 0.0,
                                            'a_wins': 0, 'b_wins': 0})
                d['times'] += 1
                d['project_amount'] += amt
                d['a_wins'] += parts[a]
                d['b_wins'] += parts[b]

    # ---- Brand / SUPPLIES ----
    brands: Dict[str, Dict[str, Any]] = {}
    supplies: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in items:
        nid = _s(r.get('announcement_id'))
        if nid not in notice_by_id:
            continue
        br = _s(r.get('brand'))
        if not br:
            continue
        brands.setdefault(br, {'name': br})
        win = names.put(canon(r.get('supplier_name')))
        if not win:                      # 空供货方无法建关系（与 build 脚本一致）
            continue
        d = supplies.setdefault((win, br), {'product_supplier': br, 'winner': win,
                                            'item_times': 0, 'item_amount': 0.0})
        d['item_times'] += 1
        d['item_amount'] += _f(r.get('total_price'))

    # ---- 补齐「只出现在关系里」的供应商节点（否则 MATCH 会静默丢关系）----
    extra = set()
    for d in coop.values():
        extra.add(d['supplier'])
    for d in cob.values():
        extra.add(d['supplier_a'])
        extra.add(d['supplier_b'])
    for d in supplies.values():
        extra.add(d['winner'])
    for name in extra:
        sup.setdefault(name, {'name': name, '_part': set(), '_win': set(),
                              'won_amount': 0.0})

    # ---- 组装输出（键名与 build_neo4j_graph.load_source 一致）----
    out: Dict[str, List[Dict[str, Any]]] = {
        'purchaser': [{'name': d['name'], 'n_notices': d['n_notices'],
                       'total_amount': round(d['total_amount'], 2),
                       'n_regions': len(d['_regions'])}
                      for d in purchasers.values()],
        'supplier': [],
        'brand': [{'name': d['name']} for d in brands.values()],
        'notice': [dict(d) for d in notice_by_id.values()],
        'cooperates': [{'purchaser': d['purchaser'],
                        'supplier': names.display(d['supplier']),
                        'coop_times': d['times'], 'coop_amount': round(d['amount'], 2),
                        'first_time': d['first_date'], 'last_time': d['last_date']}
                       for d in coop.values()],
        'cobids': [{'supplier_a': names.display(d['supplier_a']),
                    'supplier_b': names.display(d['supplier_b']),
                    'co_times': d['times'],
                    'co_project_amount': round(d['project_amount'], 2),
                    'a_wins': d['a_wins'], 'b_wins': d['b_wins']}
                   for d in cob.values()],
        'participates': [{'notice_id': d['notice_id'],
                          'supplier': names.display(d['supplier']),
                          'is_winner': int(d['is_winner']),
                          'amount': round(d['amount'], 2)}
                         for d in facts.values()],
        'supplies': [{'product_supplier': d['product_supplier'],
                      'winner': names.display(d['winner']),
                      'item_times': d['item_times'],
                      'item_amount': round(d['item_amount'], 2)}
                     for d in supplies.values()],
        'issued_by': [{'notice_id': n['notice_id'], 'purchaser': n['purchaser']}
                      for n in notice_by_id.values() if n['purchaser']],
    }
    for d in sup.values():
        disp = names.display(d['name'])            # 归一键 -> 该实体的显示名
        eid, uscc = entity_of(disp)
        part, win = len(d['_part']), len(d['_win'])
        out['supplier'].append({
            'name': disp, 'entity_id': eid, 'uscc': uscc,
            'win_times': win, 'bid_only_times': max(0, part - win),
            'participate_times': part, 'won_amount': round(d['won_amount'], 2),
        })

    # ---- 一致性指标（与 scripts/assert_graph_consistency.py 同一口径）----
    # `_winners_gap`：n_winners>0 却没有任何中标事实的公告 -> 这些公告在图上**必然**
    #   少一条 COOPERATES（S1 供应商数偏少）。理想值是空列表；非空时由断言脚本逐条解释。
    # `_winners_repaired`：本次靠「逐篇标的兜底」补上的公告（正常情况下也是空）。
    # `_cooperates_pairs` / `_winner_pairs`：COOPERATES 对数 vs 中标（采购方, 供应商）对数，
    #   差额只允许是「采购单位为空」这类已披露例外。
    win_by_notice = {f['notice_id'] for f in facts.values() if f['is_winner']}
    gaps = sorted(nid for nid, n in notice_by_id.items()
                  if _i(n.get('n_winners')) > 0 and nid not in win_by_notice)
    winner_pairs = {(notice_by_id[f['notice_id']]['purchaser'], names.display(f['supplier']))
                    for f in facts.values()
                    if f['is_winner'] and notice_by_id[f['notice_id']]['purchaser']}
    coop_pairs = {(d['purchaser'], names.display(d['supplier'])) for d in coop.values()}
    out['_winners_gap'] = gaps
    out['_winners_repaired'] = sorted(repaired)
    out['_coverage'] = (len(win_by_notice), sum(1 for n in notice_by_id.values()
                                                if _i(n.get('n_winners')) > 0))
    out['_cooperates_pairs'] = len(coop_pairs)
    out['_winner_pairs'] = len(winner_pairs)
    out['_pair_shortfall'] = sorted(winner_pairs - coop_pairs)
    return out


# ==================================================================
# 写 Neo4j
# ==================================================================

def _wipe(s, batch: int = 50000) -> int:
    """分批清空整张图（避免一次过大事务把 Neo4j 打满）。"""
    total = 0
    while True:
        r = s.run("MATCH (x) WITH x LIMIT $batch DETACH DELETE x "
                  "RETURN count(x) AS n", batch=batch).single()
        c = int(r['n']) if r else 0
        total += c
        if c == 0:
            break
    return total


def _push(s, cypher: str, rows: List[Dict[str, Any]], keys: List[str]) -> int:
    n = 0
    for i in range(0, len(rows), BATCH):
        chunk = [{k: r.get(k) for k in keys} for r in rows[i:i + BATCH]]
        s.run(cypher, rows=chunk).consume()
        n += len(chunk)
    return n


def _distinct(rows: List[Dict[str, Any]], keys: Tuple[str, ...]) -> int:
    return len({tuple(_s(r.get(k)) for k in keys) for r in rows})


def load(src: Dict[str, List[Dict[str, Any]]], uri: str, user: str,
         password: str, database: str = 'neo4j') -> Dict[str, Any]:
    """把 `derive()` 的结果整体装载进 Neo4j（先清空，再按批 UNWIND 写入）。"""
    try:
        from neo4j import GraphDatabase
    except ImportError as e:                                  # pragma: no cover
        raise RuntimeError(f'缺少 neo4j 驱动：{e}') from e

    drv = GraphDatabase.driver(uri, auth=(user, password))
    counts: Dict[str, Any] = {}
    t0 = time.time()
    try:
        drv.verify_connectivity()
        with drv.session(database=database) as s:
            for stmt in CONSTRAINTS + INDEXES:
                s.run(stmt).consume()
            counts['wiped'] = _wipe(s)

            counts['Purchaser'] = _push(
                s, "UNWIND $rows AS r MERGE (p:Purchaser {name: r.name}) "
                   "SET p.n_notices=toInteger(r.n_notices), "
                   "    p.total_amount=toFloat(r.total_amount), "
                   "    p.n_regions=toInteger(r.n_regions)",
                src['purchaser'], ['name', 'n_notices', 'total_amount', 'n_regions'])
            counts['Supplier'] = _push(
                s, "UNWIND $rows AS r MERGE (x:Supplier {name: r.name}) "
                   "SET x.entity_id=r.entity_id, x.uscc=r.uscc, "
                   "    x.win_times=toInteger(r.win_times), "
                   "    x.bid_only_times=toInteger(r.bid_only_times), "
                   "    x.participate_times=toInteger(r.participate_times), "
                   "    x.won_amount=toFloat(r.won_amount)",
                src['supplier'], ['name', 'entity_id', 'uscc', 'win_times',
                                  'bid_only_times', 'participate_times', 'won_amount'])
            counts['Brand'] = _push(
                s, "UNWIND $rows AS r MERGE (:Brand {name: r.name})",
                src['brand'], ['name'])
            counts['Notice'] = _push(
                s, "UNWIND $rows AS r MERGE (n:Notice {notice_id: r.notice_id}) "
                   "SET n.project_name=r.project_name, n.purchaser=r.purchaser, "
                   "    n.region=r.region, n.publish_time=r.publish_time, "
                   "    n.project_amount=toFloat(r.project_amount), "
                   "    n.procurement_mode=r.procurement_mode, "
                   "    n.n_winners=toInteger(r.n_winners), "
                   "    n.n_bidders=toInteger(r.n_bidders)",
                src['notice'], ['notice_id', 'project_name', 'purchaser', 'region',
                                'publish_time', 'project_amount', 'procurement_mode',
                                'n_winners', 'n_bidders'])
            counts['COOPERATES'] = _push(
                s, "UNWIND $rows AS r "
                   "MATCH (p:Purchaser {name: r.purchaser}), (x:Supplier {name: r.supplier}) "
                   "MERGE (p)-[c:COOPERATES]->(x) "
                   "SET c.times=toInteger(r.coop_times), "
                   "    c.amount=toFloat(r.coop_amount), "
                   "    c.first_date=r.first_time, c.last_date=r.last_time",
                src['cooperates'], ['purchaser', 'supplier', 'coop_times',
                                    'coop_amount', 'first_time', 'last_time'])
            counts['CO_BIDS'] = _push(
                s, "UNWIND $rows AS r "
                   "MATCH (a:Supplier {name: r.supplier_a}), (b:Supplier {name: r.supplier_b}) "
                   "MERGE (a)-[x:CO_BIDS]->(b) "
                   "SET x.times=toInteger(r.co_times), "
                   "    x.project_amount=toFloat(r.co_project_amount), "
                   "    x.a_wins=toInteger(r.a_wins), x.b_wins=toInteger(r.b_wins)",
                src['cobids'], ['supplier_a', 'supplier_b', 'co_times',
                                'co_project_amount', 'a_wins', 'b_wins'])
            counts['PARTICIPATES'] = _push(
                s, "UNWIND $rows AS r "
                   "MATCH (x:Supplier {name: r.supplier}), (n:Notice {notice_id: r.notice_id}) "
                   "MERGE (x)-[p:PARTICIPATES]->(n) "
                   "SET p.is_winner=toInteger(r.is_winner), p.amount=toFloat(r.amount)",
                src['participates'], ['notice_id', 'supplier', 'is_winner', 'amount'])
            counts['SUPPLIES'] = _push(
                s, "UNWIND $rows AS r "
                   "MATCH (x:Supplier {name: r.winner}), (b:Brand {name: r.product_supplier}) "
                   "MERGE (x)-[p:SUPPLIES]->(b) "
                   "SET p.item_times=toInteger(r.item_times), "
                   "    p.item_amount=toFloat(r.item_amount)",
                src['supplies'], ['product_supplier', 'winner', 'item_times',
                                  'item_amount'])
            counts['ISSUED_BY'] = _push(
                s, "UNWIND $rows AS r "
                   "MATCH (n:Notice {notice_id: r.notice_id}), (p:Purchaser {name: r.purchaser}) "
                   "MERGE (n)-[:ISSUED_BY]->(p)",
                src['issued_by'], ['notice_id', 'purchaser'])

            # ---- 计数校验：图的真实数量 vs platform.db 推导值 ----
            expected = {
                'Purchaser': _distinct(src['purchaser'], ('name',)),
                'Supplier': _distinct(src['supplier'], ('name',)),
                'Brand': _distinct(src['brand'], ('name',)),
                'Notice': _distinct(src['notice'], ('notice_id',)),
                'COOPERATES': _distinct(src['cooperates'], ('purchaser', 'supplier')),
                'CO_BIDS': _distinct(src['cobids'], ('supplier_a', 'supplier_b')),
                'PARTICIPATES': _distinct(src['participates'], ('notice_id', 'supplier')),
                'SUPPLIES': _distinct(src['supplies'], ('product_supplier', 'winner')),
                'ISSUED_BY': _distinct(src['issued_by'], ('notice_id', 'purchaser')),
            }
            actual: Dict[str, int] = {}
            for lab in ('Purchaser', 'Supplier', 'Brand', 'Notice'):
                actual[lab] = int(s.run(
                    f'MATCH (n:{lab}) RETURN count(n) AS n').single()['n'])
            for rel in ('COOPERATES', 'CO_BIDS', 'PARTICIPATES', 'SUPPLIES', 'ISSUED_BY'):
                actual[rel] = int(s.run(
                    f'MATCH ()-[r:{rel}]->() RETURN count(r) AS n').single()['n'])
            counts['expected'] = expected
            counts['actual'] = actual
            counts['shortfall'] = {k: (actual[k], v) for k, v in expected.items()
                                   if actual[k] != v}

            # ---- 「有中标方却查不到」的结构性校验（与 build_neo4j_graph.py 同一口径）----
            # n_winners>0 的 Notice 必须至少有一条 is_winner=1 的 PARTICIPATES；
            # 缺口逐条列出（`winners_gap`），调用方可据此告警 —— 否则 S1「供应商数」会偏少。
            counts['winners_coverage'] = (
                int(s.run(
                    'MATCH (n:Notice)<-[p:PARTICIPATES]-(:Supplier) WHERE p.is_winner = 1 '
                    'RETURN count(DISTINCT n) AS n').single()['n']),
                int(s.run(
                    'MATCH (n:Notice) WHERE toInteger(coalesce(n.n_winners,0)) > 0 '
                    'RETURN count(n) AS n').single()['n']))
            counts['winners_gap'] = [r['nid'] for r in s.run(
                'MATCH (n:Notice) WHERE toInteger(coalesce(n.n_winners,0)) > 0 '
                'AND NOT EXISTS { (n)<-[p:PARTICIPATES]-(:Supplier) WHERE p.is_winner = 1 } '
                'RETURN n.notice_id AS nid ORDER BY nid')]
    finally:
        drv.close()
    counts['elapsed_sec'] = round(time.time() - t0, 2)
    return counts


def sync_from_platform(platform_db, neo4j_uri: str = 'bolt://127.0.0.1:7687',
                       user: str = 'neo4j', password: str = 'neo4j',
                       database: str = 'neo4j') -> Dict[str, Any]:
    """把 `platform_db`（当前平台服务库）同步为 Neo4j 图里的全部内容。

    幂等：先清空整张图再写；返回装载与校验的计数明细（`shortfall` 为空 = 一致）。
    连不上 Neo4j / 缺驱动时**抛异常**，由调用方决定「跳过并只打一行警告」，
    不让上传因为图库不可用而失败。
    """
    src = derive(Path(platform_db))
    counts = load(src, neo4j_uri, user, password, database)
    counts['derived'] = {k: len(v) for k, v in src.items() if not k.startswith('_')}
    counts['derive_check'] = {
        'coverage': src.get('_coverage'),
        'winners_gap': src.get('_winners_gap') or [],
        'winners_repaired': src.get('_winners_repaired') or [],
        'cooperates_pairs': src.get('_cooperates_pairs'),
        'winner_pairs': src.get('_winner_pairs'),
        'pair_shortfall': src.get('_pair_shortfall') or [],
    }
    counts['nodes'] = sum(counts['actual'][k] for k in
                          ('Purchaser', 'Supplier', 'Brand', 'Notice'))
    counts['rels'] = sum(counts['actual'][k] for k in
                         ('COOPERATES', 'CO_BIDS', 'PARTICIPATES', 'SUPPLIES', 'ISSUED_BY'))
    counts['total'] = counts['nodes'] + counts['rels']
    return counts


# ==================================================================
# 「恢复默认数据」用的**全量图重建**：直接复用仓库的建图脚本
# ==================================================================
#
# 为什么这里不复用上面的 `sync_from_platform()`：
#   * `sync_from_platform()` 是「把**本次上传**的数据同步上图」，它从 platform.db
#     反推图（品牌取 `t1_item.brand`、供应商取标的表里出现过的名字）；
#   * 而「恢复默认数据」的口径是「回到仓库内置的完整交付集」，必须与
#     `scripts/build_neo4j_graph.py --load --wipe` 的输出**逐项一致**
#     （节点 5,645 / 关系 14,143；`scripts/verify_neo4j.py` 11/11）。
#     两者的取数来源不同（graph.db 的 v_product_supply.product_supplier vs
#     t1_item.brand），实测走 sync_from_platform 会得到 5,674 个节点
#     （关系数一致），与交付集的验收口径对不上。
#   因此全量恢复**直接调用建图脚本的 load_source() / wipe() / load()** ——
#   与命令行那条 `--load --wipe` 走的是同一份代码，口径天然一致。
#
# 注意：脚本用 `SystemExit` 报「连不上 Neo4j」（CLI 写法），这里统一转成
# RuntimeError —— HTTP 处理线程里绝不能让 SystemExit 逃出去（会终止进程）。

def _load_build_script():
    """按文件路径加载 `app/scripts/build_neo4j_graph.py`。

    不能写 `import scripts.build_neo4j_graph`：`scripts/` 不是包（没有 __init__.py），
    而 `platform.ingest` 的同款问题在 `pipeline_impl._load_ingest()` 里已有先例。
    """
    spec = importlib.util.spec_from_file_location(
        'build_neo4j_graph', _APP_DIR / 'scripts' / 'build_neo4j_graph.py')
    if spec is None or spec.loader is None:                   # pragma: no cover
        raise RuntimeError('找不到 scripts/build_neo4j_graph.py，无法重建全量图')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                              # type: ignore[union-attr]
    return mod


def graph_counts(neo4j_uri: str = 'bolt://127.0.0.1:7687', user: str = 'neo4j',
                 password: str = 'neo4j',
                 database: str = 'neo4j') -> Tuple[int, int]:
    """图的真实规模 -> `(节点数, 关系数)`（不依赖任何推导值）。"""
    from neo4j import GraphDatabase

    drv = GraphDatabase.driver(neo4j_uri, auth=(user, password))
    try:
        with drv.session(database=database) as s:
            n = int(s.run('MATCH (n) RETURN count(n) AS n').single()['n'])
            r = int(s.run('MATCH ()-[x]->() RETURN count(x) AS r').single()['r'])
    finally:
        drv.close()
    return n, r


def sync_full_graph(neo4j_uri: str = 'bolt://127.0.0.1:7687', user: str = 'neo4j',
                    password: str = 'neo4j',
                    database: str = 'neo4j') -> Dict[str, Any]:
    """用**仓库内置的完整交付集**重建全量图（先清空、再装载、最后按真实计数校验）。

    幂等：每次都先 `wipe()` 再 `load()`，因此重复调用不会叠加节点/关系。
    连不上 Neo4j / 缺驱动 / 装载后计数不一致时**抛 RuntimeError**（由调用方决定
    是中断还是降级），不会静默地把图留在半成品状态。
    """
    t0 = time.time()
    bng = _load_build_script()
    src = bng.load_source()                       # graph.db 优先，缺失回退交付表 CSV
    wiped = bng.wipe(neo4j_uri, user, password, database)
    try:
        counts = bng.load(src, neo4j_uri, user, password, database)
    except SystemExit as e:                       # 脚本的「连不上」出口
        raise RuntimeError(f'Neo4j 装载失败：{e}') from None
    shortfall = counts.get('_shortfall') or {}
    if shortfall:
        # 有关系因 MATCH 未命中被静默丢弃 -> 图是残缺的，必须报错而不是当成功
        raise RuntimeError(f'Neo4j 装载后计数不一致（关系被静默丢弃）：{shortfall}')
    gap = counts.get('_winners_gap') or []
    if gap:
        # 有中标方的公告在图上没有中标参与关系 -> S1/S4 会漏供应商，视为装载失败
        raise RuntimeError(f'Neo4j 图缺少中标方关系（n_winners>0 但无 is_winner=1 的 '
                           f'PARTICIPATES）：{len(gap)} 篇，前 5：{gap[:5]}')
    nodes, rels = graph_counts(neo4j_uri, user, password, database)
    return {
        'nodes': nodes,
        'rels': rels,
        'total': nodes + rels,
        'wiped': wiped,
        'shortfall': shortfall,
        'winners_coverage': counts.get('_winners_coverage'),
        'winners_gap': gap,
        'source_rows': {k: len(v) for k, v in src.items() if not k.startswith('_')},
        'elapsed_sec': round(time.time() - t0, 2),
    }
