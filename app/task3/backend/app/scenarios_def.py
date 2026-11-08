# -*- coding: utf-8 -*-
"""业务场景定义。

为什么把场景做成「数据」而不是「代码」：
    前端拿这个定义就能自动渲染查询表单（实体选择框 / 数字输入 / 多选），
    新增或调整场景时只改这里，前端页面一行都不用动。
    这也是三方工程合并时最不容易冲突的一块。

文案约定：面向业务用户，用词简短直白，不出现技术术语与内部代号。
"""
from __future__ import annotations

from typing import Dict, List

from .config import settings
from .schemas import ScenarioDef, ScenarioParamDef

_P = settings.API_PREFIX

_PURCHASER = ScenarioParamDef(name='purchaser', label='采购方', type='entity',
                              entity_type='purchaser', required=True,
                              placeholder='输入采购方名称',
                              help='输入关键词即可搜索')
_SUPPLIER = ScenarioParamDef(name='supplier', label='供应商', type='entity',
                             entity_type='supplier', required=True,
                             placeholder='输入供应商名称',
                             help='输入关键词即可搜索')
_SUPPLIERS = ScenarioParamDef(name='suppliers', label='供应商（可多选）',
                              type='entities', entity_type='supplier', required=True,
                              placeholder='选择 2 个及以上',
                              help='至少选 2 个')
_MIN_COUNT = ScenarioParamDef(name='min_count', label='最少次数', type='number',
                              default=1, min=1, max=100)
_TOP_N = ScenarioParamDef(name='top_n', label='显示条数', type='number',
                          default=20, min=5, max=100)


def _top(label: str, default: int, low: int = 3, high: int = 20) -> ScenarioParamDef:
    return ScenarioParamDef(name='top_n', label=label, type='number',
                            default=default, min=low, max=high)


SCENARIOS: List[ScenarioDef] = [
    ScenarioDef(
        id='s1',
        title='长期合作',
        subtitle='采购方 → 长期合作供应商',
        description='查看某采购方的长期合作供应商，含合作次数、累计金额与合作时间跨度。',
        endpoint=f'{_P}/scenarios/s1',
        chart_type='bar',
        params=[_PURCHASER, _MIN_COUNT, _TOP_N],
    ),
    ScenarioDef(
        id='s2',
        title='投标画像',
        subtitle='采购方 → 高频投标方',
        description='查看某采购方历史项目中的高频投标方，以及经常同时出现的投标组合。',
        endpoint=f'{_P}/scenarios/s2',
        chart_type='graph',
        params=[_PURCHASER, _top('TOP N', 5)],
    ),
    ScenarioDef(
        id='s3',
        title='竞对分析',
        subtitle='供应商 → 主要竞争对手',
        description='查看某供应商的主要竞争对手，以及同场竞标的全部投标方。',
        endpoint=f'{_P}/scenarios/s3',
        chart_type='graph',
        params=[_SUPPLIER, _top('TOP N', 5)],
    ),
    ScenarioDef(
        id='s4',
        title='共同客户',
        subtitle='多供应商 → 共同采购方',
        description='选择多个供应商，找出与它们都合作过的采购方，含合作次数与金额。',
        endpoint=f'{_P}/scenarios/s4',
        chart_type='bar',
        params=[_SUPPLIERS, _MIN_COUNT, _TOP_N],
    ),
    ScenarioDef(
        id='s5',
        title='共同项目',
        subtitle='多供应商 → 共同中标项目',
        description='选择多个供应商，查看它们同场竞标的项目、结果与金额。',
        endpoint=f'{_P}/scenarios/s5',
        chart_type='table',
        params=[_SUPPLIERS, _top('显示条数', 50, 10, 200)],
    ),
]

SCENARIO_MAP: Dict[str, ScenarioDef] = {s.id: s for s in SCENARIOS}
