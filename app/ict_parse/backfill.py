# -*- coding: utf-8 -*-
"""归位式回填（**只补空值、不覆盖、逐条留来源凭证**）。

背景
----
LLM 抽取是**主路径**，但它只写给它的那几个字段：`announcement.purchaser` /
`winners` / `bidders` / `project_amount` / `publish_time` 里只要有一项 LLM 没抽到，
下游（`items_task1` / `announcements_task1` / 官方三表 / 平台库 / 图库）就一直缺着 ——
而这些值常常**就躺在已经解析落盘的原文里**（`ir.json` 的 `pub_date` / `content_text` /
`summary` / 附件名单表、交付集自带的 `task2_bids.csv`）。

本模块只做一件事：**把这些"已经有证据"的值，放回它该在的字段里**（归位 = backfill）。
它不猜、不合并跨公告数据、不动已有非空值，并且每一条改动都写进侧车凭证文件。

归位规则（唯一实现；优先级从高到低，命中即止）
--------------------------------------------
============  ==============================================================  ====================
目标字段       证据（按优先级）                                                 来源标签
============  ==============================================================  ====================
`purchaser`   ① `ir.json` 正文「采购人/采购单位/招标人：X」                      `RULE_TEXT`
              ② `items_llm.json#announcement.purchaser`（LLM 抽到但没落到        `LLM_FIELD`
                 目标公告对象上时）
`winners`     ① `items_llm.json#announcement.winners`（LLM 已抽到）              `LLM_WINNERS`
              ② 附件名单表（`bidders.roster_from_rec`）里 `类别=中标人` 的名字    `ATTACHMENT_NAMELIST`
              ③ `task2_bids.csv` 的 `is_winner=1`（**仅当 UUID 精确命中**）      `BID_TABLE`
`bidders`     ① 附件名单 `类别=参与人`                                          `ATTACHMENT_NAMELIST`
              ② `task2_bids.csv` `is_winner=0`（**仅当 UUID 精确命中**）         `BID_TABLE`
`project_amount` ① 正文「中标金额/成交金额：X」（`summary` 的显式金额键优先）    `RULE_TEXT`
              ② 标的行合计（仅当本篇确有标的金额，且全文无"预算/最高限价/控制价"  `ITEM_SUM`
                 字样）
公告日期       ① `ir.json` 的发布时间字段（`pub_date`，其次 `summary` 的公告时间） `RULE_TEXT`
（`publish_time`）
============  ==============================================================  ====================

硬性约束（与规则同等重要）
--------------------------
1. **绝不覆盖已有非空值**：目标字段非空就跳过（LLM 有值就保留，一个字节都不改）。
2. **绝不跨公告串数据**：`task2_bids.csv` 只按**精确 UUID**（`notice_id`）查一次
   `dict`；查不到就跳过。绝不按名称/项目名/模糊匹配找别的公告的行。
3. **不臆造**：没有证据就留空；多个互相矛盾的证据（例如正文里既有"总中标金额"又有
   分包金额）一律不填，只记一条"未采信"计数。
4. **逐条凭证**：每条回填写一行到侧车文件 `deliverables/backfill_provenance.csv`
   （列：`announcement_id, field, old_value, new_value, source_tag, evidence_locator`），
   `evidence_locator` 形如 `out/full/<id>/items_llm.json#announcement.winners`、
   `out/full/<id>/ir.json#pub_date`、`task2_bids.csv:194`。
5. **开关**：`EDM_BACKFILL=1`（默认开）/ `EDM_BACKFILL_DRYRUN=1`（只算不写，
   连侧车文件也不写）。

记录契约（与上传管线 `process_one()` 的返回结构一致）
----------------------------------------------------
`records` 是 dict 列表，每个 dict = **一篇公告的 IR**（顶层 `content_text` /
`summary` / `pub_date` / `extracted_files` / `main_item_tables` …）+ 两个键：

    ``extraction``        主抽取结果 `{announcement, items, engine}` —— **回填目标**
    ``bidder_roster``     可选，已抽好的投标人名单（缺省时用 `bidders.roster_from_rec`）
    ``_origin``           可选，凭证路径前缀（缺省 `out/full/<公告号>`）

回填只改 `extraction.announcement` 里的那 5 个字段；`items` / `engine` 一字不动。

用法
----
    from ict_parse import backfill
    rep = backfill.apply(recs, scope={r['announcement_id'] for r in recs})
    print(rep.message())            # 归位补全：采购方 3、中标供应商 5（LLM 4/附件 1）

全量路径见 `scripts/backfill_deliverables.py`。
"""
from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import bidders
from .clean import clean_purchaser
from .official_tables import norm_base_name, norm_name

# ==================================================================
# 常量：来源标签 / 字段名 / 中文名
# ==================================================================

