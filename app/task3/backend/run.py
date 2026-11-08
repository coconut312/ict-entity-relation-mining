# -*- coding: utf-8 -*-
"""任务三平台启动器（已接入任务一 / 任务二真实数据）。

用法：
    uv run python task3/backend/run.py --port 8000
或（等价，先设好环境变量）：
    set EDM_DATA_BACKEND=real
    set EDM_ITEM_REPOSITORY=integrations.repos:SQLiteItemRepository
    set EDM_GRAPH_REPOSITORY=integrations.repos:SQLiteGraphRepository
    uvicorn app.main:app --port 8000

环境变量（均可在启动前覆盖）：
    EDM_DATA_BACKEND=real                    标记已接入真实数据
    EDM_ITEM_REPOSITORY / EDM_GRAPH_REPOSITORY  仓储实现（契约接入点 1 / 2）
    EDM_AUTH_USERS=reviewer:ict2026,admin:admin2026  登录账号（评审方用 reviewer）
    EDM_SQLITE_PATH                          交付服务库位置（默认 app/out/platform/platform.db）
    EDM_AVG_PROCESS_MS                       单篇平均处理耗时（毫秒，默认 1200）
    EDM_PIPELINE_LLM=1                       上传数据集时启用 LLM 抽取（默认仅规则层）
    EDM_PORT / EDM_HOST                      监听地址
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # app/task3/backend
APP = HERE.parents[1]                           # app
sys.path.insert(0, str(HERE))                   # 使 app.* 与 integrations.* 可导入


def _set_env_defaults() -> None:
    os.environ.setdefault('EDM_DATA_BACKEND', 'real')
    os.environ.setdefault('EDM_ITEM_REPOSITORY', 'integrations.repos:SQLiteItemRepository')
    # 图库后端默认 **Neo4j**（赛题 p1 推荐）；可用 EDM_GRAPH_REPOSITORY 覆盖回 SQLite。
    os.environ.setdefault('EDM_GRAPH_REPOSITORY',
                          'integrations.neo4j_repo:Neo4jGraphRepository')
    os.environ.setdefault('EDM_AUTH_USERS', 'reviewer:ict2026,admin:admin2026')
    os.environ.setdefault('EDM_SQLITE_PATH', str(APP / 'out' / 'platform' / 'platform.db'))
    os.environ.setdefault('EDM_AVG_PROCESS_MS', '1200')
    # 平台**只用页面「模型配置」里填的 API Key**，不读环境变量 / .env（用户要求）。
    os.environ.setdefault('EDM_KEY_SOURCE', 'ui')
    # Neo4j 连接默认值（口令是本机开发实例；不写进前端、不进日志）。
    os.environ.setdefault('NEO4J_URI', 'bolt://127.0.0.1:7687')
    os.environ.setdefault('NEO4J_USER', 'neo4j')
    os.environ.setdefault('NEO4J_PASSWORD', 'ict2026neo4j')


NEO4J_GUIDE = """\
  启动 Neo4j（本机 Community 5.25.1，未注册 Windows 服务，需保持窗口不关）：
      D:\\neo4j\\neo4j-community-5.25.1\\bin\\neo4j.bat console
    Browser: http://localhost:7474   bolt: 127.0.0.1:7687   账号: neo4j / ict2026neo4j
  重建图数据（幂等）：
      cd {app}
      $env:NEO4J_PASSWORD="ict2026neo4j"
      uv run python scripts\\build_neo4j_graph.py --load --wipe --export
  交叉验证（期望 可比项 11 项、一致 11 项）：
      uv run python scripts\\verify_neo4j.py"""


def _neo4j_ready(timeout: float = 3.0) -> tuple[bool, str]:
    """探测 Neo4j 是否可用（bolt 连接 + 一次轻查询）。不可用时**回退 SQLite**，不阻塞启动。"""
    try:
        from neo4j import GraphDatabase          # noqa: PLC0415
        drv = GraphDatabase.driver(os.environ['NEO4J_URI'],
                                   auth=(os.environ['NEO4J_USER'],
                                         os.environ['NEO4J_PASSWORD']),
                                   connection_timeout=timeout)
        with drv.session() as s:
            s.run('RETURN 1 AS ok').single()
        drv.close()
        return True, ''
    except Exception as e:                       # noqa: BLE001
        return False, f'{type(e).__name__}: {str(e)[:120]}'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default=os.getenv('EDM_HOST', '127.0.0.1'))
    ap.add_argument('--port', type=int, default=int(os.getenv('EDM_PORT', '8000')))
    ap.add_argument('--reload', action='store_true', help='开发模式热重载')
    a = ap.parse_args()

    _set_env_defaults()                         # 必须在 import app.config 之前

    # ---- 图库后端：默认 Neo4j；连不上则打印指引并回退 SQLite（服务照常可用）----
    # 注意：ACTIVE 必须**按实际配置**判定 —— 若用户显式把仓储设成 SQLite，绝不能标成 neo4j，
    # 否则「上传后同步图」会误把共享 Neo4j 图重置为本次上传的小数据集（曾发生过一次）。
    _repo = os.environ['EDM_GRAPH_REPOSITORY']
    if 'neo4j_repo' in _repo:
        ok, why = _neo4j_ready()
        if ok:
            graph_backend = 'neo4j'
        else:
            graph_backend = 'sqlite'
            os.environ['EDM_GRAPH_REPOSITORY'] = 'integrations.repos:SQLiteGraphRepository'
            print('\n[警告] Neo4j 不可用（%s）→ 已回退 SQLite 图库，平台仍可使用。\n' % why)
            print(NEO4J_GUIDE.format(app=APP))
            print()
    else:
        graph_backend = 'sqlite'          # 显式 SQLite（含副本/测试实例）→ 不同步共享图
    os.environ['EDM_GRAPH_BACKEND_ACTIVE'] = graph_backend
    # 二次保险：副本库（非默认平台库）永不触发图同步，避免测试污染共享 Neo4j。
    _default_db = str((APP / 'out' / 'platform' / 'platform.db').resolve())
    try:
        _cur_db = str(Path(os.environ['EDM_SQLITE_PATH']).resolve())
    except Exception:                        # noqa: BLE001
        _cur_db = ''
    if _cur_db and _cur_db != _default_db:
        os.environ['EDM_GRAPH_SYNC'] = '0'

    from integrations.pipeline_impl import install   # noqa: PLC0415
    install(use_llm=os.getenv('EDM_PIPELINE_LLM') == '1')

    import uvicorn                              # noqa: PLC0415
    from app.config import settings             # noqa: PLC0415
    from app.main import app                    # noqa: PLC0415

    db = Path(os.environ['EDM_SQLITE_PATH'])
    print(f'任务三平台（已接入真实数据）：http://{a.host}:{a.port}/')
    print(f'  接口文档：http://{a.host}:{a.port}/api/docs')
    print(f'  数据后端：{settings.DATA_BACKEND} | 服务库：{db} '
          f'{"(存在)" if db.exists() else "(缺失! 先运行 uv run python platform/ingest.py)"}')
    print(f'  图库后端：{graph_backend}'
          + ('（Neo4j，bolt 已连通）' if graph_backend == 'neo4j' else '（SQLite 回退）'))
    print(f'  模型 Key 来源：{os.environ.get("EDM_KEY_SOURCE")}'
          + ('（仅用页面「模型配置」填写的 Key）' if os.environ.get('EDM_KEY_SOURCE') == 'ui' else ''))
    print(f'  登录账号：{settings.AUTH_USERS.split(",")[0].split(":")[0]} / '
          f'{"*" * len(settings.AUTH_USERS.split(",")[0].split(":")[1])}')
    uvicorn.run('app.main:app' if a.reload else app, host=a.host, port=a.port,
                reload=a.reload, log_level='info')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
