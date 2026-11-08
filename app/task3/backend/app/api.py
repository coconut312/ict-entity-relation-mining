# -*- coding: utf-8 -*-
"""HTTP 路由层。

原则：**这一层只做参数校验与响应封装，不含任何业务逻辑。**
业务在 services.py，数据在 repositories.py —— 这样队友合并自己的模块时
不需要理解 HTTP 细节。

路由分两组：
    router            公开：健康检查、登录、登出
    router_protected  需登录：数据集、标的检索、场景分析、实体补全

除健康检查与登录外，全部接口都需要 Bearer token。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     Request, UploadFile)
from fastapi.security import HTTPAuthorizationCredentials

from .auth import (ROLE_WRITE, auth_service, bearer, current_user, require_roles)
from .config import settings
from .scenarios_def import SCENARIOS
from .schemas import (DatasetRestore, DatasetStats, EntitySuggestItem, HealthOut,
                      ImportJob, Item, ItemFacets, ItemFilter, LoginIn, LoginOut,
                      Page, Scenario1Out, Scenario2Out, Scenario3Out, Scenario4Out,
                      Scenario5Out, ScenarioDef, UserInfo)
from .services import (ImportBusyError, RestoreBusyError, UploadTooLargeError,
                       dataset_service, item_service, scenario_service)
from .settings_api import router as settings_router
from .export_api import router as export_router

router = APIRouter()


# ==================================================================
# 鉴权依赖
# ==================================================================

# 校验逻辑只有一份，在 auth.py（`current_user`）—— 这里只保留旧名字，
# 既有 `Depends(require_auth)` 与文档说明都不变。
require_auth = current_user


# 需要登录的接口统一挂在这个 router 上
router_protected = APIRouter(dependencies=[Depends(require_auth)])


# ==================================================================
# 上传体积：**落盘之前**就按 Content-Length 拒绝
# ==================================================================

# multipart 的表单头 / 边界开销（远小于 1 MB，留足余量即可）。
_MULTIPART_SLACK = 1048576


def _reject_oversized_request(request: Request) -> None:
    """按 `Content-Length` 在**读请求体之前**拒绝超限上传（HTTP 413）。

    为什么要有这一步：改造前是「先 `save_upload()` 整个落盘，再看文件大小」——
    2048 MB 的上限形同虚设：2C4G 机器上磁盘先被写满、接口才回 413。
    这里在拿到请求头时就判，超限**立刻** 413，一个字节都不落盘。

    流式兜底在 `DatasetService.save_upload()`（`UploadTooLargeError`）：
    客户端可以不发 Content-Length（chunked），那条路径按字节计数同样会拦。
    """
    raw = request.headers.get('content-length')
    if not raw:
        return                       # 无 Content-Length（chunked）-> 交给流式计数
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return
    limit = settings.MAX_UPLOAD_MB * 1048576
    if n > limit + _MULTIPART_SLACK:
        raise HTTPException(
            status_code=413,
            detail=f'上传内容 {n / 1048576:.0f} MB 超出上限 '
                   f'{settings.MAX_UPLOAD_MB} MB（未落盘即拒绝）')



# ==================================================================
# 系统（公开）
# ==================================================================

@router.get('/health', response_model=HealthOut, tags=['系统'],
            summary='服务状态')
def health() -> HealthOut:
    return HealthOut(version=settings.VERSION,
                     data_backend=settings.DATA_BACKEND,
                     has_data=dataset_service.has_data())


# ==================================================================
# 鉴权（公开）
# ==================================================================

@router.post('/auth/login', response_model=LoginOut, tags=['系统'],
             summary='登录')
def login(body: LoginIn) -> LoginOut:
    token = auth_service.login(body.username, body.password)
    if token is None:
        raise HTTPException(status_code=401, detail='用户名或口令不正确')
    return LoginOut(token=token,
                    username=body.username,
                    role=auth_service.role_of(body.username),
                    expires_in=settings.AUTH_TTL_SECONDS)


@router.post('/auth/logout', tags=['系统'], summary='退出登录')
def logout(cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer)):
    if cred:
        auth_service.logout(cred.credentials)
    return {'ok': True}


@router.get('/auth/me', response_model=UserInfo, tags=['系统'],
            summary='当前用户')
def me(user: str = Depends(require_auth)) -> UserInfo:
    return UserInfo(username=user,
                    role=auth_service.role_of(user),
                    auth_enabled=auth_service.enabled)


# ==================================================================
# 数据集（需登录）
# ==================================================================

# 上传入库模式：replace = 覆盖（默认，兼容旧行为）；append = 保留原数据并追加
IMPORT_MODES = ('replace', 'append')


def _begin_import_or_409() -> str:
    """占用「导入中」名额，返回本次占用的 token；拿不到立刻 409（不排队、不并发）。

    锁的释放不在 HTTP 层：它随后台处理线程结束（`DatasetService.start_import`
    里凭 token 释放）或本函数之后的异常路径释放（见两个上传端点的
    `except BaseException`）。
    """
    try:
        return dataset_service.begin_import()
    except ImportBusyError as e:
        raise HTTPException(status_code=409,
                            detail=str(e) or '正在导入，请稍候')


def _resolve_import_mode(mode_form: Optional[str], mode_query: Optional[str]) -> str:
    """确定本次上传的入库模式。

    查询参数与表单字段都接受（`mode` 同名，分别来自 query / form），表单优先 ——
    前端用 FormData 上传时把 mode 放表单里更自然，用查询串更直观，两种都支持。
    非法取值返回 400，而不是静默按 replace 处理（避免用户以为数据被追加了）。
    """
    raw = mode_form if mode_form not in (None, '') else mode_query
    resolved = (raw or 'replace').strip().lower()
    if resolved not in IMPORT_MODES:
        raise HTTPException(
            status_code=400,
            detail=f'mode 只支持 {" / ".join(IMPORT_MODES)}，收到 {raw!r}')
    return resolved


@router_protected.get('/dataset/stats', response_model=DatasetStats, tags=['数据集'],
                      summary='数据概览')
def dataset_stats() -> DatasetStats:
    return dataset_service.stats()


@router_protected.post('/dataset/import', response_model=ImportJob, tags=['数据集'],
                       summary='上传数据集并自动处理',
                       # 上传 = 整库覆盖：与 /dataset/restore 同级，只允许 admin / reviewer。
                       # 改造前这里只挂 require_auth，于是任意 `user`（只读）账号都能上传
                       # 一个小数据集把整库覆盖掉（P1：guest 可整库覆盖）。
                       dependencies=[Depends(require_roles(*ROLE_WRITE))],
                       responses={403: {'description': '权限不足：需要 admin / reviewer 角色'},
                                  409: {'description': '正在导入，请稍候（已有一次导入在进行）'},
                                  413: {'description': '上传内容超出上限'}})
async def dataset_import(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Query(
        'replace',
        description='入库模式：replace（默认，覆盖当前平台数据）'
                    '| append（保留当前平台数据，把本次解析结果追加到其后）'),
    mode_form: Optional[str] = Form(
        None, alias='mode',
        description='与查询参数 mode 等价，便于 multipart 表单直接带 mode 字段'),
) -> ImportJob:
    """接收 zip 数据集，落盘后启动处理管线。

    接收压缩包后落盘并启动处理管线，处理耗时从落盘完成时刻起算，
    至全部结果入库为止。

    **入库模式**（`mode` 可用查询参数或表单字段传，二者都支持）：
      * `replace`（默认，兼容改造前的行为）：清空后重建，库里只剩本次数据集；
      * `append`：保留库里已有数据，把本次解析结果**按行去重后追加**到其后。

    **并发**：整条管线（解包 -> 抽取 -> 整库重建）**不允许并发跑两份** ——
    两个 ingest 同时写库会把库写坏（实测 `table t1_item already exists` /
    `os.replace PermissionError`，之后库 `no such table`）。因此这里用一把
    **非阻塞**导入锁：拿不到立刻 409「正在导入，请稍候」，不排队。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail='缺少文件名')
    # 模式与体积先校验再落盘：非法取值 / 超限都不留垃圾文件
    resolved = _resolve_import_mode(mode_form, mode)
    _reject_oversized_request(request)

    # 「导入中」名额：**任何出口都释放且只释放一次**。
    #
    # 改造前的写法（已确认的缺陷 P0）：`token = _begin_import_or_409()` 在 try 之外，
    # 而 413 是在 `except UploadTooLargeError` 里 `raise HTTPException(413)` 抛出的 ——
    # **同一个 try 的后续 except 接不住「另一个 except 块里抛出的异常」**，于是
    # `except BaseException: end_import(token)` 永远不会执行，名额永久被占：
    # 之后所有上传都 409（只能重启进程）。实测：EDM_MAX_UPLOAD_MB=8 时传 10 MB
    # （无 Content-Length 的流式上传走 `save_upload()` 的分块计数）-> 413，
    # 紧接着传一个合法小包 -> 409，再也传不上去。
    #
    # 修法（与 /dataset/import-folder 同一结构）：
    #   * token 先置 None，`begin_import` 也在 try 里 —— 它抛 ImportBusy 时 token 仍是
    #     None，finally 不会误放别人的名额；
    #   * `finally` 里 `if token is not None: end_import(token)` —— 覆盖
    #     「413 / 400 / 409 / 任意异常 / BaseException」的所有出口；
    #   * 名额移交给后台线程后立刻 `token = None`（所有权已转移），
    #     否则 finally 会把后台线程正持有的名额提前放掉（= 并发保护失效）。
    token: Optional[str] = None
    try:
        token = _begin_import_or_409()
        path = dataset_service.save_upload(file.filename, file.file)
        job = dataset_service.start_import(path, file.filename, mode=resolved,
                                           lock_token=token)
        token = None                               # 名额所有权已移交后台线程
        return job
    except UploadTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))
    finally:
        if token is not None:
            dataset_service.end_import(token)      # 起线程之前失败 -> 立刻放锁（仅一次）