SRC_RULE_TEXT = "RULE_TEXT"
SRC_LLM_FIELD = "LLM_FIELD"
SRC_LLM_WINNERS = "LLM_WINNERS"
SRC_ATTACHMENT = "ATTACHMENT_NAMELIST"
SRC_BID_TABLE = "BID_TABLE"
SRC_ITEM_SUM = "ITEM_SUM"

F_PURCHASER = "purchaser"
F_WINNERS = "winners"
F_BIDDERS = "bidders"
F_AMOUNT = "project_amount"
# 「公告日期」在数据模型里的字段名是 `publish_time`（`announcements_task1.csv` 的列名、
# `t1_notice` 的列名都是它）—— 归位必须落到**真实存在的列**上，故用 `publish_time`。
F_PUBLISH = "publish_time"

FIELD_ORDER = (F_PURCHASER, F_WINNERS, F_BIDDERS, F_AMOUNT, F_PUBLISH)
FIELD_CN = {
    F_PURCHASER: "采购方",
    F_WINNERS: "中标供应商",
    F_BIDDERS: "参与人",
    F_AMOUNT: "项目金额",
    F_PUBLISH: "公告日期",
}
TAG_CN = {
    SRC_RULE_TEXT: "正文",
    SRC_LLM_FIELD: "LLM",
    SRC_LLM_WINNERS: "LLM",
    SRC_ATTACHMENT: "附件",
    SRC_BID_TABLE: "投标表",
    SRC_ITEM_SUM: "标的合计",
}
# 目标字段 -> 该字段允许的来源标签（用于统计口径自证）
FIELD_TAGS = {
    F_PURCHASER: (SRC_RULE_TEXT, SRC_LLM_FIELD),
    F_WINNERS: (SRC_LLM_WINNERS, SRC_ATTACHMENT, SRC_BID_TABLE),
    F_BIDDERS: (SRC_ATTACHMENT, SRC_BID_TABLE),
    F_AMOUNT: (SRC_RULE_TEXT, SRC_ITEM_SUM),
    F_PUBLISH: (SRC_RULE_TEXT,),
}

PROVENANCE_COLS = ["announcement_id", "field", "old_value", "new_value",
                   "source_tag", "evidence_locator"]

# 官方三表里「投标人表.类别」的两个取值（与 official_tables 同一份词）
LABEL_WINNER = "中标人"
LABEL_BIDDER = "参与人"

# 「预算/最高限价」字样 —— 出现即**禁止**用标的行合计当项目金额（用户明确要求）
BUDGET_WORDS = ("预算", "最高限价", "控制价", "拦标价", "招标控制价", "限价")

_APP_DIR = Path(__file__).resolve().parents[1]          # .../app
_DELIVERABLES = _APP_DIR / "deliverables"
_BIDS_CSV = _DELIVERABLES / "task2_bids.csv"
_FULL_DIR = _APP_DIR / "out" / "full"


# ==================================================================
# 开关 / 路径
# ==================================================================

def _flag(name: str, default: bool) -> bool:
    """环境变量开关：`1/true/yes/on` 为真，`0/false/no/off` 为假，缺省用 `default`。"""
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def enabled() -> bool:
    """总开关 `EDM_BACKFILL`（默认**开**）。关掉时 `apply()` 直接返回空报告。"""
    return _flag("EDM_BACKFILL", True)


def dry_run_default() -> bool:
    """`EDM_BACKFILL_DRYRUN=1` -> 只算不写（目标字段不落地、侧车文件不写）。"""
    return _flag("EDM_BACKFILL_DRYRUN", False)


def provenance_path() -> Path:
    """侧车凭证文件（可用 `EDM_BACKFILL_PROVENANCE` 覆盖，测试指到临时目录）。"""
    p = (os.getenv("EDM_BACKFILL_PROVENANCE") or "").strip()
    return Path(p) if p else _DELIVERABLES / "backfill_provenance.csv"


# ==================================================================
# 数据结构
# ==================================================================

@dataclass
class Patch:
    """一条「归位」：把 `field` 从 `old_value` 补成 `new_value`，来源 = `source_tag`。"""

    announcement_id: str
    field: str
    old_value: str
    new_value: str
    source_tag: str
    evidence_locator: str
    applied: bool = False

    def as_row(self) -> dict:
        return {
            "announcement_id": self.announcement_id,
            "field": self.field,
            "old_value": self.old_value,
            "new_value": _cell(self.new_value),
            "source_tag": self.source_tag,
            "evidence_locator": self.evidence_locator,
        }


