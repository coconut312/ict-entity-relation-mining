# -*- coding: utf-8 -*-
"""前后端契约（Pydantic 模型）。

**这是三方合并的单一事实来源。**
前端 `src/api/` 里的每个函数都与这里的一个模型一一对应；
外部数据只要能被组装成这里的模型，前端就无需任何改动。

字段命名规范：
    * 后端用 snake_case，前端通过 axios 拦截器统一转换（见 src/api/client.js）
    * 所有「金额」单位一律为**元**（人民币），不做单位混合
    * 所有「次数/频次」为整数
    * 时间统一用 `YYYY-MM-DD` 字符串
"""
from __future__ import annotations

from typing import Any, Dict, Generic, List, Optional, TypeVar
from pydantic import BaseModel, Field

T = TypeVar('T')


# ==================================================================
# 通用
# ==================================================================

class PageMeta(BaseModel):
    """分页元信息。"""
    page: int = Field(1, description='当前页码，从 1 开始')
    page_size: int = Field(20, description='每页条数')
    total: int = Field(0, description='总条数')
    total_pages: int = Field(0, description='总页数')


class Page(BaseModel, Generic[T]):
    """统一分页响应。"""
    items: List[T] = Field(default_factory=list)
    meta: PageMeta = Field(default_factory=PageMeta)


class ApiError(BaseModel):
    """统一错误响应体。"""
    code: str = Field(..., description='机器可读的错误码')
    message: str = Field(..., description='面向用户的中文说明')
    detail: Optional[Any] = Field(None, description='调试用细节，生产环境可为空')


class HealthOut(BaseModel):
    status: str = 'ok'
    version: str
    data_backend: str = Field(..., description="'empty' 表示尚未接入数据源，'real' 表示已接入")
    has_data: bool = Field(False, description='当前是否有可用数据')


# ==================================================================
# 登录鉴权
# ==================================================================

class LoginIn(BaseModel):
    username: str = Field(..., description='用户名')
    password: str = Field(..., description='口令')


class LoginOut(BaseModel):
    token: str = Field(..., description='后续请求放入 Authorization: Bearer <token>')
    username: str
    role: str = Field('user', description="'user' | 'admin'")
    expires_in: int = Field(..., description='有效期（秒）')


class UserInfo(BaseModel):
    username: str
    role: str = 'user'
    auth_enabled: bool = Field(True, description='是否启用了鉴权（部署校验用）')


# ==================================================================
# 标的信息（7 个核心字段 + 关系分析所需的扩展字段）
# ==================================================================

class Item(BaseModel):
    """一条标的信息。

    前 7 个字段为标的物核心信息；其余字段用于支撑采购方—供应商关系分析。
    """
    # ---- 官方 7 字段 ----
    product_name: str = Field('', description='产品服务名称')
    category: str = Field('', description='品目')
    brand: str = Field('', description='品牌（产品供应商）')
    spec: str = Field('', description='规格型号')
    unit_price: Optional[float] = Field(None, description='单价（元）')
    quantity: Optional[float] = Field(None, description='数量')
    total_price: Optional[float] = Field(None, description='总价（元）')

    # ---- 扩展字段（关系分析必需）----
    id: Optional[str] = Field(None, description='记录唯一标识')
    doc_id: Optional[str] = Field(None, description='来源公告编号')
    project_name: str = Field('', description='项目名称')
    project_amount: Optional[float] = Field(None, description='项目金额（元）')
    purchaser: str = Field('', description='采购单位')
    supplier: str = Field('', description='中标供应商')
    unit: str = Field('', description='数量单位')
    announce_date: Optional[str] = Field(None, description='公告日期 YYYY-MM-DD')
    source: str = Field('', description="来源：'html' | 'attach:pdf' | 'merged' 等")
    confidence: Optional[float] = Field(None, description='抽取置信度 0~1，低置信对象在界面上标注')

    model_config = {'extra': 'allow'}


class ItemFilter(BaseModel):
    """标的信息的筛选条件。"""
    keyword: str = Field('', description='全文关键词（产品名/规格/品牌）')
    brand: str = ''
    category: str = ''
    supplier: str = ''
    purchaser: str = ''
    date_from: str = ''
    date_to: str = ''
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    sort_by: str = Field('doc_id', description='排序字段')
    sort_order: str = Field('asc', description="'asc' | 'desc'")


