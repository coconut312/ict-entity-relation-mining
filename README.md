# 面向行业数据的智能实体挖掘与关系建模

> 「中国电子杯」第三届高校 ICT 产教融合创新大赛 · **赛题五**
> 命题单位：中国软件与技术服务股份有限公司

把中国政府采购网风格的**招标公告**（HTML + 可嵌套的附件压缩包）自动转成三样东西：

1. **结构化标的物明细** —— 7 个关键字段：产品服务名称 / 品目 / 品牌 / 规格型号 / 单价 / 数量 / 总价
2. **采购方—供应商关系图谱** —— 合作、共同投标、产品供应、双侧画像，支撑五大业务场景
3. **可视化分析检索平台** —— 可上传新数据集自动解析入库，提供检索、筛选、场景分析与结果导出

---

## 实测指标（全量 1038 篇）

| 指标 | 数值 |
|---|---|
| 公告 | 1,038 篇 |
| 标的明细 | 6,244 条 |
| 7 字段分档提取率 | **6.101 / 10**（Σ 档位分 6,333 / 1038） |
| 提取准确性（自建 gold 54 篇，语义等价口径） | **P 1.00 / R 0.99 / F1 0.99 / A 0.99** |
| LLM 抽取覆盖 | 1,038 / 1,038（零回退） |
| 关系图谱规模（Neo4j 导出） | 5,645 节点 / 14,143 关系 |
| 五大场景查询耗时 | 本地 96–250 ms（2 核 4 G 环境） |
| 平台接口冒烟 | 58 项全通过 |

字段非空率：`item_name` 1.000 / `category` 0.981 / `total_price` 0.892 / `quantity` 0.842 / `unit_price` 0.807 / `spec` 0.636 / `brand` 0.605。

---

## 架构

```
                     ┌───────────────────────────────────────────────┐
 公告 HTML ────────┐ │  ict_parse  抽取管线                            │
 附件 zip（可套娃）┘ │  ├ html_ir      公告 HTML → IR（3 种模板回退）  │
                     │  ├ attach       附件递归解压 + 类型分发          │
                     │  ├ extract      规则层（表头映射/补价/校验）     │
                     │  └ llm_extract  LLM 层（分块 + JSON 强约束）    │
                     └───────────────┬───────────────────────────────┘
                                     ▼
                    items / announcements 结构化结果
                                     │
             ┌───────────────────────┴──────────────────────┐
             ▼                                              ▼
   scripts/build_task2_graph.py                  platform/ingest.py
   实体归一 / 投标事实 / 关系视图                  SQLite 服务库重建
             │                                              │
             ▼                                              ▼
      graph.db（关系图库）  ──────────────────────►  platform.db（查询库）
                                                            │
                                                            ▼
                                             task3 平台（FastAPI + Vue 3）
                                             ├ 数据总览（上传 / 恢复 / 下载）
                                             ├ 标的检索（7 字段筛选）
                                             ├ 场景分析（S1–S5）
                                             └ 模型配置
```

**两条可独立评测的抽取路线**：LLM 主抽取（`llm_extract`）与纯规则（`extract`）。
交付采用「LLM 主抽取 + 规则做解析对齐 / 数值归一 / 一致性校验 / 跨文件补价 / 兜底」的混合架构。

---

## 技术栈

| 层 | 采用 |
|---|---|
| 抽取 | Python 3.14 · BeautifulSoup4 · lxml · pdfplumber · python-docx · pypdfium2 · openpyxl |
| 大模型 | DeepSeek `deepseek-flash`（OpenAI 兼容接口），可切换 Qwen |
| 关系建模 | SQLite 图库（默认）/ **Neo4j 5.25 Community**（可选后端，同一仓储契约） |
| 平台后端 | FastAPI · Pydantic v2 · uvicorn |
| 平台前端 | Vue 3.4 · Element Plus · ECharts · Vite（已预构建，运行无需 npm） |
| 存储 | SQLite 3（无 ORM，纯 SQL + 仓储模式） |