@dataclass
class Report:
    """一次回填的计划/执行结果（dry-run 与 apply 用同一份结构，便于逐条对比）。"""

    patches: List[Patch] = field(default_factory=list)
    skipped_has_value: Dict[str, int] = field(default_factory=dict)   # 已有非空值 -> 未触碰
    skipped_no_evidence: Dict[str, int] = field(default_factory=dict)  # 空中/无证据 -> 留空
    rejected: Dict[str, int] = field(default_factory=dict)            # 有证据但不采信
    n_records: int = 0
    n_scope: int = 0
    dry_run: bool = True
    disabled: bool = False
    provenance_written: int = 0
    provenance_file: str = ""

    # ---- 统计 ----
    def field_counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for p in self.patches:
            out[p.field] = out.get(p.field, 0) + 1
        return out

    def tag_counts(self) -> Dict[Tuple[str, str], int]:
        out: Dict[Tuple[str, str], int] = {}
        for p in self.patches:
            k = (p.field, p.source_tag)
            out[k] = out.get(k, 0) + 1
        return out

    def total(self) -> int:
        return len(self.patches)

    def message(self) -> str:
        """一句话统计（拼进 job message / CLI 输出）。

        形如 `归位补全：采购方 3、中标供应商 5（LLM 4/附件 1）`；没有可补时返回 `''`。
        dry-run 时前缀带 `（dry-run 未落地）`，**绝不**让用户误以为已经写进库。
        """
        fc = self.field_counts()
        if not fc:
            return ""
        parts: List[str] = []
        for f in FIELD_ORDER:
            n = fc.get(f) or 0
            if not n:
                continue
            tags = [(t, c) for (ff, t), c in self.tag_counts().items() if ff == f and c]
            tags.sort(key=lambda x: (-x[1], x[0]))
            detail = "、".join(f"{TAG_CN.get(t, t)} {c}" for t, c in tags if len(tags) > 1)
            parts.append(f"{FIELD_CN[f]} {n}" + (f"（{detail}）" if detail else ""))
        head = "归位补全（dry-run 未落地）：" if self.dry_run else "归位补全："
        return head + "、".join(parts)

    def lines(self, show_samples: int = 10) -> List[str]:
        """dry-run / apply 的标准报告（CLI 与上传日志共用同一份措辞）。"""
        out: List[str] = []
        tag_c = self.tag_counts()
        out.append(f"回填对象：{self.n_scope} 篇（候选 {self.n_records} 篇）"
                   f"{'｜DRY-RUN（不写任何东西）' if self.dry_run else '｜APPLY（已落地）'}")
        if self.disabled:
            out.append("EDM_BACKFILL=0：回填已关闭，未做任何事")
            return out
        fc = self.field_counts()
        out.append("可补条数（按字段 / 来源标签）：")
        for f in FIELD_ORDER:
            n = fc.get(f, 0)
            detail = "，".join(f"{TAG_CN.get(t, t)}={c}" for (ff, t), c
                              in sorted(tag_c.items()) if ff == f and c) or "—"
            out.append(f"  {FIELD_CN[f]:<6} +{n:<4} [{detail}]"
                       f"｜已有值跳过 {self.skipped_has_value.get(f, 0)}"
                       f"｜无证据留空 {self.skipped_no_evidence.get(f, 0)}"
                       + (f"｜不采信 {self.rejected[f]}" if self.rejected.get(f) else ""))
        out.append(f"  合计 +{self.total()} 条")
        if self.provenance_file:
            out.append(f"侧车凭证：{self.provenance_file}"
                       + ("（dry-run 未写入）" if self.dry_run
                          else f"（本次写入 {self.provenance_written} 行）"))
        if show_samples and self.patches:
            out.append(f"样例（前 {min(show_samples, self.total())} 条）：")
            for p in self.patches[:show_samples]:
                out.append(f"  {p.announcement_id} | {p.field} | 旧「{p.old_value}」"
                           f" -> 新「{_cell(p.new_value)[:60]}」 | {p.source_tag}"
                           f" | {p.evidence_locator}")
        return out


# ==================================================================
# 小工具
# ==================================================================

def _cell(v) -> str:
    """把列表/标量统一成单元格文本（列表用 `；` 连接，与交付表口径一致）。"""
    if isinstance(v, (list, tuple)):
        return "；".join(str(x).strip() for x in v if str(x or "").strip())
    return "" if v is None else str(v).strip()


