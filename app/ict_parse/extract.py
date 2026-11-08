"""任务一 · 规则抽取层 v0：IR -> 7 字段标的物明细表。

规则：
- 表头映射（多种列名归一，含同义词）
- 一格里多值按 `；`/`;` 拆行
- 数值/单位归一（万元/元、全角、千分位）
- 一致性校验（单价×数量≈总价）
来源：公告原文「主要标的信息」表 + 附件「报价明细表/一览表」PDF 表格。
"""
from __future__ import annotations

import re
import unicodedata

from .category import infer_category
from .clean import clean_project_name

# ---------- 表头归一到规范字段 ----------
ITEM_FIELDS = ["item_name", "category", "brand", "spec", "quantity", "unit", "unit_price", "total_price"]

HEADER_MAP = {
    # 供应商
    "供应商名称": "supplier_name", "供应商": "supplier_name", "中标供应商": "supplier_name",
    "成交供应商": "supplier_name", "成交供应商名称": "supplier_name", "投标人名称": "supplier_name", "投标人": "supplier_name",
    "中标供应商名称": "supplier_name", "企业名称": "supplier_name", "投标单位": "supplier_name",
    # 产品服务名称
    "货物名称": "item_name", "服务名称": "item_name", "名称": "item_name", "采购标的": "item_name",
    "标的名称": "item_name", "工程名称": "item_name", "报价明细内容": "item_name", "标名称": "item_name",
    "标的": "item_name", "产品名称": "item_name", "分项名称": "item_name", "项目内容": "item_name", "报价内容": "item_name", "项目名称": "item_name", "报价部分": "item_name",
    # 附件「清单/资产/设备」类表的名称列（缺了会导致整表产出 0 条）
    "资产名称": "item_name", "设备名称": "item_name", "商品名称": "item_name",
    "标的物名称": "item_name", "物料名称": "item_name", "采购名称": "item_name",
    "主要产品名称": "item_name", "产品/技术名称": "item_name", "产品技术名称": "item_name",
    "技术名称": "item_name", "工程内容": "item_name",
    # 品目
    "品目": "category", "品目名称": "category", "品目编号及品目名称": "category",
    # 品牌
    "品牌": "brand", "货物品牌": "brand",
    # 规格型号
    "规格型号": "spec", "型号": "spec", "规格": "spec", "货物型号": "spec",
    "规格要求": "spec", "规格参数": "spec", "型号规格": "spec", "货物规格": "spec",
    # 数量/单位
    "数量": "quantity", "货物数量": "quantity", "数量/单位": "quantity", "数量单位": "quantity",
    "单位": "unit", "计量单位": "unit",
    # 价格
    "单价": "unit_price", "货物单价": "unit_price", "含税单价": "unit_price",
    "金额": "total_price", "总价": "total_price", "合计": "total_price", "小计": "total_price",
    "合价": "total_price", "货物金额": "total_price", "标的金额": "total_price",
    "明细报价": "total_price", "报价": "total_price", "响应报价": "total_price", "投标报价": "total_price", "价格小计": "total_price",
    "成交金额": "total_price", "成交价": "total_price", "成交总价": "total_price", "中标（成交）金额": "total_price",
    # 注意：_nh 会消掉括号，所以「中标（成交）金额」实际归一到「中标金额」——
    # 下面这个才是真正生效的键（公告里极常见的「中标金额(元)/(万元)」都靠它）。
    "中标金额": "total_price", "中标总金额": "total_price", "成交总金额": "total_price",
    "中标价": "total_price",
    # 采购包/标段
    "包号": "package", "采购包": "package", "合同包": "package", "标段": "package", "标段号": "package", "成交包号": "package",
    "标项号": "package", "分包号": "package", "包组编号": "package",
    "标项名称": "package_name", "包名称": "package_name", "包组名称": "package_name", "标项": "package_name",
}

# 表头词；若它出现在数据行里（表头行泄漏）则跳过
HEADER_ISH = {
    "序号", "名称", "服务名称", "服务范围", "服务要求", "服务时间", "服务标准", "货物名称",
    "品目", "品牌", "规格型号", "数量", "单价", "金额", "采购内容", "包号", "单位",
}

# 「非标的」公示类表：业绩/获奖/评审/资格审查等。
# 它们常含「项目名称/项目内容」列，会被误映射成 item_name → 把历史业绩当标的物
# （实测占 item_name 假阳性的 31%，如「案例一览表：序号|项目内容|项目单位名称|标的」）。
_JUNK_TABLE_KW = (
    "业绩", "获奖", "客户名称", "签订合同", "详细信息", "中标候选人",
    "评审结果", "评标", "未中标原因", "资格审查", "符合性审查",
    # 业绩/案例类
    "项目单位名称", "案例一览", "合同签订日期", "项目单位的有效联系方式",
    # 节能/环境标志产品目录类（一级目录|二级目录|认证机构名录|依据的标准）
    "认证机构", "依据的标准", "一级目录", "二级目录",
)


def _is_junk_table(headers) -> bool:
    j = "".join(_nh(h) for h in headers)
    return any(k in j for k in _JUNK_TABLE_KW)


# 明显不是标的名称的「占位值 / 表头泄漏值」：置空，但保留该行其它字段（价格/数量等）
_JUNK_NAME = {
    "不涉及", "无", "无分包", "/", "-", "—", "…", "...", "合计", "小计", "总计",
    "产品名称", "名称", "序号", "标的", "标的名称", "项目名称", "采购内容", "采购标的",
    "服务名称", "货物名称", "品牌", "规格型号", "数量", "单价", "金额", "总价", "品目名称",
}


def _is_junk_name(v) -> bool:
    return isinstance(v, str) and (_nh(v) in _JUNK_NAME)


_SPLIT_RE = re.compile(r"[；;]\s*")
_MONEY_UNIT = {"元": 1, "圆": 1, "万": 10 ** 4, "万元": 10 ** 4, "亿": 10 ** 8, "亿元": 10 ** 8}
_RE_MONEY = re.compile(r"[¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(亿元|万元|亿|万|元|圆)?")
_RE_QTY = re.compile(r"^([0-9][0-9,]*(?:\.[0-9]+)?)\s*([^\d\s]{0,8})?$")
_RE_CODE = re.compile(r"项目编号[：:]\s*([^\s（(]+)")
_RE_SUP_TEXT = re.compile(r"供应商名称[：:]\s*([^\s，,。]+)")