---

## 目录结构

```
.
├── app/                        # 源码主体
│   ├── ict_parse/              # 抽取管线：解析 → IR → 规则层 / LLM 层
│   ├── platform/               # 数据装载（ingest）与轻量只读查看器
│   ├── scripts/                # 建图 / 导出 / 评测 / 打包等命令行工具
│   ├── task3/                  # 任务三平台
│   │   ├── backend/            #   FastAPI + 静态资源（已预构建）
│   │   └── frontend/           #   Vue 3 源码
│   ├── pyproject.toml          # 依赖（uv 管理）
│   ├── uv.lock                 # 锁定版本，可复现
│   └── README.md               # 源码级说明（任务一为主线，含任务二/三）
└── docs/                       # 交付文档
    ├── 软件版本与运行环境.md
    ├── 源码功能说明与执行步骤.md
    ├── 部署说明.md
    ├── 操作文档.md
    └── 提交说明与运行指南.md
```

---

## 快速开始

### 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Linux（Ubuntu 22.04+）或 Windows 10/11 |
| Python | 3.14（由 `uv` 自动安装，不需系统预装） |
| 内存 / 磁盘 | 2 核 4 G 可跑；装全量依赖约需 2 GB 磁盘 |
| 大模型 | 需要一个 OpenAI 兼容接口的 Key（DeepSeek / Qwen 均可） |

### 安装

```bash
cd app
uv sync                 # 按 uv.lock 装齐全部依赖
```

> 国内网络建议先设镜像：`export UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/`

### 启动平台

```bash
cd app
uv run python platform/ingest.py            # 生成服务库 out/platform/platform.db
uv run python task3/backend/run.py --host 0.0.0.0 --port 8000
```

浏览器打开 `http://127.0.0.1:8000/`，默认账号 `reviewer / ict2026`（**部署后请立即改掉**）。
在线接口文档：`http://127.0.0.1:8000/api/docs`

> ⚠️ **`ingest.py` 需要 `app/deliverables/` 下的交付表**。本仓库只含代码，未包含数据产物 ——
> 详见下方「数据依赖」。

---

## 三个任务

### 任务一 · 实体抽取

把公告与附件转成 7 字段标的明细。入口：

```bash
uv run python -m ict_parse --ids-file all_ids.txt --out out/full        # 解析 + 规则抽取
uv run python scripts/run_llm.py                                        # LLM 抽取（断点续跑）
uv run python scripts/export_task1_tables.py                            # 导出交付表 CSV
uv run python scripts/val_eval.py --src pred                            # 准确性评测
```

### 任务二 · 关系建模

```bash
uv run python scripts/build_task2_graph.py          # 建关系图库 out/task2/graph.db
uv run python scripts/task2_scenarios.py --demo     # 跑五大场景并出报告
# 可选：Neo4j
uv run python scripts/build_neo4j_graph.py --load --export
uv run python scripts/verify_neo4j.py               # 期望：可比项 11 / 一致 11
```

五大业务场景：

| 编号 | 场景 | 说明 |
|---|---|---|
| S1 | 长期合作 | 采购方 → 长期合作供应商，含合作次数、累计金额、时间跨度 |
| S2 | 投标画像 | 采购方 → 高频投标方与共同投标组合 |
| S3 | 竞对分析 | 供应商 → 竞争对手（同场投标频次、胜率） |
| S4 | 共同客户 | 多个供应商 → 共同客户 |
| S5 | 共同项目 | 多个供应商 → 共同参与的项目 |

### 任务三 · 可视化分析检索平台

- **数据总览**：规模与质量看板；上传数据集（zip / xlsx / csv / 文件夹）、一键恢复内置数据、下载官方格式
- **标的检索**：6,244 条标的的多维筛选与明细展开
- **场景分析**：S1–S5 交互查询 + 图表
- **模型配置**：填写 Base URL / API Key / 模型 id，保存即生效