@router_protected.post('/dataset/import-folder', response_model=ImportJob,
                       tags=['数据集'],
                       summary='上传文件夹（多文件 + 相对路径）并自动处理',
                       # 与 /dataset/import 同级：整库覆盖，只允许 admin / reviewer
                       dependencies=[Depends(require_roles(*ROLE_WRITE))],
                       responses={403: {'description': '权限不足：需要 admin / reviewer 角色'},
                                  409: {'description': '正在导入，请稍候（已有一次导入在进行）'},
                                  413: {'description': '上传内容超出上限'}})
async def dataset_import_folder(
    request: Request,
    files: List[UploadFile] = File(
        ..., description='同一文件夹下的所有文件；multipart 中可重复该字段'),
    paths: Optional[List[str]] = Form(
        None, description='与 files **一一对应**的相对路径（形如 sub/dir/a.html）；'
                          '不传时回退用每个文件的 filename'),
    mode: str = Query(
        'replace',
        description='入库模式：replace（默认，覆盖当前平台数据）'
                    '| append（保留当前平台数据，把本次解析结果追加到其后）'),
    mode_form: Optional[str] = Form(
        None, alias='mode',
        description='与查询参数 mode 等价，便于 multipart 表单直接带 mode 字段'),
) -> ImportJob:
    """接收**文件夹**（多个文件 + 各自相对路径），还原目录树后启动同一条处理管线。

    与单文件入口 `POST /dataset/import` 的关系：
      * 单文件入口**保持原样不动**（老前端/老脚本继续可用）；
      * 本入口只是多了一层「把 N 个文件按相对路径还原成一棵临时目录树」，
        之后交给**同一个** `dataset_service.start_import()` + 同一条管线，
        因此 4 条识别路线、replace/append 语义、进度字段完全一致。

    **相对路径**：浏览器（`<input webkitdirectory>` 与拖文件夹）会把每个文件的
    相对路径放在 `filename`（我们前端同时并行传一份 `paths` 表单字段，双保险）。
    形如 `sub/dir/a.html`。服务端**先校验全部路径再落盘**：
    绝对路径 / 盘符 / `..` 穿越 / Windows 保留名一律整体拒绝（400），
    不会在上传目录之外写出任何文件。
    """
    if not files:
        raise HTTPException(status_code=400, detail='没有收到任何文件')
    # 模式与体积先校验：非法取值不留垃圾目录，超限一个字节都不落盘
    resolved = _resolve_import_mode(mode_form, mode)
    _reject_oversized_request(request)

    rels = [p for p in (paths or []) if p is not None]
    if rels and len(rels) != len(files):
        raise HTTPException(
            status_code=400,
            detail=f'paths（{len(rels)} 个）与 files（{len(files)} 个）数量不一致，'
                   f'无法确定每个文件的相对路径')

    items = [((rels[i] if rels else None) or f.filename or '', f.file)
             for i, f in enumerate(files)]
    token = _begin_import_or_409()                 # 拿不到 -> 409（正在导入）
    try:
        try:
            dest, rel_paths, n_bytes = dataset_service.save_folder(items)
        except UploadTooLargeError as e:
            raise HTTPException(status_code=413, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        top = rel_paths[0].split('/')[0] if '/' in rel_paths[0] else '（散件）'
        label = (f'文件夹 {top}（{len(rel_paths)} 个文件 / '
                 f'{n_bytes / 1048576:.1f} MB）')
        return dataset_service.start_import(dest, label, mode=resolved,
                                            lock_token=token)
    except BaseException:
        dataset_service.end_import(token)          # 起线程之前失败 -> 立刻放锁
        raise


@router_protected.get('/dataset/import/{job_id}', response_model=ImportJob,
                      tags=['数据集'], summary='处理进度')
def dataset_import_status(job_id: str) -> ImportJob:
    job = dataset_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail='导入任务不存在')
    return job


