# -*- coding: utf-8 -*-
"""官方《输出标准样例表》三张表的**唯一生成逻辑**（总表 / 标的表 / 投标人表）。

两条产出路径**共用本模块的 `build_tables()`**，从而保证口径完全一致：
  1. 任务三下载端点 `GET /api/v1/export/official`（`task3/backend/app/export_api.py`）
     -> `build_from_platform(out/platform/platform.db)`：读**平台当前库**，即
     「用户上传什么、解析出什么，就下载什么」，与某次上传结果绑定；
  2. 提交件生成器 `scripts/export_official_format.py`
     -> 读 `out/task2/graph.db` + `deliverables/items_task1.csv`，产出
     `deliverables/official/`。

列定义（逐字对齐 `输出标准样例表.xlsx`，openpyxl 实测）
------------------------------------------------------
    总表      8 列：序号 / URL / UUID / 标段合同包 / 项目名称 / 采购单位 / 中标供应商 / 中标金额
    标的表   11 列：序号 / UUID / 标段合同包 / 产品服务名称 / 品目 / 品牌 / 规格型号 /
                     数量 / 单价 / 总价 / 类别
    投标人表  9 列：序号 / UUID / 标段合同包 / 名称 / 类别 / 通过审查 /
                     报价（万元）/ 得分 / 未中标原因

**没有「备注」列** —— 「需求 / 中标」的区分只放在 `类别` 列。
sheet 名必须为 `总表` / `标的表` / `投标人表`。

投标人表三个判定列的口径（多来源取并集，全部**有据可依**）
--------------------------------------------------------
**行来源**（谁会被写进投标人表）—— 三个来源按 `(UUID, 标段/合同包, 归一名称)` 去重：
    R1 本包标的行的供货方（`t1_item.supplier_name` / 交付表 `items_task1.csv`）；
    R2 已定位到本包的 `bid` 投标关系名（含实体规范名与别名）；
    R3 **投标人名单**（`ict_parse/bidders.py` 从《开标一览表》《报价明细》《评审结果》
       《资格审查名单》《未中标（成交）原因表》里逐行抽出的**全部**投标人；上传路径落
       `bidder_roster.csv` -> 平台库 `t1_bidder`）。R3 正是「参与人几乎缺失」的补丁：
       附件包数据集没有 `bid` 表，名单表里的非中标方原本无人可写。R3 的每一行还必须
       **能定位到本包**（`_roster_target`：单包篇 / 行自带包标识 / 名称命中本包标的供货方），
       定位不到就丢弃（宁缺勿造）；已由 R1/R2/R3 写过的人不重复写。

`类别` = 「中标人」/「参与人」/「其他」，判定来源**取并集**：
    S1 `bid.is_winner`（图库/平台库的投标关系表，真值 1）；
    S2 标的行 `is_winner`（交付表 `items_task1.csv` / 平台库 `t1_item`）；
    S3 公告级 `announcement.winners`（`out/full/<公告号>/items_llm.json`）；
    S4 **标的表兜底** —— 仅当 (a) 整批**没有任何 bid 投标关系行**、(b) 本篇标的行的
       `is_winner` **逐行为空**（既无真值也无显式假值）、(c) 该行 `supplier_name` 非空
       时，把本篇标的中出现的供货方视为**中标人**。
       依据：标的表就是「结果公示里这个包由谁供什么」的成交标的（招标/需求类公告没有
       标的行），而陪标方只出现在 bid 表/投标人表里、从不写进标的表；因此缺 `is_winner`
       标记时标的供货方即中标人（与既有「单一来源=中标方」兜底同一条口径）。
       小样本数据集没有 `bid` 表、`is_winner` 又是死列，S1–S3 全部为空，只能靠 S4。
       判据 1 保证**全量库（graph.db 有 3240 条 bid）结果逐字不变**。
    优先级（用户要求，逐条）：名称 ∈ 中标方集合 -> `中标人`（**最高**）；
        原文判「未通过」-> `其他`（官方样例 E3：`其他` + `未通过，资质不符合要求`）；
        其余 -> `参与人`。「同一人既在名单又在标的中」按名称归一判同一人 -> 只写一行，
        且走中标人那条口径（中标方必然是"通过审查"的，两条规则实际不会冲突）。
并集后**必须能定位到本包的标的行**（名称归一后命中该包 `supplier_name`）才写「中标人」；
定位不到就不写（宁缺勿造）。该包**只有唯一供应商且无中标证据**时，按「单一来源=中标方」
兜底标为中标人（与总表「中标供应商」同一份 `winners`，保证两表自洽）。
名称一律先归一（NFKC 全半角统一 + 去空白/括号/分隔符 + 大写），并另算一份「剥掉括号里
包标注」的形式（`（包2）阿里云计算有限公司` 与 `阿里云计算有限公司` 视为同一实体）。

`通过审查`：口径逐字对齐样例表「资格审查/符合性审查结果：通过 / 未通过，未通过需注明原因」——
**原因就写在本单元格里**（样例示例值 `未通过，资质不符合要求`）。判定顺序：
    1. 原文（`ir.json` 的「资格审查/符合性审查/评审结果」表，含「未通过原因 / 未通过资格审查原因 /
       被拒绝原因」列，以及脏「宽表」恢复）判为**未通过 / 不合格 / 废标 / 无效 / 否决** →
       写 `未通过，<原因原文>`（原因缺失时只写 `未通过`）；
    2. 原文判为通过、或该行是中标人、或有「参与评审」证据 → `通过`；
    3. 判不出来 → 留空（宁缺勿造）。

`未中标原因`：只回填**非中标方**行（参与人 / 其他），全部照录原文，来源按优先级合并：
    0. 投标人名单里该行自带的「未中标原因」列（`t1_bidder.lose_reason`，首现非空优先）；
    1. `ir.json` 的「未中标（成交）原因」表（含合并单元格「少一列」与原因列空位前移的补偿）；
    2. 段落式小节 `九、未中标（成交）供应商的未中标（成交）原因：供应商：原因`（含
       `未中标原因：供应商：原因` / `03 包：供应商：原因` 这类「标题/包号占了名字位」的还原）；
    3. 「宽表」脏 IR（整页被塞进一行）按**本篇名册**切分恢复（对不上不猜）。
    对不上留空。

`得分`：只在**原文明确披露**时填写，数值化后保留两位小数（float，xlsx 用 `0.00` 数字格式）；
    取「评审总得分 / 综合得分 / 总得分 / 汇总得分 / 最终得分」（优先级最高），
    其次「评审得分 / 评标得分」，再次分项（技术标/商务标/价格分…）；
    明确排除「评审报价 / 中标金额 / 报价 / 优惠率」等**非得分**列，以及带「总分制/百分制」
    口径标注或超出 0~1000 的值；同一 (供应商, 包) 出现多个不同数值时留空（不猜）。
    抽不到留空。

单位口径
--------
平台库里金额是**元**，官方三表要**万元**（写库时 ×10000）。导回时：

* 总表 `中标金额`、投标人表 `报价（万元）` → **两位小数**（样例明确要求「保留两位小数」）
* 标的表 `单价` / `总价` → **保留全部有效小数**（官方答疑 Q3：单价过低时四舍五入会变成 0，
  故必须精确换算、不压缩位数。实现用 `Decimal(str(v)) / 10000`，只去掉多余尾随零，
  **绝不因四舍五入把非零值变成 0**）

分包（标段/合同包）口径（官方答疑 Q2）
------------------------------------
* 识别规则与后缀规则唯一来源：`ict_parse/packages.py`（判据见该模块 docstring）；
* ≥2 个包 → 每包一个 `主ID_01/_02…` 后缀（首现顺序，官方明确不要求顺序）；
* 0/1 个包 → UUID 不加后缀；识别到包名则填原文包名，否则填「未分包」；
* 三张表的 `(UUID, 标段/合同包)` 由**同一个包计划**生成，保证互相自洽。

用法
----
    from ict_parse import official_tables as ot
    tables = ot.build_from_platform(r'.../out/platform/platform.db')  # -> {总表:[...], ...}
    ot.write_xlsx(r'deliverables/official/官方数据集提取结果.xlsx', tables)
    ot.write_csvs(r'deliverables/official', tables)
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence, Set

from . import packages as pkgplan
from . import noise as itemnoise

# ---- 三张标准表：sheet 名 + 列顺序（逐字对齐样例表，不得增删）----
SHEET_TOTAL = "总表"
SHEET_ITEM = "标的表"
SHEET_BIDDER = "投标人表"
SHEET_NAMES: tuple = (SHEET_TOTAL, SHEET_ITEM, SHEET_BIDDER)

TOTAL_COLS = ["序号", "URL", "UUID", "标段/合同包", "项目名称", "采购单位",
              "中标供应商", "中标金额"]
ITEM_COLS = ["序号", "UUID", "标段/合同包", "产品服务名称", "品目", "品牌",
             "规格型号", "数量", "单价", "总价", "类别"]
BIDDER_COLS = ["序号", "UUID", "标段/合同包", "名称", "类别", "通过审查",
               "报价（万元）", "得分", "未中标原因"]

COLUMNS: Dict[str, List[str]] = {
    SHEET_TOTAL: TOTAL_COLS,
    SHEET_ITEM: ITEM_COLS,
    SHEET_BIDDER: BIDDER_COLS,
}

# ---- 「金额（万元）两位小数」列：xlsx 用 number_format='0.00'，CSV 用 f'{v:.2f}' ----
# 官方样例明确要求：总表 `中标金额`、投标人表 `报价（万元）` **保留两位小数**；
# 而标的表 `单价` / `总价` 相反，要**保留全部有效小数**（答疑 Q3），所以这里**不能**整表格式化，
# 只能逐列点名。两条落盘路径（xlsx / CSV）都从这里取列名，避免只改一条而漂移。
MONEY2_COLUMNS: Dict[str, tuple] = {
    SHEET_TOTAL: ("中标金额",),
    SHEET_BIDDER: ("报价（万元）",),
}

# 默认服务库位置（app/out/platform/platform.db）；调用方可以显式覆盖
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../app
DEFAULT_PLATFORM_DB = os.path.join(_APP_DIR, "out", "platform", "platform.db")
# 逐篇解析结果（winners / 原文证据）——两条路径共用
FULL_DIR = os.path.join(_APP_DIR, "out", "full")
# 原文 HTML 正本（仓库根目录）—— 仅用于「投标人 -> 包」的原文证据补全
HTML_DIR = os.path.join(os.path.dirname(_APP_DIR), "赛题五.基准测试数据_html")

NO_PACKAGE = pkgplan.NO_PACKAGE   # 「未分包」（官方要求的写法）
LABEL_WINNER = "中标人"
LABEL_BIDDER = "参与人"
LABEL_OTHER = "其他"          # 官方样例：未通过资格审查/符合性审查者
LABEL_PASSED = "通过"

# 公告类型里出现这些词 -> 标的是「需求」（招标/需求类文件）；否则按「中标」。
_NEED_KEYS = ("招标", "需求", "采购公告")


# ==================================================================
# 取值工具（缺失/异常一律按空处理，不抛错）
# ==================================================================

def _num(v) -> Optional[float]:
    """任意单元格值 -> float；无法解析返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("，", "")
    if not s or s.lower() in ("none", "nan", "null", "-", "未公示", "无"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _dec(v) -> Optional[Decimal]:
    """任意单元格值 -> Decimal（十进制字面量解析，不经二进制浮点）。

    用 `Decimal(str(v))` 而不是 `Decimal(float)`：后者会把 0.1 变成
    0.1000000000000000055511151231257827，再除以 10000 就会产生长尾噪声。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return None
        s = repr(v)
    else:
        s = str(v)
    s = s.strip().replace(",", "").replace("，", "")
    if not s or s.lower() in ("none", "nan", "null", "-", "未公示", "无"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _strip_zeros(d: Decimal) -> Decimal:
    """去掉多余尾随零，但**保持定点写法**（不用 Decimal.normalize()，避免 250000 -> 2.5E+5）。"""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("", "-", "-0", "-0."):
        s = "0"
    return Decimal(s)


def fmt_num(v) -> str:
    """单元格 -> CSV 文本：Decimal 一律用**定点**写法（`format(v,'f')`）。

    必要性：`str(Decimal('0.00000055'))` 是 `'5.5E-7'`（科学计数法，Excel 不认）；
    而官方要求「保留全部有效小数」，所以 CSV 必须写定点十进制。
    """
    if isinstance(v, Decimal):
        return format(v, "f")
    return "" if v is None else str(v)


def fmt_money2(v) -> str:
    """金额（万元）列 -> CSV 文本：**定长两位小数**（`123.4 -> '123.40'`）。

    只用于 `MONEY2_COLS`（总表 `中标金额` / 投标人表 `报价（万元）`）；
    空值/无法解析保持空串（不写 `0.00`，不编造金额）。
    与 xlsx 的 `number_format='0.00'` 是同一个口径：两条路径写到文件里的
    都是「两位小数」，因此 CSV 与 xlsx 打开后看到的数字一致。
    """
    f = _num(v)
    return "" if f is None else f"{f:.2f}"


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "y", "是")


def _items_unlabelled(items) -> bool:
    """某篇公告的标的行里 `is_winner` 是否**逐行为空**（「标的表兜底」判据 2）。

    与 `task3/backend/integrations/neo4j_sync.py::_items_are_unlabelled` **同一判据**
    （两处实现必须一致，否则图里的中标方与下载的投标人表会对不上）：

    * 空 / None / 全空白 -> 这行算"未标注"；
    * `True` / `False` / `1` / `0` / `'是'` / `'否'` -> 这行**有标记**，整篇不启用兜底
      （显式 `False` 明确表示"不是中标方"，绝不能被兜底翻成中标人）；
    * 没有标的行 -> 不启用。
    """
    if not items:
        return False
    for it in items:
        v = it.get("is_winner")
        if isinstance(v, bool):          # 显式 True/False 都算"有标记"
            return False
        if str(v if v is not None else "").strip():
            return False
    return True


def _blank(v) -> str:
    return "" if v is None else str(v)


def _group_by(rows, key: str) -> Dict[str, List[dict]]:
    """按某个键（去空白后的字符串）分组，保持首现顺序（三表的行序依赖它）。"""
    out: Dict[str, List[dict]] = {}
    for r in rows:
        out.setdefault(_blank(r.get(key)).strip(), []).append(r)
    return out


def to_wan_2(v):
    """元 -> 万元，两位小数四舍五入（总表中标金额 / 投标人表报价）。**口径不变。**"""
    f = _num(v)
    return None if f is None else round(f / 10000.0, 2)


def to_wan_keep(v):
    """元 -> 万元，**精确换算并保留全部有效小数**（标的表单价/总价，官方答疑 Q3）。

    * 用 `Decimal(str(v)) / 10000`（十进制精确），不做任何位数截断/四舍五入；
    * 只去掉多余**尾随零**（`31.930000` -> `31.93`），有效小数一位不丢；
    * 因此 `2.5 元 -> 0.00025`、`0.125 元 -> 0.0000125`、极小值也不会变成 0；
    * 返回 `Decimal`：CSV 走 `str()` 得定点写法，xlsx 由 openpyxl 按数值写入。
    """
    d = _dec(v)
    if d is None:
        return None
    return _strip_zeros(d / Decimal(10000))


def to_qty(v):
    """数量：**按官方样例以「文本」输出**（样例中 `254` 的单元格类型是 str，非数值）。

    这样既能保住官方 Q3 要求的「特殊单位保留原格式」（`1批`/`3台` 原样写出），
    又对普通正整数输出去零后的文本（`4`/`12`）；取不到留空。
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("none", "nan"):
        return None
    f = _num(s)
    if f is None:
        return s                      # 原文含单位/非纯数值 → 照录原文
    n = int(f) if abs(f - round(f)) < 1e-9 else f
    return str(n)


# ==================================================================
# 名称归一（判定「同一实体」的唯一口径；两条产出路径共用）
# ==================================================================

_NORM_STRIP = "（）()［］[]【】{}，,、；;：:·．.-_—－/\\"
# 「（包2）」「(二标段)」这类**包标注**（剥掉后才是干净的公司名）
_PKG_AFFIX_RE = re.compile(
    r"[（(][^（()）]{0,12}?(?:采购包|合同包|包件|标包|分包|标段|包)"
    r"\s*\d{0,4}[^（()）]{0,4}[）)]")


def norm_name(s) -> str:
    """名称归一化：NFKC（半角/兼容字符统一）+ 去空白 + 去括号与分隔符 + 大写。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = re.sub(r"[\s\u3000]+", "", s)
    for ch in _NORM_STRIP:
        s = s.replace(ch, "")
    return s.upper()


def norm_base_name(s) -> str:
    """名称归一化 + **剥掉括号里的包标注**（`（包2）` / `(二标段)`）。

    必要性：关系表里有的投标人名自带包标注（原文如此），而标的行的 supplier_name
    是干净的公司名。做「同一实体」比对时若不剥掉标注，中标方/金额就会对不上。
    判据仍是有据可依：只剥**括号内的包标识**，普通公司名括号（如 `（北京）`）不动。
    """
    return _PKG_AFFIX_RE.sub("", norm_name(s))


def strip_pkg_affix(s) -> str:
    """剥掉名称里的**包标注**（`（包1）腾讯云计算…` / `宁夏君盾科技有限公司(二标段)`）。

    只动"括号里含包标识"的部分：普通公司名括号（`亚信科技（成都）有限公司`）不受影响。
    用于总表「中标供应商」列 —— 标的行没有该供应商时，取关系表里的中标人，
    但要把原文自带的包标注去掉，落回公司名。
    """
    t = str(s or "").strip()
    if not t:
        return t
    pat = r"(?:采购包|合同包|包件|标包|分包|标段|包)\s*\d{0,4}"
    t = re.sub(r"^[（(][^（()）]{0,12}?" + pat + r"[^（()）]{0,4}[）)]\s*", "", t)
    t = re.sub(r"[（(][^（()）]{0,12}?" + pat + r"[^（()）]{0,4}[）)]$", "", t)
    return t.strip()


def _name_hits(name, norms) -> bool:
    """名称（或其"剥包标注"形式）是否命中归一集合。"""
    if not name:
        return False
    return norm_name(name) in norms or norm_base_name(name) in norms


def _norm_pair(s):
    """名称 -> {归一形式, 剥包标注形式}（用于构建命中集合）。"""
    if not str(s or "").strip():
        return set()
    return {norm_name(s), norm_base_name(s)}


# ==================================================================
# 投标人 -> 包：**有据可依**的补全（不允许猜测）
# ==================================================================
#
# 分包篇里只有「该供应商出现在哪个包的标的中」这一条依据；其余靠下列原文/结构化证据补全：
#   P1 投标人名称（或实体名）里带显式包标识
#      （如原文「宁夏君盾科技有限公司（二标段）」、「（包2）阿里云计算有限公司」），
#      且该标识在本篇包组里唯一命中 -> 归属该包。证据 = 投标人名称原文。
#   P2 名称归一化后（NFKC + 去空白/全角括号）命中本篇「包 × 供应商」标的行，
#      且只命中一个包 -> 归属该包。证据 = 标的行的 supplier_name（同一实体换写法）。
#   P3 原文「评审得分排名 / 中标候选人」分节：该分节在原文里是**一张表一个包**，
#      表格上方有显式「标段名称：X」（或「X包」），表内「供应商名称」列即该包的投标人
#      -> 该投标人归属 X 对应的包。证据 = 原文分节标题 + 该投标人所在表格。
#   P4 实体别名表（graph.db `entity_alias`）里该实体的任一别名归一化后唯一命中某包
#      -> 归属该包。证据 = 同一实体的别名记录。
#   仍无线索 -> **保持丢弃**（不猜；比编造安全）。


def _pkg_tokens(text: str):
    """一段文本里**显式出现**的包标识 -> [(原文标识, 分组键), …]（不猜）。

    比 `pkgplan.detect()` 多扫几处：后者只返回第一个命中，投标人名称里
    可能同时出现公司名与包标识（「宁夏君盾科技有限公司（二标段）」）。
    """
    out = []
    if not text:
        return out
    for chunk in [str(text)] + re.findall(r"[（(]([^（()）]{1,40})[）)]", str(text)):
        hit = pkgplan.detect(chunk)
        if hit and hit not in out:
            out.append(hit)
    # 括号内的裸包标识（`（二标段）` 已被 detect 覆盖；这里补 `（X包）` 之类）
    for m in re.finditer(
            r"第\s*(?:\d{1,4}|[一二三四五六七八九十]{1,3}|[A-Za-z])\s*"
            r"(?:采购包|合同包|包件|标包|分包|标段|包)"
            r"|(?:采购包|合同包|包件|标包|分包|标段)\s*[（(]?\s*"
            r"(?:\d{1,4}|[一二三四五六七八九十]{1,3}|[A-Za-z])\s*[）)]?"
            r"|包\s*[（(]?\s*(?:\d{1,2}|[一二三四五六七八九十]{1,3}|[A-Za-z])\s*[）)]?",
            str(text)):
        tok = m.group(0).strip("（()） 、,，")
        key = pkgplan.pkg_key(tok)
        if key and key != "包#" and (tok, key) not in out:
            out.append((tok, key))
    return out


def _group_for_key(groups, key: str):
    """分组键 -> 本篇包组标签（唯一命中才返回，否则 None）。"""
    cand = [g for g in groups if g != NO_PACKAGE and pkgplan.pkg_key(g) == key]
    return cand[0] if len(cand) == 1 else None


def _group_for_evidence_label(groups, label: str):
    """原文「标段名称：X」-> 本篇包组标签。

    四级判据（全部要求唯一命中）：
      a) X 里的包标识键 == 某包组标签的键（`（二标段）` -> `二标段`）；
      b) X 的归一化串 == 某包组标签的归一化串（`三标段：第三方测评服务（等保测评）`）；
      c) X 的归一化串**以某包组标签结尾**（`…项目六标段` -> `六标段：…`）——
         只认"包标签是 X 的后缀"，避免用 `in` 把 `一标段` 匹配到 `十一标段`；
      d) 该包标识键在 X 的**括号/括号内子串**里出现（同 a，覆盖 `X（二标段）`）。
    """
    if not label:
        return None
    hit = pkgplan.detect(label)
    if hit:
        got = _group_for_key(groups, hit[1])
        if got:
            return got
    nl = norm_name(label)
    cand = [g for g in groups if g != NO_PACKAGE and norm_name(g) == nl]
    if len(cand) == 1:
        return cand[0]
    cand = [g for g in groups if g != NO_PACKAGE and nl.endswith(norm_name(g))]
    if len(cand) == 1:
        return cand[0]
    # 括号内包标识（如 `项目名（二标段）`）
    for sub in re.findall(r"[（(]([^（()）]{1,40})[）)]", label):
        h = pkgplan.detect(sub)
        if h:
            got = _group_for_key(groups, h[1])
            if got:
                return got
    return None


_RANK_SEC_RE = re.compile(r"评审得分|评审排名|评审结果|评审劳务报酬|评标委员会评审|候选人|投标人排序|综合评审排名")
_RANK_END_RE = re.compile(r"评审专家名单|代理服务收费|公告期限|其他补充事宜|凡对本次公告内容|联系方式|附件")
# 包标签行：`A包` / `第1包` / `二标段` / `1包`（整行就是包标识，不夹带其他文字）
_PKG_LABEL_RE = re.compile(
    r"^(?:标段名称|分包名称|包名称|标项名称|合同包名称|采购包名称|标段|分包|标包|包组)\s*[：:]\s*.+?$"
    r"|^(?:第\s*)?(?:\d{1,4}|[一二三四五六七八九十]{1,3}|[A-Za-z])\s*"
    r"(?:采购包|合同包|包件|标包|分包|标段|包)$")


def _label_group(groups, text: str):
    """一行文字 -> 本篇包组标签（含「标段名称：X」与裸包名两种写法），唯一命中才返回。"""
    if not text:
        return None
    got = _group_for_evidence_label(groups, text)
    if got:
        return got
    s = text.strip(" （()）").replace(" ", "")
    # 原文有多种写法：`标包A` / `包A` / `A标包` / `A包`，都归一到分组键再比对
    for cand in (s, re.sub(r"^(?P<id>[0-9A-Za-z一二三四五六七八九十]{1,3})"
                           r"(?P<u>采购包|合同包|包件|标包|分包|标段|包)$",
                           r"\g<u>\g<id>", s)):
        key = pkgplan.pkg_key(cand)
        if key and key != "包#":
            got = _group_for_key(groups, key)
            if got:
                return got
    return None


# 裸包编号（`标项号`/`包号` 列里只写了 `1` / `二` / `A`）——补成 `包1` 再按 `pkg_key` 归一成 `包#1`
_BARE_ID_RE = re.compile(r"^第?\s*(?:\d{1,4}|[一二三四五六七八九十]{1,3}|[A-Za-z])\s*$")


def _roster_target(row: dict, groups, norm_sup_pkgs: Dict[str, set],
                   single_label: Optional[str]) -> Optional[str]:
    """一条「投标人名单」行 -> 本篇包组标签（定位不到返回 None，**不猜**）。

    判据（逐条，全部有据可依）：
      L1 本篇只有 1 个包 -> 该包；
      L2 行自带包标识（`标项号/包号/标段/包名称` 列）且**唯一**命中本篇包组 -> 该包
         （裸编号 `1` 先补成 `包1`，才比得上 `采购包1`/`第一包` 这类包名）；
      L3 名称归一化后命中「包 × 标的供货方」且只命中一个包 -> 该包
         （= 该人名下确有本包的标的，与 bid 关系表的 P2 判据同一口径）；
      其余 -> 丢弃（宁缺勿造；多包篇里「没有包标识、又不在任何包标的里」的人不写）。
    """
    if single_label is not None:
        return single_label
    lbl = _blank(row.get("package_label")).strip()
    if lbl:
        got = _label_group(groups, lbl)
        if got:
            return got
        if _BARE_ID_RE.match(lbl):
            got = _group_for_key(groups, pkgplan.pkg_key("包" + lbl.lstrip("第").strip()))
            if got:
                return got
        hits = [g for g in groups if g != NO_PACKAGE and _matches_label(lbl, g)]
        if len(hits) == 1:
            return hits[0]
    for k in _norm_pair(row.get("name")):
        s = norm_sup_pkgs.get(k)
        if s and len(s) == 1:
            return list(s)[0]
    return None


def _rank_names_from_text(txt: str, known: set) -> list:
    """从「投标人排序情况」表的**文本片段**里捞出投标人姓名。

    该模板的排名表在原文里被当成**段落**（`<p>` 内嵌 `<table>`），BeautifulSoup
    把整张表压成一段文字：`排序 投标人名称 得分 1 浪潮云信息技术股份公司 90.31 …`。
    因此这里改用**花名册反查**：只认「本次公告全部投标人姓名（含实体别名）」中
    确实出现在该片段里的名字 —— 有据可依，不做任何名称猜测。
    """
    hits = []
    if not txt:
        return hits
    for nm in known:
        if nm and nm in txt and nm not in hits:
            hits.append(nm)
    # 按在原文里的出现位置排序，保持原文顺序（可复现）
    return sorted(hits, key=lambda x: txt.index(x))


def _pkg_label_of(groups, txt: str):
    """一行文字 -> 本篇包组标签；只认「标识 + `：`」或整行就是标识的写法（不猜）。"""
    if not txt:
        return None
    m = re.match(r"^(?P<tok>[^：:]{0,40}?)\s*[：:]", txt)
    if m:
        got = _label_group(groups, m.group("tok").strip())
        if got:
            return got
    stripped = txt.strip().rstrip("：:")
    if len(stripped) <= 12:
        got = _label_group(groups, stripped)
        if got:
            return got
    return None


def _fragment_pkg_texts(aid: str, groups, html_dir: str = None) -> list:
    """原文 HTML -> `[(包组标签, 该包片段文本), …]`。

    **只用「排名/评审结果」类小节**作为证据来源：
      * 小节标题（`评审得分排名` / `评标委员会评审结果` / `磋商小组成员评审结果`
        / `投标人排序情况` / `中标候选人` …）开启证据区；
      * 区内每遇到一个**能对上本篇包组**的显式包标识（`标段名称：X`、`A包`、
        `A标包`、`二标段`…）就开一段，段内文字（含被压成段落的排名表）归该包；
      * 遇到下一个包标识、下一个排名类小节、或分节结束标记（评审专家名单 /
        代理服务收费 / 公告期限 / 联系方式 / 附件…）就**收口**当前段；
      * 收口后**保持在证据区**（原文里"未成交供应商原因"这类子标题仍按包分段
        列出投标人，`A标包：` 之后的名单同样有据），但若该包标识在区外出现
        则不会开始新段——区外文字一律不进证据。
    """
    path = os.path.join(html_dir or HTML_DIR, aid + ".html")
    if not os.path.exists(path):
        return []
    try:
        from bs4 import BeautifulSoup                       # noqa: PLC0415
        soup = BeautifulSoup(open(path, encoding="utf-8", errors="replace").read(), "lxml")
    except Exception:                                        # noqa: BLE001
        return []
    content = soup.find("div", class_="vF_detail_content")
    if content is None:
        return []
    segs: list = []
    cur = None
    in_rank = False

    def close():
        nonlocal cur
        if cur is not None and "".join(cur[1]).strip():
            segs.append((cur[0], " ".join(cur[1])))
        cur = None

    for el in content.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "table"]):
        if el.name == "table":
            if in_rank and cur is not None:
                cur[1].append(" ".join(re.sub(r"\s+", " ",
                                              tr.get_text(" ", strip=True))
                                       for tr in el.find_all("tr")))
            continue
        txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if not txt:
            continue
        if _RANK_END_RE.search(txt):
            close()
            in_rank = False
            continue
        if _RANK_SEC_RE.search(txt) and not _pkg_label_of(groups, txt):
            close()                                            # 新的一小节排名
            in_rank = True
            continue
        got = _pkg_label_of(groups, txt)
        if got:
            close()
            in_rank = True
            cur = [got, []]
            continue
        if in_rank and cur is not None:
            cur[1].append(txt)
    close()
    return segs


def html_pkg_bidders(aid: str, groups=None, known_names=None, html_dir: str = None) -> dict:
    """原文 HTML -> `{本篇包组标签: [该包投标人姓名, …]}`（只用**显式**原文证据，不猜）。

    做法：`_fragment_pkg_texts` 按**包标识**把排名/评审小节切成「包 -> 片段」，
    再用**花名册反查**（本次公告全部投标人姓名 + 实体别名）取出片段里出现的名字。
    因此每个归属都由「该投标人的名字确实出现在**该包标识之后、下一个包标识之前**
    的文字里」支撑，且不会凭空造出名字。

    **同一篇里一个名字同时落在多个包的片段里 -> 全部不归属**（无法判定，
    宁缺勿造；例如原文把同一位投标人在 A、B 两个包里都列了一遍的情况）。
    `known_names` 为空则返回 `{}`（没有花名册就无法保证取到的是投标人）。
    """
    known = {str(x).strip() for x in (known_names or []) if str(x or "").strip()}
    if not known:
        return {}
    per_pkg: dict = {}
    seen_count: dict = {}
    for lbl, blob in _fragment_pkg_texts(aid, groups, html_dir):
        names = _rank_names_from_text(blob, known)
        bucket = per_pkg.setdefault(lbl, [])
        for nm in names:
            if nm not in bucket:
                bucket.append(nm)
                seen_count[nm] = seen_count.get(nm, 0) + 1
    if not per_pkg:
        return {}
    return {lbl: [nm for nm in names if seen_count.get(nm, 0) == 1]
            for lbl, names in per_pkg.items()}


# ==================================================================
# 逐篇证据（out/full）：公告级 winners/bidders + 原文「未中标原因 / 资格审查」表
# ==================================================================
# 说明：这两类证据都来自**已经解析落盘的原文**（`out/full/<公告号>/`），
# 属于"有据可依"的补强来源；文件缺失时一律按空处理（答案退化，不报错、不臆造）。
_EVID_CACHE: Dict[str, dict] = {}
# 宽表恢复缓存：(id(基础证据), 名册大小) -> (基础证据对象, 叠加后的证据)
_WIDE_CACHE: Dict[tuple, tuple] = {}

# 「未中标原因」表：表头同时含 [未中标|未成交|落标|废标] 与 [原因|理由]
_REASON_KW = ("未中标", "未成交", "落标", "废标")
_REASON_COL_KW = ("原因", "理由")
# 明显是包标识而不是"原因"的取值（防脏表头把包名当原因）
_PKG_TOKEN_RE = re.compile(
    r"^(?:第)?[0-9一二三四五六七八九十A-Za-z]{0,3}(?:标包|标段|分包|包组|包件|包)"
    r"[:：]?[0-9一二三四五六七八九十A-Za-z]{0,3}$")

# ---- 段落式「未中标原因」模板（见 `_reasons_from_section_text`）----
_REASON_SEC_HEAD_RE = re.compile(r"未\s*中标|未\s*成交|落\s*标|废\s*标")
_REASON_COL_WORD_RE = re.compile(r"原因|理由")
_REASON_SEC_END_RE = re.compile(
    r"(?:(?<=[\u4e00-\u9fa5])|(?<=\n))[一二三四五六七八九十]{1,3}\s*、"
    r"|其他补充事宜|公告期限|联系方式|附件|评审专家")
# 段落里的 `供应商名：原因` 对。名字段沿用旧口径的字符集（汉字/字母/数字/常见括号与
# 连接符，可含空格，但**不允许换行**——否则会把上一条的原因尾巴并进名字）；
# 原因段 `[^：:；;。\n]`（原因原文里常有「（专家一）：…」这类子冒号）。
_REASON_PAIR_RE = re.compile(
    r"(?<![0-9A-Za-z\u4e00-\u9fa5])"
    r"(?P<name>[\u4e00-\u9fa5A-Za-z0-9（）()·．\.\-\u3000 ]{2,60})\s*[:：]\s*"
    r"(?P<reason>[^：:；;。\n]{2,160})")
# 「名字」不能是包序号（`04 包` / `标包A` 这种）
_PKG_LIKE_NAME_RE = re.compile(
    r"^\s*(?:第)?\s*[0-9一二三四五六七八九十]{0,3}\s*(?:标包|标段|分包|包组|包件|包|项|采购包)\s*"
    r"[:：]?[0-9A-Za-z一二三四五六七八九十]{0,3}\s*$")
# 若「名字」里出现这些词，说明抓到的是小节标题/表头而不是供应商名
# （注意别把真实公司名误伤：如「中科信息安全共性技术国家工程研究中心有限公司」含「研究中心」）
_NOT_A_NAME = ("未中标", "未成交", "落标", "废标", "原因", "理由", "供应商的", "投标人的",
               "各投标人", "发布时间", "评审专家", "公告期限")
# 段首序号（`1、XX公司` / `（1）XX公司` / `1.XX公司` / `1 XX公司`）——必须带明确的分隔符
_LEAD_ORDINAL_RE = re.compile(
    r"^\s*(?:[（(]\s*[0-9０-９一二三四五六七八九十]{1,3}\s*[）)]"
    r"|[0-9０-９]{1,3}\s*[、,，.．)）]"
    r"|[0-9０-９]{1,3}\s+)"
    r"\s*")


def _strip_leading_ordinal(name: str) -> str:
    """剥掉段首的「1、/（2）/3.」等序号，但保留「3M中国有限公司」这种数字开头的真名。"""
    t = str(name or "").strip()
    m = _LEAD_ORDINAL_RE.match(t)
    if m and m.end() > 0 and len(t) - m.end() >= 4 and not t[m.end():m.end() + 1].isdigit():
        return t[m.end():].strip()
    return t
# 原因文本开头若出现这些词，说明抓到的其实是别的栏目 -> 丢弃
_BAD_REASON_TEXT = ("地址", "电话", "联系人", "原因", "名称", "金额", "合计", "备注")
# 「通过 / 未通过」措辞：不是「未中标原因」，但能给出的审查结论（用于「通过审查」列）
_FAIL_WORDS = ("未通过", "不通过", "不合格", "废标", "无效", "否决")
_PASS_WORDS = ("通过", "合格")


def _nh(s) -> str:
    """表头归一（去空白/括号/冒号/星号），用于表头语义判定。"""
    return re.sub(r"[\s\u3000()（）:：*]", "", str(s or ""))


def _norm_loose(s) -> str:
    """宽松归一（**保留括号内包标注**，只去空白与冒号），用于段落式原因按名匹配。"""
    return norm_name(str(s or "").replace(" ", "").replace("\u3000", ""))


def _sane_headers(headers) -> bool:
    """表格合理性：真实表头不会又长又多（脏 IR 里常把整页塞进 headers）。"""
    if not headers or len(headers) > 12:
        return False
    return all(len(str(h or "")) <= 24 for h in headers)


def _entity_ok(name: str) -> bool:
    """是否像市场主体名（复用规则抽取层的判据，保持同一口径）。"""
    try:
        from .extract import _valid_entity                        # noqa: PLC0415
        return bool(_valid_entity(name))
    except Exception:                                             # noqa: BLE001
        return len(str(name or "")) >= 4


def _reasons_from_section_text(txt: str) -> Dict[str, str]:
    """正文**段落式**模板 -> `{归一供应商名: 原因原文}`。

    有些公告的「未中标（成交）原因」不是表格而是一段正文：
    `九、未中标（成交）供应商的未中标（成交）原因： 标包A： 广州慧谷动力科技有限公司：评审得分较低（…）。 …`
    这里按**小节切分**（从含「未中标/未成交/落标/废标 + 原因/理由」的标题起，到下一个
    `十、`／「其他补充事宜」／「公告期限」／「联系方式」／「附件」为止），
    再在**该小节内**取 `供应商名：原因` 对；小节外的一律不看（避免把「中标信息」里的
    `供应商名称：X 供应商地址：Y` 误当原因）。仍然宁缺勿造：名字必须像市场主体名。
    """
    out: Dict[str, str] = {}
    if not txt:
        return out
    # 从**最后一次**匹配往前扫：同一篇里常常先在表格里出现「…未中标原因…」表头，
    # 真正的段落式小节在正文更后面；靠前的表头小节容易抓到脏对，靠后的小节更干净。
    heads = list(_REASON_SEC_HEAD_RE.finditer(txt))
    for m in reversed(heads):
        window = txt[m.start():m.start() + 2200]
        # 标题里要有「原因/理由」；本模板常用「未中标供应商的未中标原因」，
        # 「原因」二字可能被供应商名隔开 -> 窗口放宽到 60 字，并用标题行末兜底。
        head = window[:60]
        if not _REASON_COL_WORD_RE.search(head):
            continue
        endm = _REASON_SEC_END_RE.search(window, 6)
        seg = window[:endm.start()] if endm else window
        for pm in _REASON_PAIR_RE.finditer(seg):
            nm = pm.group("name").strip(" ：:、,，。；;")
            rs = pm.group("reason").strip(" ：:、,，。；;")
            nm = _strip_leading_ordinal(nm)
            if len(rs) < 2:
                continue
            # 形态：`未中标原因：潍坊风筝面粉有限责任公司：评审得分较低`
            # 或 `03 包：德州御景园林有限公司：评审得分较低` —— 标题/包序号被当成名字，
            # **真名落进了 reason 段**。此时把 reason 段当名字，再从其后的「：原因」处取原因。
            if (_looks_like_name(rs) and not any(k in rs for k in _NOT_A_NAME)
                    and (any(k in nm for k in ("原因", "理由"))
                         or any(k in nm for k in ("包", "包组", "标段", "分包")))):
                ridx = pm.start("reason") + len(pm.group("reason")) - len(pm.group("reason").lstrip())
                tail = seg[ridx + len(rs):ridx + len(rs) + 200]
                rm = re.match(r"\s*[:：]\s*([^：:；;。\n]{2,160})", tail)
                if rm:
                    rs2 = rm.group(1).strip(" ：:、,，。；;")
                    if len(rs2) >= 2 and not any(b in rs2[:6] for b in _BAD_REASON_TEXT):
                        out[_norm_loose(rs)] = _trim_reason(rs2)
                continue
            if not _entity_ok(nm):
                continue
            if any(k in nm for k in _NOT_A_NAME):
                continue
            if _PKG_LIKE_NAME_RE.match(nm) or _PKG_TOKEN_RE.match(nm.replace(" ", "")):
                continue
            if any(b in rs[:6] for b in _BAD_REASON_TEXT):
                continue
            # 本文内**首现优先**（与合并进逐篇证据时的 setdefault 同口径）
            out.setdefault(_norm_loose(nm), _trim_reason(rs))
    return out


def _trim_reason(s: str) -> str:
    """原因原文收尾：`…得分偏低）、下一家供应商` / `…）、下一家供应商：…` 这种
    **下一家供应商被并进上一条**的尾巴要截掉。

    判据：分隔符（、；;，,）之后紧跟一个「像市场主体名」的片段（以 公司/中心/院/社/所… 收尾），
    且该片段**紧跟冒号或以行/串结束**（后半段自己没有冒号时就是尾巴）。
    """
    t = str(s or "").strip()
    if not t or "、" not in t and "；" not in t and ";" not in t and "，" not in t:
        return t.strip(" 　：:；;，,、")
    for m in re.finditer(r"[\u4e00-\u9fa5A-Za-z0-9（）()·．\.\-\u3000 ]{4,40}?"
                         r"(?:" + _NAME_TAIL_PAT + r")", t):
        if m.start() == 0:
            continue
        prev = t[m.start() - 1]
        if prev not in "、；;，,":
            continue
        after = t[m.end():]
        if not after or after[0] in ":：" or after[0] in "、；;，,":
            t = t[:m.start() - 1]
            break
    return t.strip(" 　：:；;，,、")


def _reason_cell(row, ri: int, span: int = 2) -> str:
    """取「原因」单元格；本列为空/占位时向后补看 span 列（脏 IR 常把原因挤到下一列）。"""
    for k in range(ri, min(len(row), ri + span + 1)):
        v = str(row[k] or "").strip()
        if v and v not in ("-", "—", "－", "/", "无"):
            return v
    return ""


def _looks_like_name(v: str) -> bool:
    """单元格是否像市场主体名（「未中标原因」表里的合并单元格常把名字挤到别的列）。"""
    t = _blank(v).strip()
    if not (5 <= len(t) <= 40) or re.search(r"[:：]", t):
        return False
    if _PKG_TOKEN_RE.match(t) or t in ("通过", "合格", "未通过", "废标", "是", "否"):
        return False
    return bool(_NAME_TAIL_RE.search(t))


def _reason_pair_from_row(row, si, ri, roster_names: Set[str]):
    """从一行里取 `(名字, 原因原文)`；`si`/`ri` 可为 None。

    处理三种真实形态：
      1. 标准表：按 `si`/`ri` 列号直取；
      2. **合并单元格**（`标包名称/供应商名称/未成交原因` 且包名只在首行有值）：
         名字列读不到时，在行内找「像名字」的单元格，其**后一列**即原因；
      3. `_reason_cell` 的空列前移补偿。
    """
    nm = ""
    if si is not None and si < len(row):
        nm = str(row[si] or "").strip()
    rs = _reason_cell(row, ri) if ri is not None and ri < len(row) else ""
    # 列号错位（合并单元格少一列 / 名字列读到包名）-> 用行内定位：
    #   行内第一个「像名字」的单元格 = 供应商名，其**后一列** = 原因。
    if (not nm or not rs) or (rs and _looks_like_name(rs) and not (nm in roster_names)):
        for k in range(0, len(row) - 1):
            c = str(row[k] or "").strip()
            if not c or not (c in roster_names or _looks_like_name(c)):
                continue
            cand = _reason_cell(row, k + 1)
            if not cand or cand == c or _looks_like_name(cand):
                continue
            return c, cand
    if not nm or not rs:
        return "", ""
    return nm, rs


def _evidence_from_ir(rec: dict) -> tuple:
    """IR -> `(未中标原因 {归一名称: 原文原因}, 参与评审名单 set)`。"""
    reasons: Dict[str, str] = {}
    reviewed: Set[str] = set()
    for g in rec.get("main_item_tables", []) or []:
        headers = [str(h or "") for h in (g.get("headers") or [])]
        if not _sane_headers(headers):
            continue
        hs = [_nh(h) for h in headers]
        j = "".join(hs)
        si = ri = None
        for i, h in enumerate(hs):
            if ri is None and any(k in h for k in _REASON_KW) and any(c in h for c in _REASON_COL_KW):
                ri = i
            if si is None and ("供应商" in h or "投标人" in h or "投标单位" in h or h == "企业名称"):
                si = i
        rows = g.get("rows") or []
        # 本篇名册（只有本表已出现的名字；用于合并单元格错位时的行内定位）
        roster_names = {str(r[si] or "").strip() for r in rows
                        if si is not None and si < len(r) and str(r[si] or "").strip()}
        if si is not None and ri is not None:
            for r in rows:
                nm, rs = _reason_pair_from_row(r, si, ri, roster_names)
                nm = _strip_leading_ordinal(nm)
                if not nm or not rs or nm == rs:
                    continue
                if not (2 <= len(rs) <= 200) or _PKG_TOKEN_RE.match(rs):
                    continue
                if not _entity_ok(nm):
                    continue
                reasons.setdefault(norm_name(nm), _trim_reason(rs))
                reviewed.add(norm_name(nm))
        # 「资格审查 / 符合性审查 / 评审结果」表：名单本身即参与评审，评审结果=通过即通过
        if "审查" in j or "评审结果" in j:
            res_i = next((i for i, h in enumerate(hs) if "评审结果" in h or h == "结果"), None)
            if si is not None:
                for r in rows:
                    if si >= len(r):
                        continue
                    nm = str(r[si] or "").strip()
                    if not nm or not _entity_ok(nm):
                        continue
                    reviewed.add(norm_name(nm))
    # 段落式模板（小节限定）补齐表里没覆盖到的供应商
    for k, v in _reasons_from_section_text(rec.get("content_text") or "").items():
        reasons.setdefault(k, v)
        reviewed.add(k)
    return reasons, reviewed


# ==================================================================
# 投标人表三个判定列的**原文证据抽取**（资格审查结果 / 未中标原因 / 得分）
# ------------------------------------------------------------------
# 数据源：`out/full/<公告号>/ir.json` 的 `main_item_tables`（附件/正文解析出的表格）
#        + `content_text`（段落式模板）；缺失一律按空处理，绝不臆造。
# ==================================================================

# ---- 表头角色判定（先归一再去空白，兼容「未 中标 原因」「评审总得分」等写法）----
_COL_NAME_HINT = ("供应商名称", "投标人名称", "投标单位", "供应商", "投标人",
                  "中标供应商名称", "中标人名称", "成交供应商名称", "企业名称", "单位名称")
# 这些列虽然含「供应商」，但不是名称列（地址/信用代码/电话/联系人）
_COL_NAME_EXCL = ("地址", "信用代码", "电话", "联系人", "联系方式", "开户", "账号")

# ---- 未通过（资格审查/符合性审查不合格）判据 ----
_FAIL_PAT = re.compile(
    r"未\s*通过|不\s*通过|未\s*合格|不\s*合格|废\s*标|无效|否决|取消.{0,4}资格|"
    r"资格.{0,4}(?:不满足|不符|未达)|不符合")
_PASS_PAT = re.compile(r"^\s*(?:是|通过|合格|符合|响应|进入|有效|满足)")

# ---- 得分列：`列优先级` 越小越优先（综合得分 / 评审总得分 优先于分项）----
_SCORE_KEYS: tuple = (
    (0, ("评审总得分", "综合得分", "总得分", "汇总得分", "最终得分", "总分", "总得分", "得分合计")),
    (1, ("评审得分", "评标得分", "评审价格得分", "综合评分", "得分")),
    (2, ("技术标得分", "商务标得分", "价格评分", "详细评分", "技术得分", "商务得分",
         "经济得分", "价格得分", "经济标得分", "技术分", "商务分", "价格分")),
)
# 这些列含「得分」但绝对不是该投标人的综合得分（价格扣除后的评审报价 / 报价）
_NOT_SCORE_RE = re.compile(r"报价|金额|优惠率|折扣|下浮|费率|比例")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

# ---- 宽表（整页塞进一行）脏 IR 的恢复判据 ----
_WIDE_HDR_STOP = re.compile(
    r"^(?:供应商名称|投标人名称|投标单位|中标供应商名称|中标人名称|成交供应商名称|"
    r"中标（成交）金额|中标金额|成交金额|供应商地址|中标人地址|序号|得分|排名)$")
_WIDE_ANCHOR = re.compile(r"未中标|未成交|被拒绝|落标")
_NAME_TAIL_RE = re.compile(
    r"(公司|中心|研究院|设计院|大学|学院|院|厂|社|所|站|集团|银行|分行|支行|"
    r"医院|学校|大队|支队|总队|部|局|厅|署|部)$")
# 同上，但作为正则片段（供 `_trim_reason` 拼进模式）
_NAME_TAIL_PAT = (r"(?:公司|中心|研究院|设计院|大学|学院|院|厂|社|所|站|集团|银行|分行|支行|"
                  r"医院|学校|大队|支队|总队|部|局|厅|署)")
_NAME_PREFIX_RE = re.compile(
    r"^(?:供应商|投标人|投标单位|中标人|成交人|中标供应商|成交供应商|企业)\s*名\s*称\s*[:：]\s*")
# 花名册归一化缓存：id(roster) -> (roster对象, 长度, 归一集合)
_ROSTER_NORM_CACHE: Dict[int, tuple] = {}


def _norm_cell(s) -> str:
    """表头/单元格归一（去空白与括号冒号星号），仅用于**判据匹配**。"""
    return re.sub(r"[\s\u3000()（）:：*]", "", str(s or ""))


def _col_roles(headers) -> dict:
    """表头 -> 角色下标：`{name, reason, review, review_reason, fail_reason, score: {i: 优先级}}`。"""
    roles: dict = {"name": None, "reason": None, "review": None,
                   "review_reason": None, "fail_reason": None, "score": {}}
    for i, h in enumerate(headers):
        t = _nh(h)
        if not t:
            continue
        # 名称列
        if roles["name"] is None and not any(x in t for x in _COL_NAME_EXCL):
            if any(k in t for k in _COL_NAME_HINT):
                roles["name"] = i
        # 原因列（未中标（成交）原因 / 被拒绝原因 / 推荐理由）
        if roles["reason"] is None and any(k in t for k in _REASON_COL_KW):
            if any(k in t for k in _REASON_KW) or "被拒绝" in t or "拒绝" in t:
                roles["reason"] = i
            elif "推荐理由" in t:
                roles["reason"] = i
        # 未通过 / 审查不合格原因列
        if roles["fail_reason"] is None and ("原因" in t or "理由" in t) and (
                "未通过" in t or "不通过" in t or "不合格" in t or "未中标" not in t and "资格" in t
                and "原因" in t):
            if any(k in t for k in ("未通过", "不通过", "不合格", "未入围", "废标")):
                roles["fail_reason"] = i
        # 审查 / 评审结果列
        if roles["review"] is None and not any(k in t for k in ("原因", "理由")):
            if (("审查" in t or "评审" in t or "审核" in t)
                    and not _NOT_SCORE_RE.search(t)
                    and "得分" not in t and "分数" not in t):
                roles["review"] = i
            elif "推荐排名" in t or "是否通过" in t or "审查结果" in t or "评审结果" in t:
                roles["review"] = i
        # 得分列
        if not _NOT_SCORE_RE.search(t):
            for pri, keys in _SCORE_KEYS:
                if any(k in t for k in keys):
                    roles["score"][i] = pri
                    break
    return roles


def _is_fail(text: str) -> bool:
    return bool(text) and bool(_FAIL_PAT.search(text))


def _is_pass(text: str) -> bool:
    return bool(text) and bool(_PASS_PAT.match(text))


def _clean_reason(s: str) -> str:
    """原因原文化（去首尾空白与多余的「：」；同行内多余空白压成单空格）。"""
    t = re.sub(r"[ \t\u3000]+", " ", str(s or "")).strip()
    return t.strip("：:；;，,。 ").strip()


def _score_value(s):
    """单元格 -> float 得分（保留两位小数）；不是分数（含「总分制」等口径标注/超范围）则 None。

    只接受**明确披露的数值**：'88.41' / '93.18分' / '76.60' / '100'。
    明确拒绝：'-'、''、'详见附件'、'450（总分制）'（口径被标注为「总分制」而非百分制）。
    """
    if s is None:
        return None
    t = str(s).strip().replace(",", "").replace("，", "")
    if not t or t in ("-", "—", "－", "/", "无", "未公示"):
        return None
    if "总分制" in t or "百分制" in t or "分制" in t:
        return None
    m = _NUM_RE.search(t)
    if not m:
        return None
    # 允许「88.41分 / 88.41 分 / 得分：88.41」，拒绝「88.41元 / 88.41万元」
    if re.search(r"[元万%]|万元", t):
        return None
    try:
        v = float(m.group())
    except ValueError:
        return None
    if not (0.0 <= v <= 1000.0):
        return None
    return round(v, 2)


def _fields_from_ir(rec: dict) -> dict:
    """IR -> `{reviews, review_reasons, scores: [候选...]}`（候选 = 明细，供按包消歧）。"""
    reviews: Dict[str, str] = {}
    review_reasons: Dict[str, str] = {}
    scores: List[dict] = []
    for g in rec.get("main_item_tables", []) or []:
        headers = [str(h or "") for h in (g.get("headers") or [])]
        if not _sane_headers(headers):
            continue
        roles = _col_roles(headers)
        ni = roles["name"]
        if ni is None:
            continue
        for r in g.get("rows") or []:
            if ni >= len(r):
                continue
            raw = str(r[ni] or "").strip()
            if not raw or not _entity_ok(raw):
                continue
            k = norm_name(raw)
            # ---- 审查结果 / 未通过原因 ----
            res_txt = str(r[roles["review"]] or "").strip() if (
                roles["review"] is not None and roles["review"] < len(r)) else ""
            fail_txt = str(r[roles["fail_reason"]] or "").strip() if (
                roles["fail_reason"] is not None and roles["fail_reason"] < len(r)) else ""
            if _is_fail(res_txt) or _is_fail(fail_txt):
                reviews.setdefault(k, "fail")
                reason = _clean_reason(fail_txt) or _clean_reason(res_txt)
                if reason and reason not in ("通过", "合格"):
                    review_reasons.setdefault(k, reason)
            elif _is_pass(res_txt):
                reviews.setdefault(k, "pass")
            # ---- 得分（综合得分 / 评审总得分 / 分项得分）----
            for ci, pri in (roles["score"] or {}).items():
                if ci >= len(r):
                    continue
                v = _score_value(r[ci])
                if v is None:
                    continue
                # 该行里的包标识（标项号 / 标包名称 / 分包 …）
                label = ""
                for li in range(len(headers)):
                    if li == ni or li >= len(r):
                        continue
                    lh = _nh(headers[li])
                    if any(x in lh for x in ("标项", "标包", "标段", "分包", "包组", "包件", "项目名称")):
                        cand = _blank(r[li]).strip()
                        if cand and len(cand) <= 80:
                            label = cand
                            break
                scores.append({"name": k, "raw": raw, "score": v,
                               "pri": pri, "label": label})
    return {"reviews": reviews, "review_reasons": review_reasons, "scores": scores}


def _wide_rows(rec: dict) -> List[List[str]]:
    """取「整页被塞进一行」的脏表格行（列数很多或单元格很长）。"""
    out: List[List[str]] = []
    for g in rec.get("main_item_tables") or []:
        hs = [_blank(c).strip() for c in (g.get("headers") or [])]
        if len(hs) >= 8 or any(len(c) > 60 for c in hs):
            out.append(hs)
        for r in g.get("rows") or []:
            rr = [_blank(c).strip() for c in r]
            if len(rr) >= 8 or any(len(c) > 60 for c in rr):
                out.append(rr)
    return out


def _wide_anchor(c: str) -> bool:
    t = _nh(c)
    return bool(t) and len(t) <= 20 and bool(_WIDE_ANCHOR.search(t)) \
        and bool(_REASON_COL_WORD_RE.search(t))


def _wide_hdr_stop(c: str) -> bool:
    t = _nh(c)
    if not t or len(t) > 20:
        return False
    return bool(_WIDE_HDR_STOP.match(t)) or bool(
        re.match(r"^(?:未中标|未成交|被拒绝).{0,4}原因$", t))


def _wide_name_cell(c: str, roster: Set[str]):
    """宽表里某个单元格是否给出了供应商名；不是则 None。

    `roster` 里的名字既有原文写法也有归一定式，两种都认（原文常被插入空格/全角括号）。
    """
    v = _blank(c).strip()
    if not v:
        return None
    m = _NAME_PREFIX_RE.match(v)
    if m:
        nm = v[m.end():].strip()
        if not nm:
            return None
        return nm if (nm in roster or (_norm_pair(nm) & _roster_norms(roster))) else nm
    if v in roster or (_norm_pair(v) & _roster_norms(roster)):
        return v
    if 5 <= len(v) <= 40 and _NAME_TAIL_RE.search(v) and not re.search(r"[:：]", v):
        return v
    return None


def _roster_norms(roster: Set[str]) -> Set[str]:
    """花名册的归一化形式集合（带缓存，避免逐单元格重算）。"""
    key = id(roster)
    hit = _ROSTER_NORM_CACHE.get(key)
    if hit is None or hit[0] is not roster or hit[1] != len(roster):
        out: Set[str] = set()
        for x in roster or ():
            out |= _norm_pair(x)
        hit = (roster, len(roster), out)
        _ROSTER_NORM_CACHE[key] = hit
        if len(_ROSTER_NORM_CACHE) > 4096:
            _ROSTER_NORM_CACHE.clear()
    return hit[2]


def _wide_recover(rec: dict, roster: Set[str]) -> dict:
    """从脏宽表里恢复 `{原因, 审查: {名->'fail'/'pass'}, 审查原因: {名->原文}}`。

    宽表的单元格是**打平后的正文片段**：锚点 = 纯「未中标（成交）原因」表头单元格，
    其后顺序扫；遇到下一个表头样单元格即结束本段。只认名册里的名字，对不上不猜。
    """
    reasons: Dict[str, str] = {}
    reviews: Dict[str, str] = {}
    review_reasons: Dict[str, str] = {}
    norms = _roster_norms(roster)
    for row in _wide_rows(rec):
        for i, c in enumerate(row):
            if not _wide_anchor(c):
                continue
            j, pending = i + 1, None
            while j < len(row):
                v = row[j]
                j += 1
                if not v:
                    continue
                if j > i + 2 and _wide_hdr_stop(v):
                    break
                nm = _wide_name_cell(v, roster)
                if nm:
                    pending = nm
                    continue
                if pending is None:
                    continue
                if 2 <= len(v) <= 200 and not _wide_anchor(v):
                    key = norm_name(pending)
                    if _is_fail(v):
                        reviews.setdefault(key, "fail")
                        rs = _clean_reason(v)
                        if rs:
                            review_reasons.setdefault(key, rs)
                    reasons.setdefault(key, _trim_reason(_clean_reason(v) or v))
                    pending = None
    return {"reasons": reasons, "reviews": reviews, "review_reasons": review_reasons}


def notice_evidence(aid: str, full_dir: str = None, roster: Optional[Set[str]] = None) -> dict:
    """逐篇证据（带缓存）：`{winners, bidders, reasons, reviewed, reviews, review_reasons, scores}`。

    * `winners` / `bidders`：`items_llm.json` 的 `announcement.winners/bidders`（原文抽取结果）；
    * `reasons`：原文「未中标原因」表 / 段落式小节 -> `{归一供应商名: 原因原文}`；
    * `reviewed`：能证明"参与了评审"的供应商名（未中标原因表 / 资格审查表名单）；
    * `reviews`：资格审查/符合性审查结果 -> `{归一供应商名: 'pass' | 'fail'}`；
    * `review_reasons`：`未通过` 的**原因原文** -> `{归一供应商名: 原文}`；
    * `scores`：得分候选明细 `[{name, raw, score, pri, label}]`（按包消歧，见 `_pick_score`）。

    `roster`（可选）：本篇已知的供应商名集合（标的行供应商 / 投标关系名 / 公告级名单）。
    只在**从脏宽表恢复**时用于切分——**只对能对上的名字**填，对不上不猜。
    """
    key = aid
    hit = _EVID_CACHE.get(key)
    if roster:
        hit = None                        # 给了名册就必须带上名册重算（结果依赖它）
    if hit is not None:
        return hit
    base = os.path.join(full_dir or FULL_DIR, aid)
    ev = {"winners": set(), "bidders": set(), "reasons": {}, "reviewed": set(),
          "reviews": {}, "review_reasons": {}, "scores": []}
    p = os.path.join(base, "items_llm.json")
    if os.path.exists(p):
        try:
            ann = (json.load(open(p, encoding="utf-8")) or {}).get("announcement") or {}
            ev["winners"] = {str(x).strip() for x in (ann.get("winners") or []) if str(x or "").strip()}
            ev["bidders"] = {str(x).strip() for x in (ann.get("bidders") or []) if str(x or "").strip()}
        except Exception:                                        # noqa: BLE001
            pass
    p = os.path.join(base, "ir.json")
    if os.path.exists(p):
        try:
            rec = json.load(open(p, encoding="utf-8")) or {}
            reasons, reviewed = _evidence_from_ir(rec)
            ev["reasons"] = reasons
            ev["reviewed"] = reviewed
            # ---- 资格审查结果 / 未通过原因 ----
            fld = _fields_from_ir(rec)
            ev["reviews"] = fld["reviews"]
            ev["review_reasons"] = fld["review_reasons"]
            ev["scores"] = fld["scores"]
            ev["ir"] = rec
        except Exception:                                        # noqa: BLE001
            pass
    _EVID_CACHE[key] = ev
    return ev


def _with_wide(ev: dict, roster: Set[str]) -> dict:
    """在逐篇证据上叠加「脏宽表恢复」（依赖名册，故不进逐篇缓存）。

    返回**新的 dict**（浅拷贝 + 三张表各自拷贝），避免污染缓存里的基础证据。
    """
    rec = ev.get("ir")
    if not rec:
        return ev
    ck = (id(ev), len(roster or ()))
    hit = _WIDE_CACHE.get(ck)
    if hit is not None and hit[0] is ev:
        return hit[1]
    out = dict(ev)
    out["reasons"] = dict(ev["reasons"])
    out["reviews"] = dict(ev["reviews"])
    out["review_reasons"] = dict(ev["review_reasons"])
    out["reviewed"] = set(ev["reviewed"])
    try:
        wide = _wide_recover(rec, roster or set())
    except Exception:                                            # noqa: BLE001
        wide = None
    if wide:
        for k, v in wide["reasons"].items():
            out["reasons"].setdefault(k, v)
            out["reviewed"].add(k)
        for k, v in wide["reviews"].items():
            out["reviews"].setdefault(k, v)
        for k, v in wide["review_reasons"].items():
            out["review_reasons"].setdefault(k, v)
    _WIDE_CACHE[ck] = (ev, out)
    if len(_WIDE_CACHE) > 4096:
        _WIDE_CACHE.clear()
    return out


# ==================================================================
# 按名查表（归一 + 剥包标注两种写法都试）
# ==================================================================

def _lookup(mapping: dict, name: str, default=None):
    """在 `{归一名称: 值}` 里按名取；`norm_name` 与 `norm_base_name` 两种写法都试。"""
    if not mapping or not name:
        return default
    for k in (norm_name(name), norm_base_name(name)):
        if k and k in mapping:
            return mapping[k]
    return default


def _lookup_any(mapping: dict, name: str, default=None):
    """在 `{*归一*名称: 值}` 里按名取；`_norm_pair` 的两种写法都试（覆盖段落式键）。"""
    if not mapping or not name:
        return default
    for k in _norm_pair(name):
        if k and k in mapping:
            return mapping[k]
    return default


def _matches_label(label: str, *texts) -> bool:
    """包标识是否与给定文本之一相符（宽匹配：原始串包含、归一后包含、包 token 命中）。"""
    if not label:
        return True                      # 表里没给包标识 -> 不做包级判定
    nl, np_ = _nh(label), _norm_pair(label)
    for t in texts:
        t = _blank(t).strip()
        if not t:
            continue
        if t in label or label in t:
            return True
        if _norm_pair(t) & np_:
            return True
    tk = {k for _tok, k in _pkg_tokens(label)}
    for t in texts:
        for _tok, k in _pkg_tokens(t):
            if k in tk:
                return True
    return False


def _pick_score(cands: List[dict], name: str, *label_texts):
    """从得分候选里挑**唯一可信**的一个。

    * 只留名称匹配（归一/剥包标注）且包标识相符的候选；
    * 优先「综合/总分」列（pri 最小）；同一优先级下若出现**多个不同数值**说明
      同名被多包共用或原文本身有歧义 -> 留空（宁缺勿造）。
    """
    hit = [c for c in (cands or [])
           if name and (_norm_pair(c["name"]) & _norm_pair(name))
           and _matches_label(c.get("label", ""), *label_texts)]
    if not hit:
        return None
    best = min(c["pri"] for c in hit)
    vals = {c["score"] for c in hit if c["pri"] == best}
    return vals.pop() if len(vals) == 1 else None


# ==================================================================
# 读平台库
# ==================================================================
def _fetch(db_path: str, table: str) -> List[dict]:
    """读一张表为 dict 列表；库/表不存在或读失败时返回 []（下载端点不因此 500）。"""
    if not db_path or not os.path.exists(db_path):
        return []
    try:
        con = sqlite3.connect(db_path)
    except sqlite3.Error:
        return []
    try:
        cur = con.execute(f'SELECT * FROM "{table}"')
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _classify(notice_type: str) -> str:
    """`类别`：招标/需求类 -> 「需求」；有公告类型 -> 「中标」；都没有留空。"""
    nt = notice_type or ""
    if any(k in nt for k in _NEED_KEYS):
        return "需求"
    return "中标" if nt else ""


# ==================================================================
# 核心：结构化数据 -> 三张表（两条产出路径共用）
# ==================================================================

def build_tables(notices: Sequence[dict], items: Sequence[dict],
                 bids: Sequence[dict] = (), entities: Optional[Dict[str, str]] = None,
                 aliases: Optional[Dict[str, Set[str]]] = None, *,
                 bidder_roster: Sequence[dict] = (),
                 full_dir: Optional[str] = None, html_dir: Optional[str] = None,
                 stats: Optional[dict] = None) -> Dict[str, List[dict]]:
    """把「公告 + 标的行 + 投标关系 + 实体 + 投标人名单」加工成官方三表。

    入参字段（多余键忽略；缺失键按空处理，不抛错）：

    * `notices`: `notice_id` / `project_name` / `purchaser` / `project_amount` / `notice_type`
    * `items`  : `announcement_id` / `package_no` / `supplier_name` / `is_winner` /
                 `item_name` / `category` / `brand` / `spec` / `quantity` /
                 `unit_price` / `total_price`
    * `bids`   : `notice_id` / `entity_id` / `supplier` / `is_winner` / `amount`
    * `entities`: `{entity_id: 规范名}`；`aliases`: `{entity_id: {别名…}}`
    * `bidder_roster`（可选）: 附件/正文《开标一览表》《报价明细》《评审结果》《资格审查名单》
                 《未中标（成交）原因表》逐行抽出的**全部投标人**（`ict_parse/bidders.py`）：
                 `notice_id` / `package_label` / `name` / `quote_yuan` / `score` /
                 `review` / `review_reason` / `lose_reason`。
                 这是官方「投标人表」里**参与人**的来源 —— 之前只取标的供货方 + bid 关系，
                 附件包数据集（无 HTML、无 bid 表）因此几乎只有中标人。
                 缺省为空 = 旧口径（全量库/提交件路径不传它，结果逐字不变）。

    返回 `{总表: [...], 标的表: [...], 投标人表: [...]}`，行是 dict（键 = 官方列名）。
    """
    entities = entities or {}
    aliases = aliases or {}
    st = stats if stats is not None else {}
    for k in ("multi", "pkg", "true_multi", "bids_total", "bids_placed", "bids_dropped",
              "notices_no_item_bids", "recover_p1_name_token", "recover_p2_norm_name",
              "recover_p3_html_rank", "recover_p4_alias", "still_dropped",
              "winner_not_in_items",
              "review_fail", "review_pass", "review_blank",
              "reason_filled", "reason_blank",
              "score_filled", "score_blank",
              "roster_total", "roster_placed", "roster_dropped", "roster_added"):
        st.setdefault(k, 0)

    # ---- 标的行噪音过滤（**唯一实现** = `ict_parse/noise.py`）----
    # 官方三表是**对外提交件**：`工业`/`总价:`/`投标人名称`/`……` 这类《中小企业声明函》
    # 的行业分类词与表格行标签绝不能被当成标的行写进标的表。判据、词表、开关都在
    # `ict_parse/noise.py`（`EDM_ITEM_NOISE_FILTER=1` 默认开）；这里只调用，不复制规则。
    # 两条产出路径（提交件 `export_official_format.py` / 平台下载 `build_from_platform`）
    # 都经过本函数，因此只需在这一处过滤 —— 且对已过滤过的输入是幂等的。
    _ctx = {_blank(n.get("notice_id")).strip(): (_blank(n.get("project_name")),
                                                 _blank(n.get("purchaser")))
            for n in notices}
    _nz = itemnoise.NoiseReport()
    by_notice_items: Dict[str, List[dict]] = {}
    for _aid, _lst in _group_by(items, "announcement_id").items():
        _proj, _pur = _ctx.get(_aid, ("", ""))
        by_notice_items[_aid] = itemnoise.filter_items(
            _lst, project_name=_proj, purchaser=_pur, report=_nz, aid=_aid)
    # 统计留痕（键名带 noise_ 前缀，不污染既有计数；报告里给出「丢了什么」）
    st["noise_total"] = _nz.total_in
    st["noise_dropped"] = _nz.dropped
    for _k, _v in _nz.hits.items():
        if _v:
            st["noise_" + _k.split("_")[0]] = _v
    by_notice_bids: Dict[str, List[dict]] = {}
    for b in bids:
        by_notice_bids.setdefault(_blank(b.get("notice_id")).strip(), []).append(b)
    # 投标人名单（参与人）：与 items / bids 一样按公告号索引
    by_notice_roster: Dict[str, List[dict]] = {}
    for r in bidder_roster or ():
        by_notice_roster.setdefault(_blank(r.get("notice_id")).strip(), []).append(r)
    # 「标的表兜底」判据 1：整批**没有任何 bid 投标关系行**（数据集不带任务二关系表）。
    # 有 bid 表就以它为准 -> 兜底永不生效 -> 全量库（graph.db 有 3240 条 bid）结果逐字不变。
    bid_table_absent = not by_notice_bids

    total_rows: List[dict] = []
    item_rows: List[dict] = []
    bidder_rows: List[dict] = []

    for n in notices:
        aid = _blank(n.get("notice_id")).strip()
        n_items = by_notice_items.get(aid, [])
        n_bids = by_notice_bids.get(aid, [])
        ntype = _blank(n.get("notice_type")).strip()
        kind = _classify(ntype)

        # ---- 标段/合同包：唯一由 packages.py 计算（三张表共用同一份包计划，保证自洽）----
        labels, groups, suffixes, uuids = pkgplan.plan_items(
            n_items, aid, name_key="item_name", pkg_key_col="package_no")
        if not groups:
            groups = {NO_PACKAGE: []}
        real_pkgs = [p for p in groups if p != NO_PACKAGE]
        if len(groups) >= 2:                       # 分包判据与 packages.uuid_suffixes 一致
            st["multi"] = st.get("multi", 0) + 1
            st["pkg"] = st.get("pkg", 0) + len(real_pkgs)
            if len(real_pkgs) >= 2:
                st["true_multi"] = st.get("true_multi", 0) + 1

        # ---- 「标的表兜底」：本篇要不要把标的行的供货方当中标人 ----
        # 判据（逐条，缺一不可；与 integrations/neo4j_sync.py 同一口径）：
        #   1. 整批没有 bid 投标关系行（bid_table_absent）；
        #   2. 本篇标的行的 is_winner **逐行为空**（`_items_unlabelled`）；
        #   3. 该行 supplier_name 非空（下面按 sup_order 取，空名不参与）。
        # 依据：标的表 = 结果公示里「这个包由谁供什么」的成交标的，招标/需求类公告没有
        # 标的行；陪标方只在 bid 表/投标人表里，从不写进标的表 —— 所以缺 is_winner 标记时，
        # 标的供货方就是中标人（与既有「单一来源=中标方」兜底同一条口径）。
        item_fallback = bid_table_absent and _items_unlabelled(n_items)
        if item_fallback:
            st["item_fallback"] = st.get("item_fallback", 0) + 1

        # ---- 逐篇证据（公告级 winners + 原文未中标原因/资格审查名单/得分）----
        # 先建好本篇名册（投标关系名 + 实体规范名 + 别名），供「脏宽表」恢复切分用
        roster: set = set()
        for _b in n_bids:
            roster.add(_blank(_b.get("supplier")).strip())
            roster.add(_blank(entities.get(_b.get("entity_id"))).strip())
            for _al in (aliases.get(_b.get("entity_id")) or set()):
                roster.add(_blank(_al).strip())
        for _it in n_items:
            roster.add(_blank(_it.get("supplier_name")).strip())
        roster.discard("")
        ev = _with_wide(notice_evidence(aid, full_dir), roster)
        ev_win_norms: Set[str] = set()
        for w in ev["winners"]:
            ev_win_norms |= _norm_pair(w)

        # 供应商 -> 该供应商标的所属包（证据来源之一：标的行本身）
        sup_pkgs: Dict[str, list] = {}
        for i, it in enumerate(n_items):
            s = _blank(it.get("supplier_name")).strip()
            sup_pkgs.setdefault(s, [])
            if labels[i] not in sup_pkgs[s]:
                sup_pkgs[s].append(labels[i])
        # 归一化名称 -> 包集合（P2 判据）
        norm_sup_pkgs: Dict[str, set] = {}
        for i, it in enumerate(n_items):
            for k in _norm_pair(it.get("supplier_name")):
                norm_sup_pkgs.setdefault(k, set()).add(labels[i])
        # 原文排名分节的「包 -> 投标人」证据（P3 判据）；只在该篇确有丢弃时读一次
        html_ev: dict = {}
        html_ev_loaded = False

        # ---- 投标人 -> 包：单包篇全部归该包；分包篇按 P1..P4 逐条定位 ----
        pkg_bids: Dict[str, list] = {p: [] for p in groups}
        pkg_bids_ev: Dict[str, list] = {p: [] for p in groups}   # 仅原文档位补全（不参与中标判定）
        st["bids_total"] = st.get("bids_total", 0) + len(n_bids)
        single_label = list(groups)[0] if len(groups) == 1 else None

        # ---- 投标人名单（参与人）：逐行定位到本篇的包 ----
        # 来源 = 附件/正文表格里逐行列出的全部投标人（`ict_parse/bidders.py` 抽取 ->
        # 交付表 `bidder_roster.csv` -> 平台库 `t1_bidder`），判据见 `_roster_target`。
        # 无标的行的公告**不发**（投标人表会多出标的表没有的键，三表键集合就不自洽了）。
        roster_pkg: Dict[str, List[dict]] = {p: [] for p in groups}
        for _r in by_notice_roster.get(aid, []):
            if not n_items:
                st["roster_dropped"] = st.get("roster_dropped", 0) + 1
                continue
            st["roster_total"] = st.get("roster_total", 0) + 1
            _pkg = _roster_target(_r, groups, norm_sup_pkgs, single_label)
            if _pkg is None:
                st["roster_dropped"] = st.get("roster_dropped", 0) + 1
                continue
            roster_pkg.setdefault(_pkg, []).append(_r)
            st["roster_placed"] = st.get("roster_placed", 0) + 1

        for b in n_bids:
            name = (_blank(entities.get(b.get("entity_id"))) or _blank(b.get("supplier"))).strip()
            how = ""
            target = None
            if single_label is not None:
                target, how = single_label, "single"
            else:
                for key in (_blank(b.get("supplier")).strip(), name):
                    if key and sup_pkgs.get(key) and len(sup_pkgs[key]) == 1:
                        target, how = sup_pkgs[key][0], "item_row"
                        break
            if not n_items:
                # 无标的行时三表键集合会对不上 -> 不发（键集合约束，不是"定位失败"）
                st["bids_dropped"] = st.get("bids_dropped", 0) + 1
                st["notices_no_item_bids"] = st.get("notices_no_item_bids", 0) + 1
                continue
            if target is None:
                # ---- P1 投标人名称里带显式包标识 ----
                for txt in (_blank(b.get("supplier")), name):
                    for _tok, key in _pkg_tokens(txt):
                        got = _group_for_key(groups, key)
                        if got:
                            target, how = got, "p1_name_token"
                            break
                    if target:
                        break
            if target is None:
                # ---- P2 归一化名称命中唯一的「包 × 供应商」----
                for txt in (_blank(b.get("supplier")), name):
                    for k in _norm_pair(txt):
                        if norm_sup_pkgs.get(k) and len(norm_sup_pkgs[k]) == 1:
                            target, how = list(norm_sup_pkgs[k])[0], "p2_norm_name"
                            break
                    if target:
                        break
            if target is None:
                # ---- P4 实体别名命中唯一的「包 × 供应商」----
                acc = set()
                for al in (aliases.get(b.get("entity_id")) or set()):
                    for k in _norm_pair(al):
                        if norm_sup_pkgs.get(k):
                            acc |= norm_sup_pkgs[k]
                if len(acc) == 1:
                    target, how = list(acc)[0], "p4_alias"
            if target is None:
                # ---- P3 原文「评审得分排名」分节的「标段名称 -> 供应商名」----
                if not html_ev_loaded:
                    html_ev = html_pkg_bidders(aid, groups, roster, html_dir)
                    html_ev_loaded = True
                for ev_label, names in html_ev.items():
                    if any(_norm_pair(x) & _norm_pair(_blank(b.get("supplier")))
                           or _norm_pair(x) & _norm_pair(name) for x in names):
                        got = _label_group(groups, ev_label) or _group_for_evidence_label(
                            groups, ev_label)
                        if got:
                            target, how = got, "p3_html_rank"
                            break
            if target is None:
                st["bids_dropped"] = st.get("bids_dropped", 0) + 1
                st["still_dropped"] = st.get("still_dropped", 0) + 1
            else:
                # P3（原文排名分节）是**事后补全**：证据来自原文而非标的关系，
                # 因此只把它用于「投标人表」的行，**不参与总表中标方判定**——
                # 同一篇里 A 包的中标人若被补到 B 包，会串改 B 包的中标供应商与金额。
                if how == "p3_html_rank":
                    pkg_bids_ev.setdefault(target, []).append((b, name))
                else:
                    pkg_bids.setdefault(target, []).append((b, name))
                st["bids_placed"] = st.get("bids_placed", 0) + 1
                if how == "p1_name_token":
                    st["recover_p1_name_token"] = st.get("recover_p1_name_token", 0) + 1
                elif how == "p2_norm_name":
                    st["recover_p2_norm_name"] = st.get("recover_p2_norm_name", 0) + 1
                elif how == "p3_html_rank":
                    st["recover_p3_html_rank"] = st.get("recover_p3_html_rank", 0) + 1
                elif how == "p4_alias":
                    st["recover_p4_alias"] = st.get("recover_p4_alias", 0) + 1

        # 花名册补全：公告级 winners/bidders、已采纳的原文证据名、标的行供应商名
        #   —— 只为「脏宽表」按名字切分服务（名字必须本来就在本篇出现，不引入外部实体）
        roster_full: set = set(roster)
        roster_full |= {x for x in ev["winners"] if x}
        roster_full |= {x for x in ev["bidders"] if x}
        for _it in n_items:
            _s = _blank(_it.get("supplier_name")).strip()
            if _s:
                roster_full.add(_s)
        for _m in (ev["reviewed"], ev["reviews"], ev["reasons"]):
            try:
                roster_full |= {k for k in _m if k}
            except TypeError:
                pass

        # 名称展示用标的行写法（投标人名可能带标注后缀，如「（包2）阿里云计算有限公司」，
        # 而标的行是「阿里云计算有限公司」）；归一名 -> 标的行写法，找不到就保留原名。
        disp: Dict[str, str] = {}
        for _it in n_items:
            _s = _blank(_it.get("supplier_name")).strip()
            if _s:
                disp.setdefault(norm_name(_s), _s)
                disp.setdefault(norm_base_name(_s), _s)

        def _disp(x):
            return disp.get(norm_name(x)) or disp.get(norm_base_name(x)) or strip_pkg_affix(x)

        for pkg in groups:
            pitems = [n_items[i] for i in groups[pkg]]
            uuid = pkgplan.uuid_for(aid, suffixes.get(pkg))
            pkg_out = pkg or NO_PACKAGE

            # ---- 该包供应商（首现顺序）+ 报价合计 ----
            sup_order: List[str] = []
            for it in pitems:
                s = _blank(it.get("supplier_name")).strip()
                if s not in sup_order:
                    sup_order.append(s)
            item_amt: Dict[str, float] = {}
            for i in pitems:
                s = _blank(i.get("supplier_name")).strip()
                v = _num(i.get("total_price"))
                if v is not None:
                    item_amt[s] = item_amt.get(s, 0.0) + v
            bid_amt: Dict[str, float] = {}
            for b, name in pkg_bids.get(pkg, []):
                if name not in bid_amt and _num(b.get("amount")) is not None:
                    bid_amt[name] = _num(b.get("amount"))

            # ---- 中标人：三来源并集（bid.is_winner ∪ 标的行 is_winner ∪ 公告 winners）----
            # 三个来源都必须**能定位到本包**才算（定位不到就不写）：
            #   S1 标的行 is_winner  -> 本来就在本包标的行里；
            #   S2 bid.is_winner     -> 必须已由 P1/P2/P4/单包 判据落到本包（P3 原文档位不算，
            #                           见下方 pkg_bids_ev 的说明）；
            #   S3 公告 winners      -> 名称归一后必须命中本包标的行的供应商。
            #   S4 标的表兜底        -> 无 bid 表且本包标的行 is_winner 逐行为空时，本包
            #                           标的行的供货方即中标人（判据见上方 `item_fallback`）。
            in_pkg_norms: Set[str] = set()
            for s in sup_order:
                in_pkg_norms |= _norm_pair(s)
            cand: List[str] = []
            for it in pitems:
                if _truthy(it.get("is_winner")):
                    cand.append(_blank(it.get("supplier_name")).strip())
            if item_fallback:
                for s in sup_order:          # S4：空名会在下面的去重循环里被跳过
                    cand.append(s)
            pkg_win_bids = [(b, nm) for b, nm in pkg_bids.get(pkg, []) if _truthy(b.get("is_winner"))]
            for _b, nm in pkg_win_bids:
                cand.append(nm)
            for w in ev["winners"]:
                if _norm_pair(w) & in_pkg_norms:
                    cand.append(w)
            winners: List[str] = []
            seen_w: Set[str] = set()
            for w in cand:
                if not w:
                    continue
                key = norm_base_name(w)
                if key in seen_w:
                    continue
                seen_w.add(key)
                winners.append(w)
            if not winners:
                # 兜底（与「单一来源=中标方」同口径）：该包只有唯一供应商、且本包无中标证据
                cand_sups = [s for s in dict.fromkeys(sup_order) if s]
                if len(cand_sups) == 1 and not pkg_win_bids:
                    winners = cand_sups
            # 统计：本包中标人**不在本包标的行**里的条数（S2 原文档位而来，见报告风险项）
            for w in winners:
                if not (_norm_pair(w) & in_pkg_norms):
                    st["winner_not_in_items"] = st.get("winner_not_in_items", 0) + 1

            # ---- 总表：每个中标人一行；无中标人时留空一行 ----
            proj_amount = _num(n.get("project_amount"))
            uniq_winners = []
            seen_disp: Set[str] = set()
            for w in winners:
                d = _disp(w)
                if d and d not in seen_disp:
                    seen_disp.add(d)
                    uniq_winners.append(d)
            for w in uniq_winners or [None]:
                wbase = norm_base_name(w) if w else None
                amt = 0.0
                if w:
                    for i in pitems:
                        if wbase and norm_base_name(i.get("supplier_name")) == wbase:
                            v = _num(i.get("total_price"))
                            if v is not None:
                                amt += v
                if not amt and len(uniq_winners) <= 1:
                    amt = proj_amount or 0.0
                total_rows.append({
                    "序号": len(total_rows) + 1, "URL": "", "UUID": uuid,
                    "标段/合同包": pkg_out,
                    "项目名称": _blank(n.get("project_name")),
                    "采购单位": _blank(n.get("purchaser")),
                    "中标供应商": w or "",
                    "中标金额": to_wan_2(amt) if amt else None,
                })

            # ---- 投标人表：标的行的供应商 + 已定位到本包的投标关系行 + 投标人名单（参与人）----
            merged: Dict[str, bool] = {}
            for s in sup_order:
                merged.setdefault(s, False)
            for b, name in pkg_bids.get(pkg, []) + pkg_bids_ev.get(pkg, []):
                merged.setdefault(name, False)
            win_norms: Set[str] = set()
            for w in uniq_winners:
                win_norms |= _norm_pair(w)
            # 名单行按「归一名称」合成一个人（开标一览表给报价、评审结果表给得分、
            # 资格审查表给结论、未中标原因表给原因 —— 是同一个投标人的不同侧面）。
            roster_by_norm: Dict[str, dict] = {}
            for _r in roster_pkg.get(pkg, []):
                _nm = _blank(_r.get("name")).strip()
                if not _nm:
                    continue
                _k = norm_base_name(_nm) or norm_name(_nm)
                _cur = roster_by_norm.get(_k)
                if _cur is None:
                    _cur = {"name": strip_pkg_affix(_nm) or _nm,
                            "quote_yuan": None, "score": None, "review": "",
                            "review_reason": "", "lose_reason": ""}
                    roster_by_norm[_k] = _cur
                for _f in ("quote_yuan", "score", "review", "review_reason", "lose_reason"):
                    if _cur.get(_f) in (None, "") and _r.get(_f) not in (None, ""):
                        _cur[_f] = _r.get(_f)
            seen_norm: Set[str] = set()
            for sup in merged:
                win = bool(sup) and bool(_norm_pair(sup) & win_norms)
                merged[sup] = win
                amt = item_amt.get(sup, bid_amt.get(sup))
                reviewed = bool(sup) and norm_name(sup) in ev["reviewed"]
                # ---- 通过审查：原文资格审查/符合性审查结果优先 ----
                #   未通过 -> `未通过，<原因原文>`（样例口径：原因写在本单元格内，
                #   写「未通过，资质不符合要求」这一种写法）；
                #   原文判为通过 / 或中标人 / 或有参与评审证据 -> 通过；判不出来留空。
                vres = _lookup(ev["reviews"], sup) if sup else None
                if vres == "fail":
                    why = _lookup(ev["review_reasons"], sup) if sup else ""
                    verdict = "未通过，" + why if why else "未通过"
                elif vres == "pass" or win or reviewed:
                    verdict = LABEL_PASSED
                else:
                    verdict = ""
                # ---- 得分：原文明确披露的「综合得分 / 评审总得分 / 分项得分」----
                sc = _pick_score(ev["scores"], sup, pkg_out, uuid) if sup else None
                reason = ""
                if sup and not win:
                    reason = _lookup_any(ev["reasons"], sup) or _lookup(ev["reasons"], sup, "") or ""
                # ---- 名单里有这个人：只在**原值为空**时补（不覆盖既有证据）----
                _ro = roster_by_norm.get(norm_base_name(sup)) or roster_by_norm.get(norm_name(sup))
                if _ro:
                    if amt is None:
                        amt = _num(_ro.get("quote_yuan"))
                    if sc in (None, ""):
                        sc = _num(_ro.get("score"))
                    if not verdict:
                        if _ro.get("review") == "fail":
                            _w = _blank(_ro.get("review_reason")).strip()
                            verdict = "未通过，" + _w if _w else "未通过"
                        elif _ro.get("review") == "pass":
                            verdict = LABEL_PASSED
                    if not reason and not win:
                        reason = _blank(_ro.get("lose_reason")).strip()
                seen_norm |= _norm_pair(sup)
                st["review_fail" if verdict.startswith("未通过") else
                   ("review_pass" if verdict else "review_blank")] += 1
                st["reason_filled" if reason else "reason_blank"] += 1
                st["score_filled" if sc not in (None, "") else "score_blank"] += 1
                bidder_rows.append({
                    "序号": len(bidder_rows) + 1,
                    "UUID": uuid,
                    "标段/合同包": pkg_out,
                    "名称": sup,
                    # 类别优先级（用户要求，逐条）：中标方集合 -> 「中标人」（最高）；
                    # 原文判未通过 -> 「其他」（官方样例 E3：`其他` + `未通过，资质不符合要求`）；
                    # 其余 -> 「参与人」。中标方必然是"通过审查"的，两条规则实际不会冲突。
                    "类别": (LABEL_WINNER if win else
                             (LABEL_OTHER if verdict.startswith("未通过") else LABEL_BIDDER)),
                    "通过审查": verdict,
                    "报价（万元）": to_wan_2(amt) if amt else None,
                    "得分": sc if sc not in (None, "") else "",
                    # 未中标原因只回填参与人（原文表/段落/宽表里按 (公告, 供应商) 对上）
                    "未中标原因": reason,
                })

            # ---- 名单里**新面孔**行（= 参与人）：只补标的供货方/投标关系都没有的人 ----
            # 去重键 (UUID, 包, 归一名称)：与上面已写过的人（含中标方）按归一名称判同一人，
            # 因此「同一人既在名单又在标的中」只会出现一次，且走上面那条（中标人）口径。
            for _k, _r in roster_by_norm.items():
                _np = _norm_pair(_r["name"])
                if _np & seen_norm:
                    continue
                seen_norm |= _np
                win = bool(_np & win_norms)      # 兜底：正常到不了（中标方必已在 merged 里）
                vres = _blank(_r.get("review")).strip()
                if vres == "fail":
                    _w = _blank(_r.get("review_reason")).strip()
                    verdict = "未通过，" + _w if _w else "未通过"
                elif vres == "pass" or win:
                    verdict = LABEL_PASSED
                else:
                    # 原文没给审查结论 -> 只有在「参与评审」有证据时才判通过（宁缺勿造）
                    verdict = (LABEL_PASSED
                               if (norm_name(_r["name"]) in ev["reviewed"]) else "")
                sc = _num(_r.get("score"))
                if sc is None:
                    sc = _pick_score(ev["scores"], _r["name"], pkg_out, uuid)
                reason = ""
                if not win:
                    reason = (_blank(_r.get("lose_reason")).strip()
                              or _lookup_any(ev["reasons"], _r["name"])
                              or _lookup(ev["reasons"], _r["name"], "") or "")
                st["roster_added"] = st.get("roster_added", 0) + 1
                st["review_fail" if verdict.startswith("未通过") else
                   ("review_pass" if verdict else "review_blank")] += 1
                st["reason_filled" if reason else "reason_blank"] += 1
                st["score_filled" if sc not in (None, "") else "score_blank"] += 1
                bidder_rows.append({
                    "序号": len(bidder_rows) + 1,
                    "UUID": uuid,
                    "标段/合同包": pkg_out,
                    "名称": _r["name"],
                    # 优先级（用户要求）：中标方集合 -> 中标人（最高）；原文判未通过 -> 其他；
                    # 其余 -> 参与人。名单里的人若同时在标的/关系表里，上面那条已写，绝不会重复。
                    "类别": (LABEL_WINNER if win else
                             (LABEL_OTHER if verdict.startswith("未通过") else LABEL_BIDDER)),
                    "通过审查": verdict,
                    "报价（万元）": to_wan_2(_num(_r.get("quote_yuan")))
                    if _num(_r.get("quote_yuan")) else None,
                    "得分": sc if sc not in (None, "") else "",
                    "未中标原因": reason,
                })

        # ---- 标的表：一条标的一行 ----
        for idx, it in enumerate(n_items):
            item_rows.append({
                "序号": len(item_rows) + 1,
                "UUID": uuids[idx],
                "标段/合同包": labels[idx],
                "产品服务名称": _blank(it.get("item_name")),
                "品目": _blank(it.get("category")),
                "品牌": _blank(it.get("brand")),
                "规格型号": _blank(it.get("spec")),
                "数量": to_qty(it.get("quantity")),
                "单价": to_wan_keep(it.get("unit_price")),
                "总价": to_wan_keep(it.get("total_price")),
                "类别": kind,
            })

    return {SHEET_TOTAL: total_rows, SHEET_ITEM: item_rows, SHEET_BIDDER: bidder_rows}


def build_from_platform(platform_db: str = None, *,
                        full_dir: Optional[str] = None,
                        html_dir: Optional[str] = None,
                        stats: Optional[dict] = None) -> Dict[str, List[dict]]:
    """读**平台库**的 `t1_notice` / `t1_item` / `t1_bidder`（+ `bid` / `entity` / `entity_alias`）
    生成官方三表。

    数据源口径（与任务三下载语义一致）：
      * 标的行 = `t1_item`（上传解析落库的结果）；
      * 投标关系 = `bid`（`is_winner` 的中标标记，结构化、最可靠）；
      * **投标人名单 = `t1_bidder`**（上传解析时从《开标一览表》《报价明细》《评审结果》
        《资格审查名单》《未中标（成交）原因表》逐行抽出的全部投标人）——
        这是「参与人」的来源；表不存在时按空处理（旧库/无名单数据集 = 旧口径）；
      * 实体规范名 = `entity`，别名 = `entity_alias`（表不存在时按空处理）；
      * 公告级 winners 与原文「未中标原因/资格审查」证据来自 `out/full`（缺失则退化）。

    库为空/缺表时返回三张空表（仍是合法工作簿，只有表头）。
    """
    db = platform_db or DEFAULT_PLATFORM_DB
    raw_notices = _fetch(db, "t1_notice")
    raw_items = _fetch(db, "t1_item")

    order: List[str] = []
    seen: Set[str] = set()
    notices: List[dict] = []
    for n in raw_notices:
        aid = _blank(n.get("announcement_id")).strip()
        if aid in seen:
            continue
        seen.add(aid)
        order.append(aid)
        notices.append({
            "notice_id": aid,
            "project_name": _blank(n.get("project_name")),
            "purchaser": _blank(n.get("purchaser")),
            "project_amount": n.get("project_amount"),
            "notice_type": _blank(n.get("notice_type")).strip(),
        })
    # 只有标的、没有公告行的公告 -> 补一个空公告（保证标的表不丢行）
    for it in raw_items:
        aid = _blank(it.get("announcement_id")).strip()
        if aid not in seen:
            seen.add(aid)
            order.append(aid)
            notices.append({"notice_id": aid, "project_name": "", "purchaser": "",
                            "project_amount": None, "notice_type": ""})

    items = [{
        "announcement_id": _blank(it.get("announcement_id")).strip(),
        "package_no": _blank(it.get("package_no")).strip(),
        "supplier_name": _blank(it.get("supplier_name")),
        "is_winner": it.get("is_winner"),
        "item_name": _blank(it.get("item_name")),
        "category": _blank(it.get("category")),
        "brand": _blank(it.get("brand")),
        "spec": _blank(it.get("spec")),
        "quantity": it.get("quantity"),
        "unit_price": it.get("unit_price"),
        "total_price": it.get("total_price"),
    } for it in raw_items]

    bids = [{
        "notice_id": _blank(b.get("notice_id")).strip(),
        "entity_id": _blank(b.get("entity_id")).strip(),
        "supplier": _blank(b.get("supplier")),
        "is_winner": b.get("is_winner"),
        "amount": b.get("amount"),
    } for b in _fetch(db, "bid")]

    entities = {_blank(r.get("entity_id")).strip(): _blank(r.get("canonical_name"))
                for r in _fetch(db, "entity")}
    aliases: Dict[str, Set[str]] = {}
    for r in _fetch(db, "entity_alias"):
        nm = _blank(r.get("name")).strip()
        if nm:
            aliases.setdefault(_blank(r.get("entity_id")).strip(), set()).add(nm)

    # 投标人名单（参与人）：上传解析时落进 `t1_bidder`（见 `platform/ingest.py`）。
    # 表不存在 -> 空列表 -> 官方三表回到「只有中标方」的旧口径（不报错、不臆造）。
    bidder_roster = [{
        "notice_id": _blank(r.get("announcement_id")).strip(),
        "package_label": _blank(r.get("package_label")),
        "name": _blank(r.get("name")),
        "quote_yuan": r.get("quote_yuan"),
        "score": r.get("score"),
        "review": _blank(r.get("review")),
        "review_reason": _blank(r.get("review_reason")),
        "lose_reason": _blank(r.get("lose_reason")),
        "source": _blank(r.get("source")),
    } for r in _fetch(db, "t1_bidder")]

    return build_tables(notices, items, bids, entities, aliases,
                        bidder_roster=bidder_roster,
                        full_dir=full_dir, html_dir=html_dir, stats=stats)


# ==================================================================
# 输出：xlsx / csv
# ==================================================================

def _tables_or_build(tables: Optional[Dict[str, List[dict]]],
                     platform_db: Optional[str]) -> Dict[str, List[dict]]:
    if tables is None:
        return build_from_platform(platform_db)
    return tables


def write_xlsx(target, tables: Optional[Dict[str, List[dict]]] = None,
               platform_db: Optional[str] = None):
    """把三张表写成 xlsx（3 个 sheet，顺序 = 总表/标的表/投标人表）。

    `target` 可以是路径（str/Path），也可以是二进制流（如 io.BytesIO）——
    下载端点用后者，不必往磁盘落临时文件。
    """
    from openpyxl import Workbook                                  # noqa: PLC0415

    data = _tables_or_build(tables, platform_db)
    wb = Workbook()
    wb.remove(wb.active)
    for name in SHEET_NAMES:
        cols = COLUMNS[name]
        ws = wb.create_sheet(name)
        ws.append(cols)
        for row in data.get(name, []):
            ws.append([row.get(c) if row.get(c) is not None else None for c in cols])
        # 样例表明确要求：`得分` 为 float、保留两位小数（100 -> 100.00；不写 "100.00" 字符串）
        if "得分" in cols:
            ci = cols.index("得分") + 1
            for r in range(2, ws.max_row + 1):
                ws.cell(row=r, column=ci).number_format = "0.00"
        # 金额（万元）列：单元格值保持数值型，**显示格式**强制两位小数
        # （`123.4` 在 Excel 里显示 `123.40`；空单元格不动，不会凭空出现 0.00）
        for cname in MONEY2_COLUMNS.get(name, ()):
            if cname not in cols:
                continue
            ci = cols.index(cname) + 1
            for r in range(2, ws.max_row + 1):
                cell = ws.cell(row=r, column=ci)
                if cell.value is not None and cell.value != "":
                    cell.number_format = "0.00"
    wb.save(target)
    wb.close()
    return target


def _csv_text(name: str, rows: Sequence[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS[name], extrasaction="ignore")
    w.writeheader()
    money2 = set(MONEY2_COLUMNS.get(name, ()))
    for r in rows:
        w.writerow({c: (fmt_money2(r.get(c)) if c in money2 else fmt_num(r.get(c)))
                    for c in COLUMNS[name]})
    return buf.getvalue()


def csv_bytes(name: str, tables: Optional[Dict[str, List[dict]]] = None,
              platform_db: Optional[str] = None) -> bytes:
    """单张表的 CSV 字节（带 UTF-8 BOM，与既有下载件一致，Excel 直接可读）。"""
    if name not in COLUMNS:
        raise KeyError(name)
    data = _tables_or_build(tables, platform_db)
    return b"\xef\xbb\xbf" + _csv_text(name, data.get(name, [])).encode("utf-8")


def write_csvs(dirpath: str, tables: Optional[Dict[str, List[dict]]] = None,
               platform_db: Optional[str] = None) -> List[str]:
    """把三张表写成 `总表.csv` / `标的表.csv` / `投标人表.csv`，返回路径列表。"""
    data = _tables_or_build(tables, platform_db)
    os.makedirs(dirpath, exist_ok=True)
    out = []
    for name in SHEET_NAMES:
        p = os.path.join(dirpath, f"{name}.csv")
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            f.write(_csv_text(name, data.get(name, [])))
        out.append(p)
    return out


def meta(tables: Optional[Dict[str, List[dict]]] = None,
         platform_db: Optional[str] = None) -> dict:
    """行数 / 列名（供 `GET /export/official-meta` 与前端展示）。"""
    data = _tables_or_build(tables, platform_db)
    return {
        "tables": {name: {"rows": len(data.get(name, [])), "columns": list(COLUMNS[name])}
                   for name in SHEET_NAMES},
        "source": os.path.basename(platform_db or DEFAULT_PLATFORM_DB),
    }
