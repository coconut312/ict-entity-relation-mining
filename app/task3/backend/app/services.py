# -*- coding: utf-8 -*-
"""业务服务层：检索、场景计算、导入编排。

这一层只依赖「仓储抽象」，不关心数据存在 PostgreSQL、Neo4j 还是内存里，
因此队友替换仓储实现时这里完全不用改。
"""
from __future__ import annotations

import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .config import settings
from .pipeline import PipelineContext, get_pipeline
from .repositories import (get_graph_repository, get_item_repository,
                           reset_repositories)
from .schemas import (DatasetStats, EntitySuggestItem, ImportJob,
                      ItemFacets, ItemFilter, PageMeta, Scenario1Out,
                      Scenario2Out, Scenario3Out, Scenario4Out, Scenario5Out)


# ==================================================================
# 数据集 / 导入
# ==================================================================

class UploadTooLargeError(ValueError):
    """上传内容超出大小上限（HTTP 层转 413，与单文件上传一致）。"""


class ImportBusyError(RuntimeError):
    """已有一次数据集导入正在进行中（HTTP 层转 409）。

    为什么必须串行：`ingest()` 是**整库重建**（DROP/CREATE/INSERT 一整套），
    两个导入并发跑会互相踩同一个临时库与 `os.replace`。实测（改造前）的后果：
    一个线程报 `table t1_item already exists`、另一个报 `os.replace PermissionError`，
    最终库变成 **no such table: t1_notice**（废库，页面全部 500）。

    并发保护是这里的一把**非阻塞**名额（`begin_import`/`end_import`）：
    第二个请求**立刻**收到 409，而不是排队等第一个跑完再把库重建一遍。
    """


class RestoreBusyError(RuntimeError):
    """已有一次「恢复默认数据」正在进行中（HTTP 层转 409）。

    恢复是「整库重建 + 图库重写」：两份并发跑会互相踩（两个 ingest 同时写
    `platform.db.tmp` 再 `os.replace`，图库也会被两次 wipe 打花），
    因此第二个请求必须**立刻**收到 409，而不是排队等 60 秒后再把库再重建一遍。
    """


