# -*- coding: utf-8 -*-
"""数据集处理管线 —— **抽取逻辑的接入点**。

================================================================================
接入指引
================================================================================
把你们的抽取流程实现为 `Pipeline` 的子类，覆盖 `run()` 即可：

    class MyPipeline(Pipeline):
        def run(self, dataset_path, ctx):
            # 1. 解包、解析、抽取（调用 edm 包或你们自己的实现）
            # 2. 结果写入数据库
            # 3. 通过 ctx.report(...) 汇报进度
            ...

然后在 api 层把 `get_pipeline()` 返回的实现换掉。

`ctx.report()` 会把进度写进 ImportJob，前端轮询 /dataset/import/{job_id} 即可
实时显示阶段、进度与耗时。**耗时统计口径**：
从数据集导入完成（文件落盘）起算，到全部结果写入数据库为止。
"""
from __future__ import annotations

import time
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

from .schemas import ImportJob


class PipelineContext:
    """进度汇报上下文。"""

    def __init__(self, job: ImportJob,
                 on_change: Optional[Callable[[ImportJob], None]] = None):
        self.job = job
        self._on_change = on_change
        self._t0 = time.perf_counter()

    def report(self, stage: str, progress: float = None,
               processed: int = None, total: int = None,
               items: int = None, message: str = '') -> None:
        if stage:
            self.job.stage = stage
        if progress is not None:
            self.job.progress = max(0.0, min(1.0, progress))
        if processed is not None:
            self.job.processed_docs = processed
        if total is not None:
            self.job.total_docs = total
        if items is not None:
            self.job.total_items = items
        if message:
            self.job.message = message
        self.job.elapsed_ms = int((time.perf_counter() - self._t0) * 1000)
        if self._on_change:
            self._on_change(self.job)

    def fail(self, message: str, detail: str = '') -> None:
        self.job.status = 'failed'
        self.job.message = message
        if detail:
            self.job.errors.append(detail)
        self.job.elapsed_ms = int((time.perf_counter() - self._t0) * 1000)
        if self._on_change:
            self._on_change(self.job)


class Pipeline(ABC):
    """数据集处理管线抽象。"""

    @abstractmethod
    def run(self, dataset_path: Path, ctx: PipelineContext,
            mode: str = 'replace') -> None:
        """处理一个已落盘的数据集文件（zip 或目录）。

        `mode` 是入库模式（由 `/dataset/import` 的 mode 参数透传）：
          * `'replace'`（默认）—— 库内数据被本次数据集替换，与改造前行为一致；
          * `'append'`  —— 保留库内已有数据，把本次解析结果按行去重后追加。
        实现方必须至少接受 `replace`；不认识 `append` 时按 `replace` 处理即可。
        """


class PlaceholderPipeline(Pipeline):
    """占位管线：接收数据集并统计规模，但不做抽取。

    它**真的会读取 zip 目录结构**，因此报告的文件数是真实的。
    抽取逻辑接入后，把 `get_pipeline()` 换成真实实现即可。
    """

    def run(self, dataset_path: Path, ctx: PipelineContext,
            mode: str = 'replace') -> None:
        ctx.report('读取数据集', 0.05, message='解析压缩包目录')
        html_files = attach_files = 0
        try:
            with zipfile.ZipFile(dataset_path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename.lower()
                    if name.endswith('.html'):
                        html_files += 1
                    elif name.endswith('.zip'):
                        attach_files += 1
        except Exception as e:
            ctx.fail('数据集读取失败', f'{type(e).__name__}: {e}')
            return

        ctx.report(f'发现 {html_files} 篇公告、{attach_files} 个附件包',
                   0.9, total=html_files, processed=0)
        ctx.report('数据集已接收', 1.0, processed=html_files, items=0,
                   message='数据处理管线尚未接入，暂无可展示结果')


_pipeline: Optional[Pipeline] = None


def get_pipeline() -> Pipeline:
    """返回当前使用的管线实现。接入真实抽取逻辑时在这里替换。"""
    global _pipeline
    if _pipeline is None:
        _pipeline = PlaceholderPipeline()
    return _pipeline


def set_pipeline(p: Pipeline) -> None:
    global _pipeline
    _pipeline = p