class FacetValue(BaseModel):
    """筛选器候选项。"""
    value: str
    count: int = 0


class ItemFacets(BaseModel):
    """筛选面板的可选值（后端聚合，前端不做全量拉取）。"""
    brands: List[FacetValue] = Field(default_factory=list)
    categories: List[FacetValue] = Field(default_factory=list)
    suppliers: List[FacetValue] = Field(default_factory=list)
    purchasers: List[FacetValue] = Field(default_factory=list)


# ==================================================================
# 业务场景
# ==================================================================

class SupplierStat(BaseModel):
    """供应商维度的统计。"""
    name: str
    cooperate_count: int = Field(0, description='合作次数')
    total_amount: float = Field(0.0, description='交易总金额（元）')
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    is_product_supplier: bool = Field(False, description='是否为产品供应商（有品牌记录）')


class BidderStat(BaseModel):
    """投标主体统计。"""
    name: str
    bid_count: int = Field(0, description='参与投标次数')
    win_count: int = Field(0, description='中标次数')
    cooperate_amount: float = Field(0.0, description='相关金额（元）')


class CoBidPair(BaseModel):
    """协同投标组合。"""
    members: List[str] = Field(default_factory=list, description='组合成员，通常 2 个')
    count: int = Field(0, description='共同参与投标的次数')
    projects: List[str] = Field(default_factory=list, description='共同参与的项目名称（截断）')


class Scenario1Out(BaseModel):
    """长期合作：采购方的长期合作供应商。"""
    purchaser: str = ''
    suppliers: List[SupplierStat] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict, description='汇总指标，供图表使用')


class Scenario2Out(BaseModel):
    """投标画像：采购方的高频投标方与协同投标组合。"""
    purchaser: str = ''
    top_bidders: List[BidderStat] = Field(default_factory=list, description='高频参与投标主体 TOP-N')
    co_bid_pairs: List[CoBidPair] = Field(default_factory=list, description='高频协同参与投标组合')
    summary: Dict[str, Any] = Field(default_factory=dict)


class Scenario3Out(BaseModel):
    """竞对分析：供应商的主要竞争对手与全部投标方。"""
    supplier: str = ''
    top_competitors: List[BidderStat] = Field(default_factory=list, description='高频共同竞标主体 TOP-N')
    all_bidders: List[BidderStat] = Field(default_factory=list, description='全部参与投标主体')
    summary: Dict[str, Any] = Field(default_factory=dict)


class PurchaserStat(BaseModel):
    """采购单位维度统计。"""
    name: str
    cooperate_count: int = Field(0, description='合作频次')
    cooperate_amount: float = Field(0.0, description='合作金额（元）')
    projects: List[str] = Field(default_factory=list)


class Scenario4Out(BaseModel):
    """共同客户：多个供应商共同的采购方。"""
    suppliers: List[str] = Field(default_factory=list)
    purchasers: List[PurchaserStat] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict)


class ProjectRecord(BaseModel):
    """项目记录。"""
    project_name: str = ''
    doc_id: Optional[str] = None
    announce_date: Optional[str] = None
    amount: float = Field(0.0, description='项目总金额（元）')
    purchaser: str = ''
    winner: str = Field('', description='中标供应商')
    participants: List[str] = Field(default_factory=list, description='参与投标主体')
    winner_is_target: bool = Field(False, description='中标方是否属于查询的供应商之一')


class Scenario5Out(BaseModel):
    """共同项目：多个供应商共同参与的项目与结果。"""
    suppliers: List[str] = Field(default_factory=list)
    projects: List[ProjectRecord] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict)


# ==================================================================
# 场景元定义（前端据此自动渲染查询表单）
# ==================================================================

class ScenarioParamDef(BaseModel):
    """场景查询参数定义。"""
    name: str
    label: str
    type: str = Field('entity', description="'entity' | 'entities' | 'number' | 'select'")
    entity_type: str = Field('', description="实体类型：'purchaser' | 'supplier'")
    required: bool = True
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    placeholder: str = ''
    help: str = ''


class ScenarioDef(BaseModel):
    """一个业务场景的完整元定义。"""
    id: str = Field(..., description="'s1' ~ 's5'")
    title: str
    subtitle: str = ''
    description: str = ''
    params: List[ScenarioParamDef] = Field(default_factory=list)
    chart_type: str = Field('bar', description='建议的可视化形态，前端可按需调整')
    endpoint: str = Field('', description='该场景的查询接口路径')