class DatasetService:
    """数据集管理与导入编排。"""

    # 入库模式：replace = 覆盖（默认，兼容旧行为）；append = 保留原数据并追加
    MODES: tuple = ('replace', 'append')

    # Windows 保留设备名：这些名字写成文件会落到**设备**而不是普通文件上，一律拒绝。
    # 只在路径校验里用（与平台是 Windows 还是 Linux 无关，拒绝了不吃亏）。
    _RESERVED_NAMES = frozenset(
        ['CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$']
        + [f'COM{i}' for i in range(1, 10)]
        + [f'LPT{i}' for i in range(1, 10)]
    )

    def __init__(self) -> None:
        self._jobs: Dict[str, ImportJob] = {}
        self._lock = threading.Lock()
        # 「恢复默认数据」专用锁：**整库重建 + 图库重写**绝不能并发跑两份。
        # 用非阻塞 acquire()：拿到锁的请求干活，第二个请求立刻 409（见 restore_defaults）。
        self._restore_lock = threading.Lock()
        # 「导入中」名额：导入与恢复**共用**（两者都会整库重建 platform.db）。
        # 值是本次占用者的 **token**（而不是布尔）：后台线程结束时必须凭 token
        # 释放 —— 否则「上一轮导入的线程晚了一拍才跑完 finally」就会把**下一轮**
        # 刚拿到的名额清掉（实测：两次并发上传都拿到 200，因为第 3 次顺序导入的
        # 线程在 A 拿到名额之后才执行 end_import）。
        self._import_active: Optional[str] = None

    # ---- 导入名额（非阻塞；拿不到立刻 409）----
    def begin_import(self) -> str:
        """占住「导入中」名额，返回本次占用的 token；已被占用时**立刻**抛异常（不排队）。"""
        with self._lock:
            if self._import_active is not None:
                raise ImportBusyError('正在导入，请稍候')
            token = uuid.uuid4().hex
            self._import_active = token
            return token

    def end_import(self, token: Optional[str] = None) -> None:
        """释放「导入中」名额（幂等 + **按 token 归属**）。

        * 传 token：只有 token 与当前占用者一致才释放（后台线程用它，永远不会
          误放别人的名额）；
        * 不传 token：无条件释放（只给「起线程之前就失败」的异常路径用）。
        """
        with self._lock:
            if token is None or self._import_active == token:
                self._import_active = None

    @property
    def importing(self) -> bool:
        return self._import_active is not None

    # ---- 概览 ----
    def stats(self) -> DatasetStats:
        return get_item_repository().stats()

    def has_data(self) -> bool:
        try:
            return get_item_repository().has_data()
        except Exception:
            return False

    # ---- 恢复默认数据（仓库内置完整交付集）----
    def restore_defaults(self) -> dict:
        """用仓库内置的完整交付集重建平台库（+ Neo4j 全量图）。

        实现要点：
          * **进程内调用** `platform/ingest.py::ingest()`（见
            `integrations.pipeline_impl.restore_full_delivery`），不 subprocess：
            少一次解释器启动，也不受 cwd 影响（交付集路径是绝对路径）；
          * 并发保护是这里的一把非阻塞锁 —— 第二个请求马上拿到
            `RestoreBusyError`（HTTP 409），不会排队把库再重建一遍；
          * 无论成功失败都 `reset_repositories()`：失败时库文件也可能已被替换，
            仓储缓存必须失效，否则页面还会读到旧实例。
        """
        if not self._restore_lock.acquire(blocking=False):
            raise RestoreBusyError('正在恢复中，请稍候')
        # 与上传导入共用同一个「整库重建」名额：恢复与导入并发 = 两个 ingest 抢写
        # 同一个 platform.db，正是 P1-1 那种把库写坏的场景。
        #
        # ⚠️ `begin_import()` **必须在 try 里**（已确认的缺陷 P1：锁泄漏）：
        # 改造前它是 `token = self.begin_import()` 写在 try **之前**，一旦它抛
        # `ImportBusyError`（已有导入在跑）就**跳过 finally** —— `_restore_lock`
        # 被这次请求永久占住、再也不释放，于是「恢复默认数据」永远返回 409
        # RestoreBusy（只能重启进程）。
        # 修法：token 先置 None、begin_import 移进 try，finally 里**只在真的拿到
        # 名额时**才 `end_import(token)`（不能无条件 `end_import(None)`：token=None
        # 在 `end_import` 里是「无条件释放」，会把别人正在跑的名额清掉），
        # 然后无论哪种出口都 release 恢复锁 + 失效仓储缓存。
        token: Optional[str] = None
        try:
            token = self.begin_import()
            from integrations.pipeline_impl import restore_full_delivery  # noqa: PLC0415
            return restore_full_delivery()
        finally:
            if token is not None:
                self.end_import(token)
            self._restore_lock.release()
            reset_repositories()

    # ---- 导入 ----
    def save_upload(self, filename: str, stream) -> Path:
        """把单文件上传**分块**写到上传目录（超限立刻中止并删除半成品）。

        改造前这里是 `shutil.copyfileobj` 全量落盘，之后 API 层才比对大小 ——
        2048 MB 上限形同虚设（磁盘先被写满才回 413）。现在按 1 MB 分块计数，
        超过 `MAX_UPLOAD_MB` 立刻抛 `UploadTooLargeError`（HTTP 413）并删掉
        已经写下的字节，磁盘上不留半成品。
        """
        settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        safe = Path(filename).name or f'dataset_{uuid.uuid4().hex[:8]}.zip'
        dest = settings.UPLOAD_DIR / f'{datetime.now():%Y%m%d%H%M%S}_{safe}'
        limit = settings.MAX_UPLOAD_MB * 1048576
        total = 0
        try:
            with open(dest, 'wb') as f:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise UploadTooLargeError(
                            f'数据集 {total / 1048576:.0f} MB 超出上限 '
                            f'{settings.MAX_UPLOAD_MB} MB')
                    f.write(chunk)
        except BaseException:
            dest.unlink(missing_ok=True)          # 半成品不留
            raise
        return dest


    # ---- 文件夹上传（多文件 + 各自相对路径）----

    @classmethod
    def safe_relpath(cls, raw: Optional[str]) -> str:
        """把浏览器给的「相对路径」规范化成安全的 POSIX 相对路径。

        浏览器上传文件夹时每个文件的相对路径形如 `sub/dir/a.html`
        （Chrome/Edge/Firefox 走 `webkitRelativePath`）。这里做**白名单化**：
        只保留正常目录段，其余一律抛 ValueError（HTTP 层转 400）。

        拒绝的形态：
          * 空路径 / 只剩空段（`.`, `//`）
          * 绝对路径：`/etc/passwd`、`\\\\server\\share`
          * 盘符路径：`C:\\Windows\\x`、`C:/x`
          * **路径穿越**：任何 `..` 段（`../../x`、`a/../../x`）
          * NUL 字节
          * Windows 保留设备名（`CON` / `NUL` / `COM1` …，含带后缀的 `NUL.txt`）
        """
        s = (raw or '').strip().replace('\\', '/')
        if not s:
            raise ValueError('文件缺少相对路径')
        if '\x00' in s:
            raise ValueError(f'相对路径含非法字符：{raw!r}')
        if s.startswith('/'):
            raise ValueError(f'不允许绝对路径：{raw!r}')
        if len(s) > 1 and s[1] == ':':
            raise ValueError(f'不允许盘符路径：{raw!r}')
        parts: List[str] = []
        for seg in s.split('/'):
            if seg in ('', '.'):          # 空段与当前目录段直接丢掉（无害）
                continue
            if seg == '..':
                raise ValueError(f'不允许路径穿越（..）：{raw!r}')
            if seg.split('.')[0].upper() in cls._RESERVED_NAMES:
                raise ValueError(f'不允许系统保留名：{raw!r}')
            parts.append(seg)
        if not parts:
            raise ValueError(f'相对路径无效：{raw!r}')
        return '/'.join(parts)

    def save_folder(self, items: Iterable[Tuple[Optional[str], object]]
                    ) -> Tuple[Path, List[str], int]:
        """把「多文件 + 各自相对路径」在临时目录里还原成一棵目录树。

        `items` 是 `(相对路径, 可读二进制流)` 的序列（顺序即 files 的顺序）。

        关键安全设计：**先把所有相对路径校验完，再创建目录/写第一个字节**。
        任何一个路径不合法 -> 直接抛错，磁盘上不会留下半棵树，更不会写到
        上传目录之外（路径穿越请求因此不可能写出任何文件）。

        返回 `(目录, 规范化后的相对路径列表, 总字节数)`。
        超限抛 `UploadTooLargeError`，并清理已写入的临时目录。
        """
        pairs = [(self.safe_relpath(rel), stream) for rel, stream in items]
        if not pairs:
            raise ValueError('没有收到任何文件')
        if len(pairs) > settings.MAX_FOLDER_FILES:
            raise ValueError(f'文件夹内文件数 {len(pairs)} 超过上限 '
                             f'{settings.MAX_FOLDER_FILES}')

        settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = settings.UPLOAD_DIR / (
            f'{datetime.now():%Y%m%d%H%M%S}_folder_{uuid.uuid4().hex[:8]}')
        root = dest.resolve()
        limit = settings.MAX_UPLOAD_MB * 1048576
        total = 0
        try:
            for rel, stream in pairs:
                tgt = dest.joinpath(*rel.split('/'))
                # 双保险：解析后的真实路径必须仍在 dest 之内（防符号链接/边界情况）
                resolved = tgt.resolve()
                if resolved != root and root not in resolved.parents:
                    raise ValueError(f'相对路径越出上传目录：{rel!r}')
                tgt.parent.mkdir(parents=True, exist_ok=True)
                with open(tgt, 'wb') as f:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limit:
                            raise UploadTooLargeError(
                                f'文件夹合计 {total / 1048576:.0f} MB 超出上限 '
                                f'{settings.MAX_UPLOAD_MB} MB')
                        f.write(chunk)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)   # 半棵树不留
            raise
        return dest, [rel for rel, _ in pairs], total

    @classmethod
    def normalize_mode(cls, mode: Optional[str]) -> str:
        """规范入库模式；非法值抛 ValueError（HTTP 层转 400）。"""
        resolved = (mode or 'replace').strip().lower()
        if resolved not in cls.MODES:
            raise ValueError(f'mode 只支持 {" / ".join(cls.MODES)}，收到 {mode!r}')
        return resolved

    def start_import(self, dataset_path: Path, filename: str,
                     mode: str = 'replace', lock_token: Optional[str] = None) -> ImportJob:
        """启动导入任务。`mode` 透传给管线（replace 覆盖 / append 追加）。

        后台线程执行，因此这里只做参数规范化，真正的读写都在管线里。

        **「导入中」名额由调用方先占住**（`begin_import()` 返回的 `lock_token`，
        见 api 层），在本方法启动的后台线程**结束时凭 token** 释放 —— 整条管线
        （含整库重建）跑完之前，第二个上传请求会拿到 409，不会两个 ingest 同时写库。
        """
        mode = self.normalize_mode(mode)
        job = ImportJob(
            job_id=uuid.uuid4().hex[:12],
            filename=filename,
            status='pending',
            stage='排队中',
            started_at=datetime.now().isoformat(timespec='seconds'),
        )
        with self._lock:
            self._jobs[job.job_id] = job

        def _run() -> None:
            job.status = 'running'
            ctx = PipelineContext(job)
            try:
                get_pipeline().run(dataset_path, ctx, mode=mode)
                if job.status != 'failed':
                    job.status = 'succeeded'
                    job.progress = 1.0
                    reset_repositories()      # 让新的数据生效
            except Exception as e:
                ctx.fail('处理管线异常', f'{type(e).__name__}: {e}')
            finally:
                job.finished_at = datetime.now().isoformat(timespec='seconds')
                # 文件夹上传的「暂存目录树」是本服务创建的临时产物，处理完就删掉：
                # 它是一棵完整的上传副本，留着会把磁盘撑满（2C4G 机器上尤其危险）。
                # 单文件上传的压缩包**仍然保留**在 var/uploads，行为与改造前一致；
                # 目录判断 + 父目录判断双重保护，绝不会删到用户指定的真实目录。
                if (dataset_path.is_dir()
                        and dataset_path.parent.resolve()
                        == settings.UPLOAD_DIR.resolve()):
                    shutil.rmtree(dataset_path, ignore_errors=True)
                # 管线跑完（无论成功/失败）才释放「导入中」名额（凭 token，只放自己的）
                self.end_import(lock_token)

        threading.Thread(target=_run, daemon=True).start()
        return job

    def get_job(self, job_id: str) -> Optional[ImportJob]:
        return self._jobs.get(job_id)


