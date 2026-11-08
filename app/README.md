# 任务一 · 招采标讯标的物智能抽取引擎

「中国电子杯」第三届高校 ICT 产教融合创新大赛 · **赛题五**《面向行业数据的智能实体挖掘与关系建模》
—— **任务一「实体识别与自动化提取」** 的工程实现与使用说明。

> 本文件对应赛题「六、作品提交要求 · 6. 源码及配套资料 ——（3）必要的说明文档：源码的功能说明、执行步骤」。

---

## 1. 功能概述

把中国政府采购网风格的**公告 HTML** 与其**附件压缩包（可套娃）**，转成结构化**标的物明细表**：

- **7 个关键字段**：产品服务名称、品目、品牌、规格型号、单价、数量、总价（另附单位、包号、中标供应商、是否中标、标的类型等支撑字段）；
- **公告级字段**：项目名称/编号、采购单位、行政区域、公告时间、公告类型、项目金额、中标供应商、参与投标人（支撑任务二关系建模）；
- **两类来源都覆盖**：公告原文（概要表 / 一~九分节 / 主要标的信息表）**与** 附件（pdf/docx/doc/xlsx/图片、含多层 zip 套娃）；
- **两条可独立评测的抽取路线**：LLM 主抽取（`llm_extract`）与纯规则（`extract`），交付采用「LLM 主抽取 + 规则做解析对齐/数值归一/一致性校验/跨文件补价/兜底」的混合架构。

**当前实测（全量 1038 篇）**：篇均提取率 **6.101/10**（Σ档位分 6333 / 1038）；提取准确性 **P 1.00 / R 0.99 / F1 0.99 / A 0.99**（自建 gold 54 篇，语义等价口径；TP 1609 · FN 13 · FP 5 · TN 81，`scripts/val_eval.py`）；覆盖率 原文类 1018 篇 / 附件类 674 篇；明细 **6,244** 条；`engine=llm 1038/1038`（零回退）。

---

## 2. 运行环境

