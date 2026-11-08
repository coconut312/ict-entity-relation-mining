# -*- coding: utf-8 -*-
"""标的行噪音过滤 —— **唯一实现**（后置过滤，不重跑 LLM）。

问题（用户实测截图，采购方 伽师县教育局 / 莎车县教育局）
------------------------------------------------------
标的检索里出现了一批**不是产品**的行，它们来自《中小企业声明函》《开标一览表》
《报价明细》之类**表格的行标签/字段名/行业分类词**，被抽取层当成标的行：

  * 行业分类词：`农、林、牧、渔`、`工业`、`建筑业`、`批发业`、`零售业`、`住宿业` …
    （来源 = 中小企业声明函的「行业分类」表）
  * 表格行标签/字段名：`总价:`、`投标人名称`、`总下浮率：（统一执行一个下浮率）`、
    `中标成交金额`、`开户名称`、`小写：￥1486000.00元大写：…`
  * 纯标点/省略号：`……`、`...`、`——`、`-`
  * 全空行：名称是长句/表头，**其余 7 个字段全空**
  * 标题行：名称就是公告项目名或采购单位名

共同特征：**除名称外 品目/品牌/规格/数量/单价/总价/供应商 基本全空**（判据 4）。

为什么是「后置过滤」
------------------
  * 抽取层（`out/full/<id>/items_llm.json`）是**单价昂贵的 LLM 产物**，重跑既贵又会
    改变已固化的评测结论；噪音是**表格结构**问题，用确定性规则在出口处一次判掉即可。
  * `out/full/**` **不改**：`scripts/val_eval.py --src pred` 直接读它，改它会让
    评测口径漂移。
  * 因此本模块被四处入口**统一调用**（唯一实现，避免多处漂移）：
      1. `scripts/export_task1_tables.py`      —— 生成 deliverables/items_task1.csv
      2. `ict_parse/official_tables.py::build_tables` —— 官方三表（导出 + 平台下载共用）
      3. `platform/ingest.py`                  —— 入库前（含用户上传数据集那条路径）
      4. `scripts/build_task2_graph.py`        —— 建图库前（关系/画像表不再被噪音污染）

开关
----
`EDM_ITEM_NOISE_FILTER`：`1`（默认，开）/ `0`（关，逐行放行）。关掉后四处入口都退化为
原行为 —— 便于「同口径回滚对比」。

判据（任一命中即判噪音；按优先级取**首个**命中的规则，统计也按首个命中计）
----------------------------------------------------------------------
R1 行业分类词     名称归一后 ∈ `INDUSTRY_WORDS`
R2a 行标签词      名称归一后 ∈ `LABEL_WORDS`
R2b 冒号标签前缀  名称含冒号，且冒号**前**的归一化前缀 ∈ `LABEL_WORDS`/`LABEL_PREFIXES`
                  （`总价:（…）`、`总下浮率：（统一执行一个下浮率）`）
R2c 含冒号短名    名称含冒号、归一后长度 ≤ `COLON_LEN_MAX`(20) 且**无实质字段**
R3 纯标点         去掉标点/符号/空白后为空（`……`/`...`/`——`/`-`/`/`）
R4 全空行         品目/品牌/规格/数量/单位/单价/总价/供应商 **全部为空**
R5 名称过短       归一后汉字数 < 2 且不含数字**且不含英文字母**
R6 与标题同名     归一后 = 公告项目名 或 = 采购单位名，且**无实质字段**

「实质字段」= `PAYLOAD_FIELDS` 里任一字段有非空值。判定还带两条**回声守卫**：
某个字段的值只是名称本身的重复（实测 `item_name='总价：'` 且 `brand='总价：'`、
`item_name='创新产品或创新服务价格合计：'` 且 `spec` 是同一串）不算实质内容。

三条**保护守卫**（都是为了不误杀真实标的，实测数据见 dry-run 报告）
--------------------------------------------------------------
G1 R2c 只在**无实质字段**时生效：`包件2：安全检测分析相关服务`（`服务/信息技术服务`、
   总价 2386200、供应商 长扬科技）是**真标的**，冒号只是包号分隔符 —— 有实质字段就保留。
   （例外：`LABEL_WORDS` 里的字段名做前缀时仍判噪音，如 `总价：1200元`。）
G2 R5 要求**没有英文字母**：`UPS`/`PDA`/`BGP`/`HDMI线`/`PVC管` 是真实产品名，
   汉字数 <2 但含字母 —— 不算「名称过短」。
G3 R6 只在**无实质字段**时生效：单行服务采购的标的行名称**本来就等于项目名**
   （实测 235 行全部带供应商 + 总价，删掉会把该篇标的表清空、并断开「中标人 ↔ 标的行」，
   三表自洽断言直接失败）—— 有实质字段就保留并在报告里单列，供人工复核。

用法（dry-run，只打印不改数据）::

    cd app
    .venv\\Scripts\\python.exe -m ict_parse.noise --src llm       # 交付层 items_llm.json
    .venv\\Scripts\\python.exe -m ict_parse.noise --src rule      # 规则层 items.json（上传路径形态）
    .venv\\Scripts\\python.exe -m ict_parse.noise --csv deliverables\\items_task1.csv \\
        --announcements deliverables\\announcements_task1.csv     # 已有交付表

关闭开关跑一次同口径对照：`$env:EDM_ITEM_NOISE_FILTER='0'` 后再跑同一命令。
"""
from __future__ import annotations

