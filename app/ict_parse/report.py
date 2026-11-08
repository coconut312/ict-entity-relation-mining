"""人工核验报告生成：把 50 篇的 IR 渲染成可读的 HTML + CSV 汇总。"""
from __future__ import annotations

import csv
import html
import json
import os

KEY_ATTACH = ("报价明细表", "一览表", "采购需求", "分项报价", "开标")


def _esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def _notice_type(title: str) -> str:
    for k in ("中标", "成交", "结果", "更正", "终止"):
        if k in title:
            return k
    return "其他"


def _table_html(headers: list[str], rows: list[list[str]], maxrows: int = 8) -> str:
    parts = ["<table class='grid'>"]
    if headers:
        parts.append("<tr>" + "".join(f"<th>{_esc(h)}</th>" for h in headers) + "</tr>")
    for r in rows[:maxrows]:
        parts.append("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>")
    if len(rows) > maxrows:
        ncol = max(1, len(headers))
        parts.append(f"<tr><td colspan='{ncol}' class='more'>… 还有 {len(rows) - maxrows} 行</td></tr>")
    parts.append("</table>")
    return "".join(parts)


def _collect_flags(rec: dict) -> list[str]:
    flags: list[str] = []
    if not rec.get("sections"):
        flags.append("正文分节为空")
    if not rec.get("main_item_tables"):
        flags.append("主要标的信息表为空")
    for a in rec.get("extracted_files", []):
        note = a.get("note") or ""
        p = a.get("rel_path") or a.get("name")
        if "scanned_suspect=True" in note:
            flags.append(f"[扫描件?] {p}")
        if "not auto-extracted" in note:
            flags.append(f"[压缩未解] {p}")
        if "not a valid docx" in note:
            flags.append(f"[伪docx] {p}")
        if "text extract failed" in note:
            flags.append(f"[抽取失败] {p}")
    return flags


