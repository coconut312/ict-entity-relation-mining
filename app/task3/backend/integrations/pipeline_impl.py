# -*- coding: utf-8 -*-
r"""数据集处理管线实现（《接口契约与工程整合》接入点 3）。

支持四类上传（均为「同规范标准的数据集」，按下列顺序识别）：

1. **交付表数据集**：压缩包内含 `items_task1.csv` / `announcements_task1.csv`
   —— 直接结构化入库（秒级，真实可用）。
2. **原始数据集**：压缩包内含 `*.html`（可含同名 `_file.zip` 附件包）
   —— 走任务一/任务二的抽取链（`ict_parse.pipeline.process_one`），生成交付表后入库。
   **只要 `ict_parse.config.text_config()` 能取到 LLM key，这里就传 `use_llm=True`**
   （取值链完全沿用既有实现，本文件不复制也不修改优先级，见 `ict_parse/config.py`），
   由 `llm_extract.extract_with_llm_safe` 调 LLM 抽取并把 `source` 置为 `LLM`；
   LLM 不可用或调用失败时它自动回退规则层（`source` 保持 `MAIN_ITEM_TABLE` 等原标记）。
   未配置任何 key 时等价于纯规则层，评审环境仍可离线复现。
3. **官方标准格式**（《输出标准样例表》：总表 / 标的表 / 投标人表）：
   直接上传含这三张 sheet 的 `*.xlsx`，或上传压缩包/ CSV，内含 `标的表.csv`
   （可选 `总表.csv` / `投标人表.csv`）。**复用路线 1 的入库路径**：先把官方
   三表映射成内部交付表（`items_task1.csv` + `announcements_task1.csv`），
   再交给 `_load_ingest()`，不另写一套入库逻辑。
4. **附件包（无 HTML）**：压缩包内**没有任何 HTML**，只有附件
   （`pdf/docx/doc/rtf/wps/xls(x)/图片`，也可含 `<公告号>.zip` 套娃或多级附件目录）。
   例如赛题基准数据里的 `赛题五.基准测试数据_file/t20260202_26139731.zip`
   —— 单篇公告的附件包。识别规则：
     * 公告号：从**上传文件名或包内文件名/子目录名**里用正则 `t\d{8}_\d{8}` 提取
       （形如 `20261013213236_t20260202_26139731.zip` 也能识别）；
     * 包内含多个 `<公告号>.zip` 或多个 `<公告号>/` 附件目录时，**逐个公告**分别处理；
     * 某篇公告若已有 HTML（`赛题五.基准测试数据_html/<aid>.html` 存在），
       **优先调用路线 2 的 `process_one`**，保证与整库抽取口径一致（同样带 LLM）；
     * 否则把附件交给抽取层：**有 LLM key 时必须先走 LLM**
       （`llm_extract.extract_with_llm_safe`，把附件表格作为输入喂给模型），
       LLM 不可用/失败时回退规则层（`ict_parse.attach` + `ict_parse.extract.build`），
       `source` 保留 `MAIN_ITEM_TABLE` / `ATTACHMENT:*` 等原标记；
     * 完全识别不出公告号时，公告号由文件名/包内文件名推断，**不直接判失败**。

与路线无关的**附加产物**：`bidder_roster.csv`（投标人名单）由 `_write_tables()` 一并写出，
来源是**本次解析出来的表格**（正文表 + 附件表 + xlsx 文本表），判据见 `ict_parse/bidders.py`；
它是官方格式「投标人表」里除中标方之外那些行（参与人）的唯一来源。

官方三表的字段映射（金额单位换算：官方 **万元** -> 内部 **元**）：

    标的表 -> items_task1.csv
        UUID            -> announcement_id
        产品服务名称     -> item_name
        品目            -> category
        品牌            -> brand
        规格型号         -> spec
        数量            -> quantity（整数则写整数）
        单价 × 10000    -> unit_price
        总价 × 10000    -> total_price
        标段/合同包      -> package_no
        （常量）         -> is_winner=True / source='OFFICIAL'
        总表中该 UUID 的中标供应商 -> supplier_name

    总表 -> announcements_task1.csv
        UUID            -> announcement_id
        项目名称         -> project_name
        采购单位         -> purchaser
        中标金额 × 10000 -> project_amount
        （计数）         -> n_items

缺失的列一律按空处理（不抛错）；`.xlsx` 用 openpyxl 读取（不用 pandas）。

计时口径与赛题一致：`PipelineContext` 从 `run()` 开始计时，到全部结果写入数据库为止。
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.pipeline import Pipeline, PipelineContext

# app/ 目录（内含 ict_parse 与 platform 两个包）
_APP_DIR = Path(__file__).resolve().parents[3]
_PLATFORM_DIR = _APP_DIR / 'platform'
_DELIVERABLES = _APP_DIR / 'deliverables'
_GRAPH_DB = _APP_DIR / 'out' / 'task2' / 'graph.db'
_PLATFORM_DB = _APP_DIR / 'out' / 'platform' / 'platform.db'


def _platform_db() -> Path:
    """平台服务库位置。

    `EDM_SQLITE_PATH` 优先（与 `integrations/repos.py` 读的是同一个变量）——
    这样「读」与「写」永远是同一个库：测试时把它指向副本库，
    上传就不会碰到线上库。未设置时回退到默认位置。
    """
    return Path(os.getenv('EDM_SQLITE_PATH') or _PLATFORM_DB)


# ---- 入库模式（/dataset/import 的 mode 参数）----

MODE_REPLACE = 'replace'      # 默认：覆盖（兼容改造前行为）
MODE_APPEND = 'append'        # 保留原数据并追加（按行去重）
_MODES = (MODE_REPLACE, MODE_APPEND)


def _normalize_mode(mode: Optional[str]) -> str:
    """规范入库模式；非法值回退 replace（HTTP 层已在 api.py 拦成 400）。"""
    resolved = (mode or MODE_REPLACE).strip().lower()
    return resolved if resolved in _MODES else MODE_REPLACE

_WS_DIR = _APP_DIR.parent       # 仓库根（仅日志/说明用；上传路径不读其中任何数据）

for p in (str(_APP_DIR), str(_PLATFORM_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 官方标准格式（《输出标准样例表》）的三张表名 —— xlsx 的 sheet 名与 csv 的文件名一致
_OFFICIAL_SHEETS = ('总表', '标的表', '投标人表')
_OFFICIAL_CSVS = [f'{name}.csv' for name in _OFFICIAL_SHEETS]

# 投标人名单（参与人）交付表：列 = `ict_parse.bidders` 抽出来的字段 + 公告号。
# 它是官方「投标人表」里**除中标方之外**那些行的来源（开标一览表/评审结果/资格审查/未中标原因表），
# 也是平台库 `t1_bidder` 的装载来源（见 `platform/ingest.py`）。
_ROSTER_FILE = 'bidder_roster.csv'
_ROSTER_COLS = ['announcement_id', 'package_label', 'name', 'category', 'quote_yuan',
                'score', 'review', 'review_reason', 'lose_reason', 'source']

# 公告交付表（`announcements_task1.csv`）的完整列集。
# 为什么必须**整列集**写出：`platform/ingest.py` 建 `t1_notice` 时**列名直接取 CSV 表头**
# （`CREATE TABLE t1_notice ("announcement_id" TEXT, ...)`），少写一列 = 库里少一列，
# 之后 `/items`、S1–S5 的 SELECT 会直接 `no such column` 报错。
# 路线① 缺公告表时用标的表合成公告行，也必须按这个列集写（其余列留空）。
_ANN_COLS = ['announcement_id', 'notice_type', 'project_name', 'project_code',
             'purchaser', 'region', 'publish_time', 'project_amount', 'pricing_mode',
             'n_winners', 'n_bidders', 'n_items', 'engine',
             # 中标方/参与人**名单**（JSON 数组）：`n_winners`/`n_bidders` 只有计数，
             # 关系建模阶段需要名字（从本次上传的标的/公告重建 task2_*，见
             # `_records_from_delivery`）。平台查询只按列名取数，多两列不影响。
             'winners', 'bidders']

# 公告号（UUID）正则：赛题基准数据的公告 id 形如 t20260202_26139731
_AID_RE = re.compile(r't\d{8}_\d{8}')

# 官方样例表里的**非数据行**标记：第 2 行是「填写说明」、第 3~N 行是「填写示例 / 示例数据N」。
# 判据：`序号` 字段含下列任一子串即跳过（用户明确要求）。
_SAMPLE_ROW_MARKERS = ('说明', '填写示例', '示例数据')

# 路线 4 的「可解析附件」后缀：文档 / 表格 / 图片 / 压缩包。
# 只含 readme.txt 之类纯文本的包不算附件包（仍按「未识别的数据集格式」拒绝），
# 避免把任意 zip 都吞进附件路线。
_ATTACHMENT_EXTS = {
    '.pdf', '.doc', '.docx', '.docm', '.rtf', '.wps',
    '.xls', '.xlsx', '.xlsm', '.ppt', '.pptx', '.csv',
    '.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff',
    '.rar', '.7z', '.zip',
}

# 失败提示必须列全 4 种支持的格式（用户明确要求）
_SUPPORTED_HINT = ('支持：① 含 items_task1.csv 的压缩包；'
                   '② 含 *.html 的原始数据集压缩包；'
                   '③ 官方标准格式（xlsx 三表 / 标的表.csv）；'
                   '④ 单篇公告附件包（无 HTML，pdf/docx/xlsx/图片，'
                   '公告号由文件名或包内文件名按 tYYYYMMDD_XXXXXXXX 识别）')


class UnzipTooLargeError(ValueError):
    """解压后的内容超出上限（zip bomb / 超大附件包）—— job 直接失败并说明原因。"""


def _unzip_limits() -> Tuple[int, int, int]:
    """解压上限 -> `(单文件字节, 解压总字节, 文件数)`。

    为什么要单独设限：上传体积上限只看**压缩包大小**，一个几十 KB 的 zip 可以
    解出几十 GB（zip bomb），在 2C4G 评测机上足以把磁盘写满。

    **单文件**与**总量**已拆成两个独立上限（P1：套娃 zip 绕过解压上限）：
      * `EDM_MAX_UNZIP_MB`      解压**总量**，默认 `4 × MAX_UPLOAD_MB`；
      * `EDM_MAX_UNZIP_FILE_MB` **单文件**，默认 = 总量（与改造前口径一致）；
      * `EDM_MAX_ZIP_FILES`     条目数，默认与文件夹上传一致（`MAX_FOLDER_FILES`）。

    真正的实现只有一处：`ict_parse.attach.unzip_limits()` —— 这里直接调用它，
    保证「外层 `_unzip()`」与「`attach` 的递归解压」用**同一套数值**，
    不会各算一套而互相绕过（内层预算见 `attach.UnzipBudget`）。
    """
    from ict_parse.attach import unzip_limits                  # noqa: PLC0415
    return unzip_limits()


# ---- LLM 抽取层的可用性判断（用户要求：能取到 key 就必须用 LLM）----

def _llm_configured() -> bool:
    """是否应当走 LLM 抽取层。

    判据**只有一条**：`ict_parse.config.text_config()` 能不能取到 key。
    取 key 的优先级**完全沿用既有实现**（`ict_parse/config.py` 的
    `_text_config()`：进程环境变量 > 页面「模型配置」 > Reasonix `.env` > 默认值），
    本函数只调用 `llm_extract.available()`，不复制、不改动那套优先级。
    """
    try:
        from ict_parse import llm_extract                        # noqa: PLC0415
        return bool(llm_extract.available())
    except Exception:                                            # noqa: BLE001
        # 依赖缺失（如未装 openai）时按「不可用」处理，交给规则层，不让上传失败
        return False


def _engine_is_llm(ext: Optional[dict]) -> bool:
    """抽取结果是否真的来自 LLM 层（`llm` / `llm(chunked:2,fail:0)`）。

    `extract_with_llm_safe` 回退规则层时写的是 `rule(fallback: …)`，这里据此区分，
    **不把回退结果伪造成 LLM**。
    """
    return str((ext or {}).get('engine') or '').strip().lower().startswith('llm')


def _llm_rule_tally(n_llm: int, n_rule: int) -> str:
    """job message 里统一的「LLM N 篇 / 规则层回退 M 篇」措辞。"""
    return f'LLM {n_llm} 篇 / 规则层回退 {n_rule} 篇'


def _log_doc_timing(aid: str, tim: Dict[str, float]) -> None:
    """打一行「每篇各阶段耗时」（便于以后再排查慢在哪一段）。

    例：``[耗时] t20260202_26140146 html=0.004s unzip=0.012s attach_text=0.068s
    attach_text_rest=0.519s llm=5.001s``
    阶段含义见 `ict_parse/pipeline.py::process_one` 的 `timings`。
    """
    if not tim:
        return
    parts = ' '.join(f'{k}={v}s' for k, v in tim.items())
    print(f'[耗时] {aid} {parts}', flush=True)


# ---- 官方格式的单元格取值工具（缺失列 / 空值 / 异常值一律按空处理）----

def _strip_trailing_zeros(d: Decimal) -> Decimal:
    """去掉多余尾随零，**保持定点写法**（不用 `normalize()`，避免 250000 -> 2.5E+5）。

    与 `ict_parse.official_tables._strip_zeros` 同一套写法（下载方向已在使用），
    保证上传（万元->元）与下载（元->万元）两端都是「保留全部有效小数」。
    """
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("", "-", "-0", "-0."):
        s = "0"
    return Decimal(s)


def _text(v) -> str:
    """单元格 -> 字符串（None -> ''，整数值的浮点去掉 .0）。"""
    if v is None:
        return ''
    if isinstance(v, float) and abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return str(v).strip()


def _dec_str(v) -> str:
    """单元格 -> **定点十进制字符串**（金额专用，不丢有效小数）。

    必要性：`str(0.0000125)` 是 `'1.25e-05'`（科学计数法，Excel 不认、也可能被
    别处按错误小数位解析）。金额一律先走 `Decimal(repr(float))` 再 `format(d,'f')`
    写成定点，保证「保留全部有效小数」；`0.00025 -> '0.00025'`、`2.0 -> '2'`。
    """
    if v is None or isinstance(v, bool):
        return ''
    if isinstance(v, float):
        if v != v or v in (float('inf'), float('-inf')):
            return ''
        return format(Decimal(repr(v)), 'f')
    return str(v).strip().replace(',', '').replace('，', '')


def _num(v) -> Optional[float]:
    """单元格 -> float；无法解析为数字时返回 None（不抛错）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(',', '')
    if not s or s.lower() in ('none', 'nan', 'null', '-'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _yuan(v):
    """官方金额（万元）-> 内部金额（元）：**精确换算 ×10000，不四舍五入**。

    官方答疑 Q3：标的金额要保留**全部有效小数**（单价极小时四舍五入会变成 0）。
    所以这里用 `Decimal(str(v)) * 10000` 做十进制精确换算，只去掉多余尾随零：
        0.00025   万元 -> 2.5   元
        0.0000125 万元 -> 0.125 元
    与下载/提交件方向的 `ict_parse.official_tables.to_wan_keep`（元 /10000）互为逆运算，
    两端口径一致，往返无损。

    返回值仍是 int / float（内部交付表口径不变）：整数金额写 int，
    非整数写 float（Decimal -> float 仅在本层转换，精度损失只可能出现在
    超出 double 有效位的极端值上，而官方金额不会到那个量级）。
    """
    s = _dec_str(v)
    if not s:
        return ''
    try:
        d = Decimal(s)
    except InvalidOperation:
        return ''
    y = _strip_trailing_zeros(d * 10000)
    if y == y.to_integral_value():
        return int(y)
    return float(y)


def _qty(v):
    """数量：整数写整数，非整数照录，缺失留空。"""
    f = _num(v)
    if f is None:
        return ''
    return int(f) if abs(f - round(f)) < 1e-9 else f


def _money(v):
    """金额（元）-> int / float：保留两位小数，整数写整数（与 `_yuan` 的返回类型一致）。"""
    f = round(float(v), 2)
    return int(f) if abs(f - round(f)) < 1e-9 else f


# 官方「通过审查」单元格的判据（与下载端 `ict_parse/official_tables` 的写法互逆）：
#   '未通过，资质不符合要求' -> ('fail', '资质不符合要求') -> 下载回 '未通过，资质不符合要求'
#   '通过'                   -> ('pass', '')               -> 下载回 '通过'
#   ''                       -> ('', '')                   -> 下载回 ''（不臆造结论）
_REVIEW_FAIL_KEYS = ('未通过', '不通过', '不合格', '未入围', '废标', '淘汰')


def _parse_review_cell(v):
    """官方「通过审查」原文 -> `(review, review_reason)`（`'fail'` / `'pass'` / `''`）。"""
    s = _text(v)
    if not s:
        return '', ''
    for k in _REVIEW_FAIL_KEYS:
        if s.startswith(k) or k in s:
            rest = s[len(k):] if s.startswith(k) else s
            return 'fail', rest.strip().lstrip('，,；;：:').strip()
    if '通过' in s:
        return 'pass', ''
    return '', ''


def _load_ingest():
    """从 app/platform/ingest.py 载入 `ingest()`。

    用文件路径加载而非 `import platform.ingest` —— 后者会与标准库的 `platform` 模块同名冲突。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location('ict_ingest', _PLATFORM_DIR / 'ingest.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod.ingest


def _load_task2_build():
    """从 app/scripts/build_task2_graph.py 载入 `build()`（建图逻辑的唯一实现）。"""
    import importlib.util
    path = _APP_DIR / 'scripts' / 'build_task2_graph.py'
    spec = importlib.util.spec_from_file_location('ict_build_task2_graph', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod.build


# ==================================================================
# 关系表来源判定：**本次数据集**里有没有任务二关系表的来源
# ==================================================================
#
# 背景（已确认的缺陷）：`ingest()` 支持两条关系表来源 —— `graph.db` 或交付表 CSV。
# 上传路径原先**无条件**把仓库里的 `out/task2/graph.db` 传进去，于是上传一个
# 只有 `items_task1.csv` 的小数据集时，t1_item / t1_notice 换成了新数据，
# 而 cooperation / cobidding / *_portrait 等关系与画像表仍来自**上一份数据的图库**，
# 「场景分析」就会掺入旧关系结果。
#
# 修法：**按本次数据集的内容决定来源** ——
#   * 数据集里带完整关系表（`task2_cooperation.csv`）→ 允许用 graph.db 重建关系；
#   * 数据集里没有 → 不传 graph_db，`ingest()` 走 CSV 路径；入库前先由
#     `DeliverablePipeline._rebuild_relations()` 用本次 items/announcements **纯计算**
#     生成 `task2_*.csv`（P0-2，见下方「上传路径的关系建模」）。
# 这样「上传什么就只有什么」，同一份数据集里的标的数据与关系数据永远同源。
_TASK2_CSV_HINT = ('task2_cooperation.csv', 'task2_cobidding.csv', 'task2_bids.csv',
                   'task2_entities.csv', 'task2_supplier_portrait.csv')


def _task2_csvs(deliv: Path) -> List[str]:
    """本次交付表目录里出现的 `task2_*.csv` 文件名（递归找，目录上传可能带子目录）。"""
    found: List[str] = []
    for p in sorted(Path(deliv).rglob('task2_*.csv')):
        if p.name not in found:
            found.append(p.name)
    return found


def _relation_source(deliv: Path) -> Tuple[str, str]:
    """关系表来源 -> `(graph_db 参数, 一句短说明)`。

    只有本次数据集**自己带**关系表来源（`task2_*.csv`）时才回落到仓库 `graph.db`；
    否则返回**空字符串**（`ingest()` 约定：空串 = 明确不启用图库），
    交给 `_rebuild_relations()` 从本次 items/announcements 生成关系表（P0-2），
    绝不沿用上一份数据的关系结果。

    **说明文案只留给日志/阶段行**（`ctx.report('关系表来源', …)`）：job 的最终 message
    里不再拼这段长句（用户反馈冗长），无来源时返回空串，由 `_job_message` 自动跳过。
    """
    csvs = _task2_csvs(deliv)
    if not csvs:
        return '', ''
    if _GRAPH_DB.exists():
        return str(_GRAPH_DB), '关系表由 graph.db 重建'
    return '', f'关系表由 {len(csvs)} 份交付表重建'


def _job_message(*parts: str) -> str:
    """把若干片段拼成**一行短提示**：跳过空片段、用中文分号连接、压缩空白。

    job message 是前端进度条下方的一行小字（用户反馈越短越好），
    因此这里统一收口：不再有「（0.3s，计数校验一致）」这类与耗时/自证重复的尾巴。
    """
    out = [str(p).strip(' ；;') for p in parts if p and str(p).strip(' ；;')]
    return '；'.join(out)


# ==================================================================
# append 模式：把「库内现有数据」与「本次解析结果」合并（按行去重）
# ==================================================================
#
# 为什么这样做：`ingest()` 是「整库重建」（DROP TABLE -> CREATE -> INSERT），
# 所以 append 不能靠它本身实现，必须在调用前把新结果与库内现有行合成一套交付表。
# 好在这正是发布口径：平台库 t1_item / t1_notice 就是交付表的物化结果，
# 取出来 = 内部 CSV 行，合并后再走同一条 `_load_ingest()`（不另写入库逻辑）。

# 标的行的去重键字段（同时是交付表列名）：公告号 + 该标的自身的可辨识字段。
_ITEM_KEY_COLS = ('announcement_id', 'item_name', 'brand', 'spec', 'package_no',
                  'unit_price', 'total_price', 'quantity', 'supplier_name')
# 公告行的去重键：公告号（UUID）
_NOTICE_KEY_COLS = ('announcement_id',)
# 投标人名单行的去重键：(公告号, 包标识, 归一名称) —— 与官方「投标人表」的去重口径一致
_ROSTER_KEY_COLS = ('announcement_id', 'package_label', 'name')


_T1_ITEM = 't1_item'
_T1_NOTICE = 't1_notice'
_T1_BIDDER = 't1_bidder'


def _norm_cell(v) -> str:
    """去重比较用的单元格规范化。

    空值统一成 ''（NULL 与空串视为同一个值）；纯数字统一写法
    （`319300` / `319300.0` / `319300.00` 视为同一个值），避免同一行因格式差异被判成两行。
    """
    if v is None:
        return ''
    s = str(v).strip()
    if not s:
        return ''
    try:
        f = float(s.replace(',', ''))
    except ValueError:
        return s
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return repr(round(f, 6))


def _row_key(row: dict, cols: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(_norm_cell(row.get(c)) for c in cols)


def _read_platform_table(db: Path, table: str) -> Tuple[List[str], List[dict]]:
    """只读地读出平台库的一张表 -> (列名, 行 dict 列表)。

    库不存在 / 表不存在 / 读失败都返回 `([], [])`（append 到空库等价于 replace）。
    """
    if not db.exists():
        return [], []
    try:
        con = sqlite3.connect(f'file:{Path(db).as_posix()}?mode=ro', uri=True)
    except sqlite3.Error:
        return [], []
    try:
        cur = con.execute(f'SELECT * FROM "{table}"')
        cols = [d[0] for d in cur.description]
        return cols, [dict(zip(cols, r)) for r in cur.fetchall()]
    except sqlite3.Error:
        return [], []
    finally:
        con.close()


def _platform_snapshot(db: Path) -> Dict[str, Tuple[List[str], List[dict]]]:
    """append 开始前的库内快照（t1_item / t1_notice / t1_bidder）。"""
    return {_T1_ITEM: _read_platform_table(db, _T1_ITEM),
            _T1_NOTICE: _read_platform_table(db, _T1_NOTICE),
            _T1_BIDDER: _read_platform_table(db, _T1_BIDDER)}


def _read_csv_rows(path: Path) -> Tuple[List[str], List[dict]]:
    """读一份交付表 CSV -> (列名, 行)；不存在返回 ([], [])。"""
    if not path.exists():
        return [], []
    with open(path, encoding='utf-8-sig', newline='') as f:
        rd = csv.DictReader(f)
        return list(rd.fieldnames or []), list(rd)


def _union_cols(old: List[str], new: List[str]) -> List[str]:
    """列名并集：库内列序在前（保证既有数据不丢），新列追加。"""
    out = list(old)
    for c in new:
        if c not in out:
            out.append(c)
    return out


def _merge_rows(old: Tuple[List[str], List[dict]],
                new: Tuple[List[str], List[dict]],
                key_cols: Tuple[str, ...]) -> Tuple[List[str], List[dict]]:
    """按 `key_cols` 对行去重合并：库内已有行优先保留，新行只补不重复的。

    「原有数据保留」是 append 的语义：同一公告重复上传时，第一次入库的记录不被改写，
    因此同一份文件重复 append 不会让库变大（幂等）。
    """
    old_cols, old_rows = old
    new_cols, new_rows = new
    cols = _union_cols(old_cols, new_cols)
    merged: List[dict] = [dict(r) for r in old_rows]
    seen = {_row_key(r, key_cols) for r in old_rows}
    for r in new_rows:
        k = _row_key(r, key_cols)
        if k in seen:
            continue
        seen.add(k)
        merged.append(dict(r))
    return cols, merged


def _write_csv_rows(path: Path, cols: List[str], rows: List[dict]) -> None:
    """把合并结果写成交付表 CSV（列顺序固定，缺列填空）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, cols, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow({c: ('' if r.get(c) is None else r.get(c)) for c in cols})


def _merge_deliverables(snapshot: Dict[str, Tuple[List[str], List[dict]]],
                        new_deliv: Path, dest: Path) -> Path:
    """库内快照 ∪ 本次交付表 -> 合并后的交付表目录（供 `_load_ingest()` 整库重建）。"""
    new_items = _read_csv_rows(Path(new_deliv) / 'items_task1.csv')
    new_notices = _read_csv_rows(Path(new_deliv) / 'announcements_task1.csv')
    new_roster = _read_csv_rows(Path(new_deliv) / _ROSTER_FILE)

    item_cols, item_rows = _merge_rows(
        snapshot.get(_T1_ITEM, ([], [])), new_items, _ITEM_KEY_COLS)
    notice_cols, notice_rows = _merge_rows(
        snapshot.get(_T1_NOTICE, ([], [])), new_notices, _NOTICE_KEY_COLS)
    roster_cols, roster_rows = _merge_rows(
        snapshot.get(_T1_BIDDER, ([], [])), new_roster, _ROSTER_KEY_COLS)

    _write_csv_rows(dest / 'items_task1.csv', item_cols, item_rows)
    _write_csv_rows(dest / 'announcements_task1.csv', notice_cols, notice_rows)
    # 投标人名单也按行合并：追加模式下「库内已有的参与人」不会因为这次上传而丢失
    if roster_rows or roster_cols:
        _write_csv_rows(dest / _ROSTER_FILE, roster_cols or list(_ROSTER_COLS), roster_rows)
    # 上传集自带的 `task2_*.csv`（部分关系表 / 只有投标关系）随合并目录带过去：
    # 关系建模阶段要把它作为投标事实来源；若本次带的是完整关系表且仓库 graph.db
    # 不可用，`ingest()` 也仍能在合并目录里找到这些 CSV（改造前这里会丢）。
    for p in sorted(Path(new_deliv).glob('task2_*.csv')):
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest / p.name)
    return dest


