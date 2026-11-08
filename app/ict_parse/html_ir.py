"""公告 html -> AnnouncementIR。

针对中国政府采购网详情页（赛题基准数据实测结构）：
- meta[name=ArticleTitle] / meta[name=PubDate]
- div.table > table：公告概要键值表（td.title 为键）
- div.vF_detail_content：一~九分节正文 + 主要标的信息 <table>
"""
from __future__ import annotations

import os
import re

from bs4 import BeautifulSoup

from .models import AnnouncementIR, Attachment, TableGroup

SECTION_RE = re.compile(r"^\s*([一二三四五六七八九十]+)\s*、")
CAT_SPLIT_RE = re.compile(r"[，,;；]\s*")
# 主要标的信息表头可能出现的关键词（不同模板列名不同）
HEADER_TOKENS = (
    "品目号", "品目编号", "采购标的", "标的名称", "货物名称", "服务名称", "工程名称",
    "供应商名称", "品牌", "规格型号", "型号", "数量", "单价", "金额", "总价", "合计",
)


def _txt(el) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _announcement_id(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _is_item_header(cells: list[str]) -> bool:
    joined = "".join(cells)
    if "供应商名称" in joined:
        return True
    if cells and cells[0] == "序号":
        return True
    return len(cells) >= 3 and sum(1 for t in HEADER_TOKENS if t in joined) >= 2


def _parse_item_table(table) -> list[TableGroup]:
    """只从出现表头的行开始收集，天然忽略布局表（无表头）。表头可能用 <th> 或 <td>。"""
    groups: list[TableGroup] = []
    cur: TableGroup | None = None
    for tr in table.find_all("tr"):
        cells = [_txt(td) for td in tr.find_all(["td", "th"])]
        if not any(cells):
            continue
        if _is_item_header(cells):
            cur = TableGroup(headers=cells)
            groups.append(cur)
        elif cur is not None:
            cur.rows.append(cells)
    return groups


def parse_html(path: str) -> AnnouncementIR:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()
    soup = BeautifulSoup(html, "lxml")
    ir = AnnouncementIR(announcement_id=_announcement_id(path), source_html=path)

    # 标题 / 发布时间
    mt = soup.find("meta", attrs={"name": "ArticleTitle"})
    if mt and mt.get("content"):
        ir.article_title = mt["content"].strip()
    else:
        ir.article_title = _txt(soup.find("title"))
    mp = soup.find("meta", attrs={"name": "PubDate"})
    ir.pub_date = mp["content"].strip() if mp and mp.get("content") else _txt(soup.find(id="pubTime"))

    # 公告概要键值表
    tbl_div = soup.find("div", class_="table")
    if tbl_div:
        table = tbl_div.find("table")
        if table:
            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                i = 0
                while i < len(cells):
                    cls = cells[i].get("class") or []
                    if "title" in cls:
                        key = _txt(cells[i])
                        val = ""
                        if i + 1 < len(cells):
                            vc = cells[i + 1]
                            val = _txt(vc)
                            if key.startswith("附件"):
                                a = vc.find("a", class_="bizDownload")
                                did = a.get("id") if a and a.has_attr("id") else None
                                ir.attachments.append(Attachment(name=val, download_id=did))
                        ir.summary[key] = val
                        i += 2
                    else:
                        i += 1

    cat = ir.summary.get("品目", "")
    if cat:
        ir.categories = [c.strip() for c in CAT_SPLIT_RE.split(cat) if c.strip()]

    # 正文：分节 + 主要标的信息表
    content = soup.find("div", class_="vF_detail_content")
    if content:
        cur = "(开头)"
        sections: dict[str, str] = {}
        parts: list[str] = []
        # 注意：不同模板正文层级不同——有的 <p> 是直接子节点，
        # 有的整篇包在 <div class="protect" id="noticeArea"> 里，故递归扫全部后代。
        for el in content.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "table"]):
            if el.name == "table":
                for g in _parse_item_table(el):
                    if g.rows:
                        ir.main_item_tables.append(g)
                continue
            txt = _txt(el)
            if not txt:
                continue
            if SECTION_RE.match(txt):
                cur = txt[:48]
                sections[cur] = txt
            else:
                sections[cur] = (sections.get(cur, "") + " " + txt).strip()
            parts.append(txt)
        # 回退：部分模板正文是 key/value 表格行（如 <td class="bg"><b>一、项目名称：</b></td><td>值</td>），
        # 无 <p>/<h>，此时从表格单元格里捞分节。
        if not sections:
            for td in content.find_all("td"):
                txt = _txt(td)
                if txt and len(txt) < 60 and SECTION_RE.match(txt):
                    nxt = td.find_next_sibling("td")
                    if nxt is not None:
                        txt = f"{txt} {_txt(nxt)}".strip()
                    sections[txt[:48]] = txt
        # 回退2：第三种模板的分节标题在 <div class="second-title">五、主要标的信息</div>
        if not sections:
            for d in content.find_all(class_="second-title"):
                txt = _txt(d)
                if txt:
                    sections[txt[:48]] = txt
        ir.sections = sections
        ir.content_text = "\n".join(parts)

    return ir