import os
import re
import unicodedata

# ======================================================================
# 开关（默认开；'0'/'false'/'off'/'no' 关）
# ======================================================================
ENV_SWITCH = "EDM_ITEM_NOISE_FILTER"


def enabled() -> bool:
    """`EDM_ITEM_NOISE_FILTER` 开关：默认 **开**；显式 0/false/off/no 时关。"""
    v = os.getenv(ENV_SWITCH)
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() not in ("0", "false", "no", "off", "f", "n")


# ======================================================================
# 词表（判据的唯一出处；改词表只改这里）
# ======================================================================
# R1 行业分类词 —— 国民经济行业分类（GB/T 4754）门类名 + 《中小企业声明函》里的
# 行业分类表写法。归一化会做 NFKC（全角->半角）+ 去空白，因此 `农、林、牧、渔` 与
# `农林牧渔` 两个写法都要列（原文两种都出现过）。
INDUSTRY_WORDS: frozenset[str] = frozenset({
    "农林牧渔", "农林牧渔业", "农、林、牧、渔", "农、林、牧、渔业",
    "工业", "建筑业", "批发业", "零售业", "住宿业", "餐饮业",
    "信息传输业", "软件和信息技术服务业", "信息传输、软件和信息技术服务业",
    "租赁和商务服务业", "居民服务、修理和其他服务业", "其他未列明行业",
    "采矿业", "电力、热力、燃气及水生产和供应业",
    "交通运输、仓储和邮政业", "交通运输业", "仓储业", "邮政业",
    "房地产业", "科学研究和技术服务业",
    "水利、环境和公共设施管理业", "教育", "卫生和社会工作",
    "文化、体育和娱乐业", "公共管理、社会保障和社会组织",
})

# R2a 行标签/字段名 —— 表格表头与「合计/小计」类行标签。这些串**不可能是产品名**，
# 因此无条件判噪音（不看是否有实质字段）。
LABEL_WORDS: frozenset[str] = frozenset({
    "总价", "合计", "小计", "总计", "金额合计", "大写金额",
    "投标人名称", "供应商名称", "投标人", "供应商",
    "序号", "项目名称", "备注", "说明", "单价", "数量", "单位",
    "总下浮率", "下浮率", "投标报价", "报价合计", "合计金额",
    "标的名称", "产品名称", "服务名称",
})

# R2b 冒号前缀白名单的**补充**（实测形态：`小写：¥…大写：…`、`中标成交金额：`、
# `开户名称：`、`分项报价合计（元）：大写：`）。
# 归一化后前缀是「冒号前那一段」，故列的是那一段。
#
# ⚠ 刻意**不**收 `采购包1`/`包1`/`A包`/`分标3`/`模块1` 这类**包号**：它们是
# 「包N：真实服务名」的分隔符，不是字段名 —— 实测 `采购包1：广州-地市长途16M OTN…`
# （品目=服务、单价 532012、供应商=中国移动）是**真标的**，收进词表就会误杀。
LABEL_PREFIXES: frozenset[str] = frozenset({
    "小写", "大写", "中标成交金额", "中标金额", "成交金额", "开户名称", "开户银行",
    "分项报价合计(元)", "创新产品或创新服务价格合计", "投标总价", "评标价", "报价",
})
# 前缀前缀：前缀里含这些词也算标签（覆盖 `总价：￥123`、`分项报价合计（元）：…` 之类变体）
_LABEL_PREFIX_HINTS = ("合计", "总价", "下浮率", "小写", "大写", "开户")