# ==================================================================
# 上传路径的「关系建模」阶段（P0-2）
# ==================================================================
#
# 背景（已确认的缺陷）：`_relation_source()` 在交付目录没有 `task2_*.csv` 时返回空串，
# `ingest()` 于是 DROP 掉派生表、不重建 —— 用户上传原始 HTML / 官方 xlsx 后，
# cooperation / bid / cobidding / *_portrait **一张都不存在**，S1–S5 全空。
#
# 修法：在「抽取结果已写成 items/announcements 交付表」之后、`_load_deliverables()`
# 入库之前，用**纯计算**（不联网、不调模型）从本次上传的标的/公告重建任务二关系表：
#   * 建图逻辑复用 `scripts/build_task2_graph.py::build()`（与全量交付件同一套口径）；
#   * 产物 `task2_*.csv` 写回**本次交付目录**（`work` 下的目录，随工作区一起清理）；
#   * 中间图库写在 `deliv.parent`（同样是 `work` 内），不用 `tempfile.mkdtemp()`；
#   * 无 bid 数据时，用**标的行的供货方**作为中标事实（`is_winner=1`，
#     `source=DERIVED_FROM_ITEMS`）；有 `bidder_roster.csv` 时把参与人作为
#     `is_winner=0` 的投标事实（`source=DERIVED_FROM_ROSTER`），但绝不在无证据时编造对手；
#   * 交付集自带 `task2_bids.csv` 时以它为投标事实来源（`source=FROM_DELIVERY_BIDS`）。

# 不能当实体名的占位值（表格里的「无 / — / 详见附件」之类）
_BAD_ENTITY_NAMES = {'', '-', '--', '—', '–', '/', '无', '无。', '略', '...', '……',
                     '详见附件', '见附件', '详见招标文件', '详见投标文件'}


def _entity_name_ok(v) -> bool:
    """实体名是否可用：去空白后非空、不是占位词、不是「详见…」式引用。"""
    s = _text(v)
    if not s or s in _BAD_ENTITY_NAMES:
        return False
    if s.startswith(('详见', '见附件', '参见', '具体见')):
        return False
    return len(s) >= 2


def _truthy(v) -> bool:
    """CSV / SQLite 里的布尔单元格（`1/1.0/True/是/中标/winner` 都算真）。

    SQLite 的 REAL 列读出来是 `1.0`，所以不能只比对字符串 `'1'`；数值统一按
    「非 0 即真」判定（`0.0` / `''` / `False` 为假）。
    """
    s = _text(v).lower()
    if s in {'1', 'true', 'yes', 'y', '是', '中标', 'win', 'winner'}:
        return True
    try:
        return float(s) != 0
    except (TypeError, ValueError):
        return False


def _unique_names(values) -> List[str]:
    """有序去重（保留首次出现的顺序），只留下可用的实体名。"""
    out: List[str] = []
    seen = set()
    for v in values:
        s = _text(v)
        if not _entity_name_ok(s) or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _json_names(v) -> List[str]:
    """把公告交付表里的 `winners`/`bidders` 单元格还原成名单。

    本程序写出的是 JSON 数组（`["A","B"]`）；同时兼容手工 CSV 里的
    `A、B` / `A;B` 这类分号/顿号分隔写法，解析失败按空名单处理（不抛错）。
    """
    if isinstance(v, list):
        return _unique_names(v)
    s = _text(v)
    if not s:
        return []
    try:
        data = json.loads(s)
        if isinstance(data, list):
            return _unique_names(data)
    except (ValueError, TypeError):
        pass
    return _unique_names(re.split(r'[;；,，、\n]', s))