@router_protected.post('/dataset/restore', response_model=DatasetRestore,
                       tags=['数据集'],
                       summary='恢复默认数据（重建仓库内置完整交付集）',
                       dependencies=[Depends(require_roles(*ROLE_WRITE))],
                       responses={
                           403: {'description': '权限不足：需要 admin / reviewer 角色'},
                           409: {'description': '正在恢复中，请稍候（已有一次恢复在进行）'},
                           500: {'description': '恢复失败，detail 为可直接展示的中文原因'},
                       })
def dataset_restore() -> DatasetRestore:
    """用**仓库内置的完整交付集**覆盖当前平台数据，并把图库重建为全量图。

    用途：用户上传小样本（例如 3 篇 / 12 条）后，一键把平台恢复成
    交付时的完整数据集（**1038 篇公告 / 6244 条标的**），
    随后 `GET /dataset/stats` 与五个场景（S1–S5）都回到完整结果。

    做的事（全部在服务进程内完成）：
      1. `platform/ingest.py` 的 `ingest()` 用 `app/deliverables/` 整库重建平台库；
      2. 图库后端是 Neo4j 时（启动日志 `图库后端：neo4j`），
         再用 `scripts/build_neo4j_graph.py` 同一份代码把图重建为全量图
         （节点 5,645 / 关系 14,143）。

    **耗时取决于机器与图库规模**：本机（本地 Neo4j）实测约 1.5–2 秒；
    2C4G 云主机 / 冷缓存时可能数十秒。前端按钮期间显示「正在恢复…」。

    响应：`{ok, n_docs, n_items, graph_backend, nodes, rels, elapsed_ms}`。
      * `n_docs` / `n_items`：入库后的**实际**行数；
      * `nodes` / `rels`：Neo4j 图的**真实**节点/关系数（SQLite 图库时为 `null`）。

    错误：
      * **403**「权限不足」—— 该操作会重建整库 + 图库，只允许 admin / reviewer；
      * **409**「正在恢复中，请稍候」—— 已有一次恢复在跑（进程内锁，不允许并发）；
      * **409**「正在导入，请稍候」—— 已有一次数据集导入在跑（两者共用同一把库锁）；
      * **500** —— 重建失败，`detail` 里是可直接展示的中文原因
        （图库失败时会说明「平台库已重建」）。
    """
    try:
        result = dataset_service.restore_defaults()
    except RestoreBusyError as e:
        raise HTTPException(status_code=409,
                            detail=str(e) or '正在恢复中，请稍候')
    except ImportBusyError as e:
        raise HTTPException(status_code=409,
                            detail=str(e) or '正在导入，请稍候')
    except Exception as e:                       # noqa: BLE001
        # 失败必须是可读的中文原因（前端会直接展示 detail），而不是三段式 500
        raise HTTPException(status_code=500,
                            detail=f'恢复失败：{type(e).__name__}: {e}')
    return DatasetRestore(**result)