# 实质字段（= 官方标的表里除「产品服务名称/品目以外」的可量化列 + 供货方）
#   品目(category) 也算实质字段：有品目 = 至少是个 "类别 + 名称" 的真需求行。
PAYLOAD_FIELDS = ("category", "brand", "spec", "quantity", "unit",
                  "unit_price", "total_price", "supplier_name")

# R2c 的冒号名长度上限（归一后字符数；官方原文口径是「≤20」）
COLON_LEN_MAX = 20

# ======================================================================
# 规则 ID（统计/报告按它分组；顺序 = 优先级）
# ======================================================================
R_INDUSTRY = "R1_行业分类词"
R_LABEL = "R2a_行标签词"
R_LABEL_COLON = "R2b_冒号标签前缀"
R_COLON_SHORT = "R2c_含冒号短名(无实质字段)"
R_PUNCT = "R3_纯标点"
R_BLANK = "R4_全空行"
R_SHORT = "R5_名称过短"
R_TITLE = "R6_与项目名/采购方同名"

RULES: tuple[str, ...] = (R_INDUSTRY, R_LABEL, R_LABEL_COLON, R_COLON_SHORT,
                          R_PUNCT, R_BLANK, R_SHORT, R_TITLE)

# 守卫（规则本会命中、但按保护判据放行）—— 只为报告留痕，不影响结果
G_COLON = "G1_冒号短名但有实质字段"
G_SHORT = "G2_名称过短但含字母"
G_TITLE = "G3_与标题同名但有实质字段"
GUARDS: tuple[str, ...] = (G_COLON, G_SHORT, G_TITLE)

# ======================================================================
# 归一化 / 判据小工具
# ======================================================================
_WS = re.compile(r"\s+")
_DIGIT = re.compile(r"\d")
_LATIN = re.compile(r"[A-Za-z]")


def norm_name(v) -> str:
    """名称归一键：NFKC（全角->半角、`：`->`:`）+ 去全部空白。

    **只做无损归一**：不删汉字/数字/字母/标点，也不截断 —— 判据要能看见原文形态。
    """
    s = unicodedata.normalize("NFKC", str(v if v is not None else ""))
    return _WS.sub("", s)


def han_count(s: str) -> int:
    return sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")


def punct_only(s: str) -> bool:
    """去掉标点/符号/空白后是否为空（`……`/`...`/`——`/`-`/`/`）。"""
    for ch in s:
        if not unicodedata.category(ch).startswith(("P", "S")):
            return False
    return True


def _split_colon(n: str) -> tuple[str, bool]:
    """归一后按**第一个**冒号（NFKC 后只剩半角 `:`）切出前缀。"""
    i = n.find(":")
    return (n, False) if i < 0 else (n[:i], True)


def _is_echo(v, name_norm: str) -> bool:
    """字段值是否只是**名称本身的回声**（`item_name='总价：'` + `brand='总价：'`）。

    只认「归一后完全相等」—— 不做包含判断，避免把 `品牌=华为` 误判成
    `名称=华为交换机` 的回声（那会凭空把有货的行算成空行）。
    """
    nv = norm_name(v)
    return bool(nv) and nv == name_norm


def payload_empty(it: dict, name_norm: str | None = None) -> bool:
    """品目/品牌/规格/数量/单位/单价/总价/供应商是否**全部为空**（回声值不算）。

    空串、None、纯空白都算空；`0`、`0.0` 算**有值**（数量为 0 仍是填过的）。
    """
    n = norm_name(it.get("item_name")) if name_norm is None else name_norm
    for f in PAYLOAD_FIELDS:
        v = it.get(f)
        if v is None or str(v).strip() == "":
            continue
        if _is_echo(v, n):
            continue
        return False
    return True