# ==================================================================
# 标的信息检索
# ==================================================================

class ItemService:
    """标的信息的全字段检索、筛选与查看。"""

    def search(self, flt: ItemFilter, page: int, page_size: int):
        page = max(1, page)
        page_size = max(1, min(page_size, settings.MAX_PAGE_SIZE))
        rows, total = get_item_repository().search(flt, page, page_size)
        meta = PageMeta(
            page=page, page_size=page_size, total=total,
            total_pages=(total + page_size - 1) // page_size if page_size else 0)
        return rows, meta

    def facets(self) -> ItemFacets:
        return get_item_repository().facets()

    def suggest(self, keyword: str, entity_type: str = '', limit: int = None
                ) -> List[EntitySuggestItem]:
        limit = min(limit or settings.MAX_SUGGEST, settings.MAX_SUGGEST)
        return get_item_repository().suggest_entities(keyword, entity_type, limit)


# ==================================================================
# 场景分析
# ==================================================================

class ScenarioService:
    """业务场景查询。

    每个方法都对「实体为空 / 参数不合法」做了显式校验，返回带 error 的
    空结果而不是抛 500 —— 图谱查询失败时前端能优雅降级，演示不会中断。
    """

    def _empty_check(self, *values) -> Optional[str]:
        if any(not v for v in values):
            return '请先选择查询主体'
        return None

    def run_s1(self, purchaser: str, min_count: int, top_n: int) -> Scenario1Out:
        if not purchaser:
            return Scenario1Out(summary={'error': '请先选择采购单位'})
        return get_graph_repository().scenario1(purchaser, min_count, top_n)

    def run_s2(self, purchaser: str, top_n: int) -> Scenario2Out:
        if not purchaser:
            return Scenario2Out(summary={'error': '请先选择采购单位'})
        return get_graph_repository().scenario2(purchaser, top_n)

    def run_s3(self, supplier: str, top_n: int) -> Scenario3Out:
        if not supplier:
            return Scenario3Out(summary={'error': '请先选择中标供应商'})
        return get_graph_repository().scenario3(supplier, top_n)

    def run_s4(self, suppliers: List[str], min_count: int, top_n: int) -> Scenario4Out:
        suppliers = [s for s in (suppliers or []) if s]
        if len(suppliers) < 2:
            return Scenario4Out(suppliers=suppliers,
                                summary={'error': '至少需要选择 2 个中标供应商'})
        return get_graph_repository().scenario4(suppliers, min_count, top_n)

    def run_s5(self, suppliers: List[str], top_n: int) -> Scenario5Out:
        suppliers = [s for s in (suppliers or []) if s]
        if len(suppliers) < 2:
            return Scenario5Out(suppliers=suppliers,
                                summary={'error': '至少需要选择 2 个中标供应商'})
        return get_graph_repository().scenario5(suppliers, top_n)


dataset_service = DatasetService()
item_service = ItemService()
scenario_service = ScenarioService()
