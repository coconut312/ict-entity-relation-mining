# -*- coding: utf-8 -*-
"""提取结果下载（官方《输出标准样例表》格式）。

官方要求（《关于任务数据提取结果格式的要求及补充说明.pdf》第二条）：
「在任务三要求'实现数据集上传后自动解析、实体抽取与结构化入库'的基础上，
添加提取结果下载功能。下载的数据表格式应同样满足标准样例表要求。」

语义（用户明确纠正）
--------------------
下载的必须是「**上传数据集解析后的提取结果**」——即平台当前库
`out/platform/platform.db` 的 `t1_item` / `t1_notice`（上传解析落库的结果），
**不是**仓库里预生成的静态交付表。

因此这里不再去跑 `app/scripts/export_official_format.py`（那条路径读
`out/task2/graph.db` + `deliverables/items_task1.csv`，与某次上传无关，只用于
产出**提交件** `deliverables/official/`），改为调用 `ict_parse.official_tables`
现场读平台库生成三张标准表：

* `GET /export/official`              -> xlsx（3 sheet：总表 / 标的表 / 投标人表）
* `GET /export/official/{表名}`        -> 单张表 CSV
* `GET /export/official-meta`         -> 行数 / 列名（前端展示）

端点名称与鉴权方式完全不变（由 `api.py` 挂载时统一注入 require_auth）。
列名 / sheet 名逐字对齐 `输出标准样例表.xlsx`（总表 8 列、标的表 11 列、投标人表 9 列，
**没有「备注」列**）；金额单位由内部的**元**换算成官方的**万元**。
"""
from __future__ import annotations

import io
import os
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

# 仓库 app/ 目录（内含 ict_parse 包）。追加到 sys.path 末尾而不是开头：
# app/platform 与标准库 platform 同名，放末尾才不会抢先命中。
_APP_DIR = Path(__file__).resolve().parents[3]
if str(_APP_DIR) not in sys.path:
    sys.path.append(str(_APP_DIR))

from ict_parse import official_tables as official                # noqa: E402

# 鉴权由 api.py 挂载时统一注入（router_protected.include_router(..., dependencies=[...])），
# 这里不再逐个端点声明，避免与 api.py 中的 require_auth 定义位置耦合。

router = APIRouter(prefix="/export", tags=["export"])

XLSX = "官方数据集提取结果.xlsx"
XLSX_MIME = ("application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet")
# 服务库位置与启动参数一致（EDM_SQLITE_PATH 可覆盖）
PLATFORM_DB = os.environ.get(
    "EDM_SQLITE_PATH",
    str(_APP_DIR / "out" / "platform" / "platform.db"))

_SHEETS = {name: f"{name}.csv" for name in official.SHEET_NAMES}


def _disposition(filename: str, ascii_fallback: str = "download") -> str:
    """中文文件名必须用 RFC 5987 的 filename* 形式，否则响应头会乱码。"""
    quoted = urllib.parse.quote(filename)
    return (f"attachment; filename=\"{ascii_fallback}\"; "
            f"filename*=UTF-8''{quoted}")


def _tables() -> dict:
    """读平台当前库 -> 三张标准表（库缺失/为空时返回三张空表，不 500）。"""
    try:
        return _sanitize(official.build_from_platform(PLATFORM_DB))
    except Exception as e:                                        # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f"读取平台库失败：{type(e).__name__}: {e}")


# ==================================================================
# 下载前的**后处理**（P1-5 + P1-2 的「用户原值优先」）
# ==================================================================
# 为什么不改 `ict_parse/official_tables.py`：它是三表生成逻辑的正本（提交件
# `deliverables/official/` 也用它），改动影响面更大。这两条都是**下载出口**的
# 兜底口径，放在本模块：

_BIDDER = '投标人表'
_CAT = '类别'
# 「名称为空」的投标人行怎么处理：
#   * '（未公示投标人）'（默认）—— 补一个**占位说明**，行与 (UUID, 包) 的键都保留；
#   *  ''                    —— 直接丢掉该行（会破坏三表键集合自洽，见下）。
#
# 为什么默认是"补名字"而不是"删行"：`ict_parse/official_tables.py` 会为「本包没有任何
# 可识别投标人」的包留 **1 行结构性空名占位**（名称/类别/审查/报价/得分/原因全空），
# 理由是 `scripts/assert_official_consistency.py` 的 D 项要求
# 「标的表键集合 == 投标人表键集合」——删掉那一行会让该 (UUID, 包) 只存在于标的表，
# 下载件的三表自洽断言立刻失败。所以这里既满足「下载件名称空行 = 0」，
# 又不破坏键集合自洽：空名 -> 「（未公示投标人）」。
# （若确定要"绝不出现占位行"，把它改成 '' 即可，代价是那 15 个包的 D 项会失败。）
BLANK_NAME_PLACEHOLDER = '（未公示投标人）'
_BLANK_FILLED = 0          # 最近一次生成时被补名字/被丢掉的行数（meta 里回报）
_BLANK_DROPPED = 0


