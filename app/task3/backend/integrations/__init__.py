# -*- coding: utf-8 -*-
"""任务一 / 任务二 -> 任务三平台 的数据接入包。

按《接口契约与工程整合》的约定，本包**只新增文件**，不改动 `app/` 下的既有代码：

* `repos.py`          —— 实现 `ItemRepository` / `GraphRepository`（接入点 1 与 2）
* `pipeline_impl.py`  —— 实现 `Pipeline`（接入点 3）

装配见 `backend/run.py`（设置 `EDM_*` 环境变量并注入管线）。
"""
from __future__ import annotations

from .repos import SQLiteGraphRepository, SQLiteItemRepository  # noqa: F401

__all__ = ['SQLiteItemRepository', 'SQLiteGraphRepository']