| 项目 | 版本 / 说明 |
|---|---|
| 操作系统 | Windows 11（依赖 LibreOffice 做老式 Office 转换） |
| Python | 3.14（用 [`uv`](https://docs.astral.sh/uv/) 管理；也可直接用 `app/.venv`） |
| 主要依赖 | beautifulsoup4、lxml、pdfplumber、python-docx、openpyxl、pypdfium2、openai |
| 大模型 | DeepSeek `deepseek-flash`（默认，OpenAI 兼容）；可设 `LLM_BASE_URL`/`LLM_MODEL` 切换 Qwen `qwen-plus` |
| LibreOffice | 用于 `.doc/.wps/.rtf/伪docx` → PDF 转换（`render.py`） |

### 2.1 API Key 配置

代码按以下顺序取 key（见 `ict_parse/config.py`）：

1. Reasonix 客户端注入的 `%APPDATA%\reasonix\.env` 文件（**改完即时生效**）
2. 进程环境变量：`LLM_API_KEY` / `DEEPSEEK_API_KEY`（文本）、`QWEN_API_KEY` / `DASHSCOPE_API_KEY`（Qwen）

可选环境变量：`LLM_BASE_URL`、`LLM_MODEL`、`LLM_TIMEOUT`、`LLM_RETRIES`、`LLM_MAX_TOKENS`、`LLM_MAX_CTX`、`LLM_APP_RETRIES`、`QWEN_VL_BASE_URL`、`QWEN_VL_MODEL`。

> 未配置 key 时：LLM 层不可用，但**规则层可独立跑通**（`--vlm`/`--llm` 会自动降级），便于离线复现。

---

## 3. 目录结构

```
app/
├── ict_parse/                  核心管线
│   ├── models.py               IR 数据模型（AnnouncementIR / TableGroup / Attachment）
│   ├── html_ir.py              公告 HTML → IR（概要表 + 一~九分节 + 主要标的信息表，3 种模板回退）
│   ├── attach.py               附件 zip 递归解压（套娃）+ 类型分发 + 文本/表格抽取
│   ├── pdf_text.py             PDF 文本层与表格抽取（pdfplumber）
│   ├── render.py               doc→pdf（soffice）、pdf→图片（pypdfium2）
│   ├── vision.py               视觉模型客户端（扫描件/图片 → 管道表格文本）
│   ├── extract.py              规则抽取层（表头映射/正文成交块/跨文件补价/一致性校验）
│   ├── llm_extract.py          LLM 抽取层（分块 + JSON 强约束 + 逐级细切 + 回退）
│   ├── category.py             政府采购品目分类目录最长匹配
│   ├── pipeline.py             编排：一篇公告 → ir.json + items.json
│   ├── report.py               核验报告（HTML/CSV）
│   └── __main__.py             CLI 入口
├── scripts/                    工具链（见 §4）
├── data/gov_category.csv       品目分类目录（4,178 条）
├── valset/                     自建验证集：valset_ids.txt(100) / gold(54) / pred(100) / review.csv
├── deliverables/               **交付物**：交付表 CSV + 《过程性文档》《系统设计文档》(docx/pdf)
└── out/full/                   运行产物：每篇一个目录（ir.json / items.json / items_llm.json）
```

---

## 4. 执行步骤（从零复现）

> 所有命令在 `app` 目录下执行；先设 `$env:PYTHONIOENCODING="utf-8"` 避免中文乱码。

### 步骤 1：解析 + 规则抽取（全量，可断点续跑）

```powershell
cd app
uv run python -m ict_parse --ids-file all_ids.txt --out out/full            # 解析 + 规则抽取
uv run python -m ict_parse --ids-file all_ids.txt --out out/full --vlm      # 额外启用视觉（扫描件）
uv run python scripts/run_all.py                                            # 复用已有 items.json 出汇总统计（快）
```

产出：`out/full/<id>/ir.json`（结构化 IR）、`out/full/<id>/items.json`（规则层抽取结果）、`out/full/summary.json`。

### 步骤 2：LLM 抽取（交付路径）

```powershell
$env:OUT="out/full"; $env:IDS="all_ids.txt"; $env:CONC="4"   # CONC=并发数；已有结果会自动跳过
uv run python scripts/run_llm.py                             # 断点续跑：不会重复烧 token
# 强制重跑：$env:FORCE="1"
# 切模型：  $env:LLM_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"; $env:LLM_MODEL="qwen-plus"
```

产出：`out/full/<id>/items_llm.json`（`{announcement, items, engine}`）。

### 步骤 3：导出交付表

```powershell
uv run python scripts/export_task1_tables.py                 # -> deliverables/items_task1.csv
uv run python scripts/export_task1_tables.py --src rule      # 规则层对照 -> items_task1_rule.csv
```

导出时会先过**标的行噪音过滤**（唯一实现 `ict_parse/noise.py`，判据/词表/开关都在那个
模块的 docstring 里）：把《中小企业声明函》的行业分类词（`工业`/`建筑业`/`农林牧渔`）、
表格行标签/字段名（`总价:`/`投标人名称`/`总下浮率：（…）`）、纯标点（`……`）、全空行、
与项目名同名的标题行从出口判掉。**后置过滤，不重跑 LLM**，同一份实现被四处入口调用：
`export_task1_tables.py` / `official_tables.build_tables`（官方三表导出与平台下载）/
`platform/ingest.py`（入库，含用户上传路径）/ `build_task2_graph.py`（建图库前）。

```powershell
uv run python -m ict_parse.noise --src llm     # dry-run：每条规则命中数 + 5 条样例（只读不改数据）
uv run python -m ict_parse.noise --src rule    # 规则层同口径
$env:EDM_ITEM_NOISE_FILTER="0"                 # 关掉开关（同口径回滚对比），默认 1=开
```

### 步骤 4：精度自检与数据质量

```powershell
uv run python scripts/selfcheck.py                 # 覆盖率 + 篇级 7 字段分布（规则层）
uv run python scripts/val_export.py                # 刷新 valset/pred + review.csv
uv run python scripts/val_eval.py --src pred       # 准确性（LLM，语义等价口径）
uv run python scripts/val_eval.py --src rule       # 准确性（规则层对照）
uv run python scripts/val_eval.py --raw            # 逐字符口径（格式敏感，仅诊断）
uv run python scripts/audit_gold.py                # gold 金额回原文核验
uv run python scripts/profile_data_quality.py      # 数据质量画像（脏数据/缺失/附件难点）
uv run python scripts/audit_fields.py              # 「原文有证据但抽取为空」的靶子审计
```

### 步骤 5：产出交付文档（Word + PDF）

```powershell
$PY="python"
& $PY scripts/md_to_docx.py ..\docs\process-document.md       deliverables\过程性文档.docx
& $PY scripts/md_to_docx.py ..\docs\system-design-document.md deliverables\系统设计文档.docx
# PDF：用 LibreOffice Kit 转换（见 docs 中的说明）
```

---

## 5. 脚本清单（`scripts/`）

| 脚本 | 用途 |
|---|---|
| `run_all.py` | 全量解析+规则抽取，输出 `summary.json`（可断点续跑） |
| `run_extract.py` | 仅基于已有 `ir.json` 重跑规则抽取（**不重解压附件，很快**） |
| `run_llm.py` | LLM 抽取（并发 + 断点续跑 + 跑后统一统计） |
| `reparse_ids.py` / `repair_long_paths.py` | 定向重解析 / 修复超长路径的篇并重解析 |
| `export_task1_tables.py` | 导出交付表 CSV（LLM 版 / 规则版） |
| `val_export.py` / `val_eval.py` | 生成验证集快照 / 计算 P-R-F1 与赛题综合公式 |
| `selfcheck.py` | 覆盖率与篇级 7 字段分布自检 |
| `audit_gold.py` | gold 金额回原文核验（含合法逻辑推定豁免） |
| `audit_fields.py` | 审计「原文/附件有证据但抽取为空」的篇（定位缺陷靶子） |
| `profile_data_quality.py` | 数据质量画像（附件构成/解析难点/脏数据/缺失分布） |
| `normalize_results.py` | 把已有结果里的数值原地规范化（整数值写 int） |
| `md_to_docx.py` | Markdown → DOCX（交付文档生成器，含 CJK 字体与固定表宽） |
| `build_catalog.py` | 从《政府采购品目分类目录》PDF 构建 `data/gov_category.csv` |
| `model_evidence.py` | 大模型调用佐证（模型名/响应 id/耗时） |

---

## 5.1 任务二 · 关系分析与五大场景

`powershell
uv run python scripts/refresh_announcements.py --rebuild   # 多源合并投标参与方 + USCC（不调 API，幂等）
uv run python scripts/build_task2_graph.py                 # 建关系图库 out/task2/graph.db + 导出关系表
uv run python scripts/task2_scenarios.py --demo            # 跑 5 个业务场景并出 deliverables/task2_scenario_report.md
uv run python scripts/task2_scenarios.py -s s3 -a 中国移动通信集团广东有限公司   # 单场景交互查询
`

产出：out/task2/graph.db（entity/notice/bid/item + v_cooperation / v_cobidding / v_product_supply）、
deliverables/task2_*.csv（实体表、双侧画像、合作、共同竞标、产品供应、参与投标事实）、
deliverables/task2_scenario_report.md。详见 ../docs/task2-design.md。

## 5.2 任务三 · 可视化分析检索平台（主平台）

```powershell
uv run python platform/ingest.py                  # 1) 数据入库（_meta.total_rows=37,001，约 0.2 秒）
uv run python task3/backend/run.py --port 8000    # 2) 启动平台（Vue 3 + FastAPI）
# 浏览器 http://127.0.0.1:8000/   评审账号 reviewer / ict2026
# 接口文档 http://127.0.0.1:8000/api/docs
```

- 平台代码：`task3/backend/`（FastAPI）+ `task3/frontend/`（Vue 3 + Element Plus + ECharts，已预构建于 `task3/backend/static/`，运行无需 npm）
- **数据接入（只增不改）**：`task3/backend/integrations/`
  - `repos.py` —— 实现契约接入点 1/2：`SQLiteItemRepository`、`SQLiteGraphRepository`
  - `pipeline_impl.py` —— 实现契约接入点 3：上传数据集自动解析入库
  - `run.py` —— 启动器（设置 `EDM_*` 环境变量并注入管线）
- 冒烟测试：`uv run python task3/backend/smoke_test.py` → 通过 58 项，失败 0 项
- 详见 `../docs/task3-integration.md`（整合说明、评审操作、实测证据）

> 轻量备用界面：`platform/app.py`（端口 8899，零 npm 依赖），见 `../docs/task3-platform.md`。

## 5.3 任务二 · Neo4j 图库（可选后端，采用 Neo4j）

```powershell
uv run python scripts/build_neo4j_graph.py --export     # 只导出可移植 Cypher + CSV（无需 Neo4j）
# 有 Neo4j 时：
$env:NEO4J_URI="bolt://127.0.0.1:7687"; $env:NEO4J_USER="neo4j"; $env:NEO4J_PASSWORD="<pwd>"
uv run python scripts/build_neo4j_graph.py --load --export
$env:EDM_GRAPH_REPOSITORY="integrations.neo4j_repo:Neo4jGraphRepository"   # 平台切到 Neo4j
uv run python task3/backend/run.py --port 8000
```

- 图模型：`(:Purchaser)-[:COOPERATES]->(:Supplier)`、`(:Supplier)-[:CO_BIDS]->(:Supplier)`、
  `(:Supplier)-[:PARTICIPATES]->(:Notice)-[:ISSUED_BY]->(:Purchaser)`、`(:Supplier)-[:SUPPLIES]->(:Brand)`
- 两套实现同一契约：`integrations/repos.py::SQLiteGraphRepository`（默认，已全量验证）与
  `integrations/neo4j_repo.py::Neo4jGraphRepository`（Cypher 实现五场景）
- 导出规模：9 个 CSV / **19,788 行**（节点 5,645 + 关系 14,143）+ `deliverables/neo4j/task2_graph.cypher`
- 详见 `../docs/task2-neo4j.md`（含**验证状态与实测值**：已在真实 Neo4j 5.25.1 上装载并跑通五场景双跑对照，
  **可比项 11/11 一致**，装载耗时 4.17 s）

## 6. 交付表字段说明

**`deliverables/items_task1.csv`（明细，6,244 行）**

| 列 | 说明 |
|---|---|
| `announcement_id` | 公告 ID（与 HTML 同名） |
| `package_no` / `supplier_name` / `is_winner` | 包号 / 中标供应商 / 是否中标 |
| `item_name` / `category` / `category_code` / `item_type` | 产品服务名称 / 品目 / 品目编码 / 标的类型 |
| `brand` / `spec` | 品牌 / 规格型号 |
| `quantity` / `unit` / `unit_price` / `total_price` | 数量 / 单位 / 单价(元) / 总价(元) |
| `pricing_mode` / `discount_rate` / `price_unit` | 报价方式 / 费率值 / 计价单位（如 元/㎡） |
| `derived` / `needs_review` | 是否逻辑推定 / 是否需人工复核 |
| `source` / `engine` | 来源（LLM/附件/正文块） / 抽取引擎 |

**`deliverables/announcements_task1.csv`（公告级，1,038 行）**：另含 `project_name/project_code/purchaser/region/publish_time/project_amount/n_winners/n_bidders/n_items`。

> **清洗口径（三层说明，避免误读为"同一实现"）**：清洗规则的**唯一实现**是 `ict_parse/clean.py`
> （`clean_brand` / `clean_purchaser` / `clean_item_name` / `clean_category` / `normalize_item_fields` / `derive_fields`）。
> **建图层**（`scripts/build_task2_graph.py:33`）与**导出层**（`scripts/export_task1_tables.py:20`）**共用同一函数**（直接 `import clean.py`）；
> **评测层** `scripts/val_eval.py::norm` 是**同口径的独立实现**（其 docstring 逐条声明同口径，含 brand 的「详见/招标文件」判废规则），
> 因为评测侧还需数值比较与品目父子级等价截断 —— 即「**两层同一函数 + 评测层同口径**」，**不是三层同一函数**。
> 规则层对照表 `items_task1_rule.csv` **有意保留原始脏值**（它是 rule-vs-LLM 基线，清洗会改变已引用的基线指标）。

---

## 7. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `python -m ict_parse --llm` 没走 LLM | 检查 key 是否配好（`config.text_config()` 会读 `%APPDATA%\reasonix\.env`；`llm_extract.available()` 已修复为走 config） |
| PowerShell 遍历 `out/full` 报「找不到路径的一部分」 | 历史遗留超长路径（Windows MAX_PATH）。已由 `attach._fit_path` 修复；存量用 `scripts/repair_long_paths.py` |
| LLM 跑到一半失败 | 直接重跑同一条命令即可，已完成的篇会跳过（断点续跑）；失败原因写在 `items_llm.json` 的 `engine` 字段 |
| 担心重复消耗额度 | 用 `FORCE=0`（默认）配合 `run_llm.py`；`engine` 存在即视为已完成（**空结果篇也不会被重跑**） |
| 报 `402 Insufficient Balance` | 模型账号余额不足；可临时切 Qwen：设 `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL` |

---

## 8. 已知限制

1. **嵌套压缩包已支持 rar/7z**（bsdtar 递归解压）：全量 36 个归档（`.rar` 34 + `.7z` 2，涉 23 篇）中仅剩 **2 个包**标记 `not auto-extracted`（加密或损坏，已逐个登记不静默丢弃）；影响与复现见 `../docs/data-quality-report.md` §4.1。扫描件视觉识别仅按需启用（收益极低、耗时高）。
3. 目前为**离线脚本 + CSV** 形态，尚无服务/界面/数据库（赛题「平台内运行」要求待补）。
4. 大模型参数规模以厂商公开规格为准（通过官方 API 调用，未本地持有权重）。

---

## 9. 合规声明

核心信息抽取全程采用 **Qwen / DeepSeek 系列开源国产大模型**（Transformer 架构），未使用其它闭源模型；规则仅用于解析、表头对齐、数值归一与一致性校验，不承担核心字段抽取。详见 `../docs/task1-model-statement.md`。
