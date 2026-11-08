# -*- coding: utf-8 -*-
"""后端统一配置。

所有可调参数集中在这里，避免散落在各处的硬编码 —— 这也是
「部署环境从开发机迁移到 2 核 4G 云主机」时唯一需要改的地方。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ==================================================================
# 模型 Key 来源：**平台进程内强制 `ui`**（只认页面「模型配置」里填的 Key）
# ==================================================================
# 为什么必须在这里（而不是只在 run.py）：`uvicorn app.main:app` 是文档里写的
# 另一种启动方式，它**不会**经过 run.py 的 `_set_env_defaults()`，于是
# `ict_parse/config.py::_ui_only()` 读不到 `EDM_KEY_SOURCE`，取值链会继续往下
# 落到 `%APPDATA%/reasonix/.env` 的开发机 key —— 实测因此触发过 3 次**真实**
# LLM 调用（上传数据集时悄悄花了钱）。本模块是平台进程的**最早**入口
# （api/auth/main/services/repositories 全部 `from .config import settings`），
# 在这里 setdefault 可以保证「只要是平台进程，就一定只用页面上填的 key」。
#
# 用 `setdefault`：显式设成别的值（例如离线调试 `auto`）依然被尊重，
# 只补「没设」这一种情况 —— 也就是 `uvicorn app.main:app` 的默认启动。
os.environ.setdefault('EDM_KEY_SOURCE', 'ui')


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ('1', 'true', 'yes', 'on')


class Settings:
    """运行时配置。可通过环境变量覆盖（12-factor）。"""

    # ---- 基本信息 ----
    APP_NAME: str = '招采数据智能分析平台'
    VERSION: str = '0.1.0'
    API_PREFIX: str = '/api/v1'

    # ---- 数据源模式 ----
    # 'empty' —— 尚未接入数据源（默认）：查询返回空结果，界面显示空态与上传引导
    # 'real'  —— 已接入真实数据源的仓储实现
    DATA_BACKEND: str = os.getenv('EDM_DATA_BACKEND', 'empty')

    # ---- 仓储实现选择（数据接入的入口）----
    # 形如 'myproject.repos:PgItemRepository'，留空则回退到空实现
    ITEM_REPOSITORY: str = os.getenv('EDM_ITEM_REPOSITORY', '')
    GRAPH_REPOSITORY: str = os.getenv('EDM_GRAPH_REPOSITORY', '')

    # ---- 数据集处理 ----
    UPLOAD_DIR: Path = Path(os.getenv('EDM_UPLOAD_DIR', str(BASE_DIR / 'var' / 'uploads')))
    MAX_UPLOAD_MB: int = int(os.getenv('EDM_MAX_UPLOAD_MB', '2048'))
    # 文件夹上传：一次最多接收多少个文件（总大小仍受 MAX_UPLOAD_MB 约束）。
    # 浏览器上传文件夹时会把目录下**所有**文件都发上来（含 .git、node_modules），
    # 这个上限是给 2C4G 云主机的兜底，避免一次导入把磁盘/内存打满。
    MAX_FOLDER_FILES: int = int(os.getenv('EDM_MAX_FOLDER_FILES', '20000'))

    # ---- 登录鉴权 ----
    # 平台需要对外提供独立账号，因此内置登录能力。
    # 账号格式：'用户名:口令,用户名:口令'。部署时务必改掉默认口令。
    AUTH_USERS: str = os.getenv(
        'EDM_AUTH_USERS',
        'reviewer:ict2026,admin:admin2026',
    )
    AUTH_TTL_SECONDS: int = int(os.getenv('EDM_AUTH_TTL', str(12 * 3600)))  # 12 小时

    # ---- 查询效率约束（评分要求 <1s，含前端渲染）----
    # 这些上限同时保护 2C4G 云主机的内存与前端渲染性能
    DEFAULT_PAGE_SIZE: int = 20
    MAX_PAGE_SIZE: int = 200
    MAX_GRAPH_NODES: int = 300        # 图谱最大节点数，超出的在图上折叠
    MAX_TABLE_ROWS: int = 2000        # 单次返回的最大表格行数
    MAX_SUGGEST: int = 20             # 实体自动补全返回条数

    # ---- CORS（开发期前端跑在 Vite dev server 上）----
    CORS_ORIGINS: list = os.getenv(
        'EDM_CORS_ORIGINS',
        'http://localhost:5173,http://127.0.0.1:5173'
    ).split(',')

    # ---- 静态前端 ----
    # 生产环境把 `npm run build` 的产物放到这里，由 FastAPI 直接托管，
    # 省掉一个 Nginx 进程（2C4G 上内存很宝贵）
    STATIC_DIR: Path = Path(os.getenv('EDM_STATIC_DIR', str(BASE_DIR / 'static')))


settings = Settings()
