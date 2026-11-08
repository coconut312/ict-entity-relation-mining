"""端到端：一篇公告 -> IR + 附件递归解压与文本抽取 -> JSON。"""
from __future__ import annotations

import json
import os
import time

from . import bidders, extract, llm_extract
from .attach import extract_bundle, extract_text_for
from .html_ir import parse_html

# 「模型看得见」的附件：`llm_extract._unit_lines()` 只把**文件名含这些关键字**的附件的
# 表格/文本喂进 prompt（`报价` 还额外放开纯文本块）。其余附件（招标文件正文、
# 声明函、扫描件…）只影响**规则层**抽取，模型读不到。
#
# 这条边界就是「先解析这批 -> 立刻发 LLM -> 其余附件在等响应时补解析」的依据：
# 先解析的这批已覆盖 prompt 的全部输入，所以 prompt 与「全部解析完再发」逐字相同。
LLM_VISIBLE_KEYS = ("报价", "一览", "明细", "标的", "开标")


def prefetch_enabled() -> bool:
    """是否启用「先解析模型可见附件 -> 立刻发 LLM -> 其余附件等响应时补解析」。

    `EDM_LLM_PREFETCH=0` 可关掉，退回改造前的「所有附件解析完再发 LLM」口径 ——
    用于 A/B 计时取证，也方便排障时排除流水线本身。默认开启。
    """
    return (os.environ.get("EDM_LLM_PREFETCH") or "1").strip().lower() not in ("0", "false", "no")


def llm_visible(att) -> bool:
    """该附件是否会进入 LLM prompt（判据见 `LLM_VISIBLE_KEYS`）。"""
    nm = " ".join(x for x in (getattr(att, "name", None),
                              getattr(att, "rel_path", None)) if x)
    return any(k in nm for k in LLM_VISIBLE_KEYS)


def split_llm_visible(files):
    """把附件按「模型可见 / 只给规则层」分两批，**各自保持原顺序**。"""
    prio, rest = [], []
    for a in files:
        (prio if llm_visible(a) else rest).append(a)
    return prio, rest


def process_one(announcement_id: str, html_path: str, zip_path: str | None, out_root: str,
                use_vlm: bool = False, use_llm: bool = False,
                timings: dict | None = None,
                budget=None) -> tuple[dict, str]:
    """解析 + 抽取一篇公告，返回 `(rec, out_dir)`。

    `timings`（可选）：传入 dict 时逐阶段耗时（秒）写进去，供上传管线打印「每篇各阶段
    耗时」日志；不传时行为与改造前一致。

    `budget`（可选）：`ict_parse.attach.UnzipBudget` 解压预算对象。上传管线传入
    **同一次任务的共享预算**，让「多篇公告的附件包 + 套娃压缩包」累计受同一个上限
    约束（P1：套娃 zip 绕过解压上限）；不传时 `extract_bundle()` 自建一个。

    性能关键（用户反馈「3 篇 9.4s」的修复点）：
      * 附件解析是**纯 Python CPU 活**（pdfplumber / openpyxl / python-docx），折在 GIL 上，
        多篇并发并不能真正并行 —— 串行化的附件解析会把 LLM 请求一段段往后推，
        实测 3 篇的附件解析累计 1.5s（冷缓存/大附件时更久），全压在请求之前；
      * 于是这里拆成两段：先解析**模型看得见**的附件（通常 1~2 份报价表，几十毫秒）
        就发 LLM 请求，其余附件在等响应的时间窗里补解析（`extract_with_llm(defer=…)`）。
        LLM 输入不变、规则层输入不变 → 结果与改造前逐字段一致。
    """
    t = timings if timings is not None else {}
    z0 = time.perf_counter()
    ir = parse_html(html_path)
    t["html_sec"] = round(time.perf_counter() - z0, 3)

    out_dir = os.path.join(out_root, announcement_id)
    os.makedirs(out_dir, exist_ok=True)
    defer = None
    if zip_path and os.path.exists(zip_path):
        att_dir = os.path.join(out_dir, "attachments")
        os.makedirs(att_dir, exist_ok=True)
        z1 = time.perf_counter()
        files = extract_bundle(zip_path, att_dir, budget=budget)
        t["unzip_sec"] = round(time.perf_counter() - z1, 3)
        ir.extracted_files = files
        rec = ir.to_dict()
        # `to_dict()` 是 deepcopy；这里换成**活引用**，让 defer 里补解析出的
        # `text` / `tables` 对 rec 立即可见（否则规则层读到的仍是未解析状态）。
        rec["extracted_files"] = [a.__dict__ for a in files]

        use_llm_now = bool(use_llm and llm_extract.available())
        if use_llm_now and prefetch_enabled():
            prio, rest = split_llm_visible(files)
        else:
            prio, rest = files, []          # 改造前口径（或纯规则层）：一次性解析完再走后面
        z2 = time.perf_counter()
        for att in prio:
            extract_text_for(att, att_dir, use_vlm=use_vlm)
        t["attach_text_sec"] = round(time.perf_counter() - z2, 3)
        if rest:
            def defer() -> None:              # 在 LLM 一个响应周期内跑完（主线程）
                z3 = time.perf_counter()
                for att in rest:
                    extract_text_for(att, att_dir, use_vlm=use_vlm)
                t["attach_text_rest_sec"] = round(time.perf_counter() - z3, 3)
    else:
        rec = ir.to_dict()

    if use_llm and llm_extract.available():
        z4 = time.perf_counter()
        ext = llm_extract.extract_with_llm_safe(rec, defer=defer)
        t["llm_sec"] = round(time.perf_counter() - z4, 3)
    else:
        z5 = time.perf_counter()
        ext = extract.build(rec)
        ext["engine"] = "rule"
        t["rule_sec"] = round(time.perf_counter() - z5, 3)
    rec["extraction"] = ext
    # ---- 投标人名单（参与人）：从**已解析出来的表**里认「开标一览表 / 报价明细 /
    # 评审结果 / 资格审查名单 / 未中标原因表」，每行一个投标人（判据见 ict_parse/bidders.py）。
    # 为什么放在这里：附件解析（text/tables）刚做完，表格就在手上；ir.json 里也留一份，
    # 供「上传解析」路径把它写进交付表 `bidder_roster.csv` -> 平台库 `t1_bidder` -> 官方三表。
    # 抽不到就是空列表（答案退化，不报错、不臆造）。
    try:
        rec["bidder_roster"] = bidders.roster_from_rec(rec)
    except Exception:                                      # noqa: BLE001
        rec["bidder_roster"] = []
    with open(os.path.join(out_dir, "ir.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(out_dir, "items.json"), "w", encoding="utf-8") as f:
        json.dump(ext, f, ensure_ascii=False, indent=2, default=str)
    return rec, out_dir