def _names(v) -> List[str]:
    """winners/bidders 的既有形态容错：None / 单串 / 列表 -> 去重后的名字列表。"""
    if v is None:
        return []
    if isinstance(v, str):
        items = [x for x in re.split(r"[；;、,，]\s*", v)]
    else:
        try:
            items = list(v)
        except TypeError:
            return []
    out: List[str] = []
    for x in items:
        s = str(x or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def _empty(v) -> bool:
    """字段是否为空（None / 空串 / 空列表 / 0 —— 0 元金额视同未填）。"""
    if v is None:
        return True
    if isinstance(v, dict):
        return not v
    if isinstance(v, (list, tuple, set)):
        return not _names(v)
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return float(v) == 0.0
    s = str(v).strip()
    if not s or s == "0":
        return True
    try:
        return float(s.replace(",", "")) == 0.0
    except (ValueError, TypeError):
        return False


def _origin(rec: dict, aid: str) -> str:
    """凭证里的路径前缀（上传路线用临时目录，全量路线用 `out/full/<公告号>`）。"""
    o = str(rec.get("_origin") or "").strip()
    return o.rstrip("/") if o else f"out/full/{aid}"


# ==================================================================
# 证据 ①：task2_bids.csv（只有精确 UUID 命中才算；构建一次并缓存）
# ==================================================================

_BIDS_CACHE: Dict[str, object] = {"mtime": None, "map": {}}


def _bids_map() -> Dict[str, List[dict]]:
    """`{notice_id: [行,...]}`，每行额外带 `_line`（1 起的行号，含表头，用于凭证）。

    **只按精确 `notice_id` 索引**，不带任何模糊/前缀/名称回退 —— 这是「不跨公告串数据」
    的物理保证：别的公告的行在这个 dict 里根本查不到。
    """
    p = Path(os.getenv("EDM_BACKFILL_BIDS") or _BIDS_CSV)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _BIDS_CACHE["mtime"], _BIDS_CACHE["map"] = None, {}
        return {}
    if _BIDS_CACHE["mtime"] == mtime:
        return _BIDS_CACHE["map"]                       # type: ignore[return-value]
    out: Dict[str, List[dict]] = {}
    with open(p, encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):   # 第 2 行 = 第一条数据
            aid = str(row.get("notice_id") or "").strip()
            if not aid:                                 # 没有公告号的行无法定位 -> 不参与
                continue
            row = dict(row)
            row["_line"] = i
            out.setdefault(aid, []).append(row)
    _BIDS_CACHE["mtime"], _BIDS_CACHE["map"] = mtime, out
    return out


def bids_for(aid: str) -> List[dict]:
    """该 UUID 的投标关系行（精确命中；没有就是空列表 —— 绝不拿别的公告的行）。"""
    return list(_bids_map().get(str(aid or "").strip()) or [])


# ==================================================================
# 证据 ②：附件名单表（bidders.roster_from_rec）
# ==================================================================

def roster_of(rec: dict) -> List[dict]:
    """本篇的投标人名单行（`类别` 由 `classify_roster` 现算，`roster_from_rec` 不产出它）。"""
    rows = rec.get("bidder_roster")
    if rows is None:
        try:
            rows = bidders.roster_from_rec(rec)
        except Exception:                              # noqa: BLE001 - 名单是可选证据
            rows = []
    return [r for r in (rows or []) if str(r.get("name") or "").strip()]


def classify_roster(rec: dict, aid: str) -> List[Tuple[dict, str]]:
    """名单行 -> `(行, 类别)`。类别口径 = 官方「投标人表.类别」，另加两道守卫：

    * `类别=中标人` 需要**本公告内**的中标证据：该名字出现在本篇标的行的
      `supplier_name` 里（标的一行 = 这个包由谁供什么，与 `official_tables` 的
      "标的表兜底"同一条口径），或该 UUID 的 `task2_bids.csv` 里 `is_winner=1`；
    * **反证只在"这份投标关系表确实标过中标方"时才成立**：若同一 UUID 的投标关系行里
      **存在** `is_winner=1`，则明确写 `is_winner=0` 的名字一律按 `参与人`（它是"没中标"
      的明确反证）；若**一条 `is_winner=1` 都没有**（该篇的投标关系表根本没有中标标记），
      这条 `is_winner=0` 不构成反证，否则"全部为 0"的整篇会永远补不出中标人；
    * 名单行自带 `review == 'fail'`（资格审查未通过）-> 一律 `参与人`；
    * 其余 = `参与人`（"参与了评审"这件事由名单表本身证明）。

    返回顺序 = 名单表出现顺序（凭证按这个顺序取 locator）。
    """
    items = (rec.get("extraction") or {}).get("items") or []
    item_norms: Set[str] = set()
    for it in items:
        for k in (norm_name(str(it.get("supplier_name") or "")),
                  norm_base_name(str(it.get("supplier_name") or ""))):
            if k:
                item_norms.add(k)
    win_norms: Set[str] = set()
    lose_norms: Set[str] = set()
    for b in bids_for(aid):
        nm = str(b.get("supplier") or "").strip()
        keys = {norm_name(nm), norm_base_name(nm)} - {""}
        if str(b.get("is_winner") or "").strip() == "1":
            win_norms |= keys
        elif str(b.get("is_winner") or "").strip() == "0":
            lose_norms |= keys
    marked = bool(win_norms)                     # 这份投标关系表是否标过中标方
    out: List[Tuple[dict, str]] = []
    for r in roster_of(rec):
        nm = str(r.get("name") or "").strip()
        keys = {norm_name(nm), norm_base_name(nm)} - {""}
        if str(r.get("review") or "").strip() == "fail":
            out.append((r, LABEL_BIDDER))              # 审查未通过 -> 不可能是中标人
            continue
        if marked and (keys & lose_norms) and not (keys & win_norms):
            out.append((r, LABEL_BIDDER))              # 明确 is_winner=0 -> 反证
            continue
        if (keys & win_norms) or (keys & item_norms):
            out.append((r, LABEL_WINNER))
        else:
            out.append((r, LABEL_BIDDER))
    return out


def _roster_locator(rec: dict, origin: str, source: str) -> str:
    """名单行的凭证位置：`.../ir.json#extracted_files[<文件>]` 或 `#main_item_tables`。"""
    src = str(source or "").strip()
    if not src or src == "html":
        return f"{origin}/ir.json#main_item_tables"
    for f in (rec.get("extracted_files") or []):
        nm = str(f.get("name") or f.get("rel_path") or "") if isinstance(f, dict) else ""
        if nm == src:
            return f"{origin}/ir.json#extracted_files[{src}].tables"
    return f"{origin}/ir.json#extracted_files[{src}].tables"


# ==================================================================
# 证据 ③：正文（ir.json）
# ==================================================================

# 「采购人：X」—— 冒号后取到行尾，再按分隔符/标签截断（正文里这些字段常连在一起写）
_PURCHASER_RE = re.compile(
    r"(?:采购人|采购单位|招标人|采购机构|采购组织)\s*(?:名称)?\s*[:：]\s*([^\n]{1,80})")
_PUR_STOP_RE = re.compile(r"[，,；;。]|地\s*址|联系人|联系电话|联系方式|邮\s*编|开户|统一社会信用代码")
_PUR_LABEL_RE = re.compile(r"^(?:名称|信息|如下|详见|见附件)")

# 「中标（成交）金额：X」—— 只认 中标/成交，**绝不匹配 预算/最高限价/控制价**
_AMT_LABEL_RE = re.compile(
    r"(?:中标|成交)\s*(?:[（(]\s*成交\s*[）)])?\s*(?:总)?\s*(?:金额|价款|总价)")
_NUM_UNIT_RE = re.compile(
    r"(?:人民币)?\s*[￥¥]?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(亿元|万元|亿|万|元)?")
_UNIT_FACTOR = {"亿元": 10 ** 8, "亿": 10 ** 8, "万元": 10 ** 4, "万": 10 ** 4, "元": 1, "": 1}


def _text_of(rec: dict) -> str:
    """正文文本：`content_text` + 各小节正文（HTML 路由里两者都有，去重后按原顺序拼）。"""
    parts: List[str] = []
    ct = str(rec.get("content_text") or "").strip()
    if ct:
        parts.append(ct)
    sections = rec.get("sections") or {}
    if isinstance(sections, dict):
        for v in sections.values():
            s = str(v or "").strip()
            if s and s not in parts:
                parts.append(s)
    return "\n".join(parts)


def _parse_purchaser(rec: dict, text: Optional[str] = None) -> Tuple[str, str]:
    """正文 -> `(采购方, 定位串)`；找不到返回 `("", "")`。"""
    text = _text_of(rec) if text is None else text
    if not text:
        return "", ""
    for m in _PURCHASER_RE.finditer(text):
        raw = m.group(1)
        cut = _PUR_STOP_RE.search(raw)
        val = (raw[:cut.start()] if cut else raw).strip()
        val = val.strip("：: 　\u3000")
        if not val or len(val) < 2 or len(val) > 60:
            continue
        if _PUR_LABEL_RE.match(val) or any(w in val for w in BUDGET_WORDS):
            continue
        cleaned = clean_purchaser(val)                 # 代码式（无中日韩字符）值一律不采信
        if not cleaned:
            continue
        line = text[:m.start()].count("\n") + 1
        return cleaned, f"#content_text:line{line}"
    return "", ""


def _money_from_text(txt: str) -> Optional[float]:
    """一段文本 -> 元（`325.031万元` -> 3250310.0）。无法确定单位/无数字返回 None。"""
    m = _NUM_UNIT_RE.search(str(txt or ""))
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "").strip()
    if not unit:
        # 没有单位：`中标金额 3250310` 这类**纯元**写法只有在小数位全为 0 时敢认，
        # 其余一律不采信（"325.03" 可能是万元，猜错就是 1e4 倍错误）。
        return num if num >= 1000 and float(num).is_integer() else None
    return num * _UNIT_FACTOR.get(unit, 1)


