# -*- coding: utf-8 -*-
"""FastAPI 应用入口。

启动：
    cd backend
    uvicorn app.main:app --host 0.0.0.0 --port 8000

生产（2 核 4G 云主机）建议：
    * 只开 **1 个 worker**（内存有限，且评测只有 1 台机器）
    * 前端 `npm run build` 的产物放到 backend/static，由本服务直接托管，
      省掉一个 Nginx 进程
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router, router_protected
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
)
log = logging.getLogger('edm')

app = FastAPI(
    title=settings.APP_NAME,
    version=__version__,
    description=(
        '招采公告数据结构化与关系分析平台\n\n'
        '**使用顺序**：先调 `POST /auth/login` 获取 token，'
        '再点页面右上角 **Authorize** 填入 token，之后即可测试其余接口。'
    ),
    docs_url='/api/docs',        # Swagger UI：可交互，能直接发请求
    redoc_url='/api/redoc',      # ReDoc：只读，排版更适合通读
    openapi_url='/api/openapi.json',
)

# 开发期前端跑在 Vite dev server（5173），需要跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

app.include_router(router, prefix=settings.API_PREFIX)
app.include_router(router_protected, prefix=settings.API_PREFIX)


# ---- 上传体积：**请求体被读之前**就按 Content-Length 拒绝（HTTP 413）----
# 为什么必须是中间件：FastAPI 的 `file: UploadFile = File(...)` 参数会在**调用
# 端点函数之前**把 multipart 整个读进来（超过 1 MB 还会落到临时文件上）。
# 改造前只有端点里「先 save_upload() 再比对大小」—— 2048 MB 上限形同虚设，
# 磁盘先被写满、接口才回 413。中间件在 body 尚未被读时就能拿到 Content-Length，
# 超限**立刻**返回 413，一个字节都不落盘（端点里那层是流式兜底，见 api.py）。
_UPLOAD_PATHS = (f'{settings.API_PREFIX}/dataset/import',
                 f'{settings.API_PREFIX}/dataset/import-folder')
_UPLOAD_SLACK = 1048576          # multipart 边界/表头余量


@app.middleware('http')
async def reject_oversized_upload(request: Request, call_next):
    if request.method == 'POST' and request.url.path in _UPLOAD_PATHS:
        raw = request.headers.get('content-length')
        limit = settings.MAX_UPLOAD_MB * 1048576
        if raw:
            try:
                n = int(raw)
            except (TypeError, ValueError):
                n = 0
            if n > limit + _UPLOAD_SLACK:
                log.warning('拒绝超限上传：%s %s %.0f MB > %s MB',
                            request.method, request.url.path,
                            n / 1048576, settings.MAX_UPLOAD_MB)
                return JSONResponse(
                    status_code=413,
                    content={'code': 'payload_too_large',
                             'message': f'上传内容 {n / 1048576:.0f} MB 超出上限 '
                                        f'{settings.MAX_UPLOAD_MB} MB（未落盘即拒绝）',
                             'detail': f'content-length={n} > {limit}+{_UPLOAD_SLACK}'})
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """统一异常响应。

    图谱查询失败时前端应优雅降级而不是白屏，因此错误体固定为
    {code, message, detail} 三段式。
    """
    log.exception('未处理异常 %s %s', request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={'code': 'internal_error',
                 'message': '服务内部错误，请稍后重试',
                 'detail': f'{type(exc).__name__}: {exc}'},
    )


# ---- 静态前端托管（生产模式）----
# 开发期由 Vite 提供页面，这里是否存在目录都不影响。
if settings.STATIC_DIR.exists():
    assets = settings.STATIC_DIR / 'assets'
    if assets.exists():
        app.mount('/assets', StaticFiles(directory=str(assets)), name='assets')

    @app.get('/', include_in_schema=False)
    @app.get('/{path:path}', include_in_schema=False)
    async def spa(path: str = ''):
        """SPA 前端路由回退：所有非 API 路径都返回 index.html。"""
        if path.startswith('api/'):
            return JSONResponse(status_code=404,
                                content={'code': 'not_found', 'message': '接口不存在'})
        index = settings.STATIC_DIR / 'index.html'
        if index.exists():
            return FileResponse(str(index))
        return JSONResponse(status_code=404, content={'code': 'not_found',
                                                      'message': '前端未构建'})
else:
    @app.get('/', include_in_schema=False)
    async def root_hint():
        return {
            'app': settings.APP_NAME,
            'version': __version__,
            'hint': '前端尚未构建。开发期请运行 frontend 的 npm run dev；'
                    '生产构建请执行 npm run build 并把 dist 复制到 backend/static。',
            'api_docs': '/api/docs',
        }