def _records_from_delivery(deliv: Path) -> Tuple[List[tuple], Dict[str, object]]:
    """交付表目录 -> `build_task2_graph.build()` 的输入 `[(aid, ann, items), ...]`。

    数据只来自**本次交付目录**（items/announcements/bidder_roster/task2_bids）；
    标的行在这里过一遍 `ict_parse/noise.py`（与 `ingest` / 全量建图同一判据），
    字段清洗复用 `ict_parse/clean.py`（品牌取首个、代码式采购单位置空）。
    """
    from ict_parse.clean import clean_brand, clean_purchaser, fnum  # noqa: PLC0415
    from ict_parse.noise import NoiseReport, filter_items          # noqa: PLC0415

    _ic, item_rows = _read_csv_rows(Path(deliv) / 'items_task1.csv')
    _ac, ann_rows = _read_csv_rows(Path(deliv) / 'announcements_task1.csv')
    _rc, roster_rows = _read_csv_rows(Path(deliv) / _ROSTER_FILE)
    _bc, bid_rows = _read_csv_rows(Path(deliv) / 'task2_bids.csv')

    # ---- 标的分组（数值列还原成 float，建图函数按数值累加金额）----
    items_by_aid: Dict[str, List[dict]] = {}
    for r0 in item_rows:
        aid = _text(r0.get('announcement_id'))
        if not aid:
            continue
        r = dict(r0)
        for k in ('quantity', 'unit_price', 'total_price'):
            r[k] = fnum(r.get(k))
        if r.get('brand'):
            r['brand'] = clean_brand(r.get('brand'))
        items_by_aid.setdefault(aid, []).append(r)

    # ---- 投标人名单（参与人）按公告分组 ----
    roster_by_aid: Dict[str, List[str]] = {}
    for r in roster_rows:
        aid = _text(r.get('announcement_id'))
        if aid:
            roster_by_aid.setdefault(aid, []).append(r.get('name'))

    # ---- 交付集自带 task2_bids.csv 时，以它为投标事实（含原始 source 列）----
    bids_by_aid: Dict[str, List[tuple]] = {}
    bid_src_map: Dict[tuple, str] = {}
    bid_purchaser: Dict[str, str] = {}
    for r in bid_rows:
        aid = _text(r.get('notice_id'))
        name = _text(r.get('supplier'))
        if not aid or not _entity_name_ok(name):
            continue
        is_win = _truthy(r.get('is_winner'))
        bids_by_aid.setdefault(aid, []).append((name, is_win))
        bid_src_map[(aid, name)] = _text(r.get('source')) or 'FROM_DELIVERY_BIDS'
        if not bid_purchaser.get(aid):
            bid_purchaser[aid] = _text(r.get('purchaser'))

    # ---- 公告 + 标的 -> 建图输入 ----
    loaded: List[tuple] = []
    noise_rep = NoiseReport()
    seen_aid = set()
    for a in ann_rows:
        aid = _text(a.get('announcement_id'))
        if not aid or aid in seen_aid:
            continue
        seen_aid.add(aid)
        purchaser = clean_purchaser(_text(a.get('purchaser'))) or bid_purchaser.get(aid, '')
        ann = {
            'project_name': _text(a.get('project_name')),
            'purchaser': purchaser,
            'region': _text(a.get('region')),
            'publish_time': _text(a.get('publish_time')),
            'project_amount': fnum(a.get('project_amount')),
            'notice_type': _text(a.get('notice_type')),
            'pricing_mode': _text(a.get('pricing_mode')),
            'procurement_mode': _text(a.get('procurement_mode')),
        }
        items = filter_items(items_by_aid.get(aid, []),
                             project_name=ann['project_name'], purchaser=purchaser,
                             report=noise_rep, aid=aid)
        ann_wins = _json_names(a.get('winners'))
        ann_bids = _json_names(a.get('bidders'))
        if aid in bids_by_aid:                       # 交付集自带投标关系 -> 用它
            wins = [n for n, w in bids_by_aid[aid] if w]
            bids = [n for n, w in bids_by_aid[aid] if not w]
        else:
            # 无 bid 数据：优先用公告表的「中标方名单」，没有名单才退回标的行供货方
            # （都是本次上传的数据，不编造对手）。
            wins = ann_wins or _unique_names(r.get('supplier_name') for r in items)
            bids = [n for n in (ann_bids or _unique_names(roster_by_aid.get(aid, [])))
                    if n not in set(wins)]
        ann['winners'] = wins
        ann['bidders'] = bids
        loaded.append((aid, ann, items))

    # 个别标的行的公告号不在 announcements 表里（理论上不会，防御性兜底）：
    # 仍按「只有标的」建一条公告，避免这部分标的从关系表里消失。
    for aid, items in items_by_aid.items():
        if aid in seen_aid:
            continue
        items = filter_items(items, report=noise_rep, aid=aid)
        ann = {'winners': _unique_names(r.get('supplier_name') for r in items),
               'bidders': [], 'purchaser': bid_purchaser.get(aid, '')}
        loaded.append((aid, ann, items))

    meta = {'n_items': len(item_rows), 'n_ann': len(ann_rows),
            'bid_from_delivery': bool(bid_src_map), 'bid_src_map': bid_src_map,
            'noise_report': noise_rep}
    return loaded, meta


def _existing_bid_rows(db: Path) -> List[dict]:
    """读当前平台库的 `bid` 表 -> `task2_bids.csv` 行（append 合并用）。

    库不存在 / 表不存在（空库）返回 `[]`。`purchaser` / `project_amount` /
    `project_name` 从 `g_notice` 补齐（`task2_bids.csv` 需要这几列给建图函数）。
    """
    bcols, brows = _read_platform_table(Path(db), 'bid')
    if not bcols:
        return []
    _gcols, grows = _read_platform_table(Path(db), 'g_notice')
    gmap = {str(r.get('notice_id')): r for r in grows}
    out: List[dict] = []
    seen = set()
    for r in brows:
        nid = _text(r.get('notice_id'))
        supplier = _text(r.get('supplier'))
        if not nid or not _entity_name_ok(supplier):
            continue
        if (nid, supplier) in seen:
            continue
        seen.add((nid, supplier))
        g = gmap.get(nid) or {}
        out.append({'notice_id': nid,
                    'purchaser': _text(g.get('purchaser')),
                    'supplier': supplier,
                    'is_winner': r.get('is_winner'),
                    'amount': r.get('amount'),
                    'project_amount': g.get('project_amount'),
                    'project_name': _text(g.get('project_name')),
                    'source': 'FROM_PLATFORM_DB'})
    return out


def _write_append_bids(db: Path, merged: Path) -> int:
    """append 模式：把**库内已有投标事实**与本次新增标的/名单合并成一份 `task2_bids.csv`。

    为什么需要它：append 不能直接把「新关系」和「旧关系」两份聚合表相加（合作次数是
    聚合值，相加会重复计数）；正确做法是合并**事实层**（bid 行）后整库重算。
    库内 `bid` 表已保存所有历史参与方（含未中标者），这里逐行搬过来，再为本次
    新增公告补上「标的供货方 = 中标 / 名单参与人 = 未中标」的事实，最后写进合并目录，
    由 `_rebuild_relations()` 一次性重算全部关系表 —— 与全量建图同一口径，且天然去重。
    """
    existing = _existing_bid_rows(Path(db))
    seen_notices = {r['notice_id'] for r in existing}
    loaded, _meta = _records_from_delivery(Path(merged))
    new_rows: List[dict] = []
    for aid, ann, items in loaded:
        if aid in seen_notices:
            continue
        amt: Dict[str, float] = {}
        for it in items:
            s = it.get('supplier_name')
            tp = it.get('total_price')
            if _entity_name_ok(s) and isinstance(tp, (int, float)):
                amt[s] = amt.get(s, 0.0) + float(tp)
        for s in (ann.get('winners') or []):
            new_rows.append({'notice_id': aid, 'purchaser': ann.get('purchaser') or '',
                             'supplier': s, 'is_winner': 1,
                             'amount': amt.get(s, ''), 'project_amount':
                             ann.get('project_amount') if ann.get('project_amount') is not None else '',
                             'project_name': ann.get('project_name') or '',
                             'source': 'DERIVED_FROM_ITEMS'})
        for s in (ann.get('bidders') or []):
            new_rows.append({'notice_id': aid, 'purchaser': ann.get('purchaser') or '',
                             'supplier': s, 'is_winner': 0, 'amount': '',
                             'project_amount':
                             ann.get('project_amount') if ann.get('project_amount') is not None else '',
                             'project_name': ann.get('project_name') or '',
                             'source': 'DERIVED_FROM_ROSTER'})
    rows = existing + new_rows
    _write_csv_rows(Path(merged) / 'task2_bids.csv',
                    ['notice_id', 'purchaser', 'supplier', 'is_winner', 'amount',
                     'project_amount', 'project_name', 'source'], rows)
    return len(rows)


def _mode_note(mode: str, res: dict,
               snapshot: Optional[Dict[str, Tuple[List[str], List[dict]]]]) -> str:
    """job message 的模式说明（必须让用户一眼看出「覆盖」还是「追加」了多少）。

    replace: 覆盖：入库 N 条标的 / M 篇公告
    append : 追加：新增 x 条标的 / y 篇公告，库内合计 N 条标的 / M 篇公告
    """
    c = res.get('counts', {}) or {}
    n_item = int(c.get(_T1_ITEM, 0) or 0)
    n_notice = int(c.get(_T1_NOTICE, 0) or 0)
    if mode == MODE_APPEND and snapshot is not None:
        before_item = len(snapshot.get(_T1_ITEM, ([], []))[1])
        before_notice = len(snapshot.get(_T1_NOTICE, ([], []))[1])
        return (f'追加：新增 {max(0, n_item - before_item)} 条标的 / '
                f'{max(0, n_notice - before_notice)} 篇公告，'
                f'库内合计 {n_item} 条标的 / {n_notice} 篇公告')
    return f'覆盖：入库 {n_item} 条标的 / {n_notice} 篇公告'


# ==================================================================
# 图库同步：入库成功后把 Neo4j 图同步为「当前 platform.db 的数据」
# ==================================================================
#
# 背景（已确认的缺陷）：平台**默认图库后端是 Neo4j**（`run.py` 设
# `EDM_GRAPH_REPOSITORY=integrations.neo4j_repo:Neo4jGraphRepository`），而 Neo4j 里的图
# 是 `scripts/build_neo4j_graph.py` 从**全量语料**（`out/task2/graph.db` / 交付表）
# 一次性装载的。上传数据集只重建 `platform.db`，**不会**动 Neo4j ——
# 于是 `/dataset/stats` 已经是本次小样本，五个场景却仍然返回旧全量图的结果
# （现象：「上传数据集后场景分析不更新」）。
#
# 修法：入库成功后调 `integrations/neo4j_sync.py`，把整张图**重写**为当前库的数据
# （先分批清空、再按批写入、最后做计数校验）。图库后端不是 Neo4j 时跳过；
# 连不上/写入失败时也只打一行警告并跳过 —— 绝不让「图库不可用」导致上传失败
# （此时 SQLite 回退路径本来就读本次入库的数据）。

_NEO4J_DEFAULT_URI = 'bolt://127.0.0.1:7687'


def _graph_backend_is_neo4j() -> bool:
    """当前生效的图库后端是不是 Neo4j。

    `run.py` 探测成功会把 `EDM_GRAPH_BACKEND_ACTIVE` 设成 `neo4j` / `sqlite`
    （连不上时它已经把 `EDM_GRAPH_REPOSITORY` 改回 SQLite），这里优先看它；
    没经过 `run.py`（例如 `uvicorn app.main:app`）时退回看仓储配置字符串。
    """
    active = (os.getenv('EDM_GRAPH_BACKEND_ACTIVE') or '').strip().lower()
    if active:
        return active == 'neo4j'
    return 'neo4j' in (os.getenv('EDM_GRAPH_REPOSITORY') or '').lower()


def _neo4j_conn() -> Tuple[str, str, str, str]:
    """Neo4j 连接参数（与 `integrations/neo4j_repo.py` 的默认值一致）。"""
    return (os.getenv('NEO4J_URI') or _NEO4J_DEFAULT_URI,
            os.getenv('NEO4J_USER') or 'neo4j',
            os.getenv('NEO4J_PASSWORD') or 'neo4j',
            os.getenv('NEO4J_DATABASE') or 'neo4j')


def _one_line(txt: str) -> str:
    """把异常信息压成一行（驱动/HTTP 报错里常带换行，警告必须只占一行）。"""
    return ' '.join(str(txt).split()).strip()


def _sync_graph_after_ingest(db: Path, ctx: PipelineContext) -> str:
    """入库成功后把 Neo4j 图同步为**当前 platform.db** 的内容。

    返回一句可拼进 job message 的说明；跳过/失败时返回 `''` 并只打印一行警告
    （上传结果不受影响）。
    """
    # 显式关闭开关（`run.py` 对**非默认平台库**自动置 0，防止副本/测试实例把共享图写成小样本）。
    if str(os.getenv('EDM_GRAPH_SYNC', '1')).strip() == '0':
        print('[提示] EDM_GRAPH_SYNC=0：跳过图库同步（避免测试实例覆盖共享 Neo4j 图）')
        return ''
    if not _graph_backend_is_neo4j():
        print('[提示] 图库后端不是 Neo4j（EDM_GRAPH_REPOSITORY=%r），'
              '跳过图库同步：场景分析直接读本次入库的派生表'
              % os.getenv('EDM_GRAPH_REPOSITORY'))
        return ''
    ctx.report('同步图库', 0.97, message='正在同步 Neo4j 图…')
    try:
        from integrations import neo4j_sync                    # noqa: PLC0415
    except Exception as e:                                     # noqa: BLE001
        print('[警告] 无法加载 integrations.neo4j_sync（跳过图库同步）：'
              f'{type(e).__name__}: {_one_line(e)}')
        return ''
    uri, user, pwd, database = _neo4j_conn()
    try:
        r = neo4j_sync.sync_from_platform(db, uri, user, pwd, database=database)
    except Exception as e:                                     # noqa: BLE001
        print(f'[警告] Neo4j 图同步失败（已跳过，五个场景可能仍显示图库里的旧数据）：'
              f'{type(e).__name__}: {_one_line(e)}')
        return ''
    short = r.get('shortfall') or {}
    if short:
        print(f'[警告] Neo4j 图同步后计数与 platform.db 推导值不一致：{short}')
        return 'Neo4j 图已同步（计数有差异，详见服务端日志）'
    # job message 里只要一行短提示：节点/关系数（耗时与「校验一致」这类自证不写进 UI）
    return f'Neo4j 图已同步：{r.get("nodes")} 节点 / {r.get("rels")} 关系'


# ==================================================================
# 「恢复默认数据」：用仓库内置的完整交付集覆盖当前平台数据
# ==================================================================
#
# 场景：用户上传了小样本（例如 3 篇公告 / 12 条标的），平台被「换成」了这份小数据，
# 「场景分析」随之几乎为空。这里提供一条**一键回到仓库内置完整交付集**的路径：
#
#   1) `platform/ingest.py::ingest()` 用 `app/deliverables/`（+ `out/task2/graph.db`）
#      **整库重建**平台库 —— 进程内调用，不 subprocess（省一次解释器启动，
#      也不会踩「cwd 不对就找不到 deliverables」的坑；`ingest()` 的路径都是绝对的）；
#   2) 若图库后端是 Neo4j，再用 `neo4j_sync.sync_full_graph()` 把图**重建为全量图**
#      （与 `scripts/build_neo4j_graph.py --load --wipe` 同一份代码，口径一致）。
#
# 并发保护不在这里：由 `app/services.py` 的进程内锁负责（api 层不碰业务语义）。
# 失败时抛异常（`RuntimeError` / `OSError` …），由调用方转成 HTTP 500 的可读错误。