def _parse_amount_text(rec: dict, text: Optional[str] = None) -> Tuple[Optional[float], str, str]:
    """正文/摘要 -> `(元, 定位串, 状态)`；状态 = `ok` / `''`（无证据）/ `multi`（不采信）。"""
    summary = rec.get("summary") or {}
    if isinstance(summary, dict):
        for k in ("总中标金额", "中标金额", "成交金额", "中标（成交）金额", "中标(成交)金额"):
            if k in summary:
                v = _money_from_text(str(summary.get(k) or ""))
                if v:
                    return v, f"#summary.{k}", "ok"
        for k, v in summary.items():
            ks = str(k or "")
            if ("中标" in ks or "成交" in ks) and "金额" in ks:
                money = _money_from_text(str(v or ""))
                if money:
                    return money, f"#summary.{ks}", "ok"
    # ---- 正文逐行扫：同一行里既有金额标签、又没有"预算/最高限价"字样 ----
    hits: List[Tuple[float, int, bool]] = []
    body = _text_of(rec) if text is None else text
    for i, line in enumerate(body.splitlines(), start=1):
        if any(w in line for w in BUDGET_WORDS):
            continue                                   # 预算/控制价/最高限价 -> 绝不采信
        for m in _AMT_LABEL_RE.finditer(line):
            money = _money_from_text(line[m.end():])
            if money:
                total = any(w in line[max(0, m.start() - 6):m.end() + 2] for w in ("总", "合计"))
                hits.append((money, i, total))
                break
    if not hits:
        return None, "", ""
    uniq = {round(h[0], 2) for h in hits}
    if len(uniq) == 1:
        return hits[0][0], f"#content_text:line{hits[0][1]}", "ok"
    for money, line, total in hits:                    # 多值：只认写明"总/合计"的那一条
        if total:
            return money, f"#content_text:line{line}", "ok"
    return None, "", "multi"


