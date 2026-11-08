"""PDF 抽取：文本层 + 表格（优先文本层；返回疑似扫描标记）。

提供 pdf_probe：**单次打开** PDF 一次拿到 文本(+可选表格)，避免重复解析。

为什么要自己拼行（`_page_text`）
--------------------------------
不少招标文件的页眉/封面是「`项目名称：<值>`」这种键值行，其中**数字与英文用了
另一种字体**（如 Cambria / DengXian），基线比汉字低十几到二十几 pt。pdfplumber 的
`extract_text()` 按基线 `top` 聚行，于是这些片段被排到**另一行**：

    项目名称： 年工信部 项目光华路办公区新建总控系统（四）制式转换设备等
    2023 5G+8K -

原文是 `项目名称：2023年工信部5G+8K项目-光华路办公区新建总控系统（四）制式转换设备等`。
`项目名称：` 的取值因此**丢掉 `2023` / `5G+8K` / `-`**，写进总表就是「项目名称被缩写」
（官方口径要求保留公告原文、不得缩写 —— 属合规问题，不是显示问题）。

`_repair_key_lines()` 只修这一类行：**名称类「键：」行**（`项目名称：` / `采购项目名称：` /
`标段名称：` / `合同包名称：`），把满足全部下列条件的孤行按 x 坐标并回原处：

  1. 纯 ASCII 可见字符（数字/英文/`+`/`-`/`.` …）且长度 ≥ 2；
  2. 横向**完全落在**该键行的 x 范围内，且两侧都有键行自己的字符（夹在中间，不是追加）；
  3. 与键行**没有任何横向重叠**（`overlap ≈ 0`）—— 压在正文上的是页码/水印，不是漂移片段；
  4. **字体与键行不同**（换字体才是基线漂移的成因；同字体的是页码/页脚，必须排除）；
  5. 字号与键行相差 ≤ 0.6pt（同一处排版，只是数字/英文换了字体）；
  6. 纵向距键行 ≤ 3 倍字号。

其余行一字不动；拼行结果与 `extract_text()` 逐字节相同时才采用修补，否则原样返回
（`EDM_PDF_LINE_REPAIR=0` 可整体关掉，用于 A/B 对比与排障）。
"""
from __future__ import annotations

import logging
import os
import re

import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

# 只对**名称类**「键：」行生效（`项目名称：` / `采购项目名称：` / `标段名称：` /
# `合同包名称：` …）。为什么收紧到名称：这些字段官方要求「逐字保留原文、不得缩写」，
# 值得为它们做专门修补；而 `企业名称：`/`日期：`/`地址：` 这类行的页码/页脚巧合更多，
# 修错了反而会污染原文（实测有 PDF 会把页码拼进「企业名称」行）。
_NAME_KEY_RE = re.compile(
    r"^\s*[（(]?[一二三四五六七八九十\d]{0,3}[）)]?\s*"
    r"(?:采购)?(?:项目|标段|标包|合同包|包)名称\s*[：:]")
# 纯 ASCII 可见字符行（数字/英文/连字符/加号/斜杠…，即被换字体后漂移的那类片段）
_ASCII_ONLY_RE = re.compile(r"^[\x20-\x7e]+$")
# 同一行内的字间距阈值（与 pdfplumber `extract_text(x_tolerance=3)` 一致）
_X_TOL = 3.0
# 字号差（pt）：同一处排版里「数字/英文换了字体」不会变字号
_SIZE_TOL = 0.6


def _repair_enabled() -> bool:
    """`EDM_PDF_LINE_REPAIR=0` 可关掉「键行片段并回」（排障/对比用，默认开）。"""
    return (os.environ.get("EDM_PDF_LINE_REPAIR") or "1").strip().lower() not in (
        "0", "false", "no")


def _is_ascii_only(txt: str) -> bool:
    s = (txt or "").strip()
    return bool(s) and bool(_ASCII_ONLY_RE.match(s))


def _join_chars(chars) -> str:
    """按 x 坐标拼一串字符（间隙 > `_X_TOL` 才插空格，与 pdfplumber 同口径）。"""
    out, prev_x1 = [], None
    for c in sorted(chars, key=lambda z: (z["x0"], z["x1"])):
        if prev_x1 is not None and c["x0"] - prev_x1 > _X_TOL:
            out.append(" ")
        out.append(c["text"])
        prev_x1 = c["x1"]
    return "".join(out)


def _dominant(values) -> str:
    """取出现次数最多的值（空值忽略）。"""
    cnt: dict[str, int] = {}
    for v in values:
        if v:
            cnt[v] = cnt.get(v, 0) + 1
    if not cnt:
        return ""
    return max(cnt.items(), key=lambda kv: kv[1])[0]


def _x_overlap(a_chars, b_chars) -> float:
    """两组字符在 x 方向上的重叠宽度合计（pt）。"""
    total = 0.0
    for a in a_chars:
        for b in b_chars:
            lo, hi = max(a["x0"], b["x0"]), min(a["x1"], b["x1"])
            if hi - lo > 0.5:
                total += hi - lo
    return total