def build_report(ids: list[str], out_root: str, report_path: str, csv_path: str, maxrows: int = 8) -> dict:
    cards: list[str] = []
    csv_rows: list[dict] = []
    stat = {"ok": 0, "missing": 0, "flagged": 0}

    for aid in ids:
        jp = os.path.join(out_root, aid, "ir.json")
        if not os.path.exists(jp):
            stat["missing"] += 1
            cards.append(f"<section class='card'><h2>{_esc(aid)}</h2><p class='warn'>未生成 ir.json</p></section>")
            continue
        with open(jp, encoding="utf-8") as f:
            rec = json.load(f)

        title = rec.get("article_title", "")
        ntype = _notice_type(title)
        groups = rec.get("main_item_tables", [])
        item_rows = sum(len(g["rows"]) for g in groups)
        files = rec.get("extracted_files", [])
        flags = _collect_flags(rec)
        if flags:
            stat["flagged"] += 1
        else:
            stat["ok"] += 1

        csv_rows.append({
            "id": aid, "notice_type": ntype, "title": title, "pub_date": rec.get("pub_date", ""),
            "n_categories": len(rec.get("categories", [])), "n_summary": len(rec.get("summary", {})),
            "n_sections": len(rec.get("sections", {})), "n_item_groups": len(groups), "item_rows": item_rows,
            "n_attach_cfg": len(rec.get("attachments", [])), "n_files": len(files),
            "n_attach_tables": sum(len(a.get("tables") or []) for a in files),
            "flags": " | ".join(flags),
        })

        # ---- card ----
        h = [f"<section class='card'>"]
        h.append(f"<h2>{_esc(aid)} <span class='tag'>{_esc(ntype)}</span></h2>")
        h.append(f"<div class='title'>{_esc(title)}</div>")
        h.append(f"<div class='meta'>发布：{_esc(rec.get('pub_date'))} ｜ 品目数：{len(rec.get('categories', []))} ｜ "
                 f"分节：{len(rec.get('sections', {}))} ｜ 主要标的信息表：{len(groups)} 组 / {item_rows} 行 ｜ "
                 f"附件文件：{len(files)} ｜ <a href='{_esc(aid)}/ir.json'>ir.json</a></div>")

        # 概要
        sm = rec.get("summary", {})
        h.append("<h3>公告概要</h3><table class='kv'>")
        for k in ("采购项目名称", "采购单位", "总中标金额", "行政区域", "公告时间", "品目"):
            if k in sm:
                h.append(f"<tr><th>{_esc(k)}</th><td>{_esc(sm[k])}</td></tr>")
        h.append("</table>")

        # 主要标的信息表
        if groups:
            h.append("<h3>主要标的信息表</h3>")
            for i, g in enumerate(groups):
                h.append(f"<div class='sub'>表[{i}] 表头：{_esc(' | '.join(g['headers']))}（{len(g['rows'])} 行）</div>")
                h.append(_table_html(g["headers"], g["rows"], maxrows))
        else:
            h.append("<p class='warn'>未解析到主要标的信息表</p>")

        # 附件清单
        h.append("<h3>附件清单</h3>")
        if files:
            h.append("<table class='grid'><tr><th>文件</th><th>类型</th><th>大小</th><th>文本</th><th>表格</th><th>备注</th></tr>")
            for a in files:
                tlen = len(a.get("text") or "")
                ntab = len(a.get("tables") or [])
                h.append("<tr>"
                         f"<td class='path'>{_esc(a.get('rel_path'))}</td>"
                         f"<td>{_esc(a.get('ext'))}</td>"
                         f"<td>{a.get('size')}</td>"
                         f"<td>{tlen}</td><td>{ntab}</td>"
                         f"<td class='note'>{_esc(a.get('note'))}</td></tr>")
            h.append("</table>")
        else:
            h.append("<p class='muted'>无附件</p>")

        # 附件关键表（报价明细表/一览表）预览
        key_files = [a for a in files if any(k in (a.get("rel_path") or "") for k in KEY_ATTACH) and (a.get("tables"))]
        if key_files:
            h.append("<h3>附件关键表预览（报价明细表 / 一览表）</h3>")
            for a in key_files[:3]:
                h.append(f"<div class='sub'>{_esc(os.path.basename(a.get('rel_path') or ''))}</div>")
                for t in (a.get("tables") or [])[:1]:
                    hd = t[0] if t else []
                    h.append(_table_html(hd, t[1:], min(maxrows, 6)))

        if flags:
            h.append("<h3 class='warn'>需人工关注</h3><ul>")
            for fl in flags:
                h.append(f"<li class='warn'>{_esc(fl)}</li>")
            h.append("</ul>")

        h.append("</section>")
        cards.append("".join(h))

    # 写 CSV
    if csv_rows:
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            w.writeheader()
            w.writerows(csv_rows)

    # 写 HTML
    doc = f"""<!doctype html><html lang='zh'><head><meta charset='utf-8'>
<title>任务一 · 解析层人工核验报告（{len(ids)} 篇）</title>
<style>
 body{{font-family:'Microsoft YaHei',Arial,sans-serif;margin:16px;color:#222;background:#f6f7f9}}
 h1{{font-size:20px}} .tot{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;margin-bottom:14px}}
 .card{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px;margin:14px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}}
 h2{{font-size:16px;margin:0 0 4px}} h3{{font-size:14px;margin:14px 0 6px;color:#0b5}}
 .title{{font-weight:600;margin:2px 0 6px}} .meta{{color:#555;font-size:12px;margin-bottom:6px}}
 .tag{{font-size:12px;background:#eef;color:#226;border-radius:4px;padding:1px 6px;margin-left:6px}}
 table{{border-collapse:collapse;margin:4px 0;font-size:12px;width:100%}}
 table.grid td,table.grid th{{border:1px solid #ddd;padding:3px 6px;vertical-align:top;text-align:left}}
 table.grid th{{background:#f0f2f5}} table.kv th{{text-align:left;color:#555;width:120px;border-bottom:1px solid #eee}}
 .sub{{font-size:12px;color:#333;margin:8px 0 2px;font-weight:600}}
 .more{{color:#999}} .muted{{color:#999}} .note{{color:#a60;max-width:320px}} .path{{word-break:break-all;max-width:420px}}
 .warn{{color:#c00}} ul{{margin:4px 0 0 18px}}
</style></head><body>
<h1>任务一 · 解析层人工核验报告</h1>
<div class='tot'>共 <b>{len(ids)}</b> 篇 ｜ 无异常 <b class='ok'>{stat['ok']}</b> ｜ 需关注 <b class='warn'>{stat['flagged']}</b> ｜ 缺失 <b>{stat['missing']}</b>
 <div class='muted'>逐篇核对：概要字段、主要标的信息表（表头/行）、附件清单、附件关键表预览。CSV 汇总见同目录 summary.csv。</div></div>
{''.join(cards)}
</body></html>"""
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return stat