def restore_full_delivery(db: Optional[Path] = None) -> dict:
    """用**仓库内置的完整交付集**重建平台库（并按需重建全量图）。

    返回 `{ok, n_docs, n_items, graph_backend, nodes, rels, elapsed_ms, ...}`：
      * `n_docs` / `n_items` 取自 ingest 的**实际入库行数**（完整交付集 = 1038 / 6244）；
      * `graph_backend` 为 `'neo4j'` 时 `nodes` / `rels` 是图的**真实**计数，
        否则为 `None`（SQLite 图库直接读本次重建的派生表）。

    `db` 缺省 = 当前平台服务库（`EDM_SQLITE_PATH`）：读与写永远是同一个库。
    """
    t0 = time.time()
    target = Path(db) if db else _platform_db()
    if not _DELIVERABLES.exists():
        raise RuntimeError(f'仓库内置交付集不存在：{_DELIVERABLES}')

    # ---- 1) 平台库：进程内调用 ingest（不 subprocess，路径全为绝对路径）----
    # graph_db：完整交付集自带关系表来源，仓库里也有 out/task2/graph.db；
    # 缺失时传空串（`ingest()` 约定：空串 = 不启用图库，改由交付表 CSV 重建关系表）。
    graph_db = str(_GRAPH_DB) if _GRAPH_DB.exists() else ''
    res = _load_ingest()(str(target), deliv_dir=str(_DELIVERABLES), graph_db=graph_db)
    counts = res.get('counts') or {}
    n_docs = int(counts.get('t1_notice', 0) or 0)
    n_items = int(counts.get('t1_item', 0) or 0)
    if n_docs <= 0 or n_items <= 0:
        raise RuntimeError(f'重建后平台库仍为空（t1_notice={n_docs} / t1_item={n_items}），'
                           f'请检查交付集 {_DELIVERABLES}')

    out = {
        'ok': True,
        'n_docs': n_docs,
        'n_items': n_items,
        'graph_backend': 'sqlite',
        'nodes': None,
        'rels': None,
        'elapsed_ms': int((time.time() - t0) * 1000),
    }
    print(f'[恢复] 平台库已用仓库内置交付集重建：{n_docs} 篇公告 / {n_items} 条标的'
          f'（入库 {res.get("elapsed_sec")}s，关系表来源 '
          f'{(res.get("derived") or {}).get("source")}）')

    # ---- 2) 图库：只有 Neo4j 后端才需要（SQLite 后端读的就是本次重建的派生表）----
    if not _graph_backend_is_neo4j():
        print('[恢复] 图库后端不是 Neo4j（EDM_GRAPH_REPOSITORY=%r），'
              '只重建平台库，场景分析读本次重建的派生表'
              % os.getenv('EDM_GRAPH_REPOSITORY'))
        return out

    from integrations import neo4j_sync                          # noqa: PLC0415
    uri, user, pwd, database = _neo4j_conn()
    try:
        g = neo4j_sync.sync_full_graph(uri, user, pwd, database=database)
    except Exception as e:                                       # noqa: BLE001
        # 库已经重建好了，只有图库失败：把「已完成什么」写进错误里，便于用户重试/排查
        raise RuntimeError(
            f'平台库已重建（{n_docs} 篇 / {n_items} 条），但图库重建失败：'
            f'{type(e).__name__}: {_one_line(e)}') from e
    out.update({'graph_backend': 'neo4j', 'nodes': g['nodes'], 'rels': g['rels']})
    out['elapsed_ms'] = int((time.time() - t0) * 1000)
    out['graph_elapsed_sec'] = g.get('elapsed_sec')
    out['wiped'] = g.get('wiped')
    print(f'[恢复] Neo4j 图已重建为全量图：{g["nodes"]} 节点 / {g["rels"]} 关系'
          f'（清空 {g.get("wiped")} 个旧节点/关系，{g.get("elapsed_sec")}s）')
    return out