def _repair_key_lines(page) -> str:
    """该页文本；顺带把「换字体后漂到另一条基线的 ASCII 片段」并回名称类键行。"""
    try:
        lines = page.extract_text_lines(return_chars=True)
    except Exception:                                            # noqa: BLE001
        return page.extract_text() or ""
    base = page.extract_text() or ""
    if not lines:
        return base
    # 行与 `extract_text()` 必须一一对应，否则宁可不修（避免改变全文口径）
    if "\n".join(ln["text"] for ln in lines) != base:
        return base
    if not _repair_enabled():
        return base

    used: set[int] = set()
    patches: dict[int, str] = {}
    for i, host in enumerate(lines):
        if i in used:
            continue
        if not _NAME_KEY_RE.match(host.get("text") or ""):
            continue
        if len(host.get("text") or "") > 120:
            continue
        hchars = host.get("chars") or []
        if not hchars or _is_ascii_only(host["text"]):
            continue
        size = max((c.get("size") or 0) for c in hchars) or 9.0
        hfonts = {c.get("fontname") or "" for c in hchars}
        picked = []
        for j, orph in enumerate(lines):
            if j == i or j in used:
                continue
            txt = (orph.get("text") or "").strip()
            if not _is_ascii_only(txt) or len(txt) < 2 or len(txt) > 40:
                continue
            ochars = orph.get("chars") or []
            if not ochars:
                continue
            # ⑤ 字号必须是同一处的排版（换字体不改字号）
            osize = max((c.get("size") or 0) for c in ochars) or 0.0
            if size and osize and abs(size - osize) > _SIZE_TOL:
                continue
            # ④ 字体必须**不同** —— 换字体才是基线漂移的成因；
            #    同字体的纯 ASCII 行是页码/页脚/表格另一列，绝不能并进来。
            if _dominant([c.get("fontname") for c in ochars]) in hfonts:
                continue
            # ⑥ 纵向距离
            if abs((orph.get("top") or 0) - (host.get("top") or 0)) > 3 * size:
                continue
            # ② 横向必须「夹在」键行内部（两侧都有键行自己的字符）
            if orph["x0"] < host["x0"] - 1 or orph["x1"] > host["x1"] + 1:
                continue
            if not any(c["x0"] < orph["x0"] - 1 for c in hchars):
                continue
            if not any(c["x0"] >= orph["x1"] - 1 for c in hchars):
                continue
            # ③ 不能与键行自身的字符在 x 上重叠（压字的是水印/页码，不是漂移片段）
            if _x_overlap(ochars, hchars) > 0.5:
                continue
            picked.append(j)
        if not picked:
            continue
        chars = list(hchars)
        for j in picked:
            chars.extend(lines[j].get("chars") or [])
            used.add(j)
        patches[i] = _join_chars(chars)
    if not patches:
        return base
    return "\n".join(patches.get(i, ln["text"]) for i, ln in enumerate(lines))


def pdf_extract(path: str, max_pages: int = 8) -> tuple[str, dict]:
    texts: list[str] = []
    meta = {"pages": 0, "scanned_suspect": False}
    with pdfplumber.open(path) as pdf:
        meta["pages"] = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            if i >= max_pages:
                break
            texts.append(_repair_key_lines(page))
    text = "\n".join(texts).strip()
    checked = min(meta["pages"], max_pages)
    if checked and len(text) < 20 * checked:
        meta["scanned_suspect"] = True
    return text, meta


def _clean_cell(v) -> str:
    if v is None:
        return ""
    return re.sub(r"\s+", "", str(v))


def _collect_tables(pdf, max_pages: int) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    for i, page in enumerate(pdf.pages):
        if i >= max_pages:
            break
        try:
            for t in page.extract_tables():
                tables.append([[_clean_cell(c) for c in row] for row in t])
        except Exception:  # noqa: BLE001
            continue
    return tables


def pdf_tables(path: str, max_pages: int = 6) -> list[list[list[str]]]:
    with pdfplumber.open(path) as pdf:
        return _collect_tables(pdf, max_pages)


def pdf_probe(path: str, need_tables: bool = True,
              max_pages_text: int = 8, max_pages_tbl: int = 6) -> tuple[str, list, dict]:
    """单次打开 PDF：取文本，need_tables 时再取表格。返回 (text, tables, meta)。"""
    texts: list[str] = []
    tables: list[list[list[str]]] = []
    meta = {"pages": 0, "scanned_suspect": False}
    with pdfplumber.open(path) as pdf:
        meta["pages"] = len(pdf.pages)
        upto = max(max_pages_text, max_pages_tbl if need_tables else 0)
        for i, page in enumerate(pdf.pages):
            if i >= upto:
                break
            if i < max_pages_text:
                texts.append(_repair_key_lines(page))     # 键行片段并回（见模块 docstring）
            if need_tables and i < max_pages_tbl:
                try:
                    for t in page.extract_tables():
                        tables.append([[_clean_cell(c) for c in row] for row in t])
                except Exception:  # noqa: BLE001
                    pass
    text = "\n".join(texts).strip()
    checked = min(meta["pages"], max_pages_text)
    if checked and len(text) < 20 * checked:
        meta["scanned_suspect"] = True
    return text, tables, meta