def classify(it: dict, *, project_name: str = "", purchaser: str = "") -> tuple[str | None, str | None]:
    """判一条标的行 -> `(命中的规则 ID 或 None, 被守卫放行的守卫 ID 或 None)`。

    规则按 `RULES` 顺序判定，**首个命中**即返回（统计按首个命中计，避免一行计多次）。
    """
    n = norm_name(it.get("item_name"))
    proj = norm_name(project_name)
    pur = norm_name(purchaser)
    guard: str | None = None          # 被守卫放行的规则（只用于报告）

    # ---- R1 行业分类词 ----
    if n in INDUSTRY_WORDS:
        return R_INDUSTRY, None

    # ---- R2a 行标签/字段名（无条件）----
    if n in LABEL_WORDS:
        return R_LABEL, None

    # ---- R2b/R2c 冒号形态 ----
    if n:
        prefix, has_colon = _split_colon(n)
        if has_colon:
            if prefix in LABEL_WORDS or prefix in LABEL_PREFIXES \
                    or any(h in prefix for h in _LABEL_PREFIX_HINTS):
                return R_LABEL_COLON, None
            if len(n) <= COLON_LEN_MAX:
                if payload_empty(it, n):
                    return R_COLON_SHORT, None
                guard = G_COLON        # G1：有实质字段 -> 可能是「包N：服务名」的真标的

    # ---- R3 纯标点/省略号 ----
    if n and punct_only(n):
        return R_PUNCT, None

    # ---- R4 全空行 ----
    if payload_empty(it, n):
        return R_BLANK, None

    # ---- R5 名称过短（汉字<2 且无数字；G2：含字母不算）----
    if n and han_count(n) < 2 and not _DIGIT.search(n):
        if not _LATIN.search(n):
            return R_SHORT, None
        guard = guard or G_SHORT

    # ---- R6 与项目名/采购方同名（G3：有实质字段就保留）----
    if n and (n == proj or (pur and n == pur)):
        return None, G_TITLE          # 走到这里必然有实质字段（R4 已放行）

    return None, guard


def is_noise(it: dict, *, project_name: str = "", purchaser: str = "") -> bool:
    """便利判据：该行是否噪音（受 `EDM_ITEM_NOISE_FILTER` 开关约束）。"""
    if not enabled():
        return False
    return classify(it, project_name=project_name, purchaser=purchaser)[0] is not None


# ======================================================================
# 报告
# ======================================================================
class NoiseReport:
    """逐条命中统计 + 样例（每规则最多留 `max_samples` 条，供人工确认没误杀）。"""

    def __init__(self, max_samples: int = 5):
        self.max_samples = max_samples
        self.total_in = 0
        self.dropped = 0
        self.hits: dict[str, int] = {r: 0 for r in RULES}
        self.samples: dict[str, list[dict]] = {r: [] for r in RULES}
        self.guards: dict[str, int] = {g: 0 for g in GUARDS}
        self.guard_samples: dict[str, list[dict]] = {g: [] for g in GUARDS}
        # 规则层（rule）与交付层（llm）分别累计时用：按来源分开记账
        self.by_source: dict[str, dict] = {}

    # -- 记账 --
    def add(self, rule: str, it: dict, *, aid: str = "") -> None:
        self.dropped += 1
        self.hits[rule] = self.hits.get(rule, 0) + 1
        bucket = self.samples.setdefault(rule, [])
        if len(bucket) < self.max_samples:
            bucket.append(_sample_row(it, aid))

    def add_guard(self, guard: str, it: dict, *, aid: str = "") -> None:
        self.guards[guard] = self.guards.get(guard, 0) + 1
        bucket = self.guard_samples.setdefault(guard, [])
        if len(bucket) < self.max_samples:
            bucket.append(_sample_row(it, aid))

    def merge(self, other: "NoiseReport") -> None:
        self.total_in += other.total_in
        self.dropped += other.dropped
        for k, v in other.hits.items():
            self.hits[k] = self.hits.get(k, 0) + v
        for k, v in other.samples.items():
            self.samples.setdefault(k, []).extend(v[: max(0, self.max_samples - len(self.samples.get(k, [])))])
        for k, v in other.guards.items():
            self.guards[k] = self.guards.get(k, 0) + v
        for k, v in other.guard_samples.items():
            self.guard_samples.setdefault(k, []).extend(
                v[: max(0, self.max_samples - len(self.guard_samples.get(k, [])))])

    # -- 派生量 --
    @property
    def kept(self) -> int:
        return self.total_in - self.dropped

    def as_dict(self) -> dict:
        return {
            "total_in": self.total_in,
            "dropped": self.dropped,
            "kept": self.kept,
            "drop_rate": round(self.dropped / self.total_in, 6) if self.total_in else 0.0,
            "hits": dict(self.hits),
            "samples": {k: v for k, v in self.samples.items() if v},
            "guards": {k: v for k, v in self.guards.items() if v},
            "guard_samples": {k: v for k, v in self.guard_samples.items() if v},
        }