class DeliverablePipeline(Pipeline):
    """把上传的数据集变成数据库里的可检索结果。"""

    # 路线②「同一公告」配对用的后缀集（见 `_base_key`）：HTML 正文 + 附件压缩包。
    # `.htm` 与 `.html` 同义（前端 accept 两者都放行）；`.rar/.7z` 由 bsdtar 解（见
    # `ict_parse/attach.py`），因此它们与 HTML 同基名时也能作为附件包合并解析。
    _HTML_EXTS = ('.html', '.htm')
    _ARCHIVE_EXTS = ('.zip', '.rar', '.7z')

    def __init__(self, use_llm: bool = False, use_vlm: bool = False) -> None:
        self.use_llm = use_llm
        self.use_vlm = use_vlm

    # ---- 工具 ----
    @staticmethod
    def _unzip(dataset_path: Path, work: Path) -> int:
        """解压上传的 zip 到工作目录，返回文件数。

        **解压上限**（本次新增）：条目数、单文件大小、解压总量都有上限
        （见 `_unzip_limits`）。超限抛 `UnzipTooLargeError`，由 `run()` 转成
        一条可读的失败原因 —— 改造前只看压缩包体积，一个几 KB 的 zip 就能解出
        几十 GB（zip bomb），把评测机磁盘写满。
        """
        limit_bytes, total_bytes, limit_files = _unzip_limits()
        infos = [i for i in zipfile.ZipFile(dataset_path).infolist() if not i.is_dir()]
        if len(infos) > limit_files:
            raise UnzipTooLargeError(
                f'压缩包内文件数 {len(infos)} 超过上限 {limit_files}')
        n = 0
        total = 0
        with zipfile.ZipFile(dataset_path) as zf:
            for info in infos:
                if info.file_size and info.file_size > limit_bytes:
                    raise UnzipTooLargeError(
                        f'压缩包内单个文件 {info.filename} 解压后 '
                        f'{info.file_size / 1048576:.0f} MB 超过上限 '
                        f'{limit_bytes / 1048576:.0f} MB')
                # 防目录穿越
                rel = os.path.normpath(info.filename).lstrip('\\/')
                if rel.startswith('..') or os.path.isabs(rel):
                    continue
                tgt = work / rel
                tgt.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(tgt, 'wb') as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > total_bytes:
                            raise UnzipTooLargeError(
                                f'解压后合计 {total / 1048576:.0f} MB 超过上限 '
                                f'{total_bytes / 1048576:.0f} MB')
                        dst.write(chunk)
                n += 1
        return n

    @staticmethod
    def _stage_dir(src: Path, work: Path) -> int:
        """把上传的**目录**原样（保留相对目录结构）搬进临时工作区，返回文件数。

        前端「选择文件夹 / 拖入文件夹」上传时，api 层已经把多文件按相对路径
        还原成一棵临时目录树；这里直接扫描它，**不做任何解压**（区别于 zip 路线），
        之后 4 条识别路线看到的都是同一套 `work/` 目录结构，逻辑一行未改。

        目录穿越双保险（api 层已校验过一次）：任何一段路径为 `..` / 绝对路径，
        或解析后落到了 `work` 之外（符号链接），一律跳过，绝不写出。
        """
        n = 0
        root = src.resolve()
        for p in sorted(root.rglob('*')):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(root)
            except ValueError:                       # 理论上到不了，防御性
                continue
            if any(part in ('..', '') or Path(part).is_absolute()
                   for part in rel.parts):
                continue
            try:                                     # 符号链接指向外部 -> 跳过
                real = p.resolve()
                if real != root and root not in real.parents:
                    continue
            except OSError:
                continue
            tgt = work.joinpath(*rel.parts)
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, tgt)
            n += 1
        return n

    @staticmethod
    def _find(work: Path, names: List[str]) -> Dict[str, Path]:
        out: Dict[str, Path] = {}
        for p in work.rglob('*'):
            if p.is_file() and p.name in names and p.name not in out:
                out[p.name] = p
        return out

    # ---- 路线① 缺公告表时：用**本次上传**的标的表合成公告行 ----
    @staticmethod
    def _synthesize_announcements(items_csv: Path,
                                  dest_dir: Path) -> Tuple[int, str]:
        """从 `items_task1.csv` 合成 `announcements_task1.csv` -> `(公告数, 提示文案)`。

        **只统计本次上传的标的行**（按 `announcement_id` 分组数 `n_items`），
        其余公告字段一律留空。绝不读仓库基准交付件 —— 改造前这里是
        `shutil.copy(_DELIVERABLES/'announcements_task1.csv')`，用户上传 1 篇公告的
        items-only 数据集，库里却被塞进 1038 篇**仓库基准**公告（UUID 全不对应）。

        返回 0 表示标的表里没有任何带公告号的行（调用方据此判失败并给可读提示）。
        """
        _cols, items = _read_csv_rows(Path(items_csv))
        counts: Dict[str, int] = {}
        for r in items:
            aid = str(r.get('announcement_id') or '').strip()
            if not aid:                       # 没有公告号的标的行无法归属任何公告
                continue
            counts[aid] = counts.get(aid, 0) + 1
        if not counts:
            return 0, ''
        # 按 `_ANN_COLS` 整列集写出：库里 t1_notice 的列由 CSV 表头决定，少一列就少一列
        rows = [{'announcement_id': aid, 'n_items': n} for aid, n in counts.items()]
        _write_csv_rows(Path(dest_dir) / 'announcements_task1.csv',
                        list(_ANN_COLS), rows)
        return len(rows), (f'缺少 announcements_task1.csv，已按上传的标的表合成 '
                           f'{len(rows)} 篇公告（仅公告号与标的数，其余公告字段为空）')

    # ---- 归位式回填（入库前；只补空值、逐条留凭证）----
    def _backfill(self, recs: List[dict], ctx: PipelineContext) -> str:
        """对**本次上传的 UUID** 做归位式回填，返回可拼进 job message 的一行统计。

        * 唯一实现 = `ict_parse/backfill.py`（归位规则/开关/侧车凭证都在那里）；
        * `scope` = 本次上传的公告号集合 —— 回填**绝不会**扩到本次上传之外的公告；
        * 只补空字段：LLM 已给值的字段一个字节都不动（`EDM_BACKFILL=0` 可整体关掉，
          `EDM_BACKFILL_DRYRUN=1` 只算不写）；
        * 回填失败**不影响入库**（打印一行警告后继续，宁可少补也不让 job 失败）。
        """
        try:
            from ict_parse import backfill                    # noqa: PLC0415
        except Exception as e:                                # noqa: BLE001
            print(f'[警告] 无法加载 ict_parse.backfill（跳过归位回填）：'
                  f'{type(e).__name__}: {_one_line(e)}')
            return ''
        scope = {str(r.get('announcement_id') or '').strip() for r in recs}
        scope.discard('')
        for r in recs:
            if not r.get('_origin'):
                r['_origin'] = f"upload/out/{r.get('announcement_id') or ''}"
        try:
            rep = backfill.apply(recs, scope=scope)
        except Exception as e:                                # noqa: BLE001
            print(f'[警告] 归位回填失败（已跳过，入库照常）：{type(e).__name__}: {_one_line(e)}')
            return ''
        for line in rep.lines(show_samples=0):                # 逐字段统计进服务端日志
            print(line)
        if ctx is not None and rep.patches:
            ctx.report('归位回填', 0.9, message=rep.message())
        return rep.message()

    def _write_tables(self, recs: List[dict], dest: Path) -> tuple[int, int]:
        """把抽取结果写成平台约定的交付表（items/announcements CSV + 投标人名单 CSV）。

        `bidder_roster.csv` 是**投标人表「参与人」的来源**（《开标一览表》《报价明细》
        《评审结果》《资格审查名单》《未中标（成交）原因表》逐行给出的全部投标人）：
        抽不到就是只有表头的空文件（`ingest()` 装成空表，官方三表不受影响）。
        """
        dest.mkdir(parents=True, exist_ok=True)
        item_cols = ['announcement_id', 'package_no', 'supplier_name', 'is_winner', 'item_name',
                     'category', 'category_code', 'item_type', 'brand', 'spec', 'quantity',
                     'unit', 'unit_price', 'total_price', 'pricing_mode', 'discount_rate',
                     'price_unit', 'derived', 'needs_review', 'source', 'engine']
        ann_cols = list(_ANN_COLS)
        ni = na = 0
        roster_rows: List[dict] = []
        with open(dest / 'items_task1.csv', 'w', encoding='utf-8-sig', newline='') as fi, \
             open(dest / 'announcements_task1.csv', 'w', encoding='utf-8-sig', newline='') as fa:
            wi, wa = csv.DictWriter(fi, item_cols, extrasaction='ignore'), \
                csv.DictWriter(fa, ann_cols, extrasaction='ignore')
            wi.writeheader()
            wa.writeheader()
            for rec in recs:
                aid = rec.get('announcement_id') or rec.get('id') or ''
                ext = rec.get('extraction') or {}
                ann = ext.get('announcement') or {}
                items = ext.get('items') or []
                for it in items:
                    row = {'announcement_id': aid, 'engine': ext.get('engine', 'rule')}
                    row.update({k: ('' if v is None else v) for k, v in it.items()})
                    wi.writerow(row)
                    ni += 1
                arow = {'announcement_id': aid, 'engine': ext.get('engine', 'rule'),
                        'n_items': len(items)}
                arow.update({k: ('' if v is None else v) for k, v in ann.items()})
                # 计数列必须**由 winners/bidders 列表回填**，不能指望抽取结果里已有
                # `n_winners`/`n_bidders`：抽取层（items_llm.json）的 announcement 只有
                # `winners`/`bidders` 两个**名字列表**，没有计数字段 —— 改造前这里直接
                # `arow.update(ann)`，于是上传后 `t1_notice.n_winners/n_bidders` **恒为空**，
                # 平台库 `g_notice`/Neo4j `Notice` 的这两个属性全成 0（"公告有 3 家中标方，
                # 图上却写 0"）。与 `scripts/export_task1_tables.py` 的交付件口径一致。
                w_list = ann.get('winners')
                b_list = ann.get('bidders')
                arow['n_winners'] = (len(w_list) if isinstance(w_list, list)
                                     else arow.get('n_winners') or '')
                arow['n_bidders'] = (len(b_list) if isinstance(b_list, list)
                                     else arow.get('n_bidders') or '')
                # 名单写进交付表（JSON 数组）：关系建模阶段据此还原中标方/参与人，
                # 只有计数时无法区分"谁中标"，S1 的合作关系会漏掉这些公告。
                arow['winners'] = (json.dumps(w_list, ensure_ascii=False)
                                   if isinstance(w_list, list) else '')
                arow['bidders'] = (json.dumps(b_list, ensure_ascii=False)
                                   if isinstance(b_list, list) else '')
                wa.writerow(arow)
                na += 1
                for r in (rec.get('bidder_roster') or []):
                    roster_rows.append({'announcement_id': aid, **{
                        c: ('' if r.get(c) is None else r.get(c)) for c in _ROSTER_COLS
                        if c != 'announcement_id'}})
        _write_csv_rows(dest / _ROSTER_FILE, _ROSTER_COLS, roster_rows)
        return ni, na

    # ---- 路线 3：官方标准格式（总表 / 标的表 / 投标人表）----
    @staticmethod
    def _rows_from_csv(path: Path) -> List[dict]:
        """读一份 CSV 为 dict 列表（表头列名去空白，缺列不影响）。"""
        with open(path, encoding='utf-8-sig', newline='') as f:
            return [{('' if k is None else k.strip()): v
                     for k, v in row.items()} for row in csv.DictReader(f)]

    @staticmethod
    def _rows_from_xlsx(path: Path) -> Dict[str, List[dict]]:
        """读 .xlsx 里的官方三表，返回 {表名: 行列表}。

        用 openpyxl（只读模式）而非 pandas —— 这里只取值，不要覆盖写回工作簿。
        缺 sheet、缺列、空行都容错；一张标准表都没有时返回 {}。
        """
        from openpyxl import load_workbook                     # noqa: PLC0415

        out: Dict[str, List[dict]] = {}
        wb = load_workbook(filename=str(path), read_only=True, data_only=True)
        try:
            for name in _OFFICIAL_SHEETS:
                if name not in wb.sheetnames:
                    continue
                rows: List[dict] = []
                header: Optional[List[str]] = None
                for row in wb[name].iter_rows(values_only=True):
                    if header is None:
                        # 首行按表头处理（跳过全空行）
                        if row and any(_text(c) for c in row):
                            header = [_text(c) for c in row]
                        continue
                    if not row or not any(_text(c) for c in row):
                        continue                                   # 跳过空行
                    rows.append({h: (row[i] if i < len(row) else None)
                                 for i, h in enumerate(header) if h})
                out[name] = rows
        finally:
            wb.close()
        return out

    @classmethod
    def _load_official(cls, dataset_path: Path, work: Path) -> Optional[Dict[str, List[dict]]]:
        """识别官方标准格式；识别不到返回 None。

        接受的形态（按顺序）：
          a) 直接上传的 `*.xlsx`：含名为 总表 / 标的表 / 投标人表 的 sheet；
          b) 压缩包内（或 work 目录内）的 `标的表.csv`（可选 总表.csv / 投标人表.csv）；
          c) 压缩包内的 `*.xlsx`（同上）；
          d) 直接上传的 `.csv`：文件名含「标的表」，或表头就是官方标的表列。
        """
        suffix = dataset_path.suffix.lower()
        # 目录上传（文件夹）时 dataset_path 是目录，后缀无意义：
        # a) / d) 两个「直接读上传文件本身」的分支只在**单文件**时成立；
        # 目录里的 xlsx / 标的表.csv 由 b) / c) 分支从 work 里发现。
        is_file = dataset_path.is_file()

        # a) 直接上传 xlsx
        if is_file and suffix in ('.xlsx', '.xlsm'):
            try:
                return cls._rows_from_xlsx(dataset_path) or None
            except Exception:                    # 损坏/加密的工作簿 -> 按未识别处理
                return None

        # b) 解包目录内的官方 CSV（标的表是必需的，其余可选）
        csvs = cls._find(work, _OFFICIAL_CSVS)
        if '标的表.csv' in csvs:
            # 键统一成表名（去掉 .csv），与 xlsx 分支的 sheet 名一致
            return {name[:-4]: cls._rows_from_csv(p) for name, p in csvs.items()}

        # c) 压缩包内的 xlsx
        for p in sorted(work.rglob('*.xlsx')):
            try:
                tables = cls._rows_from_xlsx(p)
            except Exception:                    # 同上：读不了就换下一个候选
                continue
            if tables:
                return tables

        # d) 直接上传的 csv（未解包，work 为空）
        if is_file and suffix == '.csv':
            rows = cls._rows_from_csv(dataset_path)
            cols = set(rows[0].keys()) if rows else set()
            if '标的表' in dataset_path.name or {'UUID', '产品服务名称'} <= cols:
                return {'标的表': rows}
        return None

    # ---- 官方格式的非数据行过滤 ----
    @staticmethod
    def _is_sample_row(row: dict) -> bool:
        """官方样例表里的「填写说明 / 填写示例 / 示例数据」行 —— 一律不入库。

        判据（用户明确要求）：`序号` 字段含 `说明` / `填写示例` / `示例数据`。
        样例表的第 2 行是填写说明、第 3~N 行是填写示例与示例数据N，都要跳过。
        """
        s = _text(row.get('序号'))
        return any(k in s for k in _SAMPLE_ROW_MARKERS)

    @staticmethod
    def _valid_aid(row: dict) -> bool:
        """UUID（公告号）必须能匹配 `t\\d{8}_\\d{8}`，不匹配的（含空值）跳过。

        用 `search` 而非 `fullmatch`：同一公告的多标段行会写成
        `t20260202_26141082_01` 这种带后缀的形式，同样算合法公告号。
        """
        return bool(_AID_RE.search(_text(row.get('UUID'))))

    @staticmethod
    def _usable_rows(rows: List[dict]) -> List[dict]:
        """滤掉说明/示例行与 UUID 不合法的行。"""
        return [r for r in rows
                if not DeliverablePipeline._is_sample_row(r)
                and DeliverablePipeline._valid_aid(r)]

    @staticmethod
    def _pkg_key(label) -> str:
        """包标识的比较写法：空 / 「未分包」/ 分隔符一律视为同一组（不猜包名）。"""
        s = _text(label).strip()
        if not s or s in ('未分包', '/', '-', '—', '\\', '无', 'N/A'):
            return ''
        return re.sub(r'\s+', '', s)

    @staticmethod
    def _winners_for(groups: Dict[str, List[Tuple[str, object]]], pkg: str
                     ) -> List[Tuple[str, object]]:
        """某 (UUID, 包) 的中标人列表 `[(供应商, 中标金额元), …]`（**保持原文顺序**）。

        总表按 `标段/合同包` 归组；该 UUID 只有一个包组时直接用它（上传件的包标识
        写法常与标的表不完全一致，例如标的表写 `包1`、总表写 `采购包1`）；
        多包时按包匹配，匹配不上才退回该 UUID 的全部中标行（宁可多保留，不丢人）。
        """
        if not groups:
            return []
        if pkg in groups:
            return list(groups[pkg])
        if len(groups) == 1:
            return list(next(iter(groups.values())))
        return [w for lst in groups.values() for w in lst]

    @staticmethod
    def _official_to_recs(tables: Dict[str, List[dict]]) -> Tuple[List[dict], int]:
        """官方三表 -> 路线 1 的抽取结果结构（供 `_write_tables` 直接落成内部交付表）。

        返回 `(recs, skipped)`：`skipped` 是被跳过的说明/示例/无效 UUID 行数
        （用于给用户一个「为什么没有数据」的明确交代，而不是静默成功）。

        三条**必须原样保留用户数据**的口径（本次修复的缺陷）：

        1. **投标人表全部行都要留**（P1-2）。改造前只把行数记进 `skipped` 统计，
           `recs` 里没有名单 —— 于是「上传 1785 行投标人表，下载回来只剩 1170 行，
           得分 / 通过审查 / 未中标原因全丢」。现在逐字段搬进
           `rec['bidder_roster']` -> `bidder_roster.csv` -> 平台库 `t1_bidder`，
           **不做任何重新推断**（`类别` 也一并留着，下载端可用它覆盖推断值）。
        2. **同一 UUID 的多个中标人都要留**（P1-3）。改造前
           `winner.setdefault(aid, …)` 只留第一个中标供应商，其余中标人消失；
           现在按 (UUID, 包) 收集全部中标行，每个中标人各写一行。
        3. **每个中标人自己的中标金额**（P1-3）。改造前标的行只有一行、`总价` 是
           合并后的合计（实测 157.96 + 81.90 万元 -> 剩一行 239.86），
           现在该包有 k>1 个中标人时，按中标人各写一组标的行，金额取总表给
           **该中标人**的中标金额（多标的行时按原总价比例拆分并把余数留给末行，
           保证该中标人合计仍等于总表金额）。
        """
        total_rows = tables.get('总表', [])
        item_rows = tables.get('标的表', [])
        bidder_rows = tables.get('投标人表', [])
        kept_total = DeliverablePipeline._usable_rows(total_rows)
        kept_item = DeliverablePipeline._usable_rows(item_rows)
        kept_bidder = DeliverablePipeline._usable_rows(bidder_rows)
        skipped = ((len(total_rows) - len(kept_total))
                   + (len(item_rows) - len(kept_item))
                   + (len(bidder_rows) - len(kept_bidder)))

        # ---- 总表：UUID -> 公告级字段；(UUID, 包) -> [(中标供应商, 中标金额元), …] ----
        winners_by_aid: Dict[str, Dict[str, List[Tuple[str, object]]]] = {}
        amounts_of_aid: Dict[str, List[object]] = {}
        ann_of: Dict[str, dict] = {}
        for r in kept_total:
            aid = _text(r.get('UUID'))
            pkg = DeliverablePipeline._pkg_key(r.get('标段/合同包'))
            sup = _text(r.get('中标供应商'))
            amt = _yuan(r.get('中标金额'))
            lst = winners_by_aid.setdefault(aid, {}).setdefault(pkg, [])
            if not any(_norm_cell(s) == _norm_cell(sup) for s, _ in lst):
                lst.append((sup, amt))
            if sup:
                amounts_of_aid.setdefault(aid, []).append(amt)
            ann_of.setdefault(aid, {
                'project_name': _text(r.get('项目名称')),
                'purchaser': _text(r.get('采购单位')),
            })
        # 项目金额：单中标人 = 该行中标金额；多中标人 = 各中标金额之和（总合同额）
        for aid, amts in amounts_of_aid.items():
            nums = [a for a in amts if isinstance(a, (int, float))]
            if nums:
                ann_of.setdefault(aid, {})['project_amount'] = (
                    _money(nums[0]) if len(nums) == 1 else _money(sum(nums)))

        # ---- 标的表：按 (UUID, 包) 归组，再按该包的中标人展开成行 ----
        by_pkg: Dict[Tuple[str, str], List[dict]] = {}
        pkg_order: List[Tuple[str, str]] = []
        items_of: Dict[str, List[dict]] = {}
        order: List[str] = []
        for r in kept_item:
            aid = _text(r.get('UUID'))
            key = (aid, DeliverablePipeline._pkg_key(r.get('标段/合同包')))
            if key not in by_pkg:
                by_pkg[key] = []
                pkg_order.append(key)
            if aid not in items_of:
                items_of[aid] = []
                order.append(aid)
            by_pkg[key].append({
                'package_no': _text(r.get('标段/合同包')),
                'item_name': _text(r.get('产品服务名称')),
                'category': _text(r.get('品目')),
                'brand': _text(r.get('品牌')),
                'spec': _text(r.get('规格型号')),
                'quantity': _qty(r.get('数量')),
                'unit_price': _yuan(r.get('单价')),          # 万元 -> 元
                'total_price': _yuan(r.get('总价')),         # 万元 -> 元
                'is_winner': True,
                'source': 'OFFICIAL',
            })

        for key in pkg_order:
            aid, pkg = key
            rows = by_pkg[key]
            winners = DeliverablePipeline._winners_for(winners_by_aid.get(aid) or {}, pkg)
            if len(winners) <= 1:
                # 单中标人（或总表没给中标供应商）：保持改造前的一行一口径
                sup = winners[0][0] if winners else ''
                for it in rows:
                    row = dict(it)
                    row['supplier_name'] = sup
                    items_of[aid].append(row)
                continue
            # ---- 多中标人：每个中标人各写一组标的行（金额 = 总表给该人的中标金额）----
            base = [(_num(it.get('total_price')) or 0.0) for it in rows]
            base_total = sum(base)
            for sup, amt in winners:
                amt_f = _num(amt)
                others = 0.0
                for idx, it in enumerate(rows):
                    row = dict(it)
                    row['supplier_name'] = sup
                    if amt_f is None:
                        # 总表没给这个中标人的金额：留空（不臆造，也不把合计重复算 k 遍）
                        row['total_price'] = ''
                        row['unit_price'] = ''
                    elif len(rows) == 1:
                        row['total_price'] = _money(amt_f)
                        qty = _num(it.get('quantity'))
                        row['unit_price'] = _money(amt_f / qty) if qty else _money(amt_f)
                    elif base_total > 0:
                        share = (amt_f - others if idx == len(rows) - 1
                                 else round(amt_f * base[idx] / base_total, 2))
                        share = round(share, 2)
                        others += share
                        row['total_price'] = _money(share)
                        qty = _num(it.get('quantity'))
                        row['unit_price'] = _money(share / qty) if qty else _money(share)
                    items_of[aid].append(row)

        # 只有总表、没有标的行的 UUID 也保留（公告级信息可检索，n_items=0）
        for aid in ann_of:
            if aid not in items_of:
                items_of[aid] = []
                order.append(aid)

        # ---- 投标人表：逐行原样搬进 bidder_roster（公告号 + 用户给的字段原值）----
        roster_of: Dict[str, List[dict]] = {}
        for r in kept_bidder:
            aid = _text(r.get('UUID'))
            nm = _text(r.get('名称'))
            if not nm:
                continue            # 名称为空的行不入库（下载端的同名过滤见 export_api）
            review, reason = _parse_review_cell(r.get('通过审查'))
            roster_of.setdefault(aid, []).append({
                'package_label': _text(r.get('标段/合同包')),
                'name': nm,
                'category': _text(r.get('类别')),
                'quote_yuan': _yuan(r.get('报价（万元）')),   # 万元 -> 元
                'score': _qty(r.get('得分')),
                'review': review,
                'review_reason': reason,
                'lose_reason': _text(r.get('未中标原因')),
                'source': 'OFFICIAL',
            })

        recs = [{'announcement_id': aid,
                 'extraction': {'engine': 'official',
                                'announcement': ann_of.get(aid, {}),
                                'items': items_of[aid]},
                 'bidder_roster': roster_of.get(aid, [])}
                for aid in order]
        return recs, skipped

    # ---- 入库（两条模式共用同一条 _load_ingest 路径）----

    @staticmethod
    def _rebuild_relations(deliv: Path, ctx: PipelineContext,
                           scratch: Optional[Path] = None) -> str:
        """从本次交付表**纯计算**重建 `task2_*.csv`，返回可拼进 job message 的一句说明。

        只在「本次数据集没有完整任务二关系表」时调用（判据：交付目录里没有
        `task2_cooperation.csv`）。建图逻辑复用 `scripts/build_task2_graph.py::build()`，
        因此上传路径与全量交付件是同一套实体归一/金额回退/视图口径。

        `scratch` 是**本次上传的工作目录**（`run()` 里的 `work`）：中间图库写到
        `scratch/_task2_graph.db`，随 `finally` 的 `rmtree(work)` 一起清理。
        绝不能退回 `tempfile.mkdtemp()`（会被工作区清理漏掉）或 `deliv.parent`
        （路由①的交付目录可能就是 `work` 本身，`parent` 会落到系统临时目录，
        多个 job 共用同一个 `merged_deliverables`）。

        失败时只打印一行警告并返回空串（入库照常，关系表按空关系处理）——
        与上传路径的其他「增强步骤」（归位回填/图库同步）保持一致：绝不因为
        附加能力失败而让整次导入失败。
        """
        deliv = Path(deliv)
        if not (deliv / 'items_task1.csv').exists():
            return ''
        scratch = Path(scratch) if scratch is not None else deliv.parent
        ctx.report('关系建模', 0.9,
                   message='正在从本次上传的标的/公告重建任务二关系表…')
        try:
            loaded, meta = _records_from_delivery(deliv)
            if not loaded:
                return ''
            if meta.get('bid_from_delivery'):
                src_map = meta.get('bid_src_map') or {}

                def _bid_source(aid: str, supplier: str, is_winner) -> str:
                    return src_map.get((aid, supplier)) or 'FROM_DELIVERY_BIDS'  # type: ignore[union-attr]
            else:
                def _bid_source(aid: str, supplier: str, is_winner) -> str:
                    return 'DERIVED_FROM_ITEMS' if is_winner else 'DERIVED_FROM_ROSTER'
            res = _load_task2_build()(
                loaded,
                str(scratch / '_task2_graph.db'),        # 中间产物：work 内，随工作区清理
                str(deliv),                              # task2_*.csv 写回本次交付目录
                bid_source=_bid_source,
            )
        except Exception as e:                       # noqa: BLE001
            print(f'[警告] 关系表重建失败（已跳过，按空关系入库）：'
                  f'{type(e).__name__}: {_one_line(e)}')
            return ''
        stats = res.get('stats') or {}
        msg = ('关系表重建：%d 张（cooperation %d / bid %d）'
               % (len(stats), stats.get('cooperation', 0), stats.get('bids', 0)))
        ctx.report('关系建模', 0.9, message=msg)
        return msg

    def _load_deliverables(self, deliv: Path, ctx: PipelineContext, mode: str,
                           snapshot: Optional[Dict[str, Tuple[List[str], List[dict]]]],
                           work: Optional[Path] = None,
                           ) -> Tuple[dict, str]:
        """把交付表写进平台库，返回 `(ingest 结果, 模式说明文案)`。

        `mode == 'append'` 时**先**把库内现有 t1_item / t1_notice 取出来，
        与本次交付表按行去重合并成一套新交付表，再走原封不动的 `_load_ingest()`
        —— 效果 = 原有 ∪ 新增，且识别/抽取逻辑一行未改。

        **关系建模阶段**（P0-2）：交付目录没有 `task2_cooperation.csv` 时，先调用
        `_rebuild_relations()` 从本次 items/announcements 生成 `task2_*.csv`，再入库；
        此时 `graph_db` 显式置空，让 `ingest()` 走「交付表 CSV」路径 ——
        绝不用仓库里全量 `graph.db` 的关系结果去污染这份上传数据。
        """
        deliv = Path(deliv)
        db = _platform_db()
        scratch = Path(work) if work is not None else deliv.parent
        rebuild_why = ''
        if mode == MODE_APPEND and snapshot is not None:
            merged = _merge_deliverables(snapshot, deliv, scratch / 'merged_deliverables')
            ctx.report('合并库内数据', 0.9,
                       message=f'追加模式：库内 '
                               f'{len(snapshot.get(_T1_NOTICE, ([], []))[1])} 篇公告 / '
                               f'{len(snapshot.get(_T1_ITEM, ([], []))[1])} 条标的 '
                               f'与本次解析结果按行去重合并')
            graph_db, why = _relation_source(deliv)
            # 追加模式的合并目录里只有任务一的表 + 本次的 task2_bids（若有）；
            # 关系表是否要重建看**本次上传**的目录（不能看合并目录）。
            need_rebuild = not (deliv / 'task2_cooperation.csv').exists()
            if need_rebuild:
                # 合并事实层（库内 bid ∪ 本次新增），再整库重算关系表：
                # 聚合表不能直接相加，合并 bid 行后重算才能既保留历史参与方又不重复计数。
                _write_append_bids(db, merged)
                rebuild_why = self._rebuild_relations(merged, ctx, scratch=scratch)
                graph_db = ''
            deliv = merged
        else:
            graph_db, why = _relation_source(deliv)
            need_rebuild = not (deliv / 'task2_cooperation.csv').exists()
            if need_rebuild:
                rebuild_why = self._rebuild_relations(deliv, ctx, scratch=scratch)
                graph_db = ''
        res = _load_ingest()(str(db), deliv_dir=str(deliv), graph_db=graph_db)
        # 阶段行给一句短的来源说明；`why` 为空（本次数据集没有 task2_*.csv）时
        # 最终 job message 里不再出现这段说明（用户反馈冗长）。
        if need_rebuild:
            why = rebuild_why or '关系表：本次数据无关系表来源，自动重建未产出（按空关系入库）'
            ctx.report('关系表来源', 0.95,
                       message='关系表：由本次上传的标的/公告重建'
                               if rebuild_why else why)
        else:
            ctx.report('关系表来源', 0.95,
                       message=why or '关系表：本次数据集无 task2_*.csv，按空关系重建')
        # 入库成功后立刻把**图库**同步为「当前 platform.db 的数据」：
        # 默认后端是 Neo4j，它的图不会因为重建 platform.db 而变，
        # 不同步的话五个场景会继续返回图库里的旧全量结果（本次要修的缺陷）。
        graph_note = _sync_graph_after_ingest(db, ctx)
        if graph_note:
            why = f'{why}；{graph_note}'
        return res, _mode_note(mode, res, snapshot), why

    # ---- 主流程 ----
    def run(self, dataset_path: Path, ctx: PipelineContext,
            mode: str = MODE_REPLACE) -> None:
        """处理数据集：`mode='replace'` 覆盖（默认）/ `'append'` 保留原数据并追加。

        `dataset_path` 既可以是**压缩包 / 单个 xlsx / csv 文件**，也可以是**目录**
        （前端「选择文件夹 / 拖入文件夹」上传，api 层已把多文件按相对路径还原成
        一棵临时目录树）。目录**跳过解压**，直接扫描其内容，之后 4 条识别路线
        与单文件上传完全一致。
        """
        mode = _normalize_mode(mode)
        # 解压预算超限的异常类型：由 `ict_parse.attach` 定义，这里的路线②/④
        # 要把它从「单篇容错」里救出来，判成 job 失败（见下方两个 except）。
        from ict_parse.attach import UnzipBudgetExceeded       # noqa: PLC0415
        # append 必须在**任何写入之前**取快照：ingest() 是整库重建，
        # 一旦开始写，库内原有数据就没了。
        snapshot = (_platform_snapshot(_platform_db())
                    if mode == MODE_APPEND else None)
        dataset_path = Path(dataset_path)
        is_dir = dataset_path.is_dir()
        # 目录上传时统一带上前缀，让 job 的 message 能看出「这次是文件夹上传、扫到几个文件」
        src = ''
        work = Path(tempfile.mkdtemp(prefix='edm_import_'))
        try:
            if is_dir:
                n_files = self._stage_dir(dataset_path, work)
                not_zip = ''
                ctx.report('扫描上传目录', 0.05,
                           message=f'目录上传：扫描到 {n_files} 个文件'
                                   f'（{"追加到现有数据" if mode == MODE_APPEND else "覆盖现有数据"}）')
            else:
                ctx.report('解包数据集', 0.05,
                           message=f'解析压缩包目录（{"追加到现有数据" if mode == MODE_APPEND else "覆盖现有数据"}）')
                # 非压缩包文件（直接上传的 .xlsx / .csv）在这里必然解包失败，
                # 不直接报错，交给后面的路线 3 识别。
                try:
                    n_files = self._unzip(dataset_path, work)
                    not_zip = ''
                except zipfile.BadZipFile as e:
                    n_files, not_zip = 0, str(e)
                except UnzipTooLargeError as e:
                    # 解压上限（zip bomb / 超大附件包）：明确失败，不留半棵树
                    ctx.fail('数据集解压后超出上限', str(e))
                    return
            if not n_files and not not_zip:
                ctx.fail('上传的文件夹为空' if is_dir else '数据集为空',
                         f'{dataset_path.name} 内没有文件')
                return
            if is_dir:
                src = f'目录上传：扫描到 {n_files} 个文件；'

            # ---- 路线 1：交付表数据集（最快，直接入库）----
            csvs = self._find(work, ['items_task1.csv', 'announcements_task1.csv'])
            if 'items_task1.csv' in csvs:
                ctx.report('识别交付表数据集', 0.35,
                           message=f'{src}识别路线①：交付表数据集（{len(csvs)} 个交付表）')
                src_dir = csvs['items_task1.csv'].parent
                # ⚠️ 缺公告表时**用本次上传的标的表合成**，绝不读仓库基准交付件。
                # 改造前这里是 `shutil.copy(_DELIVERABLES/'announcements_task1.csv', src_dir)`：
                # 用户只上传一份只含 1 篇公告的 items_task1.csv，库里却被塞进
                # **仓库基准的 1038 篇公告**（P1：路线① 数据串车 / 仓库基准回退）。
                # 那批公告的 UUID 全部来自仓库交付集，与本次上传毫无关系。
                synth = ''
                if 'announcements_task1.csv' not in csvs:
                    n_ann, synth = self._synthesize_announcements(
                        csvs['items_task1.csv'], src_dir)
                    if not n_ann:
                        ctx.fail('缺少 announcements_task1.csv',
                                 f'{dataset_path.name} 里的 items_task1.csv 没有任何带公告号'
                                 f'（announcement_id）的标的行，无法合成公告表；'
                                 f'请一并提供 announcements_task1.csv')
                        return
                res, note, why = self._load_deliverables(src_dir, ctx, mode, snapshot,
                                                          work=work)
                c = res.get('counts', {})
                ctx.report('写入数据库', 1.0, processed=c.get('t1_notice', 0),
                           total=c.get('t1_notice', 0), items=c.get('t1_item', 0),
                           message=_job_message(
                               f'{src}识别路线①：交付表数据集', note, why, synth))
                return

            # ---- 路线 2：原始 HTML 数据集（规则层抽取后入库）----
            # 前端「直接多选 .html 公告文件」也是走文件夹入口：api 层把多文件按相对路径
            # 还原成目录树后，`work/` 根下就是「一篇公告 = 一个 .html」的平铺集合，
            # 所以这里**不要求任何目录前缀**，平铺文件名即公告号（`<aid>.html`）。
            # `.htm` 与 `.html` 同义（前端 accept 两者都放行），一并纳入路线②。
            htmls = [p for p in work.rglob('*')
                     if p.is_file() and p.suffix.lower() in self._HTML_EXTS]
            if htmls:
                # 解压预算超限 -> job 直接失败（不能让单篇容错把它记成一条 note）
                try:
                    return self._run_html(dataset_path, work, ctx, htmls, mode,
                                          snapshot, src)
                except UnzipBudgetExceeded as e:
                    ctx.fail('数据集解压后超出上限', str(e))
                    return

            # ---- 路线 3：官方标准格式（xlsx 三表 / 标的表.csv）----
            official = self._load_official(dataset_path, work)
            if official is not None:
                ctx.report('识别官方标准格式', 0.35,
                           message=f'{src}识别路线③：官方标准格式（'
                                   + '；'.join(f'{k} {len(v)} 行'
                                               for k, v in official.items()) + '）')
                # 官方样例表本身（第 2 行填写说明、第 3~N 行填写示例/示例数据）也要能上传成功：
                # 非数据行在这里被过滤掉，结果为 0 行时**不再判失败**（只是没有任何数据入库）。
                recs, skipped = self._official_to_recs(official)
                deliv = work / 'deliverables'
                bf = self._backfill(recs, ctx)           # 归位回填（只补空值）
                ni, na = self._write_tables(recs, deliv)     # 复用路线 1 的交付表写法
                res, note, why = self._load_deliverables(deliv, ctx, mode, snapshot,
                                                          work=work)
                c = res.get('counts', {})
                msg = _job_message(
                    f'{src}识别路线③：官方标准格式', note, why, bf,
                    (f'已跳过 {skipped} 行「填写说明/填写示例/示例数据」或无效 UUID'
                     if skipped else ''),
                    '' if ni else '该表没有有效数据行，未入库任何标的')
                ctx.report('写入数据库', 1.0, processed=na, total=na,
                           items=c.get('t1_item', ni), message=msg)
                return

            # ---- 路线 4：附件包（无 HTML）----
            # 到这里说明：没有交付表、没有 HTML、也不是官方三表 —— 只可能是「单篇公告的附件包」。
            try:
                return self._run_attachments(dataset_path, work, ctx, mode, snapshot, src)
            except UnzipBudgetExceeded as e:
                ctx.fail('数据集解压后超出上限', str(e))
                return

        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ---- 路线②：HTML 与附件包的「同一公告」配对 ----

    @classmethod
    def _base_key(cls, p) -> str:
        """同一公告的判定基名：反复去掉 `.html/.htm/.zip/.rar/.7z` 与 `_file` 后缀。

        例（都 -> `t20260202_26139731`）：
            `t20260202_26139731.html`
            `t20260202_26139731.zip`
            `t20260202_26139731_file.zip`
            `t20260202_26139731_file.rar`
            `20261013213236_t20260202_26139731_file.zip` -> 前缀保留（对不上 HTML，走公告号兜底）
        """
        name = Path(p).name
        exts = cls._HTML_EXTS + cls._ARCHIVE_EXTS
        changed = True
        while changed:
            changed = False
            low = name.lower()
            for ext in exts:
                if low.endswith(ext):
                    name = name[:-len(ext)]
                    changed = True
                    break
            if not changed and low.endswith('_file'):
                name = name[:-len('_file')]
                changed = True
        return name

    def _pair_html_archives(self, dataset_path: Path, work: Path,
                            htmls: List[Path]) -> Tuple[Dict[str, Optional[Path]], List[Path]]:
        """把 HTML 与附件包按「同一公告」配对。

        返回 `({str(html): 附件包 Path|None}, [未配对的压缩包])`。

        判据（按可信度从高到低）：
          1. **同一基名**（`_base_key`）—— 覆盖 `<aid>.html` + `<aid>.zip`、
             `<aid>.html` + `<aid>_file.zip`，以及赛题数据的**跨目录**布局
             （`赛题五.基准测试数据_html/<aid>.html` + `赛题五.基准测试数据_file/<aid>.zip`）；
          2. 文件名或所在目录里能认出同一个公告号（`<aid>/xxx.zip` 这类「一个公告一个目录」的附件包）。
        多个候选时优先 `.zip`（`process_one` 的附件包入口对 zip/rar/7z 都支持，zip 最稳）。
        """
        archives = [p for p in work.rglob('*')
                    if p.is_file() and p.suffix.lower() in self._ARCHIVE_EXTS]
        by_key: Dict[str, List[Path]] = {}
        for p in archives:
            by_key.setdefault(self._base_key(p), []).append(p)
        pairs: Dict[str, Optional[Path]] = {}
        used: set = set()
        for h in sorted(htmls):
            key = self._base_key(h)
            cand = [p for p in by_key.get(key, []) if p not in used]
            if not cand:
                aid = self._infer_aid(h.stem)
                if aid:
                    cand = [p for p in archives if p not in used
                            and (self._infer_aid(p.name)
                                 or self._infer_aid(p.parent.name)) == aid]
            pick: Optional[Path] = None
            if cand:
                cand.sort(key=lambda z: (0 if z.suffix.lower() == '.zip' else 1, str(z)))
                pick = cand[0]
                used.add(pick)
            pairs[str(h)] = pick
        orphans = [p for p in sorted(archives) if p not in used]
        return pairs, orphans

    def _extract_orphan_archives(self, dataset_path: Path, work: Path,
                                 ctx: PipelineContext, orphans: List[Path], budget,
                                 use_llm: bool, tally: List[int]) -> List[dict]:
        """未与 HTML 配对的压缩包 -> 按附件路线解析出的 rec 列表（并入同一批交付表）。

        复用 `_run_attachments(collect_only=True)`：同一套附件解析/抽取口径，只是**不写库**
        （本次导入只会整库重建一次，见 `_load_deliverables`）。
        `tally` 是调用方的 `[n_llm, n_rule]`（传 list 以便回写计数），让 job message 的
        「LLM N 篇 / 规则层回退 M 篇」把附件路线这几篇也算进去。
        """
        orphan_set = set(orphans)
        groups = [g for g in self._attachment_groups(dataset_path, work)
                  if g.get('zip') in orphan_set]
        if not groups:
            return []
        recs = self._run_attachments(dataset_path, work, ctx, collect_only=True,
                                     only_groups=groups, budget=budget,
                                     use_llm_override=use_llm) or []
        for rec in recs:
            if _engine_is_llm(rec.get('extraction')):
                tally[0] += 1
            else:
                tally[1] += 1
        if recs:
            ctx.report(f'补解析未配对的附件包 {len(recs)} 篇', 0.85,
                       message=f'{len(groups)} 个未与 HTML 配对的附件包已按附件路线并入本次结果')
        return recs

    def _run_html(self, dataset_path: Path, work: Path, ctx: PipelineContext,
                  htmls: List[Path], mode: str = MODE_REPLACE,
                  snapshot: Optional[Dict[str, Tuple[List[str], List[dict]]]] = None,
                  src: str = '') -> None:
        """路线 2：原始 HTML 数据集 —— 逐篇抽取（有 key 走 LLM，失败回退规则层）后入库。

        **同一公告号的 HTML 与附件包合并成一条任务**（P1：原先「有 HTML 就只走 HTML」，
        附件包被静默忽略 —— 页面显示有公告标题，却没有附件里的报价/标的）。
        配对规则见 `_pair_html_archives()`：同一基名（去 `.html/.htm/.zip/.rar/.7z`
        与 `_file` 后缀）即视为同一篇公告，**HTML 作正文、压缩包作附件包**一起交给
        `process_one(html, zip)`（该函数的 `zip_path` 参数本就支持两者同传）。
        没配上的压缩包（只有 zip、或基名对不上）仍按附件路线解析，结果并入同一批交付表。
        """
        from ict_parse.pipeline import process_one      # noqa: PLC0415
        from ict_parse.attach import UnzipBudget, UnzipBudgetExceeded  # noqa: PLC0415

        # **整次导入共用一个解压预算**：套娃压缩包的累计字节数跨篇共享上限
        # （P1：外层 51 KB 的 zip -> 内层 zip -> 50 MB 文件 之前可以直接落盘）。
        budget = UnzipBudget()

        # 用户要求：只要 text_config() 能取到 key 就必须用 LLM（进程环境变量 / .env / 页面设置均可）。
        # `process_one` 内部再判一次 available()，并在可预期的 LLM 失败时回退规则层。
        use_llm = self.use_llm or _llm_configured()
        out_root = work / 'out'
        recs: List[dict] = []
        n_llm = n_rule = 0
        total = len(htmls)
        # HTML <-> 附件包 配对（跨目录也能配上：赛题数据是 `_html/` 与 `_file/` 两个目录）
        pairs, orphan_archives = self._pair_html_archives(dataset_path, work, htmls)
        n_paired = sum(1 for v in pairs.values() if v is not None)
        pair_note = (f'；其中 {n_paired} 篇已与附件包合并解析' if n_paired else '')
        orphan_note = (f'；另有 {len(orphan_archives)} 个未配对的附件包按附件路线补解析'
                       if orphan_archives else '')
        if use_llm:
            ctx.report('识别原始 HTML 数据集', 0.1, total=total,
                       message=f'{src}识别路线②：原始 HTML 数据集；{total} 篇公告，'
                               f'走 LLM 抽取层（失败自动回退规则层）{pair_note}{orphan_note}')
        else:
            ctx.report('识别原始 HTML 数据集', 0.1, total=total,
                       message=f'{src}识别路线②：原始 HTML 数据集；{total} 篇公告，'
                               f'未配置 LLM key，走规则层抽取{pair_note}{orphan_note}')
        ordered = sorted(htmls)

        def _zip_for(html: Path) -> Optional[str]:
            """该篇 HTML 的附件包：先看配对结果，再退回旧的「同目录同名」查找。"""
            zp = pairs.get(str(html))
            if zp is not None and Path(zp).exists():
                return str(zp)
            aid = html.stem
            for cand in (html.with_name(f'{aid}_file.zip'),
                         work / f'{aid}_file.zip',
                         html.with_name(f'{aid}.zip')):
                if cand.exists():
                    return str(cand)
            return None

        def _extract_one(html: Path) -> Tuple[dict, str]:
            """单篇 HTML（+配对的附件包）-> (rec, engine)。异常由调用方记错误。"""
            aid = html.stem
            tim: Dict[str, float] = {}
            zp = _zip_for(html)
            rec, out_dir = process_one(aid, str(html), zp, str(out_root),
                                       use_vlm=self.use_vlm, use_llm=use_llm, timings=tim,
                                       budget=budget)
            if zp:
                # 合并解析的**并集**口径：附件侧的「需求/分包清单」里 HTML 没覆盖到的条目
                # 也并进来（否则「HTML+zip」反而比「只传 zip」少行 —— 与合并解析的预期相反）
                z0 = time.perf_counter()
                added = self._merge_attachment_items(rec, out_dir)
                if added:
                    tim['merge_attach_items'] = round(time.perf_counter() - z0, 3)
            _log_doc_timing(aid, tim)
            # run() 里 process_one 已把 extraction 放进 rec
            return rec, ('LLM' if _engine_is_llm(rec.get('extraction')) else '规则层')

        if use_llm and _llm_configured() and total > 1:
            # ---- 并发抽取（只并发「每篇一次 LLM 抽取」；默认 6 路，EDM_LLM_CONCURRENCY 可调）----
            # 结果按**输入顺序**汇总（索引映射）；ctx.report 只在主线程按完成篇数汇报，
            # 避免多线程乱序推进度。单篇失败被隔离成 exc，仍按原逻辑记错误、不中断整批。
            from integrations import llm_parallel                   # noqa: PLC0415
            done = 0

            def _on_done(idx: int, value, exc) -> None:
                nonlocal done
                done += 1
                aid = ordered[idx].stem
                if exc is not None:
                    ctx.job.errors.append(f'{aid}: {type(exc).__name__}: {exc}')
                    engine = '异常'
                else:
                    engine = value[1] if value[1] == 'LLM' else '规则层（回退）'
                ctx.report(f'抽取第 {done}/{total} 篇', 0.1 + 0.75 * done / total,
                           processed=done, total=total,
                           message=f'{aid}（{engine}）；已完成 {done}/{total} 篇')

            results = llm_parallel.map_ordered(
                [lambda h=h: _extract_one(h) for h in ordered], on_done=_on_done)
            for value, exc in results:               # 输入顺序（而不是完成顺序）
                if isinstance(exc, UnzipBudgetExceeded):
                    raise exc                        # 解压超限 -> job 失败（不吞）
                if exc is not None or value is None:
                    continue
                rec, _engine = value
                recs.append(rec)
                if _engine_is_llm(rec.get('extraction')):
                    n_llm += 1
                else:
                    n_rule += 1
        else:
            # 规则层（未配置 LLM key）保持原样：单线程逐篇，行为与改造前完全一致
            for i, html in enumerate(ordered, 1):
                aid = html.stem
                engine = 'LLM' if use_llm else '规则层'
                try:
                    rec, _ = _extract_one(html)
                    if _engine_is_llm(rec.get('extraction')):
                        n_llm += 1
                        engine = 'LLM'
                    else:
                        n_rule += 1
                        engine = '规则层' + ('（回退）' if use_llm else '')
                    recs.append(rec)
                except UnzipBudgetExceeded:
                    raise                               # 解压超限 -> job 失败（不吞）
                except Exception as e:                       # noqa: BLE001
                    ctx.job.errors.append(f'{aid}: {type(e).__name__}: {e}')
                    engine = '异常'
                # 进度每篇推进（LLM 比规则层慢得多，这里必须持续反馈）
                ctx.report(f'抽取第 {i}/{total} 篇', 0.1 + 0.75 * i / total,
                           processed=i, total=total, message=f'{aid}（{engine}）')

        # ---- 未配对的压缩包：按附件路线补解析，结果并入同一批交付表（不丢数据）----
        if orphan_archives:
            tally = [n_llm, n_rule]
            recs.extend(self._extract_orphan_archives(
                dataset_path, work, ctx, orphan_archives, budget, use_llm, tally))
            n_llm, n_rule = tally

        deliv = work / 'deliverables'
        bf = self._backfill(recs, ctx)               # 归位回填（只补空值、逐条留凭证）
        ni, na = self._write_tables(recs, deliv)
        if not ni:
            ctx.fail('抽取结果为空', f'处理 {total} 篇后没有任何标的物')
            return
        res, note, why = self._load_deliverables(deliv, ctx, mode, snapshot, work=work)
        c = res.get('counts', {})
        ctx.report('写入数据库', 1.0, processed=na, total=total, items=c.get('t1_item', ni),
                   message=_job_message(f'{src}识别路线②：原始 HTML 数据集',
                                        f'{note}（{_llm_rule_tally(n_llm, n_rule)}）',
                                        pair_note.lstrip('；'), bf, why))

    # ==============================================================
    # 路线 4：单篇公告的附件包（包内没有 HTML）
    # ==============================================================

    @staticmethod
    def _infer_aid(*names) -> str:
        """从文件名 / 目录名 / 包内文件名里提取公告号（`t\\d{8}_\\d{8}`）。

        例：上传文件名 `20261013213236_t20260202_26139731.zip`
        -> `t20260202_26139731`；包内 `t20260408_26371938.zip` -> 同名公告号。
        提取不到返回 ''（由调用方继续用文件名兜底，不因此判失败）。
        """
        for n in names:
            m = _AID_RE.search(str(n or ''))
            if m:
                return m.group(0)
        return ''

    def _attachment_groups(self, dataset_path: Path, work: Path) -> List[dict]:
        """把解包后的内容按「公告」分组。

        返回 `[{'aid': str, 'zip': Path|None, 'files': [Path]}]`，分组依据：
          * 包内的 `<公告号>.zip`（可多个）—— 每个子压缩包 = 一篇公告的附件包；
          * 附件目录 —— 第一层目录名里含公告号时，同目录文件归为一篇；
          * 其余散件 —— 归到「由上传文件名推断出的公告号」那一组（通常只有一篇）。
        """
        groups: List[dict] = []

        def _group_for(aid: str, key: str) -> dict:
            for g in groups:
                if g['key'] == key:
                    return g
            g = {'key': key, 'aid': aid, 'zip': None, 'files': []}
            groups.append(g)
            return g

        for z in sorted(work.rglob('*.zip')):
            aid = self._infer_aid(z.name) or self._infer_aid(dataset_path.name)
            _group_for(aid, aid or f'zip:{z.relative_to(work)}')['zip'] = z

        for p in sorted(work.rglob('*')):
            if not p.is_file() or p.suffix.lower() == '.zip':
                continue
            rel = p.relative_to(work)
            # 多级目录时优先用第一层目录名（「多个附件目录」形态）
            aid = self._infer_aid(rel.parts[0]) if len(rel.parts) > 1 else ''
            aid = aid or self._infer_aid(p.name) or self._infer_aid(dataset_path.name)
            _group_for(aid, aid or 'files')['files'].append(p)
        return groups

    # ------------------------------------------------------------------
    # 注意（P1-4，已删除 `_bench_zip` / `_bench_html`）：
    # 改造前这两个方法会在上传路径里**兜底读仓库自带的基准数据**
    # （`赛题五.基准测试数据_html/<aid>.html`、`赛题五.基准测试数据_file/<aid>.zip`）：
    #     zp   = g['zip'] or self._bench_zip(aid)
    #     html = self._bench_html(aid)
    # 后果（实测）：用户只上传自己的 `报价表.csv`、而目录名恰好等于某个基准公告号时，
    # 解析的是**仓库里的另一家企业**的 HTML/附件包，用户自己的文件完全没用上 ——
    # 提取结果与用户上传的数据毫无关系（数据可信度问题）。
    # 现在**上传路径只允许使用本次上传提供的文件**：没有对应附件就按"无附件"处理、
    # 没有 HTML 就按"无 HTML"处理（走附件规则层），并在 job message 里如实说明。
    # ------------------------------------------------------------------

    @staticmethod
    def _zip_candidates(files: List[Path], dest: Path) -> Optional[str]:
        """把一组散落附件临时打成一个 zip（内容 = 用户上传的附件，绝不掺入仓库数据）。"""
        if not files:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, arcname=f.name)
        return str(dest)

    @staticmethod
    def _probe_pdf_tables(atts: List[dict], root_dir: str) -> None:
        """PDF 附件补抽表格（路线④ 与「HTML+附件包」合并解析都用）。

        `ict_parse.attach.extract_text_for` 只对文件名含「报价/一览/明细/标的/开标」的 PDF
        抽表格；附件包里常常只有一份《××招标文件.pdf》，按文件名判定拿不到表格，
        于是整包一条标的都抽不出来。这里对取不到表格的 PDF 补抽一次表格
        （仍然只走规则层，不调用 LLM；表格里的「预算/最高限价」列会被 `_canon` 主动忽略）。

        文本已经解析过时**只补表格**（`pdf_tables`），不重复抽一遍正文 ——
        合并解析里这步对每篇公告都要跑，省掉一次全文抽取是实打实的提速。
        """
        for a in atts:
            if (a.get('ext') or '').lower() != '.pdf' or a.get('tables'):
                continue
            p = os.path.join(root_dir, a.get('rel_path') or a.get('name') or '')
            if not os.path.exists(p):
                continue
            try:
                from ict_parse.pdf_text import pdf_probe, pdf_tables   # noqa: PLC0415
                if a.get('text'):
                    tables = pdf_tables(p, max_pages=6)
                    if tables:
                        a['tables'] = tables
                else:
                    text, tables, _meta = pdf_probe(p, need_tables=True)
                    if tables:
                        a['tables'] = tables
                    a['text'] = text
            except Exception as e:                              # noqa: BLE001
                a['note'] = f'{a.get("note") or ""}; pdf table probe failed: {e}'.strip('; ')

    @staticmethod
    def _loose_attachments(files: List[Path], work: Path) -> List[dict]:
        """散落文件 -> 与 `ict_parse.attach.extract_bundle` 同构的 Attachment 结构。"""
        out = []
        for f in files:
            try:
                rel = str(f.relative_to(work))
            except ValueError:
                rel = f.name
            out.append({'name': f.name, 'download_id': None, 'rel_path': rel,
                        'ext': f.suffix.lower(), 'size': f.stat().st_size,
                        'text': None, 'tables': None, 'note': None})
        return out

    @staticmethod
    def _name_from_filename(a: dict) -> str:
        """附件文件名 -> 项目名候选（去日期前缀 / 发售稿等前置词 / 尾部文件名词）。"""
        n = Path(a.get('name') or '').stem
        n = re.sub(r'^\s*\d{6,8}[\s\-_]*', '', n)
        while True:                                   # 去掉「发售稿/定稿」等前置词
            m2 = re.match(r'^\s*(发售稿|定稿|终稿|送审稿|最终版)[\s\-_]*', n)
            if not m2:
                break
            n = n[m2.end():]
        n = re.sub(r'[\s\-_]*(发售稿|定稿|终稿|招标文件|采购文件|项目文件|响应文件|'
                   r'投标文件|中标公告|成交公告|结果公告|公告)\s*$', '', n)
        return n.strip(' -_')

    @classmethod
    def _guess_project(cls, text: str, atts: List[dict]) -> str:
        """公告/项目名称：附件正文里的「项目名称：…」为主，文件名候选作**完整性校验**。

        P0（合规：「保留公告原文，不得缩写」）：PDF 文本层里，用了另一种字体的数字/英文
        会被排到**另一条基线**（见 `ict_parse/pdf_text.py` 的说明），于是
        `项目名称：2023年工信部5G+8K项目-光华路…` 抽出来变成 `年工信部 项目光华路…`
        —— 丢的正是 `2023` / `5G+8K` / `-`。`pdf_text` 已在线宽层面修补；这里再加一道
        **无损校验**：若正文候选是文件名候选的「删字版」（`clean.is_char_deleted_from`），
        说明文本层确实丢了字符，改用文件名候选（它同样来自公告原文的附件名，且更长更全）。
        两个候选都不完整时保持正文候选原样，不臆造。
        """
        from ict_parse.clean import clean_project_name, is_char_deleted_from  # noqa: PLC0415

        m = re.search(r'项目名称[：:]\s*([^\n\r]{2,80})', text or '')
        text_name = clean_project_name(m.group(1)) if m else ''
        file_name = ''
        for a in atts:
            cand = cls._name_from_filename(a)
            if cand:
                file_name = clean_project_name(cand)
                break
        if text_name and file_name and is_char_deleted_from(text_name, file_name):
            return file_name
        return text_name or file_name

    _NAME_COL_HINTS = ('采购内容', '采购名称', '包名称', '标项名称', '货物名称', '服务名称',
                       '标的名称', '产品名称', '设备名称', '项目内容', '采购标的', '名称')

    def _demand_items(self, atts: List[dict]) -> List[dict]:
        """兜底：规则层一条标的都没抽到时，从附件表格里抽「需求清单」。

        只做两件事：把「采购内容 / 包名称 / …」这类未登记在表头映射里的名称列**改名**为
        已知同义词（`货物名称`）后复用 `extract.items_from_table`，其余字段口径完全沿用
        （预算/最高限价列会被 `_canon` 主动忽略，不会把预算当成交价）。
        """
        from ict_parse import extract as _extract              # noqa: PLC0415

        out: List[dict] = []
        for a in atts:
            for t in (a.get('tables') or []):
                if len(t) < 2:
                    continue
                hi = _extract._pick_header_row(t)
                if hi is None:                                  # 表头不是已知列名 -> 自己找名称列
                    for i, row in enumerate(t[:6]):
                        if any(any(h in str(c) for h in self._NAME_COL_HINTS) for c in row):
                            hi = i
                            break
                if hi is None:
                    continue
                headers = [('' if c is None else str(c)) for c in t[hi]]
                if not any(_extract._canon(headers)):
                    continue
                if 'item_name' not in _extract._canon(headers):
                    fixed = list(headers)
                    for i, h in enumerate(fixed):
                        if any(k in h for k in self._NAME_COL_HINTS):
                            fixed[i] = '货物名称'                # 映射到 item_name
                            break
                    if 'item_name' not in _extract._canon(fixed):
                        continue
                    headers = fixed
                rows = [[('' if c is None else str(c)) for c in r] for r in t[hi + 1:]]
                try:
                    out.extend(_extract.items_from_table(
                        headers, rows, source='ATTACHMENT_DEMAND'))
                except Exception as e:                          # noqa: BLE001
                    continue
        return out

    # ---- 路线②「HTML + 附件包」合并上传时的标并集 ----

    @staticmethod
    def _item_key(name) -> str:
        """判重键：去空白、去尾部括注（`LCD显示大屏（第一部分）` == `LCD显示大屏`）、小写。"""
        s = re.sub(r'\s+', '', str(name or ''))
        s = re.sub(r'[（(][^（）()]{0,16}[）)]$', '', s)
        return s.lower()

    def _merge_attachment_items(self, rec: dict, out_dir: str) -> int:
        """把附件「需求/分包清单」里 HTML 没覆盖到的标的并进 `rec['extraction']['items']`。

        为什么必须做（P1：合并解析的**并集**语义）：同一公告的 HTML 只公示中标信息的
        「主要标的信息」表（往往只覆盖其中几个包、几条代表货物），而附件里的需求/分包
        清单更全。若「HTML+zip」只保留 HTML 那几行，行数会**少于只传 zip**（实测 3 < 4），
        用户会认为「合并了反而更少」。这里按名称判重后取并集：
          * 已覆盖的条目**不新增**，只补空缺字段（不覆盖已有价格/供应商）；
          * 未覆盖的条目按附件侧原样并入（`source` 保持 `ATTACHMENT_DEMAND`，可追溯）；
          * 附件侧没有任何表格时什么都不做（不因合并而变慢/变脏）。

        返回并入的行数（0 表示附件侧没有新增信息）。
        """
        atts = rec.get('extracted_files') or []
        if not atts:
            return 0
        # 与路线④ 同口径：附件 PDF 若因文件名不像「报价类」而没抽表，这里补抽一次表
        root = os.path.join(out_dir or '', 'attachments')
        if not os.path.isdir(root):
            return 0
        self._probe_pdf_tables(atts, root)
        demand = self._demand_items(atts)
        if not demand:
            return 0
        items = (rec.get('extraction') or {}).setdefault('items', [])
        index = {}
        for it in items:
            k = self._item_key(it.get('item_name'))
            if k:
                index.setdefault(k, it)
        # 分包口径归一：合并进来的行**沿用本篇已有的包标识**（公告口径优先）。
        # 为什么必须归一：附件需求清单里的包号（`1/2/3/4`，来自招标文件的分包）与公告
        # 「第3包/第4包」不是同一套口径；混着用会让 `packages.uuid_suffixes` 把一篇公告
        # 拆成多个 UUID，进而让官方「总表」多出「中标供应商为空、金额=公告总额」的行。
        # HTML 行没有包标识时，这里也置空 -> 全部落「未分包」同一个桶（与本篇口径一致）。
        pkgs = {str(it.get('package_no') or '').strip() for it in items}
        pkg = pkgs.pop() if len(pkgs) == 1 else ''
        added = 0
        for d in demand:
            k = self._item_key(d.get('item_name'))
            if not k:
                continue
            cur = index.get(k)
            if cur is not None:
                for f in ('brand', 'spec', 'quantity', 'unit', 'unit_price',
                          'total_price', 'supplier_name', 'category'):
                    if cur.get(f) in (None, '') and d.get(f) not in (None, ''):
                        cur[f] = d[f]
                continue
            row = {'announcement_id': rec.get('announcement_id'), **d,
                   'package_no': pkg}
            items.append(row)
            index[k] = row
            added += 1
        return added

    # ---- 路线 4 的 LLM 抽取 ----

    @staticmethod
    def _attachment_tables(atts: List[dict]) -> List[dict]:
        """附件表格 -> 与 `main_item_tables` 同构的「表组」（LLM 的输入单元）。"""
        out: List[dict] = []
        for a in atts:
            for t in (a.get('tables') or []):
                if not t or len(t) < 2:
                    continue
                out.append({
                    'headers': [('' if c is None else str(c)) for c in t[0]],
                    'rows': [[('' if c is None else str(c)) for c in r] for r in t[1:]],
                })
        return out

    def _attachments_with_llm(self, rec: dict, atts: List[dict],
                              ctx: PipelineContext) -> Optional[dict]:
        """路线 4 无 HTML 时的 LLM 抽取：LLM 成功 -> 结果；回退/异常 -> None。

        回退时返回 None（而不是规则层结果），调用方会用**原始 rec** 再跑一次原规则层，
        这样「LLM 失败回退」的产物与改造前完全一致（含 `MAIN_ITEM_TABLE` 等 source 标记）。
        """
        from ict_parse import llm_extract                        # noqa: PLC0415

        if not llm_extract.available():
            return None
        rec_llm = dict(rec)
        tables = self._attachment_tables(atts)
        if tables:                                   # 让模型看得到附件表格，而不是只有标题
            rec_llm['main_item_tables'] = tables
        try:
            ext = llm_extract.extract_with_llm_safe(rec_llm)
        except Exception as e:                       # noqa: BLE001
            # 非「可预期失败」（代码 bug / 依赖缺失等）：记痕并回退规则层，不让上传失败
            ctx.job.errors.append(f'{rec.get("announcement_id")}: LLM 异常 {type(e).__name__}: {e}')
            return None
        return ext if _engine_is_llm(ext) else None

    def _run_attachments(self, dataset_path: Path, work: Path,
                         ctx: PipelineContext, mode: str = MODE_REPLACE,
                         snapshot: Optional[Dict[str, Tuple[List[str], List[dict]]]] = None,
                         src: str = '',
                         only_groups: Optional[List[dict]] = None,
                         collect_only: bool = False,
                         budget=None,
                         use_llm_override: Optional[bool] = None,
                         ) -> Optional[List[dict]]:
        """路线 4：附件包 —— 逐篇公告解析**本次上传的**附件并入库。

        抽取口径（用户要求「上传的数据都用 LLM 跑」）：
          * 只要 `text_config()` 能取到 LLM key，就**必须**先走 LLM 抽取层
            （`llm_extract.extract_with_llm_safe`）；LLM 不可用或调用失败时
            由它内建回退规则层，`source` 保持 `MAIN_ITEM_TABLE` / `ATTACHMENT:*` 原标记；
          * **只用本次上传的文件**：改造前会用仓库基准 HTML/附件包兜底，导致
            「用户上传自己的报价表、目录名恰好等于某基准公告号」时解析出的是
            **另一家企业**的公告（P1-4）。两条兜底已删除；
          * 本次上传没有 HTML 时按「无 HTML」处理（附件规则层），
            没有附件时如实记进 `job.errors`；
          * 完全没 key 时 = 纯规则层，评审环境离线可复现。

        `collect_only=True`（路线②合并解析用）：只**解析**、把 rec 列表返回给调用方，
        不写交付表、不整库重建、不报进度 —— 让「HTML 有了、但还有没配上的压缩包」这种
        混合上传也能把附件数据并进同一批交付表（原先这些包被静默忽略）。
        """
        from ict_parse.attach import (UnzipBudget, UnzipBudgetExceeded,  # noqa: PLC0415
                                      extract_bundle, extract_text_for)
        from ict_parse import bidders as _bidders                      # noqa: PLC0415
        from ict_parse import extract as _extract                      # noqa: PLC0415

        # 整次导入共用一个解压预算（P1：套娃压缩包跨层/跨篇累计，见 attach.UnzipBudget）
        budget = budget or UnzipBudget()

        # 只要取得到 key 就必须用 LLM（见 _llm_configured 的说明）
        use_llm = (self.use_llm or _llm_configured()
                   if use_llm_override is None else bool(use_llm_override))
        groups = (only_groups if only_groups is not None
                  else self._attachment_groups(dataset_path, work))
        usable = [p for g in groups for p in ([g['zip']] if g['zip'] else []) + g['files']
                  if p and p.suffix.lower() in _ATTACHMENT_EXTS]
        if not usable:
            # 提示文案与单文件上传**完全一致**（用户要求：识别不到时给同样的「支持格式」提示）；
            # 目录上传时 message 里额外带上「扫到几个文件」，便于定位是「空目录」还是「格式不支持」。
            if collect_only:
                ctx.job.errors.append(f'{src or ""}未识别的附件包格式' if src else '未识别的附件包格式')
                return []
            ctx.fail(f'{src}未识别的数据集格式' if src else '未识别的数据集格式',
                     _SUPPORTED_HINT)
            return None

        if not collect_only:
            ctx.report('识别附件包（无 HTML）', 0.2,
                       total=len(groups),
                       message=f'{src}识别路线④：附件包（无 HTML）；'
                               f'{len(groups)} 篇公告的附件包，附件 {len(usable)} 个；'
                               f'未找到公告 HTML 正文，只用本次上传的附件解析；'
                               + ('抽取走 LLM（失败自动回退规则层）' if use_llm else '未配置 LLM key，走规则层'))

        recs: List[dict] = []
        n_att = 0
        n_llm = n_rule = 0
        fallback_aid = dataset_path.stem
        for g in groups:
            n_att += (1 if g['zip'] else 0) + len(g['files'])

        def _parse_one(gi: int, g: dict) -> Tuple[dict, str]:
            """单篇公告附件包 -> (rec, engine 文案)。异常抛给调用方（单篇失败不影响整批）。"""
            aid = g['aid'] or fallback_aid
            att_dir = work / 'attach' / (g['aid'] or f'g{gi}')
            att_dir.mkdir(parents=True, exist_ok=True)

            # 附件来源：**只可能是本次上传提供的文件**（包内子压缩包 / 散件）。
            # 改造前这里还有 `zp = g['zip'] or self._bench_zip(aid)` 与
            # `html = self._bench_html(aid)` 两条兜底：目录名恰好等于某个基准公告号时，
            # 解析用的是**仓库里那篇基准公告**的 HTML/附件包（内容属于另一家企业），
            # 用户上传的文件完全没用上（P1-4）。现在两条兜底都已删除：
            #   * 没有 HTML -> 按「无 HTML」处理（下方的附件规则层，只用用户文件）；
            #   * 没有附件 -> 如实记进 job.errors（「未找到该公告的附件」），
            #     仍按公告级信息入库，不静默假装成功。
            if not g['files'] and g['zip'] is None:
                ctx.job.errors.append(f'{aid}: 未找到该公告的附件（本次上传未提供对应文件）')

            # 没有 HTML：附件文本/表格抽出后交给抽取层
            tim = {}
            z0 = time.perf_counter()
            if g['zip'] is not None:
                atts = [a.__dict__ for a in extract_bundle(str(g['zip']), str(att_dir),
                                                          budget=budget)]
            else:
                atts = self._loose_attachments(g['files'], work)
            for a in atts:
                a['rel_path'] = a.get('rel_path') or a.get('name')

            class _A:
                """把附件 dict 包成「可写属性视图」：**属性写入直接落到 dict 上**。

                必要性（已确认的缺陷）：`extract_text_for()` 用属性赋值写回抽取结果
                （`att.text` / `att.tables` / `att.note`）。原先这里用
                `self.__dict__.update(d)` 做了一次**拷贝**，于是附件解析出来的
                文本与表格全被写进这个临时包装对象、随后丢弃 ——
                路线④（附件包、无 HTML）因此永远抽不到标的/投标人，只剩公告级信息。
                改成写穿之后，解析结果才能被 `extract.build()` / `bidders` 读到。
                """

                def __init__(self, d):
                    object.__setattr__(self, '_d', d)

                def __getattr__(self, k):
                    try:
                        return self._d[k]
                    except KeyError:
                        raise AttributeError(k) from None

                def __setattr__(self, k, v):
                    self._d[k] = v

            root_dir = str(att_dir) if g['zip'] is not None else str(work)
            for a in atts:
                extract_text_for(_A(a), root_dir, use_vlm=self.use_vlm)
            self._probe_pdf_tables(atts, root_dir)
            tim['attach_text_sec'] = round(time.perf_counter() - z0, 3)

            art_title = ' '.join(a.get('name') or '' for a in atts)
            text = '\n'.join(a.get('text') or '' for a in atts)
            rec = {
                'announcement_id': aid,
                'source_html': '',
                'article_title': art_title,
                'pub_date': '',
                'summary': {},
                'categories': [],
                'sections': {},
                'content_text': '',          # 不放附件原文，避免把正文误当成交信息
                'main_item_tables': [],
                'attachments': [],
                'extracted_files': atts,
            }
            engine = '规则层'
            try:
                if use_llm:
                    # LLM 只能读「表」形态的输入：把附件表格临时挂到 main_item_tables 上，
                    # 与 HTML 路线同构，让模型真的看得到附件内容（否则只有标题+空概要）。
                    # 回退时仍用**原始 rec** 走原规则层，保证与改造前完全一致。
                    z_llm = time.perf_counter()
                    ext = self._attachments_with_llm(rec, atts, ctx)
                    tim['llm_sec'] = round(time.perf_counter() - z_llm, 3)
                    if ext is not None:
                        engine = 'LLM'
                    else:
                        ext = _extract.build(rec)
                        ext['engine'] = 'rule'
                        engine = '规则层（回退）'
                else:
                    ext = _extract.build(rec)
                    ext['engine'] = 'rule'
                if not ext.get('items'):
                    ext['items'] = self._demand_items(atts)     # 需求清单兜底
                ann = ext.setdefault('announcement', {})
                if not ann.get('project_name'):
                    ann['project_name'] = self._guess_project(text, atts)
                rec['extraction'] = ext
            except Exception as e:                              # noqa: BLE001
                ctx.job.errors.append(f'{aid}: {type(e).__name__}: {e}')
                engine = '规则层'
                rec['extraction'] = {'engine': 'rule',
                                     'announcement': {'project_name': self._guess_project(text, atts)},
                                     'items': self._demand_items(atts)}
            # ---- 投标人名单（参与人）：从附件表格里认「开标一览表 / 报价明细 / 评审结果 /
            # 资格审查名单 / 未中标（成交）原因表」，每行一个投标人（判据见 ict_parse/bidders.py）。
            # 只在**没有 HTML** 的分支需要显式做一次 —— 有 HTML 时 `process_one` 已经算好了。
            try:
                rec['bidder_roster'] = _bidders.roster_from_rec(rec)
            except Exception:                                   # noqa: BLE001
                rec['bidder_roster'] = []
            _log_doc_timing(aid, tim)
            return rec, '附件 · ' + engine

        if use_llm and _llm_configured() and len(groups) > 1:
            # ---- 并发抽取（只并发「每篇一次 LLM 抽取」；默认 6 路，EDM_LLM_CONCURRENCY 可调）----
            # 与路线 2 同一套策略：结果按输入顺序汇总，ctx.report 只在主线程按完成篇数汇报。
            from integrations import llm_parallel                   # noqa: PLC0415
            done = 0

            def _on_done(idx: int, value, exc) -> None:
                nonlocal done
                done += 1
                gi = idx + 1
                aid = groups[idx]['aid'] or fallback_aid
                if exc is not None:
                    ctx.job.errors.append(f'{aid}: {type(exc).__name__}: {exc}')
                    engine = '异常'
                else:
                    engine = value[1]
                ctx.report(f'解析第 {gi}/{len(groups)} 篇', 0.15 + 0.7 * gi / len(groups),
                           processed=gi, total=len(groups),
                           message=f'{aid}（{engine}）；已完成 {done}/{len(groups)} 篇')

            results = llm_parallel.map_ordered(
                [lambda gi=gi, g=g: _parse_one(gi, g)
                 for gi, g in enumerate(groups, 1)], on_done=_on_done)
            for value, exc in results:               # 输入顺序（而不是完成顺序）
                if isinstance(exc, UnzipBudgetExceeded):
                    raise exc                        # 解压超限 -> job 失败（不吞）
                if exc is not None or value is None:
                    continue
                rec, _engine = value
                recs.append(rec)
                if _engine_is_llm(rec.get('extraction')):
                    n_llm += 1
                else:
                    n_rule += 1
        else:
            # 规则层（未配置 LLM key）保持原样：单线程逐篇，行为与改造前完全一致
            for gi, g in enumerate(groups, 1):
                aid = g['aid'] or fallback_aid
                engine = '规则层'
                try:
                    rec, engine = _parse_one(gi, g)
                    recs.append(rec)
                    if _engine_is_llm(rec.get('extraction')):
                        n_llm += 1
                    else:
                        n_rule += 1
                except UnzipBudgetExceeded:
                    raise                               # 解压超限 -> job 失败（不吞）
                except Exception as e:                       # noqa: BLE001
                    ctx.job.errors.append(f'{aid}: {type(e).__name__}: {e}')
                    engine = '异常'
                ctx.report(f'解析第 {gi}/{len(groups)} 篇', 0.15 + 0.7 * gi / len(groups),
                           processed=gi, total=len(groups), message=f'{aid}（{engine}）')

        if collect_only:                     # 只解析（供路线②合并），不写库
            return recs

        deliv = work / 'deliverables'
        bf = self._backfill(recs, ctx)               # 归位回填（只补空值、逐条留凭证）
        ni, na = self._write_tables(recs, deliv)
        # 附件包里只有图片/扫描件等无从提取内容时，也**不判失败**：
        # 至少把公告级信息入库，用户能看到「识别到几篇、抽到几条」。
        res, note, why = self._load_deliverables(deliv, ctx, mode, snapshot, work=work)
        c = res.get('counts', {})
        n_items = c.get('t1_item', ni)
        n_notice = c.get('t1_notice', na)
        msg = _job_message(
            f'{src}识别路线④：附件包（无 HTML）',
            f'{note}（{_llm_rule_tally(n_llm, n_rule)}；附件 {n_att} 个）',
            # P1-4：如实告诉用户「没有 HTML 正文，结论只来自本次上传的附件」
            '未找到该公告的 HTML 正文，已按本次上传的附件解析（不读仓库基准数据）',
            bf,
            why,
            '' if n_items else '附件里没有可提取的标的明细，仅入库公告级信息')
        ctx.report('写入数据库', 1.0, processed=n_notice, total=len(groups),
                   items=n_items, message=msg)


def install(use_llm: bool = False) -> None:
    """把本管线注入平台（等价于修改 get_pipeline()，但无需改动既有文件）。"""
    from app.pipeline import set_pipeline

    set_pipeline(DeliverablePipeline(use_llm=use_llm))