def _parse_publish(rec: dict) -> Tuple[str, str]:
    """`ir.json` 的发布时间字段 -> `(发布时间的原文, 定位串)`。"""
    for key in ("pub_date", "publish_time", "publish_date"):
        v = str(rec.get(key) or "").strip()
        if v:
            return v, f"#{key}"
    summary = rec.get("summary") or {}
    if isinstance(summary, dict):
        for k in ("公告时间", "发布时间", "公告日期"):
            v = str(summary.get(k) or "").strip()
            if v:
                return v, f"#summary.{k}"
    return "", ""


def _item_sum(rec: dict) -> Tuple[Optional[float], str]:
    """标的行合计 -> `(元, 定位串)`；没有标的金额时返回 `(None, "")`。"""
    items = (rec.get("extraction") or {}).get("items") or []
    total = 0.0
    n = 0
    for i, it in enumerate(items):
        v = it.get("total_price")
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f <= 0:
            continue
        total += f
        n += 1
    if not n or total <= 0:
        return None, ""
    return round(total, 2), f"#items[total_price×{n}]"


# ==================================================================
# 归位规则（唯一实现）
# ==================================================================

def _plan_record(rec: dict, aid: str, rep: Report) -> List[Patch]:
    """一篇公告 -> 应回填的补丁列表（**不改任何数据**，只算）。"""
    ext = rec.get("extraction") or {}
    ann = ext.get("announcement") or {}
    origin = _origin(rec, aid)
    patches: List[Patch] = []
    # 正文/名单都是"贵操作"（正文拼串、附件表格扫描），一篇只算一次
    text = _text_of(rec)
    _classified: List[Tuple[dict, str]] = []

    def _roster() -> List[Tuple[dict, str]]:
        if not _classified:
            _classified.extend(classify_roster(rec, aid))
        return _classified

    def _skip(field_name: str) -> None:
        rep.skipped_has_value[field_name] = rep.skipped_has_value.get(field_name, 0) + 1

    def _noev(field_name: str) -> None:
        rep.skipped_no_evidence[field_name] = rep.skipped_no_evidence.get(field_name, 0) + 1

    def _reject(field_name: str) -> None:
        rep.rejected[field_name] = rep.rejected.get(field_name, 0) + 1

    # ---------------- 采购方 ----------------
    old_pur = ann.get(F_PURCHASER)
    if not _empty(old_pur):
        _skip(F_PURCHASER)
    else:
        val, loc = _parse_purchaser(rec, text)
        tag = SRC_RULE_TEXT
        if not val:
            llm = rec.get("llm_announcement") or {}
            if llm is not ann:
                cand = str((llm or {}).get(F_PURCHASER) or "").strip()
                if cand:
                    val, loc, tag = cand, f"{origin}/items_llm.json#announcement.purchaser", SRC_LLM_FIELD
        if val:
            patches.append(Patch(aid, F_PURCHASER, _cell(old_pur), val, tag,
                                 f"{origin}/ir.json{loc}" if loc.startswith("#") else loc))
        else:
            _noev(F_PURCHASER)

    # ---------------- 中标供应商 ----------------
    old_win = ann.get(F_WINNERS)
    win_norms: Set[str] = set()
    if not _empty(old_win):
        _skip(F_WINNERS)
        for n in _names(old_win):
            win_norms |= {norm_name(n), norm_base_name(n)}
    else:
        pick: Optional[Tuple[List[str], str, str]] = None
        # ① LLM 已抽到（目标公告对象与 LLM 输出不是同一个东西时才算"归位"）
        llm = rec.get("llm_announcement") or {}
        if llm is not ann:
            names = _names((llm or {}).get(F_WINNERS))
            if names:
                pick = (names, SRC_LLM_WINNERS, f"{origin}/items_llm.json#announcement.winners")
        # ② 附件名单表里 类别=中标人
        if pick is None:
            rows = [(r, lab) for r, lab in _roster() if lab == LABEL_WINNER]
            if rows:
                pick = ([str(r.get("name")).strip() for r, _ in rows], SRC_ATTACHMENT,
                        _roster_locator(rec, origin, rows[0][0].get("source")))
        # ③ task2_bids.csv is_winner=1（精确 UUID）
        if pick is None:
            bw = [b for b in bids_for(aid) if str(b.get("is_winner") or "").strip() == "1"]
            names = [str(b.get("supplier") or "").strip() for b in bw]
            names = [n for n in names if n]
            if names:
                pick = (names, SRC_BID_TABLE, f"task2_bids.csv:{bw[0].get('_line')}")
        if pick:
            names, tag, loc = pick
            for n in names:
                win_norms |= {norm_name(n), norm_base_name(n)}
            patches.append(Patch(aid, F_WINNERS, _cell(old_win), names, tag, loc))
        else:
            _noev(F_WINNERS)

    # ---------------- 参与人（投标人名单） ----------------
    old_bid = ann.get(F_BIDDERS)
    if not _empty(old_bid):
        _skip(F_BIDDERS)
    else:
        pick2: Optional[Tuple[List[str], str, str]] = None
        rows = [(r, lab) for r, lab in _roster() if lab == LABEL_BIDDER]
        names = [str(r.get("name")).strip() for r, _ in rows]
        names = [n for n in dict.fromkeys(names) if n and norm_name(n) not in win_norms]
        if names:
            pick2 = (names, SRC_ATTACHMENT,
                     _roster_locator(rec, origin, rows[0][0].get("source")))
        if pick2 is None:
            bl = [b for b in bids_for(aid) if str(b.get("is_winner") or "").strip() == "0"]
            names = [str(b.get("supplier") or "").strip() for b in bl]
            names = [n for n in dict.fromkeys(names) if n and norm_name(n) not in win_norms]
            if names:
                pick2 = (names, SRC_BID_TABLE, f"task2_bids.csv:{bl[0].get('_line')}")
        if pick2:
            names, tag, loc = pick2
            patches.append(Patch(aid, F_BIDDERS, _cell(old_bid), names, tag, loc))
        else:
            _noev(F_BIDDERS)

    # ---------------- 项目金额 ----------------
    old_amt = ann.get(F_AMOUNT)
    if not _empty(old_amt):
        _skip(F_AMOUNT)
    else:
        money, loc, state = _parse_amount_text(rec, text)
        if money:
            patches.append(Patch(aid, F_AMOUNT, _cell(old_amt), f"{money:.2f}", SRC_RULE_TEXT,
                                 f"{origin}/ir.json{loc}"))
        elif state == "multi":
            _reject(F_AMOUNT)                      # 正文里多个互不相同的金额 -> 不猜
        else:
            # ② 标的行合计：**唯一判据** = 本篇正文里没有任何"预算/最高限价/控制价"字样。
            #    为什么这么严：`预算金额` 与 `中标金额` 在同一个页面里长得一模一样，
            #    一旦把预算当成交金额写进总表，金额就整体错了（宁可留空）。
            budget = any(w in text for w in BUDGET_WORDS)
            total, iloc = _item_sum(rec)
            _noev(F_AMOUNT)
            if total and not budget:
                patches.append(Patch(aid, F_AMOUNT, _cell(old_amt), f"{total:.2f}", SRC_ITEM_SUM,
                                     f"{origin}/ir.json{iloc}"))
            elif total and budget:
                # 有标的金额、但正文出现预算/限价字样 -> 明确"不采信"（统计里单列）
                _reject(F_AMOUNT)

    # ---------------- 公告日期 ----------------
    old_pub = ann.get(F_PUBLISH)
    if not _empty(old_pub):
        _skip(F_PUBLISH)
    else:
        val, loc = _parse_publish(rec)
        if val:
            patches.append(Patch(aid, F_PUBLISH, _cell(old_pub), val, SRC_RULE_TEXT,
                                 f"{origin}/ir.json{loc}"))
        else:
            _noev(F_PUBLISH)

    return patches