def _sample_row(it: dict, aid: str = "") -> dict:
    """样例只留「判据看得见」的字段，便于人工快速判定有没有误杀。"""
    return {
        "aid": aid or it.get("announcement_id") or "",
        "item_name": it.get("item_name"),
        "品目": it.get("category"),
        "品牌": it.get("brand"),
        "规格": (str(it.get("spec"))[:20] if it.get("spec") else ""),
        "数量": it.get("quantity"),
        "单价": it.get("unit_price"),
        "总价": it.get("total_price"),
        "供应商": it.get("supplier_name"),
    }


def format_report(rep: NoiseReport, *, title: str = "标的行噪音过滤 dry-run",
                  show_samples: int = 5) -> str:
    """人可读报告：每规则命中数 + 样例 + 守卫放行数 + 总计。"""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(title)
    lines.append("=" * 78)
    lines.append("开关 %s=%s | 输入 %d 行" % (ENV_SWITCH, "1" if enabled() else "0", rep.total_in))
    if rep.total_in:
        pct = 100.0 * rep.dropped / rep.total_in
        lines.append("将丢弃 %d 行 / 保留 %d 行（丢弃占 %.2f%%）"
                     % (rep.dropped, rep.kept, pct))
    else:
        lines.append("将丢弃 0 行 / 保留 0 行")
    lines.append("-" * 78)
    lines.append("%-32s %8s  %s" % ("规则", "命中", "样例"))
    for rule in RULES:
        n = rep.hits.get(rule, 0)
        lines.append("%-32s %8d" % (rule, n))
        if show_samples:
            for s in rep.samples.get(rule, [])[:show_samples]:
                lines.append("      · %s | %s" % (s["aid"], _sample_line(s)))
    lines.append("-" * 78)
    lines.append("保护守卫（本会命中、按保护判据放行）：")
    any_guard = False
    for g in GUARDS:
        n = rep.guards.get(g, 0)
        if not n:
            continue
        any_guard = True
        lines.append("  %-30s %8d" % (g, n))
        if show_samples:
            for s in rep.guard_samples.get(g, [])[:show_samples]:
                lines.append("      · %s | %s" % (s["aid"], _sample_line(s)))
    if not any_guard:
        lines.append("  （无）")
    lines.append("-" * 78)
    lines.append("合计：命中 %d 行（%s）" % (
        rep.dropped,
        "、".join("%s=%d" % (r.split("_")[0], n) for r, n in rep.hits.items() if n) or "无"))
    return "\n".join(lines)


def _sample_line(s: dict) -> str:
    return ("名称=%r 品目=%r 品牌=%r 规格=%r 数量=%r 单价=%r 总价=%r 供应商=%r"
            % (s["item_name"], s["品目"], s["品牌"], s["规格"], s["数量"],
               s["单价"], s["总价"], s["供应商"]))


# ======================================================================
# 对外主接口：一行/一批
# ======================================================================
def filter_items(items, *, project_name: str = "", purchaser: str = "",
                 report: NoiseReport | None = None, aid: str = "",
                 force: bool | None = None) -> list:
    """过滤一批标的行，返回**保留**的行（顺序不变）。

    * `project_name` / `purchaser`：本批所属公告的项目名与采购单位（R6 判据用）。
    * `report`：传入 `NoiseReport()` 即累计逐条命中统计（传 None 则不计，零开销）。
    * `force`：`None` = 跟随 `EDM_ITEM_NOISE_FILTER`；`True/False` = 显式开/关（测试用）。

    开关关闭时**逐行放行**（原样返回，连统计都不变），便于同口径回滚对比。
    """
    if force is None:
        force = enabled()
    if not force:
        if report is not None:
            report.total_in += len(items)
        return list(items)
    keep: list = []
    for it in items:
        rule, guard = classify(it, project_name=project_name, purchaser=purchaser)
        if report is not None:
            report.total_in += 1
            if rule:
                report.add(rule, it, aid=aid)
            elif guard:
                report.add_guard(guard, it, aid=aid)
        if rule is None:
            keep.append(it)
    return keep