上传路径会自动完成「解析 → 抽取 → 结构化入库 → **重建任务二关系表**」，全程不调模型即可完成关系建模。

---

## 数据依赖（重要）

本仓库**只含代码**，以下数据产物未包含，需自行准备或生成：

| 缺少项 | 用途 | 如何获得 |
|---|---|---|
| `app/data/gov_category.csv` | 政府采购品目分类词表（`ict_parse/category.py` 依赖，**缺失会直接报错**） | 下载《政府采购品目分类目录》PDF 后运行 `uv run python scripts/build_catalog.py` |
| `app/deliverables/items_task1.csv` 等 | 平台入库的输入（6,244 条标的 / 1,038 篇公告） | 用赛题原始数据集跑任务一管线后 `export_task1_tables.py` 导出 |
| `app/out/task2/graph.db` | 关系表来源 | 跑 `scripts/build_task2_graph.py` 生成 |
| `app/out/full/` | 逐篇原始抽取 JSON | 跑 `python -m ict_parse` 生成 |
| `app/valset/gold/` | 准确性评测的 gold 集 | 自建/随赛题资料提供 |

---

## 平台环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `EDM_DATA_BACKEND` | `real` | 数据后端标记 |
| `EDM_SQLITE_PATH` | `app/out/platform/platform.db` | 服务库位置 |
| `EDM_AUTH_USERS` | `reviewer:ict2026,admin:admin2026` | 登录账号，格式 `用户:口令,...` |
| `EDM_ITEM_REPOSITORY` | `integrations.repos:SQLiteItemRepository` | 标的仓储实现 |
| `EDM_GRAPH_REPOSITORY` | `integrations.repos:SQLiteGraphRepository` | 图仓储实现（可换 `neo4j_repo:Neo4jGraphRepository`） |
| `EDM_KEY_SOURCE` | `ui` | 为 `ui` 时**只认页面「模型配置」里的 Key**，忽略环境变量与 `.env` |
| `EDM_MAX_UPLOAD_MB` | `2048` | 单次上传大小上限 |
| `EDM_ITEM_NOISE_FILTER` | `1` | 标的行噪音过滤开关 |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | — | 启用 Neo4j 后端时需要 |

---

## 文档

| 文档 | 内容 |
|---|---|
| [软件版本与运行环境](docs/软件版本与运行环境.md) | 软件版本、依赖、运行环境规格 |
| [源码功能说明与执行步骤](docs/源码功能说明与执行步骤.md) | 各模块职责与逐条执行步骤 |
| [部署说明](docs/部署说明.md) | 在全新 2 核 4 G 云主机上从零部署（含 systemd、FAQ） |
| [操作文档](docs/操作文档.md) | 平台四个页面的使用方法 |
| [提交说明与运行指南](docs/提交说明与运行指南.md) | 交付物清单、指标复算方式、官方格式答疑落实 |
| [app/README.md](app/README.md) | 源码级说明（任务一为主线） |

---

## 已知限制

1. **准确性 gold 非独立人工标注** —— 0.99 属"与同族标注的一致性"，不等于人工核验精度；需独立盲标才能把该结论变为可采信。
2. **模型参数量以厂商公开规格为准** —— 全程通过官方 API 调用，未本地持有权重。
3. **嵌套压缩包** —— 已支持 rar / 7z；加密包无法解开（全量数据中 2 例，已登记不静默丢弃）。
4. **视觉模型** —— 设置页可配置，但当前版本上传路径的 `use_vlm` 恒为关闭，扫描件走文本/规则降级。
5. **数据产物未随仓库** —— 见上方「数据依赖」。

---

## 说明

本项目为参赛作品，源码用于学习交流与技术评审。
涉及的大模型均通过官方 API 调用（DeepSeek / Qwen 系列），未包含任何第三方模型的权重或商业系统源码。
