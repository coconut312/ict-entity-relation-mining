# -*- coding: utf-8 -*-
"""后端服务：可视化分析检索平台。

分层（这一层的划分就是为了三方工程合并时不互相踩踏）：

    api.py           HTTP 层：只做参数校验与响应封装，不含业务逻辑
    schemas.py       前后端契约（Pydantic 模型）—— 单一事实来源
    services.py      业务逻辑：检索、场景计算、导入编排
    repositories.py  数据访问**抽象接口** ← 外部数据从这里接入
    scenarios_def.py 五大业务场景的元定义（前端据此自动渲染表单）

合并约定：
    队友实现 repositories 中的抽象类即可，api / services / 前端 均无需改动。
    详见 docs/工程/任务三-接口契约与工程整合.md
"""

__version__ = '0.1.0'
