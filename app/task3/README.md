# 可视化分析检索平台（任务三）

招采数据分析平台的前后端完整实现。本包**自包含**，拿到即可运行与接入数据。

| | |
|---|---|
| 技术栈 | 前端 Vue 3 + Vite + Element Plus + ECharts ｜ 后端 FastAPI + Pydantic |
| 后端接口 | 16 个，全部有 OpenAPI 文档 |
| 自动化测试 | 58 项接口冒烟测试 |
| 当前状态 | 已接入真实交付数据（1038 篇公告 / 6244 条标的），可登录使用 |

---

## 一、目录结构

```
任务三-可视化分析检索平台/
│
├── README.md                    本文件
│
├── backend/                     后端服务（FastAPI）
│   ├── app/
│   │   ├── main.py              应用入口
│   │   ├── config.py            全部可调配置（部署时只改这里）
│   │   ├── schemas.py        ★  前后端契约 —— 数据结构在这里定义
│   │   ├── repositories.py   ★  数据访问抽象 —— 接入数据改这里
│   │   ├── pipeline.py       ★  处理管线抽象 —— 接抽取代码改这里
│   │   ├── services.py          业务逻辑
│   │   ├── api.py               HTTP 路由（分公开 / 需登录两组）
│   │   ├── auth.py              登录鉴权（内存 token，无外部依赖）
│   │   ├── scenarios_def.py     五个业务场景的元定义
│   │   └── __init__.py
│   ├── static/                  ★ 前端构建产物（已构建好，可直接部署）
│   ├── var/uploads/             数据集上传落盘目录
│   ├── requirements.txt         后端依赖
│   └── smoke_test.py            58 项冒烟测试
│
├── frontend/                    前端应用（Vue 3 + Vite）
│   ├── src/
│   │   ├── api/                 接口封装 + HTTP 客户端
│   │   ├── views/               页面
│   │   │   ├── Login.vue            登录
│   │   │   ├── Overview.vue         数据总览（含数据集上传）
│   │   │   ├── ItemSearch.vue       标的检索
│   │   │   ├── ScenarioWorkbench.vue 场景分析
│   │   │   └── scenario/            五个场景各自的明细组件
│   │   ├── components/          通用组件
│   │   ├── router/              路由与登录守卫
│   │   ├── store/               登录态
│   │   ├── styles/              全局样式与动效令牌
│   │   └── utils/               格式化工具
│   ├── node_modules/            前端依赖（**已包含，可离线直接运行**）
│   ├── index.html
│   ├── package.json
│   ├── package-lock.json
│   ├── vite.config.js           构建产物直出 backend/static
│   └── .npmrc                   npm 缓存改到工程内
│
└── docs/                        文档
    ├── 接口契约与工程整合.md      ★ 必读：接口清单 + 三方合并步骤 + 部署
    └── 评审方操作说明.md          评审账号、功能验证、性能指标测试方法
```

★ 标记的是接入数据时最需要看的四个位置。

---

## 二、快速开始

### 后端

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate            # Linux: source .venv/bin/activate
pip install -r requirements.txt
python smoke_test.py              # 期望：通过 58 项，失败 0 项
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 前端

```bash
cd frontend
npm run dev                       # http://localhost:5173，已配置 /api 代理
```

`node_modules` 已随包提供，**无需 `npm install`**。
若想减小体积，可以删掉 `node_modules` 后自行执行 `npm install`。

### 生产模式（单进程同时提供接口与页面）

```bash
cd frontend && npm run build      # 产物直出 backend/static
cd ../backend && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

> `backend/static/` 里已经有一份构建好的产物，所以**拿到包直接起后端就能看到完整页面**，
> 不必先装前端依赖。

### 访问

| | |
|---|---|
| 系统地址 | <http://127.0.0.1:8000> |
| 接口文档 | <http://127.0.0.1:8000/api/docs> |
| 评审账号 | `reviewer` / `ict2026` |
| 管理账号 | `admin` / `admin2026` |

在接口文档里测试受保护接口的顺序：先执行 `POST /api/v1/auth/login` 拿 token，
再点右上角 **Authorize** 填入 token。

---

## 三、接入你的数据（重要）

后端**不依赖任何具体数据源**。你要做的只有两件事：

### 1. 实现两个抽象类

```python
# my_repos.py（放在你自己的模块里，或 backend/app/ 下）