def _nh(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")   # 全角/康熙部首(⽬⾼⼈) -> 标准字符
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"(.)\1+", r"\1", s)            # 折叠重复字符(附件PDF双打/重影，如 数数量量->数量)
    s = s.replace("：", ":")
    s = re.sub(r"\{[^}]*\}", "", s)            # 去掉 {报价形式…} 等花括号合并标注
    s = re.sub(r"\([^)]*\)", "", s)            # 去掉 (元)/(包) 等括注
    return s


def _canon(headers: list[str]) -> list[str | None]:
    out: list[str | None] = []
    for h in headers:
        nh = _nh(h)
        if nh not in HEADER_MAP and any(k in nh for k in ("率", "折扣", "让利", "折率")):
            out.append("rate")     # 率型列（费率/折扣率/优惠率/下浮率）——非单价
        elif any(k in str(h) for k in ("最高限价", "控制价", "预算")):
            out.append(None)       # 最高限价/控制价/预算价——非成交单价/总价
        else:
            out.append(HEADER_MAP.get(nh))
    return out


def _pick_header_row(t: list[list[str]], scan: int = 4) -> int | None:
    """表头不一定在第一行：若首行无可用列名，则在前几行里找映射命中>=2 的行。"""
    if sum(1 for c in _canon(t[0]) if c) >= 1:
        return 0
    for i in range(1, min(scan, len(t))):
        if sum(1 for c in _canon(t[i]) if c) >= 2:
            return i
    return None