# ==================================================================
# 对外入口
# ==================================================================

def apply(records: Sequence[dict], scope: Optional[Iterable[str]] = None, *,
          dry_run: Optional[bool] = None,
          write_provenance: bool = True) -> Report:
    """对 `records` 做归位式回填。

    * `scope`：本次要处理的公告号集合（**缺省 = 全部**）。上传路线传「本次上传的 UUID」，
      保证回填永远不会越界碰别的公告。
    * `dry_run`：缺省读 `EDM_BACKFILL_DRYRUN`；为真时**只算不写**（目标字段与侧车文件都不动）。
    * `write_provenance`：`False` 时不追加侧车文件（`--apply` 全量脚本自己控制写入时机）。

    返回 `Report`：`patches` = 逐条补丁（含 dry-run 的"计划"），`message()` 给 job message 用。
    """
    rep = Report(dry_run=(dry_run_default() if dry_run is None else bool(dry_run)))
    recs = [r for r in (records or []) if isinstance(r, dict)]
    rep.n_records = len(recs)
    if not enabled():
        rep.disabled = True
        return rep
    scope_set: Optional[Set[str]] = None
    if scope is not None:
        scope_set = {str(x).strip() for x in scope if str(x or "").strip()}
    rows: List[dict] = []
    n_scope = 0
    for rec in recs:
        aid = str(rec.get("announcement_id") or (rec.get("extraction") or {})
                  .get("announcement", {}).get("announcement_id") or "").strip()
        if not aid:
            continue
        if scope_set is not None and aid not in scope_set:
            continue
        n_scope += 1
        ext = rec.get("extraction")
        if not isinstance(ext, dict):
            continue
        ann = ext.get("announcement")
        if not isinstance(ann, dict):
            continue
        patches = _plan_record(rec, aid, rep)
        if not patches:
            continue
        if not rep.dry_run:
            for p in patches:
                if p.field == F_WINNERS:
                    ann[F_WINNERS] = list(p.new_value)          # 列表字段保持列表
                elif p.field == F_BIDDERS:
                    ann[F_BIDDERS] = list(p.new_value)
                elif p.field == F_AMOUNT:
                    ann[F_AMOUNT] = float(p.new_value)
                else:
                    ann[p.field] = p.new_value
                p.applied = True
                rows.append(p.as_row())
        rep.patches.extend(patches)
    rep.n_scope = n_scope
    if rows and write_provenance:
        rep.provenance_written = append_provenance(rows)
        rep.provenance_file = str(provenance_path())
    elif rep.patches and rep.dry_run:
        rep.provenance_file = str(provenance_path())
    return rep


