"""Markdown -> DOCX 生成器（面向本项目的交付文档）。

支持：标题(#~####)、段落、**加粗**、`行内代码`、管道表格、围栏代码块、有序/无序列表、
引用块、水平线。中文使用显式 w:eastAsia 字体，避免 Word 中中文字形回退。

用法:
    <python> scripts/md_to_docx.py <input.md> <output.docx> [--title "文档标题"]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

CJK = "微软雅黑"
LATIN = "Segoe UI"
MONO = "Consolas"
MONO_CJK = "宋体"


def _set_font(run, name=LATIN, cjk=CJK, size=None, bold=None, color=None):
    run.font.name = name
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.append(rf)
    rf.set(qn("w:ascii"), name)
    rf.set(qn("w:hAnsi"), name)
    rf.set(qn("w:eastAsia"), cjk)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = RGBColor(*color)


def _setup_styles(doc: Document) -> None:
    for sname, size in (("Normal", 10.5),):
        st = doc.styles[sname]
        st.font.size = Pt(size)
        st.font.name = LATIN
        rpr = st.element.get_or_add_rPr()
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = OxmlElement("w:rFonts")
            rpr.append(rf)
        rf.set(qn("w:ascii"), LATIN)
        rf.set(qn("w:hAnsi"), LATIN)
        rf.set(qn("w:eastAsia"), CJK)
        st.paragraph_format.space_after = Pt(4)
        st.paragraph_format.line_spacing = 1.25
    for i, sz in ((0, 22), (1, 16), (2, 13.5), (3, 12), (4, 11)):
        st = doc.styles[f"Heading {i}"] if i else doc.styles["Title"]
        st.font.size = Pt(sz)
        st.font.name = LATIN
        st.font.bold = True
        st.font.color.rgb = RGBColor(0x1F, 0x38, 0x64) if i else RGBColor(0x0F, 0x24, 0x40)
        rpr = st.element.get_or_add_rPr()
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = OxmlElement("w:rFonts")
            rpr.append(rf)
        rf.set(qn("w:ascii"), LATIN)
        rf.set(qn("w:hAnsi"), LATIN)
        rf.set(qn("w:eastAsia"), CJK)


def _shade(el, fill: str) -> None:
    sh = OxmlElement("w:shd")
    sh.set(qn("w:val"), "clear")
    sh.set(qn("w:fill"), fill)
    el.append(sh)


INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def _add_runs(par, text: str, size: float | None = None) -> None:
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            # 加粗内部可能还嵌着 `行内代码`（如 **明细表 `items_task1.csv`（6244 行）**），
            # 这里再切一层，否则反引号会被原样写进文档。
            for sub in re.split(r"(`[^`]+`)", part[2:-2]):
                if not sub:
                    continue
                if sub.startswith("`") and sub.endswith("`") and len(sub) > 2:
                    r = par.add_run(sub[1:-1])
                    _set_font(r, name=MONO, cjk=MONO_CJK, size=(size or 10.5) - 1.2, bold=True)
                    _shade(r._element.get_or_add_rPr(), "F2F2F2")
                else:
                    r = par.add_run(sub)
                    _set_font(r, size=size, bold=True)
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            r = par.add_run(part[1:-1])
            _set_font(r, name=MONO, cjk=MONO_CJK, size=(size or 10.5) - 1.2)
            _shade(r._element.get_or_add_rPr(), "F2F2F2")
        else:
            r = par.add_run(part)
            _set_font(r, size=size)


def _add_table(doc: Document, rows: list[list[str]], avail_cm: float = 16.4) -> None:
    if not rows:
        return
    ncol = max(len(r) for r in rows)
    t = doc.add_table(rows=len(rows), cols=ncol)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False

    # 固定布局 + 按内容长度分配列宽，保证整表不超出可用宽度（否则会被裁切）
    tblpr = t._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblpr.append(layout)
    tw = OxmlElement("w:tblW")
    tw.set(qn("w:w"), str(int(avail_cm * 567)))
    tw.set(qn("w:type"), "dxa")
    tblpr.append(tw)
    weights = []
    for j in range(ncol):
        longest = max((len(str(r[j])) if j < len(r) else 0) for r in rows)
        weights.append(min(max(longest, 4), 46))
    total = sum(weights) or 1
    widths = [max(1.15, avail_cm * w / total) for w in weights]
    scale = avail_cm / sum(widths)
    widths = [w * scale for w in widths]
    for j in range(ncol):
        for i in range(len(rows)):
            t.cell(i, j).width = Cm(widths[j])
        t.columns[j].width = Cm(widths[j])

    for i, row in enumerate(rows):
        for j in range(ncol):
            cell = t.cell(i, j)
            cell.text = ""
            par = cell.paragraphs[0]
            par.paragraph_format.space_after = Pt(1)
            par.paragraph_format.line_spacing = 1.05
            txt = row[j] if j < len(row) else ""
            _add_runs(par, txt, size=8.5)
            if i == 0:
                for r in par.runs:
                    r.font.bold = True
                _shade(cell._tc.get_or_add_tcPr(), "DCE6F1")
    doc.add_paragraph()


def _add_code(doc: Document, lines: list[str]) -> None:
    for ln in lines:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = 1.0
        p.paragraph_format.left_indent = Cm(0.4)
        r = p.add_run(ln if ln else " ")
        _set_font(r, name=MONO, cjk=MONO_CJK, size=8.5)
        _shade(p._p.get_or_add_pPr(), "F7F7F7")


def _add_image(doc: Document, md_dir: str, rel: str, alt: str) -> bool:
    """插入图片（宽度 15.5cm、居中）+ 图注（图片 alt 文本，居中、小字灰色）。

    md 里的写法：`![图 1 系统总体架构](images/architecture.png)`
    相对路径按 **Markdown 文件所在目录** 解析；文件不存在时返回 False，交由调用方报错。
    """
    path = Path(rel) if os.path.isabs(rel) else (Path(md_dir) / rel)
    if not path.exists():
        print(f"[警告] 图片不存在，已跳过：{rel}（解析为 {path}）", file=sys.stderr)
        return False
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(2)
    p.add_run().add_picture(str(path), width=Cm(15.5))
    if alt.strip():
        cap = doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap.paragraph_format.space_after = Pt(8)
        cap.paragraph_format.line_spacing = 1.0
        r = cap.add_run(alt.strip())
        _set_font(r, size=9.5, bold=True, color=(0x44, 0x44, 0x44))
    return True


def convert(md_path: str, out_path: str) -> tuple[int, int, int, int]:
    # `utf-8-sig`：兼容带 BOM 的 Markdown（否则首行 `# 标题` 会被当成普通段落）
    text = open(md_path, encoding="utf-8-sig").read()
    lines = text.splitlines()
    doc = Document()
    _setup_styles(doc)
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)      # A4
    sec.left_margin = sec.right_margin = Cm(2.0)
    sec.top_margin = sec.bottom_margin = Cm(2.0)

    n_par = n_tab = n_code = n_img = 0
    md_dir = os.path.dirname(os.path.abspath(md_path))
    i = 0
    first_h1 = True
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()

        # 显式分页标记：`<!-- pagebreak -->`（用于「封面/目录 -> 预览页 -> 正文」的分页）
        if s.lower() in ("<!-- pagebreak -->", "<!--pagebreak-->"):
            doc.add_page_break()
            i += 1
            continue

        # 图片：`![图注](相对路径)` —— 单独成行时插入图片 + 图注
        m = re.match(r"^!\[([^\]]*)\]\(([^)\s]+)\)$", s)
        if m:
            if _add_image(doc, md_dir, m.group(2), m.group(1)):
                n_img += 1
            i += 1
            continue

        if s.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            _add_code(doc, buf)
            n_code += 1
            continue

        # 表格：当前行以 | 开头且下一行是分隔行
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            rows = []
            hdr = [c.strip() for c in s.strip("|").split("|")]
            rows.append(hdr)
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            _add_table(doc, rows)
            n_tab += 1
            continue

        if s.startswith("#"):
            m = re.match(r"^(#{1,5})\s+(.*)$", s)
            if m:
                lvl = len(m.group(1))
                txt = m.group(2)
                if lvl == 1 and first_h1:
                    p = doc.add_paragraph(style="Title")
                    _add_runs(p, txt)
                    first_h1 = False
                else:
                    p = doc.add_paragraph(style=f"Heading {min(lvl, 4)}")
                    _add_runs(p, txt, size=None)
                n_par += 1
                i += 1
                continue

        if s in ("---", "***", "___"):
            p = doc.add_paragraph()
            ppr = p._p.get_or_add_pPr()
            bd = OxmlElement("w:pBdr")
            b = OxmlElement("w:bottom")
            b.set(qn("w:val"), "single")
            b.set(qn("w:sz"), "6")
            b.set(qn("w:color"), "A6A6A6")
            bd.append(b)
            ppr.append(bd)
            i += 1
            continue

        if s.startswith("> "):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.5)
            _add_runs(p, s[2:], size=10)
            for r in p.runs:
                r.font.color.rgb = RGBColor(0x44, 0x44, 0x44)
            n_par += 1
            i += 1
            continue

        m = re.match(r"^[-*]\s+(.*)$", s)
        if m:
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_after = Pt(2)
            _add_runs(p, m.group(1))
            n_par += 1
            i += 1
            continue
        m = re.match(r"^(\d+)[.)]\s+(.*)$", s)
        if m:
            p = doc.add_paragraph(style="List Number")
            p.paragraph_format.space_after = Pt(2)
            _add_runs(p, m.group(2))
            n_par += 1
            i += 1
            continue

        if not s:
            i += 1
            continue

        p = doc.add_paragraph()
        _add_runs(p, s)
        n_par += 1
        i += 1

    doc.save(out_path)
    return n_par, n_tab, n_code, n_img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("md")
    ap.add_argument("docx")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    np_, nt, nc, ni = convert(args.md, args.docx)
    print(f"{args.md} -> {args.docx} | 段落 {np_} | 表格 {nt} | 代码块 {nc} | 图片 {ni}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
