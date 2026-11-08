# -*- coding: utf-8 -*-
"""交付/图库层的字段清洗规则（唯一定义处，避免多处漂移）。

背景（实测脏数据）：
  * `brand` 常见多值串：`禾丰、开旭`、`上海英方、华为、阳光高科`、
    `H3C、惠普等详见投标文件三、分项报价表` —— 品牌应是一个主体，多值会污染"产品供应商"关系。
  * `purchaser` 偶见代码式值：`zycgr21071303`（项目编号串入）—— 会污染采购单位画像。
"""
from __future__ import annotations

import math
import re

_CJK = re.compile(r"[\u4e00-\u9fff]")
_CODE = re.compile(r"^[A-Za-z0-9\[\]\(\)\-_/\.]{4,}$")
_NUM = re.compile(r"^\s*\d+\s*[\.、)）]\s*")
# 「等…详见/附件/具体内容」式尾巴（实测形态：`鼎信通达等，具体内容详见附件`、
# `广州纳微等，详见投标文件中的《分项报价表》`）—— 逗号/顿号可有可无，中间可夹短说明
_TAIL = re.compile(r"等[\s，,、]*[^，,。;；]{0,16}?(?:详见|附件|具体内容|其他|以上).*$")
# 非品牌文案（招标/投标要求原文误入 brand）：清洗后仍含这些词就不是品牌
_NOT_BRAND = re.compile(r"(满足|招标文件|投标文件|响应文件|详见|要求)")


def clean_brand(b: str | None) -> str:
    """多值品牌串 -> 首个品牌；去编号前缀、『等…详见』尾巴、『品牌：』前缀。

    清洗后若仍像**要求文案**（含 满足/招标文件/投标文件/详见 等），判为无效品牌返回空串。
    """
    if not b:
        return b or ""
    s = str(b).strip()
    s = _TAIL.sub("", s).strip()                       # 先去「等…详见…」尾巴
    s = re.split(r"[；;、|]", s)[0].strip()
    s = re.sub(r"[，,]\s*详见.*$", "", s).strip()       # 再去「，详见…」
    s = _NUM.sub("", s)
    if "：" in s or ":" in s:
        s = re.split(r"[：:]", s)[-1].strip()
    s = re.sub(r"^(品牌|制造商|生产厂家)[:：]?", "", s).strip()
    if _NOT_BRAND.search(s):
        return ""
    return s


def clean_purchaser(v: str | None) -> str:
    """代码式（无中日韩字符的纯代码串）采购单位置空，避免污染画像。"""
    s = (v or "").strip()
    if s and _CODE.match(s) and not _CJK.search(s):
        return ""
    return s


# 不可见字符（零宽、双向控制、BOM、C0/C1 控制符）—— 只清这些，**不动任何可见字符**。
# 为什么单列一条：官方口径要求「项目名称：采购项目全称，保留公告原文，不得缩写」，
# 项目名里的数字/英文/连字符/标点（`2023`、`5G+8K`、`项目-光华路`）是原文的一部分，
# 任何「归一」都不能删（P0 合规问题：曾把 `2023年工信部5G+8K项目-光华路…` 削成
# `年工信部 项目光华路…`）。
_INVISIBLE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff"
    "\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def clean_project_name(v: str | None) -> str:
    """项目/标段名称的**唯一**清洗：只去不可见字符与首尾空白，其余逐字保留。

    与 `clean_brand` / `clean_item_name` 的口径**故意不同**：后两者是为「实体归一/
    关系建模」服务的（多值取首个、去编号前缀），把同一套判据套到项目名上就会
    删掉原文里的数字/英文/连字符 —— 实测导致总表「项目名称」与公告原文不一致（违规）。
    因此这里**只做无损操作**：去零宽/BOM/控制符 + `strip()`；内部空格、括号、连字符、
    数字、英文一律原样保留。

    例：`'  2023年工信部5G+8K项目-光华路办公区新建总控系统（四）制式转换设备等 '`
        -> `'2023年工信部5G+8K项目-光华路办公区新建总控系统（四）制式转换设备等'`
    """
    if v is None:
        return ""
    return _INVISIBLE.sub("", str(v)).strip()


_WS_RE = re.compile(r"\s+")