def append_provenance(rows: Sequence[dict]) -> int:
    """把补丁逐条追加到侧车文件（缺文件时先写表头）。返回写入行数。"""
    if not rows:
        return 0
    p = provenance_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists() or p.stat().st_size == 0
    with open(p, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, PROVENANCE_COLS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


# ==================================================================
# 全量路径：out/full/<id>/{ir.json, items_llm.json} <-> 记录
# ==================================================================

def load_record(aid: str, full_dir: Optional[Path] = None) -> Optional[dict]:
    """从 `out/full/<公告号>/` 还原一条"记录"（IR + 主抽取结果）。

    * `items_llm.json` 决定 `extraction`（LLM 主抽取结果 = 回填目标）；
    * `ir.json` 提供原文证据（正文/摘要/发布时间/附件表）；
    * `ir.json` 缺失时只靠 `items_llm.json`，证据变少但不会报错。

    **`out/full` 里没有该公告号 = 返回 None**（脚本据此跳过，绝不从别处凑数据）。
    """
    root = Path(full_dir or _FULL_DIR)
    d = root / str(aid).strip()
    llm_p = d / "items_llm.json"
    if not llm_p.exists():
        return None
    try:
        ext = json.load(open(llm_p, encoding="utf-8")) or {}
    except (OSError, ValueError):
        return None
    if not isinstance(ext.get("announcement"), dict):
        ext["announcement"] = {}
    ext["announcement"].setdefault("announcement_id", str(aid).strip())
    ir_p = d / "ir.json"
    rec: dict = {}
    if ir_p.exists():
        try:
            rec = json.load(open(ir_p, encoding="utf-8")) or {}
        except (OSError, ValueError):
            rec = {}
    rec.setdefault("announcement_id", str(aid).strip())
    rec["extraction"] = ext
    rec["llm_announcement"] = ext.get("announcement")
    rec["_origin"] = f"out/full/{aid}"
    return rec


def persist_record(rec: dict, full_dir: Optional[Path] = None) -> bool:
    """把回填后的 `extraction.announcement` 写回 `items_llm.json`（只覆盖那 5 个字段）。

    写回是**幂等**的：已有非空值不会被改（`apply()` 的规则保证），重复执行结果不变。
    只重写 `announcement` 里被回填的键，`items` / `engine` 原样保留。
    """
    aid = str(rec.get("announcement_id") or "").strip()
    if not aid:
        return False
    p = Path(full_dir or _FULL_DIR) / aid / "items_llm.json"
    if not p.exists():
        return False
    ext = rec.get("extraction") or {}
    ann = ext.get("announcement") or {}
    data = json.load(open(p, encoding="utf-8")) if p.stat().st_size else {}
    target = data.get("announcement")
    if not isinstance(target, dict):
        target = {}
        data["announcement"] = target
    changed = False
    for f in (F_PURCHASER, F_WINNERS, F_BIDDERS, F_AMOUNT, F_PUBLISH):
        if f in ann and target.get(f) != ann.get(f):
            if _empty(target.get(f)) and not _empty(ann.get(f)):
                target[f] = ann.get(f)
                changed = True
    if not changed:
        return False
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return True