# ==================================================================
# 数据集管理
# ==================================================================

class ImportJob(BaseModel):
    """数据集导入任务。"""
    job_id: str
    filename: str = ''
    status: str = Field('pending', description="'pending'|'running'|'succeeded'|'failed'")
    progress: float = Field(0.0, description='0~1')
    stage: str = Field('', description='当前阶段中文说明')
    total_docs: int = 0
    processed_docs: int = 0
    total_items: int = 0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    elapsed_ms: Optional[int] = Field(None, description='总耗时（毫秒）')
    message: str = ''
    errors: List[str] = Field(default_factory=list)


class DatasetStats(BaseModel):
    """数据集概览统计（首页用）。"""
    n_docs: int = 0
    n_items: int = 0
    n_suppliers: int = 0
    n_purchasers: int = 0
    n_brands: int = 0
    total_amount: float = 0.0
    date_min: Optional[str] = None
    date_max: Optional[str] = None
    avg_process_ms: Optional[float] = Field(None, description='单篇平均处理耗时（毫秒）')
    field_fill_rate: Dict[str, float] = Field(
        default_factory=dict, description='各字段完整度，0~1')


class DatasetRestore(BaseModel):
    """「恢复默认数据」的结果。

    语义：用**仓库内置的完整交付集**（`app/deliverables/`，1038 篇 / 6244 条）
    覆盖当前平台数据，并把图库一并重建为全量图。
    失败不返回本模型，而是 409（已有恢复在进行中）/ 500（重建失败）+ 可读 detail。
    """
    ok: bool = Field(True, description='是否成功（失败走 409 / 500，错误信息在 detail）')
    n_docs: int = Field(0, description='恢复后的公告篇数（完整交付集 = 1038）')
    n_items: int = Field(0, description='恢复后的标的条数（完整交付集 = 6244）')
    graph_backend: str = Field('sqlite', description="图库后端：'neo4j' | 'sqlite'")
    nodes: Optional[int] = Field(None, description='Neo4j 节点数（非 Neo4j 后端为 null）')
    rels: Optional[int] = Field(None, description='Neo4j 关系数（非 Neo4j 后端为 null）')
    elapsed_ms: int = Field(0, description='整次恢复耗时（毫秒，含图库重建）')


class EntitySuggestItem(BaseModel):
    """实体自动补全项。"""
    id: str = ''
    name: str
    type: str = Field('', description="'purchaser' | 'supplier'")
    count: int = 0


# ==================================================================
# 模型配置（API Key，页面快捷输入）
# ==================================================================

class ModelGroup(BaseModel):
    """一组模型的**生效值**。

    key 只回显掩码（如 `****1234`），**任何响应都不包含明文**。
    """
    base_url: str = ''
    model: str = ''
    has_key: bool = False
    key_masked: str = Field('', description='仅末 4 位，如 ****1234')
    source: str = Field('未配置', description="key 来源：'环境变量' | '页面输入' | '未配置'")


class ModelSettingsOut(BaseModel):
    """模型配置的当前生效值（文本组平铺，视觉组嵌套）。"""
    base_url: str = ''
    model: str = ''
    has_key: bool = False
    key_masked: str = ''
    source: str = '未配置'
    vision: ModelGroup = Field(default_factory=ModelGroup)


class ModelSettingsIn(BaseModel):
    """模型配置的写入请求。

    字段**缺省 = 不修改**，**空字符串 = 清除该项**（回落环境变量 / 默认值）。
    """
    api_key: Optional[str] = Field(None, description='文本模型 key；空字符串表示清除')
    base_url: Optional[str] = None
    model: Optional[str] = None
    vision_api_key: Optional[str] = Field(None, description='视觉 key；空字符串表示清除')
    vision_base_url: Optional[str] = None
    vision_model: Optional[str] = None


class ModelTestOut(BaseModel):
    """连通性测试结果。

    失败也是 200 + `ok=false` + 可读 `error`（不抛 500、不回显 key）。
    """
    ok: bool
    model: str = ''
    base_url: str = ''
    latency_ms: int = 0
    error: Optional[str] = None
