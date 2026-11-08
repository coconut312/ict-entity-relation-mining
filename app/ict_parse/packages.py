# -*- coding: utf-8 -*-
"""标段/合同包（分包）识别 —— 唯一规则定义处（官方答疑 Q2）。

官方口径（答疑原文）
--------------------
* `标段/合同包` 未分包的填写「未分包」，多标段/多包**逐行列出**；
* UUID 后缀（`主ID_01/_02…`）**仅为分包情况下的子 ID 命名格式参考**，编码顺序
  **不做要求**（公示顺序/自然排序/首字母均可）；精度评分**按主 ID 分组**，只看
  分包提取是否准确，只要**正确分包**即可；
* 三张表（总表/标的表/投标人表）里对应数据必须**逻辑自洽** —— 同一个包在三张表里
  必须用**同一后缀 + 同一包名**。

识别规则（本模块的全部判据，宁缺勿造）
--------------------------------------
只看原文里**显式出现**的包标识，不做语义猜测：

R1 结构化字段：`package_no` 单元格非空 → 该值即原文包名（上传官方格式表时走这条）。
R2 `第<N><单位>`：`第1包` / `第二标段` / `第3采购包` / `第1合同包`（N=阿拉伯数字或中文数字）。
R3 `<单位><N>`：`合同包3` / `采购包1` / `标包A` / `包件2` / `标段2` / `分包1`。
R4 `[（(]?包<N>[）)]?`：`包1` / `（包2）`（左侧不得是数字/字母，避免命中 `邮编100001包` 之类）。
R5 `<N><单位>`：`3标段` / `2采购包` / `1合同包`（单位不含单字「包」，避免命中 `1包装`）。
R6 命名式：`标段/合同包/采购包/标包/分包 + 名称 + ：+ 名称` → 取名称为包名
   （官方样例示例数据2 即此形态：`安全支撑系统、特征库升级`）。
R7 带名包号：`包件2：安全检测分析相关服务` → 取「包号+分隔符+名称」整段原文为包名，
   但括号里的名称若是**公司名/供应商名**则不取（那是投标人不是包名）。

分组键（key）
-------------
`包/包件/采购包/合同包/标包/分包/标段` 统一成同一维度，键 = `包#<编号>`；
编号把中文数字归一成阿拉伯数字（`一标段` 与 `第1包` 同键）。这样同一篇公告里
`一标段`/`第1包` 不会因写法不同被拆成两个包。

后缀规则（Q2 + 官方样例）
-------------------------
* 一篇公告**识别出 ≥2 个不同包** → 视为分包：按**首次出现顺序**给 `_01/_02/…`
  （顺序官方明确不要求，此处取可复现的首现顺序）；`标段/合同包` 填原文包名。
* **只识别出 0 或 1 个包** → 视为未分包：UUID **不加后缀**（官方样例示例数据2 即
  单包有名、UUID 无后缀）；识别出 1 个包时 `标段/合同包` 仍填该原文包名，
  否则填「未分包」。
* 同一篇公告里既有多包又有无标识的行 → 无标识的行归「未分包」组，UUID 不加后缀。

边界（明确不做的）
------------------
* 不从附件文件名反推**某个标的行**属于哪个包（缺行级证据，属猜测）；附件名只用于
  `scripts/scan_packages.py` 的旁证统计。
* 不把「共分 3 个包」这类**包数量描述**当成包标识。
* 不识别「包」作为普通词（包装/包含/包裹/包邮/包厢）。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

NO_PACKAGE = "未分包"

# 编号：阿拉伯数字（1~4 位）或中文数字（一~九十九）或字母（标包A）
_CN_DIGITS = "一二三四五六七八九十"
_ID = r"(?:\d{1,4}|[%s]{1,3}|[A-Za-z])" % _CN_DIGITS
_ID2 = r"(?:\d{1,2}|[%s]{1,3}|[A-Za-z])" % _CN_DIGITS     # 裸「包」用窄编号，避免邮编之类误命中
# 单位：多字单位优先，单字「包」最后
_UNIT = r"(?:采购包|合同包|包件|标包|分包|标段|包)"
_UNIT_MULTI = r"(?:采购包|合同包|包件|标包|分包|标段)"       # 不含单字「包」
# 「包」作为普通词时的右侧续字（命中即否决）
_PACK_WORD = r"(?![装卸含裹邮厢括销量])"
_NEG_LEFT = r"(?<![0-9A-Za-z.\-])"          # 左侧不能是数字/字母（避免 100001包 / IP包）

_RE_R2 = re.compile(_NEG_LEFT + r"第\s*(?P<id>" + _ID + r")\s*(?P<u>" + _UNIT_MULTI + r")")
_RE_R2B = re.compile(_NEG_LEFT + r"第\s*(?P<id>" + _ID2 + r")\s*(?P<u>包)" + _PACK_WORD)
_RE_R3 = re.compile(_NEG_LEFT + r"(?P<u>采购包|合同包|包件|标包|分包|标段)\s*[（(]?\s*(?P<id>" + _ID + r")\s*[）)]?")
_RE_R4 = re.compile(_NEG_LEFT + r"包\s*[（(]?\s*(?P<id>" + _ID2 + r")\s*[）)]?" + _PACK_WORD)
_RE_R5 = re.compile(_NEG_LEFT + r"(?P<id>" + _ID + r")\s*(?P<u>" + _UNIT + r")" + _PACK_WORD)
# 命名式：标段名称：X
_RE_R6 = re.compile(r"(?:采购包|合同包|包件|标包|分包|标段)\s*名称\s*[：:]\s*(?P<name>[^\s。;；|]{2,40})")
# 带名包号：<标识>：<名称>
_RE_R7 = re.compile(
    _NEG_LEFT + r"(?P<tok>(?:第\s*" + _ID + r"\s*" + _UNIT + r"|(?:采购包|合同包|包件|标包|分包|标段)\s*[（(]?\s*" + _ID + r"\s*[）)]?|包\s*[（(]?\s*" + _ID2 + r"\s*[）)]?|" + _ID2 + r"\s*(?:采购包|合同包|包件|标包|分包|标段|包)))"
    r"\s*[：:]\s*(?P<name>[^\s。;；|，,]{2,30})")

_RE_IDU = re.compile(r"^(?P<id>" + _ID + r")(?P<u>" + _UNIT + r")$")
_RE_UID = re.compile(r"^(?P<u>" + _UNIT + r")(?P<id>" + _ID + r")$")

# 公司/供应商名特征 —— 括号内容若命中则不当作包名
_COMPANY = re.compile(r"(公司|有限|事务所|集团|中心|研究院|设计院|大学|学院|厂$|商行|个体|合作社|联合体|分公司|银行|医院)")
# 明显不是包名的尾巴
_BAD_NAME = re.compile(r"(声明函|承诺函|报价明细|招标文件|投标文件|附件|详见|中小微?企业|本国产品)")


def cn2int(s: str) -> Optional[int]:
    """中文数字 -> int（支持 一~九十九）；失败返回 None。"""
    if re.fullmatch(r"\d+", s):
        return int(s)
    tbl = {c: i + 1 for i, c in enumerate(_CN_DIGITS)}
    if not s or any(c not in tbl for c in s):
        return None
    if s == "十":
        return 10
    if len(s) == 1:
        return tbl[s]
    if s[0] == "十":
        return 10 + (tbl[s[1]] if len(s) > 1 else 0)
    if len(s) == 2 and s[1] == "十":
        return tbl[s[0]] * 10
    if len(s) == 3 and s[1] == "十":
        return tbl[s[0]] * 10 + tbl[s[2]]
    return None


def pkg_key(token: str) -> str:
    """包标识 -> 分组键（单位统一、编号归一）。

    `包/包件/采购包/合同包/标包/分包/标段` 都归到 `包#<编号>`，编号转阿拉伯数字；
    所以 `一标段`、`第1包`、`包1`、`1包`、`合同包1` 全部同键 `包#1`。
    形状无法识别时退回原文（仍可作键，只是不参与跨写法归一）。
    """
    s = re.sub(r"\s+", "", str(token or "")).strip("（()）")
    if not s:
        return s
    body = re.sub(r"^第", "", s)
    for rx in (_RE_IDU, _RE_UID):
        m = rx.match(body)
        if m:
            break
    else:
        return s
    ident, unit = m.group("id"), m.group("u")
    n = cn2int(ident)
    return "包#%s" % (str(n) if n is not None else ident.upper())


def detect(text: Optional[str]) -> Optional[Tuple[str, str]]:
    """从一段文本识别包标识。

    返回 `(原文包名, 分组键)`；没有**显式**包标识时返回 `None`（不猜）。
    优先「命名式」（信息更全），其次带名包号，最后纯包号。
    """
    if not text:
        return None
    s = str(text)
    m = _RE_R6.search(s)
    if m:
        name = m.group("name").strip()
        if len(name) >= 2 and not _COMPANY.search(name) and not _BAD_NAME.search(name):
            return name, "包#" + _norm_name(name)
    m = _RE_R7.search(s)
    if m:
        name = m.group("name").strip()
        if not _COMPANY.search(name) and not _BAD_NAME.search(name):
            label = re.sub(r"\s+", "", m.group("tok") + "：" + name)
            return label, pkg_key(m.group("tok"))
    for rx in (_RE_R2, _RE_R2B, _RE_R3, _RE_R5, _RE_R4):
        m = rx.search(s)
        if m:
            tok = m.group(0).strip("（()） 、,，")
            key = pkg_key(tok)
            if key == "包#":                        # 没抓到编号 -> 不认
                continue
            return re.sub(r"\s+", "", tok), key
    return None


def _norm_name(name: str) -> str:
    return re.sub(r"[\s、,，;；]+", "", name)


def labels_of_notice(items: Sequence[dict], *, name_key: str = "item_name",
                     pkg_key_col: str = "package_no") -> Tuple[List[str], Dict[str, List[int]]]:
    """给一篇公告的标的行分配原文包名。

    返回 `(labels, groups)`：
      * `labels[i]` = 第 i 行的包名（`NO_PACKAGE` 表示该行无显式包标识）
      * `groups` = {包名: [行下标…]}，键顺序 = 首次出现顺序
    """
    labels: List[str] = []
    groups: Dict[str, List[int]] = {}
    key_to_label: Dict[str, str] = {}                  # 键 -> 首现原文包名
    for i, it in enumerate(items):
        raw = str(it.get(pkg_key_col) or "").strip()
        hit = None
        if raw and raw.lower() not in ("none", "nan"):
            key = pkg_key(raw) if re.search(_UNIT, raw) else ("原文值#" + raw)
            hit = (raw, key)
        else:
            hit = detect(it.get(name_key))
        if hit:
            label = key_to_label.get(hit[1])
            if label is None:                           # 同一包的不同写法 -> 复用首现包名
                label = hit[0]
                key_to_label[hit[1]] = label
        else:
            label = NO_PACKAGE
        labels.append(label)
        groups.setdefault(label, []).append(i)
    return labels, groups


def uuid_suffixes(groups: Dict[str, List[int]]) -> Dict[str, Optional[int]]:
    """按首次出现顺序给「真分包」分配 _01/_02…；未分包与单包场景返回 None。

    判定：该篇公告的**包组数 ≥ 2**（组 = 不同包名，含「未分包」这一组）才算分包。
    这样「1 个有名包 + 若干无包标识的行」也会被分开挂 UUID，不会出现
    「同一 UUID 同时对应『未分包』和『第一包』」的自相矛盾。
    「未分包」组本身**不加后缀**（它就是"没有包"的桶），UUID = 主 ID。
    """
    real = [lbl for lbl in groups if lbl != NO_PACKAGE]
    out: Dict[str, Optional[int]] = {NO_PACKAGE: None}
    if len(groups) >= 2:
        for n, lbl in enumerate(real, start=1):
            out[lbl] = n
    else:
        for lbl in real:
            out[lbl] = None
    return out


def uuid_for(main_id: str, suffix: Optional[int]) -> str:
    """主 ID + 后缀 -> UUID（`_01/_02…`）。"""
    return main_id if not suffix else "%s_%02d" % (main_id, suffix)


# ---------------- 便捷入口（给导出层用） ----------------

def plan_items(items: Sequence[dict], main_id: str, *, name_key: str = "item_name",
               pkg_key_col: str = "package_no") -> Tuple[List[str], Dict[str, List[int]],
                                                         Dict[str, Optional[int]], List[str]]:
    """一次算完：`(labels, groups, suffixes, uuids)`，`uuids[i]` 为第 i 行的 UUID。"""
    labels, groups = labels_of_notice(items, name_key=name_key, pkg_key_col=pkg_key_col)
    suffixes = uuid_suffixes(groups)
    uuids = [uuid_for(main_id, suffixes.get(lbl)) for lbl in labels]
    return labels, groups, suffixes, uuids