from app.repositories import ItemRepository, GraphRepository
from app.schemas import DatasetStats, ItemFacets, ItemFilter, \
                        Scenario1Out, Scenario2Out, Scenario3Out, \
                        Scenario4Out, Scenario5Out


class PgItemRepository(ItemRepository):
    """标的信息（任务一的抽取结果）。"""
    def __init__(self):
        import psycopg
        self.conn = psycopg.connect("postgresql://user:pwd@localhost/db")

    def has_data(self) -> bool: ...
    def stats(self) -> DatasetStats: ...
    def search(self, flt: ItemFilter, page: int, page_size: int): ...
    def facets(self) -> ItemFacets: ...
    def suggest_entities(self, keyword, entity_type='', limit=20): ...


class Neo4jGraphRepository(GraphRepository):
    """关系模型（任务二）。五个方法对应五个业务场景。"""
    def scenario1(self, purchaser, min_count=1, top_n=20) -> Scenario1Out: ...
    def scenario2(self, purchaser, top_n=5) -> Scenario2Out: ...
    def scenario3(self, supplier, top_n=5) -> Scenario3Out: ...
    def scenario4(self, suppliers, min_count=1, top_n=20) -> Scenario4Out: ...
    def scenario5(self, suppliers, top_n=50) -> Scenario5Out: ...
```

**每个方法要返回的字段，看 `backend/app/schemas.py` 里的模型定义**——
那里是唯一的数据结构来源，不要凭猜测构造返回值。

### 2. 用环境变量启用

```bash
set EDM_ITEM_REPOSITORY=my_repos:PgItemRepository
set EDM_GRAPH_REPOSITORY=my_repos:Neo4jGraphRepository
set EDM_DATA_BACKEND=real
```

构造函数需要**零参数**（配置从 `app.config.settings` 读）。

### 3. 如果要接抽取流程

实现 `backend/app/pipeline.py` 里的 `Pipeline`，在其中调用你的抽取代码，
并把 `get_pipeline()` 换成你的实现。进度通过 `ctx.report(...)` 汇报，
前端会自动轮询显示。

**前端一行都不用改。** 场景的标题、说明、查询参数全部由后端
`scenarios_def.py` 下发，前端只认识参数类型（实体 / 多实体 / 数字）。

---

## 四、已验证内容

| 项目 | 结果 |
|---|---|
| 后端冒烟测试 | 58 项全部通过（含 6 项鉴权专项） |
| 前端构建 | 通过，产物已输出到 `backend/static/` |
| 生产模式 | 单进程同时提供接口与页面，已验证 |
| 空数据表现 | 未接入数据时返回空结果，页面显示空态而非报错 |

**未验证**：真实数据规模下的查询响应时间。目前库里没有数据，
所以 `/items` 分页与 `/items/facets` 聚合的真实性能还没有测过。
接入数据后需用 1000 条量级实测一次——这关系到验收时的查询效率评分。

---

## 五、注意事项

1. **端口**：后端固定 8000，前端开发服务器 5173，两者不冲突。
2. **数据源模式**：`EDM_DATA_BACKEND` 取值 `empty`（未接入）/ `real`（已接入），
   只影响状态展示；实际用哪个仓储由 `EDM_*_REPOSITORY` 决定。
3. **`backend/static/` 是构建产物**。改了前端代码后必须重新 `npm run build`，
   否则页面不会更新；开发时用 `npm run dev` 不受影响。
4. **评测环境是 2 核 4G 无 GPU**。请勿引入 `torch` / `transformers` / `paddleocr`
   等重型依赖，也不要在评测机上跑 `vite dev`（开发服务器常驻内存 1 GB+）。
5. **登录口令**部署前请通过 `EDM_AUTH_USERS` 环境变量改掉，
   格式为 `用户名:口令,用户名:口令`。

---

## 六、相关文档

| 文档 | 内容 |
|---|---|
| `docs/接口契约与工程整合.md` | 完整接口清单、三方合并步骤、2C4G 部署、评分点对应 |
| `docs/评审方操作说明.md` | 评审账号、功能验证明细、性能指标测试方法 |