def _norm(s) -> str:
    return re.sub(r'\s+', '', str(s if s is not None else ''))


# 「没有分包」的几种写法：上传件的 `标段/合同包` 是空串，下载端写成「未分包」。
# 两者是**同一组**，配对的键必须归一，否则「用户给的类别」永远对不上（P1-2）。
_NO_PKG = {'', '未分包', '/', '-', '—', '\\', '无', 'N/A'}


def _pkg(v) -> str:
    s = _norm(v)
    return '' if s in _NO_PKG else s


def _user_categories() -> dict:
    """平台库 `t1_bidder.category` = **用户上传件里的类别原值**。

    键 `(UUID, 标段/合同包, 归一名称)` -> 类别。表不存在 / 没有该列 -> 空 dict
    （旧库照旧走推断值，不报错）。
    """
    db = Path(PLATFORM_DB)
    if not db.exists():
        return {}
    try:
        con = sqlite3.connect(f'file:{db.as_posix()}?mode=ro', uri=True)
    except sqlite3.Error:
        return {}
    try:
        cur = con.execute('SELECT announcement_id, package_label, name, category '
                          'FROM t1_bidder')
        out = {}
        for aid, pkg, name, cat in cur.fetchall():
            nm = _norm(name)
            if nm and _norm(cat):
                out[(_norm(aid), _pkg(pkg), nm)] = str(cat).strip()
        return out
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def _sanitize(tables: dict) -> dict:
    """下载前的后处理（两件事，都有明确的线上证据）。

    1. **投标人表里名称为空的行一律不留空名**（P1-5）。线上件出现过
       `t20260209_26163080_01/_02` 这种「空名 + 报价」行：根因在
       `official_tables.py` 的 `sup_order` 会把空串当成一个供应商
       （`merged.setdefault('')` -> 写出一条 `名称=''` 的投标人行）。
       那份文件不在本次修复范围内，因此在**下载出口**兜掉：保留行与键，
       把空名补成 `BLANK_NAME_PLACEHOLDER`（「（未公示投标人）」），并把 `序号` 重排。
       这样「下载件名称空行 = 0」与「三表键集合自洽」同时成立。
    2. **类别用用户上传件的原值**（P1-2 的「优先信任用户给的类别，不重新推断覆盖」）：
       `t1_bidder.category` 是上传路线③时逐行搬进来的原值，命中
       `(UUID, 包, 归一名称)` 就用它覆盖下载端的推断值；没命中则保持推断值。
    """
    global _BLANK_DROPPED, _BLANK_FILLED
    _BLANK_DROPPED = _BLANK_FILLED = 0
    rows = tables.get(_BIDDER) or []
    cats = _user_categories()
    kept = []
    for r in rows:
        if not _norm(r.get('名称')):
            if not BLANK_NAME_PLACEHOLDER:
                _BLANK_DROPPED += 1
                continue
            r = {**r, '名称': BLANK_NAME_PLACEHOLDER}
            _BLANK_FILLED += 1
        if cats:
            k = (_norm(r.get('UUID')), _pkg(r.get('标段/合同包')), _norm(r.get('名称')))
            cat = cats.get(k)
            if cat:
                r = {**r, _CAT: cat}
        kept.append(r)
    for i, r in enumerate(kept, 1):
        r['序号'] = i
    out = dict(tables)
    out[_BIDDER] = kept
    return out


@router.get("/official", summary="下载官方格式提取结果（xlsx，三张标准表）")
def download_official() -> Response:
    """现场按平台当前库生成 xlsx（= 用户上传后解析出来的提取结果）。"""
    buf = io.BytesIO()
    official.write_xlsx(buf, _tables())
    return Response(content=buf.getvalue(), media_type=XLSX_MIME,
                    headers={"Content-Disposition": _disposition(XLSX, "official.xlsx")})


@router.get("/official/{sheet}", summary="下载单张标准表（CSV：总表/标的表/投标人表）")
def download_sheet(sheet: str) -> Response:
    if sheet not in _SHEETS:
        raise HTTPException(status_code=404,
                            detail="未知表名，可选：" + "/".join(_SHEETS))
    body = official.csv_bytes(sheet, _tables())
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": _disposition(_SHEETS[sheet], "table.csv")})


@router.get("/official-meta", summary="官方格式下载的元信息（行数/列数），便于前端展示")
def official_meta() -> dict:
    m = official.meta(_tables())
    return {"xlsx": XLSX, "source": "platform.db（上传解析结果）",
            # 被下载出口处理过的「名称为空」投标人行数（P1-5；补占位名或删除）
            "blank_bidder_rows_filled": _BLANK_FILLED,
            "blank_bidder_rows_dropped": _BLANK_DROPPED,
            "tables": m["tables"]}