def is_char_deleted_from(short: str | None, full: str | None,
                         *, ascii_only: bool = True) -> bool:
    """`short` 是否是 `full` 的「**删字版**」（忽略空白后是它的真子序列）。

    用途（P0：项目名被截断的兜底判据）：同一篇公告的项目名常有两个来源 ——
    PDF 文本层（`项目名称：…`）与附件文件名。PDF 里「不同字体的数字/英文被排到
    另一条基线」时，文本层会**整体丢掉**这些字符（`2023`/`5G+8K`/`-`），
    得到的是完整名的删字版；据此可以判定「文本层不可信、应以更长的那份为准」，
    而不是靠长度比较或相似度阈值（会被同名不同项目的公告误伤）。

    `ascii_only=True`（默认，也是唯一该用的口径）还要求**被删掉的字符全是 ASCII**
    （数字/英文/`+`/`-`/`.` …）—— 因为「换字体」只会发生在数字/英文串上，
    汉字不可能因此消失。这条限制是为了挡掉另一种误判：附件文件名常比正文多出
    `…报价明细附件`/`…招标文件` 之类**中文尾巴**，若按「纯子序列」判就会被当成
    「正文缺字」，反把文件名的尾巴写进项目名（实测：`…保险服务项目报价明细附件`）。

    返回 True 需同时满足：去空白后是子序列关系、`short` 严格更短且非空、
    且（`ascii_only` 时）删掉的字符全部是 ASCII。
    """
    a = _WS_RE.sub("", clean_project_name(short))
    b = _WS_RE.sub("", clean_project_name(full))
    if not a or not b or len(a) >= len(b):
        return False
    # 逐字符对撞：只允许「b 多出若干字符」，并把多出来的字符收集起来
    i = 0
    deleted: list[str] = []
    for ch in b:
        if i < len(a) and a[i] == ch:
            i += 1
        else:
            deleted.append(ch)
    if i != len(a):
        return False
    if ascii_only and not all(ord(c) < 128 for c in deleted):
        return False
    return True