# ==================================================================
# 标的检索（需登录）
# ==================================================================

@router_protected.get('/items', response_model=Page[Item],
                      tags=['标的检索'],
                      summary='标的多条件检索')
def list_items(
    keyword: str = Query('', description='关键词'),
    brand: str = '',
    category: str = '',
    supplier: str = '',
    purchaser: str = '',
    date_from: str = '',
    date_to: str = '',
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    sort_by: str = 'doc_id',
    sort_order: str = 'asc',
    page: int = 1,
    page_size: int = Query(20, ge=1, le=200),
):
    flt = ItemFilter(keyword=keyword, brand=brand, category=category,
                     supplier=supplier, purchaser=purchaser,
                     date_from=date_from, date_to=date_to,
                     price_min=price_min, price_max=price_max,
                     sort_by=sort_by, sort_order=sort_order)
    rows, meta = item_service.search(flt, page, page_size)
    return Page[Item](items=[Item(**r) for r in rows], meta=meta)


@router_protected.get('/items/facets', response_model=ItemFacets,
                      tags=['标的检索'], summary='筛选项取值')
def item_facets() -> ItemFacets:
    return item_service.facets()


# ==================================================================
# 场景分析（需登录）
# ==================================================================

@router_protected.get('/scenarios', response_model=List[ScenarioDef],
                      tags=['场景分析'],
                      summary='场景定义')