# ======================================================================
# CLI：dry-run（只读，不写任何数据；可选 --json/--md 落报告）
# ======================================================================
def _iter_ext(root: str, fname: str):
    """按 all_ids.txt 顺序读 out/full/<id>/<fname>（缺文件跳过）。"""
    import json

    ids_path = "all_ids.txt"
    if not os.path.exists(ids_path):
        raise SystemExit("缺少 all_ids.txt（请在 app/ 目录下运行）")
    ids = [ln.strip() for ln in open(ids_path, encoding="utf-8") if ln.strip()]
    for aid in ids:
        p = os.path.join(root, aid, fname)
        if not os.path.exists(p):
            continue
        ext = json.load(open(p, encoding="utf-8"))
        yield aid, (ext.get("announcement") or {}), (ext.get("items") or [])


def _run_ext(src: str, root: str, rep: NoiseReport) -> None:
    fname = "items_llm.json" if src == "llm" else "items.json"
    for aid, ann, items in _iter_ext(root, fname):
        if not items:
            continue
        filter_items(items, project_name=ann.get("project_name") or "",
                     purchaser=ann.get("purchaser") or "", report=rep, aid=aid)


def _run_csv(items_csv: str, ann_csv: str | None, rep: NoiseReport) -> None:
    import csv

    ann = {}
    if ann_csv and os.path.exists(ann_csv):
        with open(ann_csv, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                ann[r.get("announcement_id") or ""] = (r.get("project_name") or "",
                                                       r.get("purchaser") or "")
    with open(items_csv, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            aid = r.get("announcement_id") or ""
            proj, pur = ann.get(aid, ("", ""))
            # CSV 行本身就是 dict，键与 items JSON 同名（category/brand/spec/…）
            filter_items([r], project_name=proj, purchaser=pur, report=rep, aid=aid)


def main(argv=None) -> int:
    import argparse
    import json
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(
        description="标的行噪音过滤 dry-run（只读；命中统计 + 样例）")
    ap.add_argument("--src", choices=["llm", "rule", "csv"], default="llm",
                    help="llm=out/full/*/items_llm.json（交付层，默认）；"
                         "rule=items.json（规则层）；csv=--csv 指定的交付表")
    ap.add_argument("--root", default="out/full")
    ap.add_argument("--csv", default="deliverables/items_task1.csv")
    ap.add_argument("--announcements", default="deliverables/announcements_task1.csv")
    ap.add_argument("--json", default="", help="把统计落成 JSON（可选）")
    ap.add_argument("--md", default="", help="把报告落成 Markdown（可选）")
    ap.add_argument("--samples", type=int, default=5)
    a = ap.parse_args(argv)

    rep = NoiseReport(max_samples=max(a.samples, 5))
    title = "标的行噪音过滤 dry-run"
    if a.src == "csv":
        _run_csv(a.csv, a.announcements, rep)
        title += "（%s）" % a.csv
    else:
        _run_ext(a.src, a.root, rep)
        title += "（%s 层：out/full/*/%s）" % (
            "交付" if a.src == "llm" else "规则", "items_llm.json" if a.src == "llm" else "items.json")
    text = format_report(rep, title=title, show_samples=a.samples)
    print(text)

    if a.json:
        os.makedirs(os.path.dirname(a.json) or ".", exist_ok=True)
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep.as_dict(), f, ensure_ascii=False, indent=2)
        print("统计 -> %s" % a.json)
    if a.md:
        os.makedirs(os.path.dirname(a.md) or ".", exist_ok=True)
        with open(a.md, "w", encoding="utf-8") as f:
            f.write("```\n" + text + "\n```\n")
        print("报告 -> %s" % a.md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