def _pipe_table(text: str) -> list[list[str]]:
    """把 VL 输出的管道符文本解析成表格行：`a | b | c`。"""
    rows: list[list[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        if set(line) <= set("|-: "):
            continue  # 跳过 markdown 分隔行
        rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows


def _split(v):
    if v is None:
        return [None]
    parts = [p.strip() for p in _SPLIT_RE.split(str(v))]
    return parts or [None]


_RATE_RE = re.compile(r"[%％]|折扣|优惠率|费率|下浮|浮动率|指导价")


def _canon_num(x):
    """数值规范化：**整数值输出 int**（与 gf/官方数据表的写法一致，避免 264800.0 与 264800 被判不等），
    非整数保留 2 位小数。"""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return x
    return int(f) if f == int(f) else round(f, 2)


def norm_money(s):
    if s is None:
        return None
    raw = str(s)
    # 官方口径①：非明确金额(百分比/费率/指导价/折扣等)不当金额
    if ("%" in raw or "％" in raw) or (_RATE_RE.search(raw) and "元" not in raw):
        return None
    t = unicodedata.normalize("NFKC", raw)          # 全角(含（）) -> 半角
    t = re.sub(r"[\s,()]", "", t)                   # 去空白/千分位/括号，修 195（万元） 匹配不到“万元”
    m = _RE_MONEY.search(t)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    return _canon_num(num * _MONEY_UNIT.get(m.group(2) or "元", 1))


def norm_qty(s):
    if s is None:
        return (None, None)
    t = re.sub(r"\s+", "", str(s))
    m = _RE_QTY.match(t)
    if not m:
        return (None, None)
    try:
        q = float(m.group(1).replace(",", ""))
    except ValueError:
        return (None, None)
    return (_canon_num(q), m.group(2) or None)


_RE_SUP_PKG = (
    re.compile(r"^[（(]\s*(?:第)?\s*(?:[0-9]+|[一二三四五六七八九十]+)\s*(?:包|标段)\s*[）)]\s*"),
    re.compile(r"^(?:第)?\s*(?:[0-9]+|[一二三四五六七八九十]+)\s*(?:包|标段)\s*[：:]\s*"),
)


def _clean_supplier(s):
    if not s:
        return s
    s = unicodedata.normalize("NFKC", str(s)).strip()   # 康熙部首/全角 -> 标准字符
    s = re.sub(r"^第?\s*[0-9]+\s*包[^：:]*[：:]", "", s)  # 去「第3包 xxx：」前缀
    for r in _RE_SUP_PKG:
        s = r.sub("", s)                                 # 去「（包1）」「一标段：」前缀
    return s.strip() or None


def _pkg_from_name(name: str) -> str | None:
    """从文件名/文本推断采购包号（包1/第1包/合同包1/采购包1）。"""
    m = re.search(r"合同包\s*([0-9]+)|采购包\s*[（(]?\s*([0-9]+)|第\s*([0-9]+)\s*包|包\s*[（(]\s*([0-9]+)", name or "")
    if m:
        for grp in m.groups():
            if grp:
                return grp
    return None


# ---------- 明细抽取 ----------
def items_from_table(headers, rows, *, default_supplier=None, source="MAIN_ITEM_TABLE", package_no=None):
    if _is_junk_table(headers):
        return []                      # 业绩/获奖/评审类公示表：不是标的物
    canon = _canon(headers)
    colmeta = []
    for h in headers:
        nh = _nh(h)
        mu = re.search(r"元\s*/\s*([^\s)）]+)", str(h))
        hs = str(h)
        tax = False if "不含税" in hs else (True if "含税" in hs else None)
        colmeta.append({"price_unit": mu.group(1) if mu else None, "tax": tax})
    items = []
    for r in rows:
        cols = {}
        colm = {}
        for i, c in enumerate(canon):
            if c is None:
                continue
            cols[c] = _split(r[i] if i < len(r) else None)
            colm[c] = colmeta[i]
        if not any(k in cols for k in ("item_name", "brand", "spec")):
            continue  # 需含标的名/品牌/规格列，排除中标结果表、投标报价表、评审表
        n = 1
        for c in ("item_name", "brand", "spec", "quantity", "unit_price", "total_price"):
            if c in cols:
                n = max(n, len(cols[c]))

        def g(c, k):
            arr = cols.get(c)
            if not arr:
                return None
            if k < len(arr):
                return arr[k]
            return arr[-1] if len(arr) == 1 else None

        for k in range(n):
            q, u = norm_qty(g("quantity", k))
            item = {
                "item_name": g("item_name", k),
                "category": g("category", k),
                "brand": g("brand", k),
                "spec": g("spec", k),
                "quantity": q,
                "unit": g("unit", k) or u,
                "unit_price": norm_money(g("unit_price", k)),
                "total_price": norm_money(g("total_price", k)),
                "rate": g("rate", k),
                "supplier_name": _clean_supplier(g("supplier_name", k)) or default_supplier,
                "package_no": g("package", k) or package_no,
                "package_name": g("package_name", k),
                "price_unit": next((cm["price_unit"] for cm in colmeta if cm["price_unit"]), None),
                "tax_included": next((cm["tax"] for cm in colmeta if cm["tax"] is not None), None),
                "source": source,
                "derived": False,
                "needs_review": False,
                "raw": {headers[i]: (r[i] if i < len(r) else None) for i in range(len(headers))},
            }
            rv = item.get("rate")
            if rv is not None:
                rs = str(rv).strip()
                is_rate = ("%" in rs or "％" in rs) or (re.fullmatch(r"\d+(?:\.\d+)?", rs) is not None and float(rs) < 100)
                if not is_rate:                       # 该列实为金额(大数值/带元) -> 当单价
                    if item["unit_price"] is None:
                        item["unit_price"] = norm_money(rs)
                    item["rate"] = None
            item["item_type"] = _item_type(item)
            _reconcile(item)
            nm = item.get("item_name")
            if _is_junk_name(nm):
                item["item_name"] = None      # 占位/表头泄漏值不当名称；该行价格等仍保留
                nm = None
            if isinstance(nm, str) and nm.strip() in HEADER_ISH and not (item["unit_price"] or item["total_price"]):
                continue  # 表头行泄漏
            if not any(item[f] for f in ("item_name", "unit_price", "total_price")):
                continue
            items.append(item)
    return items


def _item_type(item):
    cat = item.get("category")
    if cat:
        seg = str(cat).split("/")[0]
        if seg in ("货物", "服务", "工程"):
            return seg
    if item.get("brand") or item.get("spec"):
        return "货物"
    return None


def _reconcile(item):
    up, q, tp = item.get("unit_price"), item.get("quantity"), item.get("total_price")
    if tp is None and up is not None and q:
        item["total_price"] = _canon_num(up * q)   # 单价×数量 -> 总价
        item["derived"] = True
    if up is None and tp is not None and q:
        item["unit_price"] = _canon_num(tp / q)    # 总价/数量 -> 单价（官方口径④）
        item["derived"] = True
    if up and q and item.get("total_price"):
        base = max(abs(item["total_price"]), 1)
        if abs(up * q - item["total_price"]) / base > 0.01:
            item["needs_review"] = True


# ---------- 公告级 ----------
def _notice_type(title):
    for k in ("中标", "成交", "结果", "更正", "终止"):
        if k in (title or ""):
            return k
    return "其他"


def _project_code(rec):
    for k, v in rec.get("sections", {}).items():
        if "项目编号" in k:
            m = _RE_CODE.search(k) or _RE_CODE.search(v)
            if m:
                return m.group(1)
    m = _RE_CODE.search(rec.get("content_text", ""))
    return m.group(1) if m else None


def _section_text(rec, *keys):
    for k, v in rec.get("sections", {}).items():
        if any(x in k for x in keys):
            return v
    return None


_SUP_HEADER_KEYS = ("供应商名称", "供应商", "中标供应商", "成交供应商", "投标人名称", "投标人",
                    "中标供应商名称", "投标单位", "投标单位名称", "企业名称", "单位名称")
# 这些表列出的是**参与投标人**（含中标方）：评审结果/资格审查/未中标原因/报价公示…
_BIDDER_TABLE_KW = ("未中标", "评审结果", "资格审查", "符合性", "报价公示", "投标单位",
                    "评审情况", "开标记录", "评标结果", "未通过", "通过")
_CO_SUFFIX = ("公司", "厂", "中心", "研究院", "研究所", "集团", "事务所", "银行", "大学",
              "学院", "学校", "合作社", "联合体", "商行", "经营部", "门市部", "农场", "林场")
_RE_CO_IN_NAME = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{4,42}?(?:" + "|".join(_CO_SUFFIX) + r")")
# 排除：招标/采购/代理/交易等机构名与文件名词（避免把采购人或模板名当投标人）
_CO_NAME_BAD = ("招标", "采购", "代理", "公共资源", "交易中心", "声明函", "报价", "明细",
                "一览", "附件", "文件", "模板", "清单", "图纸", "合同")
# 表头词/占位词被当成主体名的兜底黑名单（实测「企业名称」曾以 15 次中标进入 TOP5）
_JUNK_ENTITY = {
    "企业名称", "供应商名称", "单位名称", "投标人名称", "中标供应商名称", "中标供应商",
    "成交供应商名称", "成交供应商", "供应商", "投标人", "投标单位", "名称", "序号", "单位",
    "合计", "小计", "备注", "无", "不涉及", "详见附件", "第一名", "第二名", "第三名",
    "中标（成交）金额", "评审结果", "资格审查", "通过",
}
_RE_PAREN = re.compile(r"[（(]([^（）()]{4,60})[）)]")
_VALID_SUFFIX_RE = re.compile(r"(?:" + "|".join(_CO_SUFFIX) + r")[）)】]?$")
# 报价/评审附件的轮次与序次前缀（实测「第1轮-山东世纪读秀信息科技有限公司」与原名并存，
# 会把同一实体拆成两家，污染共同竞标分析）
_RE_ROUND_PREFIX = re.compile(
    r"^\s*(?:第\s*[0-9一二三四五六七八九十]+\s*(?:轮|次|阶段|包|标段)\s*[-—－–:：.]?\s*"
    r"|最终(?:报价|轮)?\s*[-—－–:：.]?\s*|首轮\s*[-—－–:：.]?\s*|[0-9]{1,2}\s*[-—－–.、]\s*)+")


def _valid_entity(x) -> bool:
    """是不是一个「像市场主体名称」的实体。

    用于挡住附件名/表格里混进来的非主体词（实测「投标人承诺函」「包1供应商评审情况表」
    曾污染共同竞标分析）。
    """
    s = str(x or "").strip()
    if not (4 <= len(s) <= 60) or s in _JUNK_ENTITY:
        return False
    if any(b in s for b in _CO_NAME_BAD):
        return False
    if _VALID_SUFFIX_RE.search(s):
        return True
    # 弱条件：包含机构词且足够长（如「…商贸行(个体工商户)」）
    return any(suf in s for suf in _CO_SUFFIX) and len(s) >= 6


def _company_names(text: str) -> list[str]:
    """从文本（此处用于附件文件名）中抽主体名称。

    优先取**括号内**的内容 —— 声明函/报价表普遍命名成 `xxx（公司全称）.pdf`，
    直接对整串做后缀匹配会切出「中国移动通信集团」这类截断名（实测污染共同竞标分析）。
    统一做 NFKC 归一：附件 PDF 里常见康熙部首变体（`⼈`/`⼼`/`⽀`），
    不归一会把同一实体算成两家（这是「实体归一」的主要收益来源）。
    """
    text = unicodedata.normalize("NFKC", str(text or ""))
    cands = _RE_PAREN.findall(text) or [text]
    out: list[str] = []
    for c in cands:
        c = _RE_ROUND_PREFIX.sub("", c.strip()).strip()
        if not c.endswith(_CO_SUFFIX):
            cut = None
            for suf in _CO_SUFFIX:
                i = c.find(suf)
                if i >= 0:
                    cut = c[: i + len(suf)]
                    break
            if cut is None:
                continue                     # 找不到机构后缀 -> 不是主体名，直接丢弃
            c = cut
        c = _RE_ROUND_PREFIX.sub("", c).strip()
        if _valid_entity(c):
            out.append(c)
    return out


def _bidders_from_attachments(rec, exclude=()) -> list[str]:
    """从附件名抽参与投标方。

    实测依据：中小企业声明函、投标（响应）报价明细表等**一个投标人一份**，
    文件名常直接含投标人全称（如 `…声明函（陕西瑞铭尔医疗器械有限公司）.pdf`，实测 657 篇有此类附件）。
    只信任「声明函/报价/投标/评审/成交」语境的附件，并排除采购人/项目名/模板名。
    """
    out = []
    for a in rec.get("extracted_files", []):
        nm = (a.get("name") or "") or (a.get("rel_path") or "")
        if not any(k in nm for k in ("声明函", "报价", "投标", "评审", "资格审查", "成交")):
            continue
        base = nm.replace("\\", "/").split("/")[-1]
        base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", base)
        for s in _company_names(base):
            if any(x and (s in x or x in s) for x in exclude):
                continue
            out.append(s)
    return out


_RE_TXT_ORG = re.compile(
    r"(?:供应商|投标人|报价人|响应人|中标人|成交供应商|磋商人|竞标人|单位|企业)(?:名称)?[：:]\s*"
    r"([^\s，,。；;：:、|]{4,40})")


def _bidders_from_attachment_text(rec, exclude=()) -> list[str]:
    """从**附件正文**抽参与投标方（声明函/投标函/报价表正文里的「供应商名称：xxx」）。

    实测：12140 个附件有文本，其中 **1212 个**含「xx名称：」式主体信号 —— 比只读文件名更全。
    只扫描名称与投标相关的附件，并限制每份前 6000 字符以控制成本。
    """
    out: list[str] = []
    for a in rec.get("extracted_files", []):
        t = unicodedata.normalize("NFKC", a.get("text") or "")
        if not t:
            continue
        for m in _RE_TXT_ORG.finditer(t[:6000]):
            # 取标签后的一个短窗口，再用后缀锚定的 _company_names 抽，
            # 避免把后续正文（如「…有限公司）的（项目名称」）一起吞进来
            win = re.split(r"[\n\r]", t[m.start(1): m.start(1) + 60])[0]
            for s in _company_names(win):
                if not any(x and (x in s or s in x) for x in exclude):
                    out.append(s)
    return out


_RE_TXT_USCC = re.compile(r"统一社会信用代码[：:\s]*([0-9A-HJ-NPQRTUWXY]{18})")
_RE_USCC_COL = ("统一社会信用代码", "统一社会信用代码/税号", "社会信用代码", "信用代码")


def _supplier_uscc(rec) -> dict:
    """采集「供应商名 -> 统一社会信用代码」映射（原文表优先，其次附件正文）。

    用途：作为**实体唯一键**做关系建模的消歧（同名不同实体/异名同实体）。
    实测源数据里 USCC 公布率不高（102 篇有列、42 组有效对），故同时保留名称归一作为回退。
    """
    m: dict[str, str] = {}
    for g in rec.get("main_item_tables", []):
        hdr = g.get("headers") or []
        canon = [_nh(h) for h in hdr]
        ui = next((i for i, c in enumerate(canon) if c in {_nh(k) for k in _RE_USCC_COL}), None)
        if ui is None:
            continue
        si = next((i for i, c in enumerate(canon) if c in {_nh(k) for k in _SUP_HEADER_KEYS}), None)
        if si is None:
            continue
        for r in g.get("rows", []):
            if ui < len(r) and si < len(r):
                u = str(r[ui]).strip().replace(" ", "").upper()
                nm = _clean_supplier(r[si])
                if len(u) == 18 and nm and _valid_entity(nm):
                    m[nm] = u
    for a in rec.get("extracted_files", []):
        t = a.get("text") or ""
        if not t:
            continue
        for mm in _RE_TXT_USCC.finditer(t[:6000]):
            ctx = t[max(0, mm.start() - 120):mm.start()]
            for nm in _company_names(ctx[-60:]):
                m.setdefault(nm, mm.group(1))
    return m


_RE_PROC_MODE = re.compile(r"采购方式[：:]\s*([^\s，,。；;（()）]{2,14})")
_PROC_KW = ("单一来源", "竞争性磋商", "竞争性谈判", "询价采购", "邀请招标", "公开招标",
            "框架协议", "网上竞价", "定点采购", "电子卖场")


def _procurement_mode(rec) -> str | None:
    """采购方式（结构化抽取，避免用关键字裸匹配造成过度命中）。

    优先取正文「采购方式：X」，其次概要字段，最后标题关键词。
    用途：任务二用户画像的维度（单一来源/竞争性磋商/公开招标…），
    也用于解释「为什么该篇只有一个投标主体」。
    """
    m = _RE_PROC_MODE.search((rec.get("content_text") or "")[:8000])
    if m:
        return m.group(1).strip()
    sm = rec.get("summary") or {}
    for k in ("采购方式", "招标方式"):
        if sm.get(k):
            return str(sm[k]).strip()
    t = rec.get("article_title") or ""
    for kw in _PROC_KW:
        if kw in t:
            return kw
    return None


_RE_MFR = (
    # 「制造商为XXX（企业名称）」——名字在标签**之前**（中小企业声明函常见写法）
    re.compile(r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{4,42}?)\s*[（(](?:企业|单位)名称[）)]"),
    re.compile(r"制造商为\s*([^\s（(，,。；;、]{4,40})"),
    re.compile(r"(?:制造商|生产厂家|生产企业)(?:名称)?[：:]\s*([^\s，,。；;（(]{4,40})"),
)
_RE_MFR_PREFIX = re.compile(r"^(?:制造商|生产厂家|生产企业)(?:为|是)?\s*")
# 声明函正文里的模板语/占位符（实测会抽出「本公司(联合体)参加」这类非主体名）
_MFR_BAD = ("本公司", "联合体", "参加", "采购单位", "项目名称", "声明函", "标的名称",
            "{", "}", "如下", "以上", "从业人员", "营业收入")


def _manufacturers_from_declaration(rec, exclude=()) -> list[str]:
    """从**中小企业声明函正文**抽产品供应商（制造商）。

    实测写法：`制造商为伽师县昆仑肉业有限责任公司（企业名称）` —— 名称在标签前，
    通用的「xx名称：」后置写法抽不到。注意这是**产品供应商**（品牌/制造方）而非投标人，
    正好补 S1「中标供应商 + 产品供应商」中的后者。
    模板型声明函（正文是 `{供应商名称}` 占位符）无解，属数据本身不披露。
    """
    out: list[str] = []
    for a in rec.get("extracted_files", []):
        nm = a.get("name") or ""
        t = a.get("text") or ""
        if not t or ("声明函" not in nm and "中小企业" not in t[:300]):
            continue
        t = unicodedata.normalize("NFKC", t)[:8000]
        for rx in _RE_MFR:
            for m in rx.finditer(t):
                s = _RE_ROUND_PREFIX.sub("", m.group(1).strip().strip("（）()")).strip()
                s = _RE_MFR_PREFIX.sub("", s).strip()
                if any(b in s for b in _MFR_BAD):
                    continue
                # 括号必须闭合（否则是截断名，如「六地质大队(河北省…应用中心」）
                if (s.count("（") + s.count("(")) != (s.count("）") + s.count(")")):
                    continue
                if _valid_entity(s) and not any(x and (x in s or s in x) for x in exclude):
                    out.append(s)
    return out


def _winners_bidders(rec):
    """返回 (winners, losers)。losers = 参与投标人 - 中标人（赛题对「投标参与方」的定义）。"""
    winners, participants = [], []
    norm_keys = {_nh(k) for k in _SUP_HEADER_KEYS}
    for g in rec.get("main_item_tables", []):
        headers = g.get("headers") or []
        j = "".join(headers)
        sup = None
        for i, h in enumerate(headers):
            if _nh(h) in norm_keys:
                sup = i
                break
        if sup is None:
            continue
        names = []
        for r in g["rows"]:
            if sup >= len(r) or not r[sup]:
                continue
            nm = _clean_supplier(r[sup])
            if (nm and not any(c in nm for c in ",，、") and "名单" not in nm
                    and len(nm) >= 4 and _valid_entity(nm)):
                names.append(nm)
        # 评审结果/资格审查/未中标原因/报价公示等表列出的是「参与投标人」
        if any(k in j for k in _BIDDER_TABLE_KW):
            participants.extend(names)
        # 中标人：注意「未中标」表也含「中标」子串，必须排除，否则会把未中标方误判为中标方
        if ("中标" in j or "成交" in j) and "未中标" not in j:
            winners.extend(names)
    # 正文「三、中标(成交)信息」里的 供应商名称：
    sec = _section_text(rec, "中标", "成交", "采购结果") or ""
    winners.extend(_RE_SUP_TEXT.findall(sec))
    # 附件名线索（声明函/报价表一人一份）
    sm = rec.get("summary", {}) or {}
    participants.extend(_bidders_from_attachments(rec, exclude=(sm.get("采购单位"), sm.get("采购项目名称"))))
    # 附件正文线索（比文件名更全：1212 个附件含「xx名称：」）
    participants.extend(_bidders_from_attachment_text(rec, exclude=(sm.get("采购单位"), sm.get("采购项目名称"))))

    def dedup(xs):
        seen, out = set(), []
        for x in xs:
            x = _RE_ROUND_PREFIX.sub("", str(x or "").strip()).strip()
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    def merge_prefix(xs):
        """同篇内若 A 是 B 的严格前缀，视为 A 被截断，丢弃 A（保留全称）。"""
        xs = list(xs)
        return [a for i, a in enumerate(xs)
                if not any(a != b and b.startswith(a) and len(b) > len(a)
                           for j, b in enumerate(xs) if j != i)]

    w = merge_prefix(dedup(winners))
    losers = [x for x in merge_prefix(dedup(participants)) if x not in set(w)]
    return w, losers


def _rate_col_kind(rec: dict):
    """存在“率型”价格列(费率/折扣率/优惠率/下浮率)且其值确为率(带% 或 裸数<100)时才认定。"""
    def kind_of(h):
        nh = _nh(h)
        if "下浮" in nh:
            return "下浮率"
        if "费率" in nh or "取费" in nh:
            return "费率"
        if "折扣" in nh or "优惠" in nh or "让利" in nh:
            return "折扣率"
        return None

    def rate_like(v):
        s = str(v).strip()
        return ("%" in s or "％" in s) or (re.fullmatch(r"\d+(?:\.\d+)?", s) is not None and float(s) < 100)

    tables = [(g.get("headers", []), g.get("rows", [])) for g in rec.get("main_item_tables", [])]
    for a in rec.get("extracted_files", []):
        for t in (a.get("tables") or []):
            if t:
                tables.append((t[0], t[1:]))
    for headers, rows in tables:
        for j, h in enumerate(headers):
            k = kind_of(h)
            if not k:
                continue
            vals = [str(r[j]) for r in rows if j < len(r) and r[j] not in (None, "")]
            if vals and sum(1 for v in vals if rate_like(v)) >= max(1, len(vals) // 2):
                return k
    return None


RATE_MODES = {"下浮率", "费率", "折扣率", "单价合同", "总价包干", "框架协议", "据实结算"}


def _pricing_mode(rec: dict) -> str:
    """识别报价方式 -> pricing_mode。"""
    txt = " ".join([rec.get("content_text", ""), rec.get("summary", {}).get("采购项目名称", ""),
                    " ".join(rec.get("categories", []))])

    def has(*ws):
        return any(w in txt for w in ws)

    if has("框架协议", "协议供货", "入围", "定点采购", "定点服务"):
        return "框架协议"
    if has("据实结算", "按实结算"):
        return "据实结算"
    if re.search(r"下浮率\s*[:：]?\s*\d", txt):   # “下浮率:6.5” 才算；排除模板提示“可填写下浮率…”
        return "下浮率"
    for g in rec.get("main_item_tables", []):
        for r in g.get("rows", []):
            for c in r:
                if c and ("（%）" in c or "%％" in c or "%" in c and "报价" in c):
                    return "下浮率"
    if norm_money(rec.get("summary", {}).get("总中标金额")) == 0:
        return "据实结算"
    rk = _rate_col_kind(rec)
    if rk:
        return rk
    if has("综合单价", "工程量清单", "单价合同"):
        return "单价合同"
    if has("包干", "包死", "固定总价"):
        return "总价包干"
    return "固定总价"


def _discount_bid(rec: dict) -> bool:
    return _pricing_mode(rec) in RATE_MODES


def _fill_fields_from(items: list, donors: list) -> None:
    """用 donors 给 items **补**缺失字段（brand/spec/quantity/单价/总价），不新增条目。

    用于「招标文件/合同等非报价类附件」——它们的清单可以佐证字段，
    但不应当作成交明细条目（否则 item_name 假阳性来自这些表）。
    """
    def key(it):
        s = _nh(it.get("spec"))
        if s:
            return ("spec", s)
        n = _nh(it.get("item_name"))
        return ("name", n) if n else None

    idx: dict = {}
    for d in donors:
        k = key(d)
        if k:
            idx.setdefault(k, d)
    for it in items:
        k = key(it)
        d = idx.get(k) if k else None
        if not d or d is it:
            continue
        for f in ("brand", "spec", "quantity", "unit", "unit_price", "total_price", "supplier_name"):
            if it.get(f) in (None, "", []) and d.get(f) not in (None, "", []):
                it[f] = d[f]


def _price_pairs_from_tables(rec: dict, winners: list) -> list:
    """从「只有供应商 + 金额、没有名称列」的结果表里取 (供应商, 单价, 总价)。

    这类表（如 `序号|中标供应商名称|中标供应商地址|中标金额(元)`）建不出标的条目，
    但它的金额是真成交价 —— 取出来按供应商并进已有条目（官方口径②：以中标金额为准）。
    仅接受**中标供应商**的金额，避免把落标方的投标报价当成交价。
    """
    wset = {_nh(w) for w in (winners or []) if w}
    if not wset:
        return []
    out = []
    for g in rec.get("main_item_tables", []):
        canon = _canon(g["headers"])
        if any(c in ("item_name", "brand", "spec") for c in canon if c):
            continue                       # 有名称列 -> 走正常条目路径
        if "total_price" not in canon and "unit_price" not in canon:
            continue
        si = canon.index("supplier_name") if "supplier_name" in canon else None
        ti = canon.index("total_price") if "total_price" in canon else None
        ui = canon.index("unit_price") if "unit_price" in canon else None
        for r in g["rows"]:
            sup = _clean_supplier(r[si]) if si is not None and si < len(r) else None
            if not sup or _nh(sup) not in wset:
                continue
            tp = norm_money(r[ti]) if ti is not None and ti < len(r) else None
            up = norm_money(r[ui]) if ui is not None and ui < len(r) else None
            if tp is None and up is None:
                continue
            out.append((sup, up, tp))
    return out


def _fill_prices_by_supplier(items: list, pairs: list) -> None:
    """按供应商把金额并入条目：仅当该供应商在 items 中**唯一对应一条**时才填（多包同供应商不猜）。"""
    by_sup: dict = {}
    for it in items:
        k = _nh(it.get("supplier_name") or "")
        if k:
            by_sup.setdefault(k, []).append(it)
    for sup, up, tp in pairs:
        group = by_sup.get(_nh(sup))
        if not group or len(group) != 1:
            continue
        it = group[0]
        if it.get("unit_price") is None and up is not None:
            it["unit_price"] = up
        if it.get("total_price") is None and tp is not None:
            it["total_price"] = tp
        it["price_source"] = "RESULT_TABLE"


def _fill_prices_from_siblings(items: list) -> None:
    """跨文件整合：① 原文缺规格型号 -> 去附件同名标的补 spec；② 按(名称+规格型号)补单价/总价。"""
    def nh(x):
        return _nh(x or "")

    def from_file(it):
        return (it.get("source") or "").startswith(("ATTACHMENT", "VLTEXT"))

    # ① 缺 spec 的行 -> 从附件(同名)标的取 spec
    spec_donor = {}
    for it in items:
        n = nh(it.get("item_name"))
        if n and it.get("spec") and from_file(it):
            spec_donor.setdefault(n, it)
    for it in items:
        n = nh(it.get("item_name"))
        if n and not it.get("spec") and n in spec_donor and spec_donor[n] is not it:
            it["spec"] = spec_donor[n].get("spec")
            it["spec_source"] = spec_donor[n].get("source")

    # ② 补价格：优先按 规格型号 匹配（型号通常唯一）；否则回退名称
    GENERIC_SPEC = {"定制", "无", "不详", "-", "—", "/", "响应", "详见", "国产", "见附件"}

    def mkey(it):
        s = nh(it.get("spec"))
        if s and s not in GENERIC_SPEC and len(s) >= 2:
            return ("spec", s)
        n = nh(it.get("item_name"))
        return ("name", n) if n else None

    filled = set()
    donors = {}
    for it in items:
        k = mkey(it)
        if k and (it.get("unit_price") is not None or it.get("total_price") is not None):
            donors.setdefault(k, it)
    for it in items:
        k = mkey(it)
        if k and k in donors and it is not donors[k] and it.get("unit_price") is None and it.get("total_price") is None:
            src = donors[k]
            it["unit_price"] = src.get("unit_price")
            it["total_price"] = src.get("total_price")
            it["price_source"] = src.get("source")


NULL_PRICE_MODES = {"下浮率", "费率", "折扣率"}


def _apply_pricing(rec: dict, ann: dict, items: list) -> None:
    """按报价方式处理：率类(下浮率/费率/折扣率)单价/总价无效 -> 置空+存rate+备注；
    其它(据实/框架/单价合同/包干)仅标注，不破坏可能有效的金额。"""
    mode = _pricing_mode(rec)
    ann["pricing_mode"] = mode
    for it in items:
        # 0 元不是有效价格：公告里的 0 表示「无固定成交额/未公示」
        # （实测 `中标金额(万元)：0【在预算金额（最高限价）内，按实际发生结算】`），
        # 官方口径②要求标未公示/无，输出 0 会被判为错误值。
        for f in ("unit_price", "total_price"):
            if it.get(f) == 0:
                it[f] = None
        r = it.get("rate")
        if r is not None:
            it["discount_rate"] = r
        if r is not None or mode == "下浮率":
            up = it.get("unit_price")
            if up is not None and up < 200 and it.get("discount_rate") is None:
                it["discount_rate"] = up        # 裸率（无%的小数值）
            it["unit_price"] = None
            it["total_price"] = None
            it["derived"] = False
            it["needs_review"] = True
            it["pricing_mode"] = mode
            it["note"] = f"报价方式={mode}：公告“单价”列为率(非金额)，据实/按协议结算，不自动算总价"
        elif mode != "固定总价":
            it["pricing_mode"] = mode
            it["note"] = f"报价方式={mode}：计价口径特殊(据实/框架/包干)，以原文为准"


_PRICE_HEADER_KW = ("单价", "总价", "合价", "金额", "报价", "价格", "中标价", "成交价")


def _table_has_price(t):
    """表格表头是否含价格列（以文档内容为准，不依赖文件名）。"""
    if not t or not t[0]:
        return False
    for h in t[0]:
        nh = _nh(h)
        if any(k in nh for k in _PRICE_HEADER_KW):
            return True
    return False


_PROSE_KEYS = {
    "名称": "item_name", "货物名称": "item_name", "标的名称": "item_name",
    "产品名称": "item_name", "服务名称": "item_name", "项目内容": "item_name", "标的": "item_name",
    "品牌": "brand", "品牌厂家": "brand",
    "规格型号": "spec", "型号": "spec", "规格": "spec",
    "数量": "quantity",
    "单价": "unit_price",
    "总价": "total_price", "合价": "total_price", "中标单价": "unit_price", "中标价": "total_price",
}


def _items_from_prose_items(rec):
    """正文「主要标的信息」的散文式 key-value 块（名称/品牌/规格型号/数量/单价…）→ 标的行。"""
    ct = rec.get("content_text") or ""
    i = ct.find("主要标的")
    if i < 0:
        return []
    seg = ct[i:]
    stops = [p for p in (seg.find("五、"), seg.find("评审专家"), seg.find("代理服务")) if p > 0]
    if stops:
        seg = seg[:min(stops)]
    items: list[dict] = []
    cur: dict = {}

    def flush():
        if any(cur.get(k) is not None for k in ("item_name", "spec", "brand", "quantity", "unit_price", "total_price")):
            items.append({"source": "ANNTEXT_PROSE", **cur})

    for ln in seg.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"^(?:采购包|标包|标段|分包|包)\s*[（(]?\s*([0-9A-Za-z一二三四五六七八九十]+)\s*[）)]?\s*[：:]?\s*$", ln)
        if m:
            flush()
            cur = {"package_no": m.group(1)}
            continue
        m = re.match(r"^([^：:]{1,12})[：:]\s*(.+?)\s*$", ln)
        if not m:
            continue
        field = _PROSE_KEYS.get(_nh(m.group(1)))
        v = m.group(2)
        if not field:
            continue
        if field == "item_name" and cur.get("item_name"):
            flush()
            cur = {}
        if field == "quantity":
            q, u = norm_qty(v)
            cur["quantity"], cur["unit"] = q, u
        elif field in ("unit_price", "total_price"):
            cur[field] = norm_money(v)
        else:
            cur[field] = v
    flush()
    return [it for it in items if it.get("item_name") or it.get("unit_price") is not None or it.get("total_price") is not None]


def _items_from_announcement_text(rec, project_name=None):
    """正文「三、中标(成交)信息」的 key-value 块：标段名称/供应商名称 + 成交(中标)金额。"""
    ct = rec.get("content_text") or ""
    if not ct:
        return []
    out = []
    seg = None
    sup = None
    pkg = None
    for raw in ct.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        m = re.match(r"^(?:标段|包)名称[：:]\s*(.+)$", ln)
        if m:
            seg = m.group(1).strip()
            continue
        m = re.match(r"^(?:中标|成交)?供应商名称[：:]\s*(.+)$", ln)
        if m:
            sup = m.group(1).strip()
            pm = re.search(r"标?包?\s*([0-9]+)", sup)
            pkg = pm.group(1) if pm else None
            continue
        # 兼容 “中标（成交）金额：...（万元）” 与 “成交金额(万元)：值”
        m = re.match(r"^(?:中标|成交)(?:（成交）|\(成交\))?金额(?:[（(]\s*([^）)]*?)\s*[）)])?\s*[：:]\s*(.+)$", ln)
        if m and (sup or seg):
            paren, val = m.group(1), m.group(2)
            # 括号里是“(万元)”时用原始值算，避免 norm_money 先按“元”四舍五入
            money = norm_money(val + "万元") if (paren and "万元" in paren) else norm_money(val)
            if money is not None:
                out.append({
                    "item_name": seg or sup,
                    "supplier_name": sup,
                    "total_price": money,
                    "package_no": pkg,
                    "source": "ANNTEXT",
                })
                seg = None
    return out


_PLACE_SPEC = re.compile(r"招标文件|中标文件|磋商文件|采购文件|谈判文件|采购人要求|详见|详情查看|见附件|^null$|^none$")


def clean_specs(items):
    """占位式 spec（详见招标文件/完全响应/满足…要求/null）→ None；顺带去掉尾部“，等”。"""
    for it in items:
        s = it.get("spec")
        if isinstance(s, str):
            s = re.sub(r"[，,]\s*(等|等等)\s*$", "", s).strip()
            it["spec"] = None if (not s or _PLACE_SPEC.search(s)) else s
    return items


def build(rec: dict) -> dict:
    """IR dict -> {announcement, items}."""
    sm = rec.get("summary", {})
    winners, losers = _winners_bidders(rec)
    ann = {
        "announcement_id": rec["announcement_id"],
        # 项目名称：**逐字保留原文**（官方口径「不得缩写」）。这里只去不可见字符与
        # 首尾空白 —— `2023` / `5G+8K` / `项目-光华路` 里的数字、英文、连字符都是
        # 公告原文的一部分，任何归一函数（`clean_brand` / `clean_item_name` 那类
        # 「实体归一」判据）都不能套到这里来（P0 合规缺陷）。
        "project_name": clean_project_name(
            sm.get("采购项目名称") or _section_text(rec, "项目名称")),
        "project_code": _project_code(rec),
        "purchaser": sm.get("采购单位"),
        "region": sm.get("行政区域"),
        "publish_time": sm.get("公告时间") or rec.get("pub_date"),
        "notice_type": _notice_type(rec.get("article_title", "")),
        "project_amount": norm_money(sm.get("总中标金额")),
        "project_amount_text": sm.get("总中标金额"),
        "categories": rec.get("categories", []),
        "winners": winners,
        "bidders": winners + [x for x in losers if x not in winners],
    }

    items = []
    # 1) 公告原文「主要标的信息」表
    for g in rec.get("main_item_tables", []):
        if not g["rows"]:
            continue
        items.extend(items_from_table(g["headers"], g["rows"], source="MAIN_ITEM_TABLE"))
    # 2) 附件分两档（以文档内容为准，但区分优先级，避免把招标文件的清单/业绩表当成交明细）
    #    ① 报价类：文件名含 报价/明细/一览/标的/开标/成交，或 VLM 识别出的管道表 —— 总是采用
    #    ② 仅因「表格内容含价格列」才入选的（招标文件/合同等）—— 只在本篇「还没有任何价格」时兜底
    def _take(afiles):
        got = []
        for a in afiles:
            nm = " ".join(x for x in (a.get("name"), a.get("rel_path")) if x)
            src_name = (a.get("name") or nm).replace("/", "\\").split("\\")[-1] or "?"
            if a.get("tables"):
                for t in a["tables"]:
                    if len(t) < 2:
                        continue
                    hi = _pick_header_row(t)
                    if hi is None:
                        continue
                    got.extend(items_from_table(t[hi], t[hi + 1:], source="ATTACHMENT:" + src_name,
                                                package_no=_pkg_from_name(src_name)))
            elif a.get("text") and "|" in a["text"]:
                rows = _pipe_table(a["text"])
                if len(rows) >= 2:
                    got.extend(items_from_table(rows[0], rows[1:], source="VLTEXT:" + src_name))
        return got

    tier1, tier2 = [], []
    for a in rec.get("extracted_files", []):
        # 超长路径时 attach 会把 rel_path 压成 `_deep/<截断名>-<hash>`，
        # 故关键字匹配用「原始文件名 + 存储路径」拼接，避免丢失「报价/明细」等关键词。
        nm = " ".join(x for x in (a.get("name"), a.get("rel_path")) if x)
        name_kw = any(k in nm for k in ("报价", "一览", "明细", "标的", "开标", "成交"))
        is_vlm = "VLM" in (a.get("note") or "")
        if name_kw or (is_vlm and a.get("text") and "|" in a["text"]):
            tier1.append(a)
        elif any(_table_has_price(t) for t in (a.get("tables") or [])):
            tier2.append(a)
    items.extend(_take(tier1))
    # ② 仅内容命中（招标文件/合同等）：先用其清单**补字段、不新增条目**；
    #    若本篇合并后仍**没有总价**，再只用其中「带价格的行」兜底
    #    （覆盖「价只在招标文件里」的篇；不带价格的行是清单/业绩，不入条目）。
    t2 = _take(tier2)
    if t2:
        _fill_fields_from(items, t2)
        if not any(it.get("total_price") is not None for it in items):
            items.extend([d for d in t2
                          if d.get("total_price") is not None or d.get("unit_price") is not None])

    # 3) 正文「中标(成交)信息」的成交金额 -> 按供应商名回填无价行
    t_items = _items_from_announcement_text(rec, ann.get("project_name"))
    if t_items:
        tmap = {}
        for ti in t_items:
            tmap.setdefault(_nh(ti.get("supplier_name") or ti.get("item_name") or ""), ti)
        matched = 0
        for it in items:
            if it.get("unit_price") is not None or it.get("total_price") is not None:
                continue
            ti = tmap.get(_nh(it.get("supplier_name") or ""))
            if ti:
                it["total_price"] = ti["total_price"]
                it["price_source"] = "ANNTEXT"
                if (it.get("item_name") or "").startswith("详见") or not it.get("item_name"):
                    it["item_name"] = ti["item_name"]
                matched += 1
        # 表全是占位行且一条也没配上 -> 用正文块整体替换
        if matched == 0 and not any(
            (it.get("unit_price") is not None or it.get("total_price") is not None) for it in items
        ):
            items = [it for it in items if not (it.get("item_name") or "").startswith("详见")]
            items.extend(t_items)

    # 3b) 正文「主要标的信息」散文块（名称/规格型号/数量/单价…）
    if not any((it.get("unit_price") is not None or it.get("total_price") is not None) for it in items):
        p_items = _items_from_prose_items(rec)
        if p_items:
            items = [it for it in items if not (it.get("item_name") or "").startswith("详见")]
            items.extend(p_items)

    # 兜底补齐 + winners 标注
    default_cat = ann["categories"][0] if ann["categories"] else None
    default_sup = winners[0] if len(winners) == 1 else None
    for it in items:
        if not it.get("category"):
            inf = infer_category(it.get("item_name"), it.get("spec"), it.get("brand"))
            if inf:
                it["category"] = inf["path"]
                it["category_code"] = inf["code"]
        if not it.get("category") and default_cat:
            it["category"] = default_cat
        if not it.get("supplier_name") and default_sup:
            it["supplier_name"] = default_sup
        sn = it.get("supplier_name")
        it["is_winner"] = (sn in winners) if (sn and winners) else None

    _fill_prices_from_siblings(items)
    _fill_prices_by_supplier(items, _price_pairs_from_tables(rec, winners))
    for it in items:
        _reconcile(it)          # 跨文件合并后，再按 单价×数量 推总价
    _apply_pricing(rec, ann, items)
    clean_specs(items)

    return {"announcement": ann, "items": items}


def metrics(rec_extraction: dict) -> dict:
    items = rec_extraction["items"]
    n = len(items)
    nonnull = {f: sum(1 for it in items if it.get(f) not in (None, "", [])) for f in ITEM_FIELDS}
    return {
        "n_items": n,
        "nonnull": nonnull,
        "nonnull_rate": {f: (round(v / n, 3) if n else 0.0) for f, v in nonnull.items()},
        "n_review": sum(1 for it in items if it.get("needs_review")),
    }