def list_scenarios() -> List[ScenarioDef]:
    return SCENARIOS


@router_protected.get('/scenarios/s1', response_model=Scenario1Out,
                      tags=['场景分析'],
                      summary='长期合作')
def scenario1(purchaser: str = Query(..., description='采购方'),
              min_count: int = Query(1, ge=1),
              top_n: int = Query(20, ge=1, le=200)):
    return scenario_service.run_s1(purchaser, min_count, top_n)


@router_protected.get('/scenarios/s2', response_model=Scenario2Out,
                      tags=['场景分析'],
                      summary='投标画像')
def scenario2(purchaser: str = Query(..., description='采购方'),
              top_n: int = Query(5, ge=1, le=50)):
    return scenario_service.run_s2(purchaser, top_n)


@router_protected.get('/scenarios/s3', response_model=Scenario3Out,
                      tags=['场景分析'],
                      summary='竞对分析')
def scenario3(supplier: str = Query(..., description='供应商'),
              top_n: int = Query(5, ge=1, le=50)):
    return scenario_service.run_s3(supplier, top_n)


@router_protected.get('/scenarios/s4', response_model=Scenario4Out,
                      tags=['场景分析'],
                      summary='共同客户')
def scenario4(suppliers: List[str] = Query(..., description='供应商，可重复传参'),
              min_count: int = Query(1, ge=1),
              top_n: int = Query(20, ge=1, le=200)):
    return scenario_service.run_s4(suppliers, min_count, top_n)


@router_protected.get('/scenarios/s5', response_model=Scenario5Out,
                      tags=['场景分析'],
                      summary='共同项目')
def scenario5(suppliers: List[str] = Query(..., description='供应商，可重复传参'),
              top_n: int = Query(50, ge=1, le=500)):
    return scenario_service.run_s5(suppliers, top_n)


# ==================================================================
# 实体（需登录）
# ==================================================================

@router_protected.get('/entities/suggest', response_model=List[EntitySuggestItem],
                      tags=['实体'], summary='实体搜索建议')
def suggest_entities(q: str = '', type: str = '', limit: int = 20):
    return item_service.suggest(q, type, limit)


# ==================================================================
# 模型配置 / API Key（需登录，实现见 settings_api.py）
# ==================================================================

# 挂到 router_protected 下即继承 require_auth；再显式传一次 dependencies，
# 不依赖「父 router 依赖是否向下传递」这一实现细节。
router_protected.include_router(settings_router,
                                dependencies=[Depends(require_auth)])

# 提取结果下载（官方《输出标准样例表》格式）：规范第二条要求
router_protected.include_router(export_router,
                                dependencies=[Depends(require_auth)])
