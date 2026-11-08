# -*- coding: utf-8 -*-
"""数据访问层 —— **外部数据接入的唯一对接点**。

================================================================================
接入指引
================================================================================
外部数据只要分别实现下面两个抽象类，平台的后端与前端**不需要任何改动**：

    标的信息（实体抽取结果）  ->  实现 ItemRepository
    关系模型（场景数据）      ->  实现 GraphRepository

然后通过环境变量或配置切换实现：

    EDM_ITEM_REPOSITORY  = "my_task1.repos:PostgresItemRepository"
    EDM_GRAPH_REPOSITORY = "my_task2.repos:Neo4jGraphRepository"
    EDM_DATA_BACKEND     = "real"

格式为 `模块路径:类名`，构造函数需要**零参数**（配置从 app.config.settings 读）。
================================================================================

未配置实现时，回退到本模块的 Empty 实现：所有查询返回空结果。
这样平台在任何阶段都能正常启动与浏览 —— 页面显示空态与引导，而不是报错白屏。
"""
from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from .config import settings
from .schemas import (BidderStat, CoBidPair, DatasetStats, EntitySuggestItem,
                      FacetValue, ItemFacets, ItemFilter, ProjectRecord,
                      PurchaserStat, Scenario1Out, Scenario2Out, Scenario3Out,
                      Scenario4Out, Scenario5Out, SupplierStat)

log = logging.getLogger('edm.repo')


# ==================================================================
# 抽象接口
# ==================================================================

class ItemRepository(ABC):
    """标的信息的访问接口。

    用 PostgreSQL / SQLite / CSV / Parquet 实现均可，只要满足这些方法。
    """

    @abstractmethod
    def has_data(self) -> bool:
        """是否有数据。前端据此决定显示内容还是空态引导。"""

    @abstractmethod
    def stats(self) -> DatasetStats:
        """数据概览统计。"""

    @abstractmethod
    def search(self, flt: ItemFilter, page: int, page_size: int
               ) -> Tuple[List[Dict[str, Any]], int]:
        """条件检索。返回 (当前页记录, 总条数)。

        实现要求：
            * 必须走数据库索引 / 预计算，**不要在 Python 侧全表扫描**，
              单次查询需在 1 秒内完成（含前端渲染）。
        """

    @abstractmethod
    def facets(self) -> ItemFacets:
        """筛选面板的取值（品牌 / 品目 / 供应商 / 采购方）。"""

    @abstractmethod
    def suggest_entities(self, keyword: str, entity_type: str = '',
                         limit: int = 20) -> List[EntitySuggestItem]:
        """实体搜索建议（场景查询表单用）。"""


class GraphRepository(ABC):
    """关系模型的访问接口。

    五个方法分别对应五个业务场景。推荐用 Neo4j + Cypher 实现，
    也可以用 PostgreSQL 递归 CTE，或**预计算物化表**（速度最快）。
    """

    @abstractmethod
    def scenario1(self, purchaser: str, min_count: int = 1,
                  top_n: int = 20) -> Scenario1Out:
        """长期合作：采购方的长期合作供应商。"""

    @abstractmethod
    def scenario2(self, purchaser: str, top_n: int = 5) -> Scenario2Out:
        """投标画像：采购方的高频投标方与协同投标组合。"""

    @abstractmethod
    def scenario3(self, supplier: str, top_n: int = 5) -> Scenario3Out:
        """竞对分析：供应商的主要竞争对手与全部投标方。"""

    @abstractmethod
    def scenario4(self, suppliers: List[str], min_count: int = 1,
                  top_n: int = 20) -> Scenario4Out:
        """共同客户：多个供应商共同的采购方。"""

    @abstractmethod
    def scenario5(self, suppliers: List[str], top_n: int = 50) -> Scenario5Out:
        """共同项目：多个供应商共同参与的项目与结果。"""


# ==================================================================
# 默认实现：无数据源
# ==================================================================

class EmptyItemRepository(ItemRepository):
    """尚未接入数据源时的默认实现：一切查询返回空结果。

    这不是"假数据"——它诚实地表示当前库里没有数据，
    前端据此展示空态与上传引导。
    """

    def has_data(self) -> bool:
        return False

    def stats(self) -> DatasetStats:
        return DatasetStats()

    def search(self, flt: ItemFilter, page: int, page_size: int):
        return [], 0

    def facets(self) -> ItemFacets:
        return ItemFacets()

    def suggest_entities(self, keyword: str, entity_type: str = '',
                         limit: int = 20) -> List[EntitySuggestItem]:
        return []


class EmptyGraphRepository(GraphRepository):
    """尚未接入关系数据时的默认实现：返回各场景的空结果。

    保留查询主体（如采购方名称）回填，便于前端区分
    "没查" 与 "查了但没有结果"。
    """

    def scenario1(self, purchaser: str, min_count: int = 1, top_n: int = 20) -> Scenario1Out:
        return Scenario1Out(purchaser=purchaser)

    def scenario2(self, purchaser: str, top_n: int = 5) -> Scenario2Out:
        return Scenario2Out(purchaser=purchaser)

    def scenario3(self, supplier: str, top_n: int = 5) -> Scenario3Out:
        return Scenario3Out(supplier=supplier)

    def scenario4(self, suppliers: List[str], min_count: int = 1, top_n: int = 20) -> Scenario4Out:
        return Scenario4Out(suppliers=suppliers)

    def scenario5(self, suppliers: List[str], top_n: int = 50) -> Scenario5Out:
        return Scenario5Out(suppliers=suppliers)


# ==================================================================
# 工厂：按配置装配实现
# ==================================================================

def _load_class(path: str):
    """加载 'module.path:ClassName'。"""
    mod_name, _, cls_name = path.partition(':')
    if not mod_name or not cls_name:
        raise ValueError(f'仓储实现配置格式应为 "模块:类名"，当前为 {path!r}')
    mod = importlib.import_module(mod_name)
    return getattr(mod, cls_name)


_item_repo: Optional[ItemRepository] = None
_graph_repo: Optional[GraphRepository] = None


def get_item_repository() -> ItemRepository:
    global _item_repo
    if _item_repo is None:
        if settings.ITEM_REPOSITORY:
            try:
                _item_repo = _load_class(settings.ITEM_REPOSITORY)()
                log.info('标的信息仓储: %s', settings.ITEM_REPOSITORY)
            except Exception as e:
                # 配置了实现却加载失败时，宁可降级到空实现也不要让整个服务起不来，
                # 但必须把原因打到日志里，避免"静默地没有数据"。
                log.error('加载 %s 失败，降级为空实现: %s: %s',
                          settings.ITEM_REPOSITORY, type(e).__name__, e)
                _item_repo = EmptyItemRepository()
        else:
            _item_repo = EmptyItemRepository()
    return _item_repo


def get_graph_repository() -> GraphRepository:
    global _graph_repo
    if _graph_repo is None:
        if settings.GRAPH_REPOSITORY:
            try:
                _graph_repo = _load_class(settings.GRAPH_REPOSITORY)()
                log.info('关系模型仓储: %s', settings.GRAPH_REPOSITORY)
            except Exception as e:
                log.error('加载 %s 失败，降级为空实现: %s: %s',
                          settings.GRAPH_REPOSITORY, type(e).__name__, e)
                _graph_repo = EmptyGraphRepository()
        else:
            _graph_repo = EmptyGraphRepository()
    return _graph_repo


def reset_repositories() -> None:
    """导入新数据集后清空缓存，使下次访问重新装配。"""
    global _item_repo, _graph_repo
    _item_repo = None
    _graph_repo = None
