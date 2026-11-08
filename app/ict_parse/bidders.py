# -*- coding: utf-8 -*-
"""投标人名单（参与人）抽取 —— 官方「投标人表」的**第三类行来源**。

背景（已确认真缺口）
--------------------
上传「附件包」数据集（路线④：无 HTML、无 `task2_bids.csv`）时，投标人表的行原先只有两个来源：
「标的表的供货方（= 中标方）」+「`bid` 投标关系表」。这批数据没有 `bid` 表，于是只剩中标方
（实测：总表 182 行 / 标的表 586 行 / **投标人表 182 行**，类别 = 中标人 179 / 参与人 3）。
但附件里几乎总有《开标一览表》《报价明细》《评审结果（得分汇总）》《资格审查名单》
《未中标（成交）原因表》—— 这些表**逐行列出全部投标人**，正是「参与人」的来源。

本模块只做一件事：**从已经解析出来的表格里认出这几类表，并把每行读成一个投标人**。
它不做「谁中标」的判定（那是 `official_tables.build_tables()` 的职责），也不写任何表。

判据（三条，缺一不可）
----------------------
1. **名称列**：表头含 `投标人 / 供应商 / 投标单位 / 响应人`（含 `投标人名称`、`中标供应商名称`
   这类写法），且该列名不含 `地址 / 信用代码 / 电话 / 联系人 / 开户 / 账号 / 邮编 / 邮箱`；
2. **佐证列**：表头至少含下列之一 —— `报价`（投标报价/评审报价/单价报价…）、`得分`、`总分`、
   `排名`/`排序`、`审查结果`/`资格审查`/`符合性`、`评审结果`、`未中标原因`；
   这一条把「主要标的信息表」「中标结果表」这类**没有投标人证据列**的表挡在外面；
3. **反证列**（防「预算表 / 需求表 / 报价明细表」误判）：表里若出现
   `品牌 / 规格型号 / 型号 / 制造商 / 产地 / 技术参数 / 技术要求 / 数量 / 计量单位 /
   产品名称 / 货物名称 / 标的名称 / 服务名称 / 采购标的 / 品目名称 / 采购内容 / 需求`
   之一 —— 那是「一行一个货物/需求」的明细表，**整表丢弃**（宁可少抽，不可把货物当投标人）。

每行还要过「像不像市场主体名」的判据（复用 `extract._valid_entity`），并排除
`合计/小计/总计/备注/说明/评审专家/采购人` 等占位值、包标识（`包1`/`二标段`）、表头泄漏值。
名称一律按 `official_tables.norm_name` 归一（NFKC + 去空白 + 去括号分隔符 + 大写），
与三张表其它来源（标的供货方、bid 关系名、公告 winners）**同一口径**，去重才不会漏。

抽到的字段（有就填、没有留空，绝不臆造）
----------------------------------------
    name            投标人名称（原文写法，已去首尾空白）
    package_label   该行自带的包标识原文（`标项号/包号/标段/包名称` 列；没有则空）
    quote_yuan      报价（**元**；单元格/表头自带 万元/亿元 会换算，单位判据见 `quote_yuan`）
    score           得分（float，两位小数；口径同 `official_tables._score_value`）
    review          `'pass'` / `'fail'` / `''`（资格审查/符合性/评审结论列）
    review_reason   未通过的原因原文（`未通过资格审查原因`/`被拒绝原因` 列）
    lose_reason     未中标（成交）原因原文（`未中标原因` 列）
    source          该行来自哪个表（HTML 正文表 / 附件文件名）

用法
----
    from ict_parse import bidders
    rows = bidders.roster_from_rec(rec)          # rec = 解析层那篇公告的 IR dict
    rows = bidders.roster_from_tables(atts)      # 只要附件表格也行（list[list[str]]）
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .extract import _valid_entity, norm_money
from .official_tables import (_NOT_SCORE_RE, _PKG_TOKEN_RE, _SCORE_KEYS, _clean_reason,
                              _is_fail, _is_pass, _nh, _score_value, norm_base_name,
                              norm_name)

# ---- 1) 名称列 ----
NAME_KW = ("投标人", "供应商", "投标单位", "响应人", "投标商", "报价人", "竞标人", "投标方")
NAME_EXCL = ("地址", "信用代码", "统一社会信用代码", "电话", "联系人", "联系方式",
             "开户", "账号", "邮编", "邮箱", "法定代表", "编码", "备注")

# ---- 2) 佐证列 ----
CORROB_KW = ("报价", "得分", "总分", "排名", "排序", "审查结果", "资格审查", "符合性",
             "未中标原因", "未成交原因", "评审结果", "结论")

# ---- 3) 反证列（明细/需求/预算表）----
DETAIL_KW = ("品牌", "规格型号", "规格", "型号", "制造商", "产地", "技术参数", "技术要求",
             "参数要求", "性能指标", "数量", "计量单位", "产品名称", "货物名称", "标的名称",
             "服务名称", "采购标的", "品目名称", "采购内容", "需求", "预算", "最高限价",
             "控制价")

# ---- 报价列：`列优先级` 越小越优先（投标报价 / 评审报价 优先于单价报价）----
QUOTE_KEYS: tuple = (
    (0, ("投标报价", "投标总价", "响应报价", "开标报价", "报价总计", "总报价", "投标价")),
    (1, ("评审报价", "开标一览", "报价")),
    (2, ("单价报价", "综合单价", "分项报价")),
)
# 含这些词的列**不是**该投标人的报价（得分/费率/工期/数量/备注…）
NOT_QUOTE_KW = ("得分", "评分", "分值", "折扣", "优惠率", "费率", "比例", "下浮",
                "工期", "期限", "数量", "单位", "权重", "保证金",
                "备注", "说明", "排名", "排序", "名次")

# ---- 包标识列 ----
PKG_COL_KW = ("标项号", "标项名称", "标包", "标段", "分包", "包组", "包件", "包号",
              "合同包", "采购包", "包名称")

_ORDINAL_RE = re.compile(
    r"^\s*(?:[（(]\s*[0-9０-９一二三四五六七八九十]{1,3}\s*[）)]"
    r"|[0-9０-９]{1,3}\s*[、,，.．)）:：]"
    r"|[0-9０-９]{1,3}\s+)")
_UNIT_RE = re.compile(r"(亿元|万元|亿|万|元|圆)")
_RATE_GUARD_RE = re.compile(r"[%％]|折扣|优惠率|费率|下浮|浮动率|折扣率|权重")
_SPLIT_NAMES_RE = re.compile(r"[、；;]")
# 「名字」明显不是投标人的占位值
_JUNK_NAMES = {
    "合计", "小计", "总计", "合计金额", "总合计", "合价", "备注", "无", "-", "—", "－", "/", "…",
    "序号", "说明", "投标人", "供应商", "投标单位", "响应人", "投标人名称", "供应商名称",
    "投标单位名称", "响应人名称", "中标供应商", "中标人", "成交供应商", "评审专家",
}
# 「名字」里出现这些词 -> 不是市场主体名（文档条款/表头泄漏/占位值）
# 注意：`_valid_entity` 的弱判据只要求「含机构词」（`_CO_SUFFIX` 里有「联合体」），
# 实测把「本项目对于联合体的要求」当成了投标人 —— 这里补一层**文档措辞**黑名单。
_JUNK_IN_NAME = ("合计", "小计", "总计", "评审专家", "评标委员会", "磋商小组", "采购人",
                 "采购单位", "招标人", "备注", "说明", "序号", "见附件", "详见", "无此",
                 "未通过", "未中标", "原因", "本项目", "适用于", "要求", "须知", "条款",
                 "情形", "承诺", "响应文件", "投标文件", "招标文件")


def _unit_of(text: str) -> str:
    """一段文本里的金额单位（`亿元/万元/亿/万/元/圆`），没有则空串。"""
    m = _UNIT_RE.search(unicodedata.normalize("NFKC", str(text or "")))
    return m.group(1) if m else ""


def _unit_factor(unit: str) -> float:
    return {"亿元": 1e8, "亿": 1e8, "万元": 1e4, "万": 1e4, "元": 1.0, "圆": 1.0}.get(unit, 1.0)


def _cell(row: Sequence, i: Optional[int]) -> str:
    if i is None or i >= len(row):
        return ""
    return unicodedata.normalize("NFKC", str(row[i] if row[i] is not None else "")).strip()


def quote_yuan(cell, header: str = "") -> Optional[float]:
    """一行里的报价 -> **元**（float）；不是明确金额则 None。

    单位判据（逐级，全部有据可依）：
      1. 单元格自带单位（`12.5万元` / `1000元` / `1.2亿`）-> 按它换算（`extract.norm_money` 同一口径）；
      2. 否则看**表头**单位（`评审报价（万元）` / `单价报价（元）`）；
      3. 都没有单位 -> 按量级保守判断：`>= 1 万` 视为**元**，`< 1 万` 视为**万元**。
         依据：政府采购单个合同包的报价以「元」计几乎都 ≥ 1 万；以「万元」计几乎都 < 1 万。
      含 `%`/折扣/费率/优惠率/权重等的单元格**一律不当金额**（宁缺勿造）。
    """
    txt = str(cell or "").strip()
    if not txt or _RATE_GUARD_RE.search(unicodedata.normalize("NFKC", txt)):
        return None
    val = norm_money(txt)                       # 元（无单位时按元返回原始数值）
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if _unit_of(txt):
        return round(v, 6)                      # 单元格自带单位 -> norm_money 已换算好
    hu = _unit_of(header)
    if hu:
        return round(v * _unit_factor(hu), 6)   # 表头带单位 -> 乘回
    return round(v if v >= 10000.0 else v * 10000.0, 6)


def _name_ok(v: str) -> bool:
    """单元格是否是一个「像市场主体名」的投标人（防占位值/表头/包标识混入）。"""
    s = str(v or "").strip()
    if not s or s in _JUNK_NAMES:
        return False
    if not (4 <= len(s) <= 80):
        return False
    if _PKG_TOKEN_RE.match(s.replace(" ", "")):
        return False
    if any(k in s for k in _JUNK_IN_NAME):
        return False
    return bool(_valid_entity(s))


def _clean_name(v: str) -> str:
    """投标人名称清洗：去首尾空白、压空白、剥段首序号（`1、XX公司`）。"""
    s = re.sub(r"[\s\u3000]+", " ", unicodedata.normalize("NFKC", str(v or ""))).strip()
    m = _ORDINAL_RE.match(s)
    if m and len(s) - m.end() >= 4:
        s = s[m.end():].strip()
    return s


def split_names(v: str) -> List[str]:
    """一个单元格里的多个投标人（`A有限公司、B有限公司`）-> 名字列表（不猜，只按分隔符切）。"""
    s = _clean_name(v)
    if not s:
        return []
    parts = [p.strip() for p in _SPLIT_NAMES_RE.split(s) if p.strip()]
    if len(parts) >= 2 and all(_name_ok(p) for p in parts):
        return parts
    return [s]


def _roles(headers: Sequence) -> Optional[dict]:
    """表头 -> 角色下标；**不是投标人名单表**返回 None。

    `{name, quote, score, review, fail_reason, lose_reason, pkg}`
    判据见模块 docstring（名称列 + 佐证列 + 反证列）。
    """
    hs = [_nh(h) for h in headers]
    name_i = None
    for i, h in enumerate(hs):
        if not h or any(x in h for x in NAME_EXCL):
            continue
        if any(k in h for k in NAME_KW):
            name_i = i
            break
    if name_i is None:
        return None
    others = [(i, h) for i, h in enumerate(hs) if i != name_i and h]
    if not any(any(k in h for k in CORROB_KW) for _i, h in others):
        return None
    if any(any(k in h for k in DETAIL_KW) for _i, h in others):
        return None

    roles: dict = {"name": name_i, "quote": None, "score": None, "review": None,
                   "fail_reason": None, "lose_reason": None, "pkg": None}
    # 报价列（按优先级；同优先级取最左）
    best = None
    for pri, keys in QUOTE_KEYS:
        for i, h in others:
            if any(k in h for k in NOT_QUOTE_KW):
                continue
            if any(k in h for k in keys):
                if best is None or pri < best[0] or (pri == best[0] and i < best[1]):
                    best = (pri, i)
        if best is not None:
            break
    roles["quote"] = best[1] if best else None
    # 得分列：`official_tables._SCORE_KEYS` 同一套优先级（综合得分/评审总得分 > 评审得分 > 分项），
    # 并排除「评审报价/中标金额/优惠率」等**非得分**列（与 official_tables 同一判据）。
    score_pri: Dict[int, int] = {}
    for i, h in others:
        if _NOT_SCORE_RE.search(h):
            continue
        for pri, keys in _SCORE_KEYS:
            if any(k in h for k in keys):
                score_pri[i] = pri
                break
    if score_pri:
        roles["score"] = min(score_pri, key=lambda i: (score_pri[i], i))
    for i, h in others:
        # 审查 / 评审结论列（不能含「原因/理由」，那不是结论）
        if roles["review"] is None and not any(k in h for k in ("原因", "理由")):
            if any(k in h for k in ("审查结果", "资格审查", "符合性", "评审结果",
                                    "审核结果", "是否通过", "结论", "推荐排名")):
                if not any(k in h for k in ("得分", "评分", "分值", "报价")):
                    roles["review"] = i
        # 未通过原因列
        if roles["fail_reason"] is None and ("原因" in h or "理由" in h):
            if any(k in h for k in ("未通过", "不通过", "不合格", "未入围", "废标",
                                    "无效", "被拒绝", "拒绝")):
                roles["fail_reason"] = i
        # 未中标（成交）原因列
        if roles["lose_reason"] is None and any(k in h for k in ("未中标", "未成交", "落标", "废标")):
            if "原因" in h or "理由" in h:
                roles["lose_reason"] = i
        # 包标识列
        if roles["pkg"] is None and any(k in h for k in PKG_COL_KW):
            roles["pkg"] = i
    # 「结论」列不能同时被当成未中标原因
    if roles["lose_reason"] is not None and roles["lose_reason"] == roles["review"]:
        roles["lose_reason"] = None
    return roles


_VERDICT_ONLY_RE = re.compile(r"^(?:未|不)?(?:通过|合格)|^废标|^无效|^否决|^取消.{0,4}资格")


def _fail_reason(res_txt: str, fail_txt: str) -> str:
    """审查结论「未通过」的原因原文：优先专用原因列，其次结论列里结论之外的部分。

    `资格审查结果` 列常常只写一个「未通过」——**这本身不是原因**，不能写成
    「未通过，未通过」（原实现会这样）。只有结论列除结论外还写了别的话才照录。
    """
    r = _clean_reason(fail_txt)
    if r and not _VERDICT_ONLY_RE.match(r):
        return r
    cand = _VERDICT_ONLY_RE.sub("", _clean_reason(res_txt)).strip("：:，,、;；。 ")
    return cand if len(cand) >= 2 else ""


def _row_record(row: Sequence, roles: dict, source: str, headers: Sequence = ()) -> List[dict]:
    """一行 -> 0..N 条投标人记录（一个单元格里可能有多个名字）。"""
    out: List[dict] = []
    quote_hdr = _cell(headers, roles["quote"]) if roles.get("quote") is not None else ""
    for nm in split_names(_cell(row, roles["name"])):
        if not _name_ok(nm):
            continue
        review, reason = "", ""
        rv = _cell(row, roles["review"])
        fr = _cell(row, roles["fail_reason"])
        if _is_fail(rv) or _is_fail(fr):
            review = "fail"
            reason = _fail_reason(rv, fr)
        elif _is_pass(rv):
            review = "pass"
        out.append({
            "name": nm,
            "package_label": _cell(row, roles["pkg"]),
            "quote_yuan": quote_yuan(_cell(row, roles["quote"]), quote_hdr),
            "score": _score_value(_cell(row, roles["score"])) if roles["score"] is not None else None,
            "review": review,
            "review_reason": reason,
            "lose_reason": _clean_reason(_cell(row, roles["lose_reason"]))
            if roles["lose_reason"] is not None else "",
            "source": source,
        })
    return out


def _rows_of(t) -> List[List[str]]:
    """把一张表（list[list] 或 `{'headers','rows'}`）统一成「列表的列表」。"""
    if isinstance(t, dict):
        rows = [[("" if c is None else str(c)) for c in (t.get("headers") or [])]]
        rows += [[("" if c is None else str(c)) for c in r] for r in (t.get("rows") or [])]
        return rows
    out: List[List[str]] = []
    for r in t or []:
        if isinstance(r, dict):
            out.append([("" if v is None else str(v)) for v in r.values()])
        else:
            out.append([("" if c is None else str(c)) for c in (r or [])])
    return out


def roster_from_table(t, source: str = "", header_scan: int = 6) -> List[dict]:
    """一张表 -> 投标人记录列表（表头可能在前几行，逐行扫到「像名单表头」的那一行为止）。"""
    rows = _rows_of(t)
    if len(rows) < 2:
        return []
    hi, roles = None, None
    for i in range(min(header_scan, len(rows))):
        got = _roles(rows[i])
        if got is not None:
            hi, roles = i, got
            break
    if roles is None:
        return []
    out: List[dict] = []
    for r in rows[hi + 1:]:
        if not any(str(c).strip() for c in r):
            continue
        out.extend(_row_record(r, roles, source, rows[hi]))
    return out


def roster_from_tables(tables: Iterable, source: str = "") -> List[dict]:
    """多张表 -> 投标人记录（不做跨表合并，交给 `merge_rows`）。"""
    out: List[dict] = []
    for t in tables or ():
        out.extend(roster_from_table(t, source=source))
    return out


def _attr(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def pipe_rows(text: str) -> List[List[str]]:
    """「管道符文本」-> 表格行（`a | b | c`）。

    为什么需要它：`.xlsx` 附件的 `attach._do_xlsx()` 除 `tables` 外还产出**文本**（每行
    `a | b | c`），`docx` 的段落里也有同样形态；而**已经落盘的历史 `ir.json`**（`out/full`）
    里的 xlsx 只有文本、没有 `tables`。这里把文本按管道符还原成表，判据（表头 / 反证列 /
    实体名）与 `tables` 路径**完全一致**，两条来源合起来才不漏。
    """
    out: List[List[str]] = []
    for line in str(text or "").splitlines():
        s = line.strip()
        if not s or "|" not in s:
            continue
        if set(s) <= set("|-: "):
            continue
        out.append([c.strip() for c in s.strip("|").split("|")])
    return out


def _sheet_blocks(text: str) -> List[str]:
    """xlsx 文本按 `[sheet]` 分块（每个 sheet 一张表，避免把下一张表的表头当数据行）。"""
    blocks: List[str] = []
    cur: List[str] = []
    for line in str(text or "").splitlines():
        if line.strip().lower().startswith("[sheet]"):
            if cur:
                blocks.append("\n".join(cur))
            cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks or [str(text or "")]


def roster_from_rec(rec: dict) -> List[dict]:
    """一篇公告的 IR -> 投标人记录（**同一 (名称, 包标识) 已合并**，可直接落 CSV/入库）。

    数据源三处，都是「已经解析落盘的内容」：
      * `rec['main_item_tables']`：公告正文里的表（HTML 路由的表格，含评审结果/资格审查表）；
      * `rec['extracted_files'][*]['tables']`：附件解析出的表（pdf/docx 类的开标一览表 /
        报价明细 / 评审结果 / 资格审查名单 / 未中标原因表 —— 正是本缺陷要补的那一批）；
      * `rec['extracted_files'][*]['text']`：xlsx 类附件只有文本（每行 `a | b | c`），
        按管道符还原成表再判一次（`pipe_rows` / `_sheet_blocks`）。
    表格顺序即原文顺序：合并时**首现优先**（后面的表只补前面没有的字段）。
    """
    out: List[dict] = []
    for g in (rec.get("main_item_tables") or []):
        out.extend(roster_from_table(g, source="html"))
    for f in (rec.get("extracted_files") or []):
        src = str(_attr(f, "name") or _attr(f, "rel_path") or "attachment")
        for t in (_attr(f, "tables") or []):
            out.extend(roster_from_table(t, source=src))
        text = _attr(f, "text") or ""
        if text and "|" in text:
            for block in _sheet_blocks(text):
                rows = pipe_rows(block)
                if rows:
                    out.extend(roster_from_table(rows, source=src))
    return merge_rows(out)


def merge_rows(rows: Iterable[dict]) -> List[dict]:
    """按 `(归一名称, 归一包标识)` 合并：同一人出现在多张表里 -> 一行，字段取「首现非空」。

    这是「开标一览表给报价、评审结果表给得分、资格审查表给结论、未中标原因表给原因」
    四条证据合成一个人的关键 —— 它们本来就是同一个投标人的不同侧面。
    """
    out: List[dict] = []
    idx: Dict[Tuple[str, str], dict] = {}
    for r in rows or ():
        nm = str(r.get("name") or "").strip()
        if not nm:
            continue
        key = (norm_base_name(nm) or norm_name(nm),
               norm_name(str(r.get("package_label") or "")))
        cur = idx.get(key)
        if cur is None:
            cur = {"name": nm, "package_label": str(r.get("package_label") or ""),
                   "quote_yuan": None, "score": None, "review": "", "review_reason": "",
                   "lose_reason": "", "source": str(r.get("source") or "")}
            idx[key] = cur
            out.append(cur)
        for k in ("quote_yuan", "score", "review", "review_reason", "lose_reason"):
            if cur.get(k) in (None, "") and r.get(k) not in (None, ""):
                cur[k] = r.get(k)
    return out