def fnum(v) -> float | None:
    """宽松数值解析（去千分位/空值/非数字 -> None）。"""
    s = str(v if v is not None else "").strip().replace(",", "")
    if not s or s.lower() in ("none", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _bad_num(x) -> bool:
    """非有限数（nan / ±inf）判为无效（Q4 合理性校验 R1）。"""
    return x is None or not math.isfinite(x)


def _is_rate(row: dict) -> bool:
    pm = str(row.get("pricing_mode") or "") + str(row.get("discount_rate") or "")
    return any(k in pm for k in ("率", "下浮", "折扣"))


_CAT_TAIL = re.compile(r"类$")
_ITEM_LEAD = re.compile(r"^[●○·•*★☆\-–—>》]+")
_ITEM_NUM = re.compile(r"^[（(]?\d{1,2}[）)、.．]\s*")


def clean_item_name(v: str | None) -> str:
    """去掉标的名称的编号前缀与项目符号（实测来源：`●汽车电商服务平台…`）。"""
    if not v:
        return v or ""
    s = _ITEM_NUM.sub("", str(v).strip())
    return _ITEM_NUM.sub("", _ITEM_LEAD.sub("", s)).strip()


def clean_category(v: str | None) -> str:
    """分类去尾部「类」（`服务类` -> `服务`）；**不改品目层级**（细粒度本身不是缺陷）。"""
    if not v:
        return v or ""
    return _CAT_TAIL.sub("", str(v).strip())


def normalize_item_fields(row: dict) -> list[str]:
    """输入侧字段归一（交付表里的值本身就要干净），返回被改动的字段名。"""
    changed: list[str] = []
    for f, fn in (("item_name", clean_item_name), ("category", clean_category)):
        old = row.get(f)
        new = fn(old)
        if new != old:
            row[f] = new
            changed.append(f)
    return changed


def derive_fields(row: dict) -> list[str]:
    """按《官方回答》口径做**逻辑推定填空**，返回本次补出的字段名。

    原文：「若给出部分字段，可以通过逻辑推定出其他未公示信息的，应计算并填入
    （如公布了总价和数量，未公布单价，则应使用总价/数量推定单价；公布了单价和数量，
    则应使用单价*数量推定总价）」。率类项目（下浮率/费率/折扣率）不适用除法推定。

    官方答疑 Q4 追加口径（本函数第二部分）
    -------------------------------------
    「存在逻辑推断关系、但**计算后数值不合理**的情况，**不建议计算**，可置空后备注。」
    因此本函数只在**必要且合理**时推定；不合理时**保持置空**并把可追溯标记
    `needs_review` 置为真（官方三表没有「备注」列，故不用新增列；矛盾原因另写在
    `row["review_reason"]` 里供调用方记录，不进入交付表列）。

    判据（全部量化，边界见下）
    --------------------------
    R0 一致性体检（三值齐备时先查，命中即整行不推定）：
       `diff = |单价×数量 − 总价|`；
       `容差 = max(0.01 万元等值 = 100 元, 原文总价的 1%)`（用户建议口径，单位统一为元）；
       另加**相对兜底**：`diff > 10% × max(|总价|, |单价×数量|)` 也判矛盾 ——
       因为金额很小时 100 元的绝对下限会掩盖明显录入错误（如 100×3=300 却写 250）。
       两者取「或」：任一命中即为不合理，不推定并标记 needs_review。
    R1 派生值必须**有限且 > 0**（0、负数、inf/nan 一律不填）。
    R2 量级/形态：
       * 派生数量：`总价/单价` 必须 ≥ 1 且 ≤ 1e9 —— `< 1` 等价于「单价 > 总价」，量级荒谬；
       * 派生单价：**数量必须为正整数**（`数量非整数却用于除尽` 不推定）、且 ≥ 1（否则单价 > 总价）；
       * 派生总价：`单价 × 数量` 必须有限且 > 0，且两个因子都 > 0。
    R3 率类（下浮率/费率/折扣率）项目**不做除法推定**（原有口径，保留）。
    R4 其余合法推定行为不受影响：只有命中 R0/R1/R2 才放弃——
       三值中缺一个且其余两个合法时，推定照旧发生并标 `derived=True`。
    """
    filled: list[str] = []

    def _mark(reason: str) -> None:
        row["needs_review"] = True
        if not row.get("review_reason"):
            row["review_reason"] = reason

    tp, up, qt = fnum(row.get("total_price")), fnum(row.get("unit_price")), fnum(row.get("quantity"))
    rate = _is_rate(row)

    # ---- R0：三值齐备时的一致性体检（矛盾 -> 整行不推定，留标记）----
    if tp is not None and up is not None and qt is not None:
        diff = abs(up * qt - tp)
        tol_abs = max(100.0, 0.01 * abs(tp))
        tol_rel = 0.10 * max(abs(tp), abs(up * qt))
        if diff > tol_abs or (diff > 0 and diff > tol_rel):
            _mark("三值矛盾：|单价%s×数量%s−总价%s|=%.6g > 容差(max(0.01万元等值=100元, 总价1%%)=%.6g, 相对10%%=%.6g)"
                  % (up, qt, tp, diff, tol_abs, tol_rel))
            return filled

    # 数量 = 总价 / 单价
    if qt is None and tp is not None and up not in (None, 0.0):
        if up > 0:
            v = tp / up
            if v >= 1 and v <= 1e9 and not _bad_num(v):
                row["quantity"] = int(round(v)) if abs(v - round(v)) < 1e-9 else round(v, 4)
                filled.append("quantity")
                qt = fnum(row["quantity"])
            else:
                _mark("推定数量=%s 不合理（要求 1 ≤ 总价/单价 ≤ 1e9；单价>总价或非有限）" % v)
        else:
            _mark("单价非正（%s），不推定数量" % up)
    # 单价 = 总价 / 数量（率类排除；数量必须为正整数）
    if up is None and not rate and tp is not None and qt not in (None, 0.0):
        if qt > 0 and abs(qt - round(qt)) < 1e-9:
            v = tp / qt
            if v > 0 and v <= tp and not _bad_num(v):
                row["unit_price"] = int(round(v)) if abs(v - round(v)) < 1e-9 else round(v, 4)
                filled.append("unit_price")
            else:
                _mark("推定单价=%s 不合理（要求 0 < 单价 ≤ 总价：单价>总价即数量<1）" % v)
        elif abs(qt - round(qt)) >= 1e-9:
            _mark("数量非整数（%s）却用于除尽，不推定单价" % qt)
        else:
            _mark("数量非正（%s），不推定单价" % qt)
    # 总价 = 单价 × 数量
    if tp is None and up is not None and qt is not None:
        if up > 0 and qt > 0:
            v = up * qt
            if v > 0 and not _bad_num(v):
                row["total_price"] = int(round(v)) if abs(v - round(v)) < 1e-9 else round(v, 2)
                filled.append("total_price")
            else:
                _mark("推定总价=%s 不合理（非有限或非正）" % v)
        else:
            _mark("单价/数量非正（%s×%s），不推定总价" % (up, qt))

    if filled:
        row["derived"] = "True"          # 标注为推定值，便于人工复核
    return filled
