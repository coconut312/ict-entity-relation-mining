# -*- coding: utf-8 -*-
"""上传抽取的**线程池并发**（篇级调度 + 块级 LLM 调用）。

背景
----
上传数据集时 `pipeline_impl` 逐篇调 `llm_extract.extract_with_llm_safe`（路线 2 的
`*.html`、路线 4 的附件包），单篇 LLM 调用约 2~5s；串行时 3~4 篇就要 10s+。LLM 调用是
**网络等待**，并发即可把总耗时压到「单篇量级」。

两层并发（本模块 = 篇级调度，`ict_parse.llm_extract` = 块级调用）
------------------------------------------------------------------
* 本模块只负责「把 N 篇分给 N 个线程去**解析**（解压/PDF/Excel/规则层）」——
  解析是纯 CPU 活（GIL），并发只为了让各篇的解析与**别人的 LLM 等待**重叠；
* 真正的 LLM 调用在 `ict_parse.llm_extract` 的**全局共享块池**里跑，任务粒度是
  「一个 chunk 的一次调用」。一篇的 2~8 块一次性提交，多篇的块同池并行，
  因此 `EDM_LLM_CONCURRENCY` 约束的是**同时在飞的 LLM 请求数**（而不是篇数）；
* 篇级线程在等块结果时是阻塞的（不占池），两层池互不嵌套，不会互相饿死。

设计约束（与串行结果必须一致）
------------------------------
* 结果按**输入顺序**汇总（索引映射），不因完成顺序打乱标的行顺序；
* 同一篇的块结果按 `chunk_index` 合并（在 `llm_extract._call_plan` 内保证）；
* `ctx.report` 只在**主线程**里按完成数调用（回调 `on_done`），不从工作线程写进度；
* 单篇失败被包成 `(None, exc)` 交给调用方，由调用方按原逻辑回退规则层/记错误，
  不让一篇失败中断整批。

并发数
------
`EDM_LLM_CONCURRENCY`（默认 **12**，夹紧到 1~32；实测 20 篇规模下 12 优于 16 约 9%）。
同一个变量同时是块池上限（`ict_parse.llm_extract.llm_concurrency()` 读同一口径）。
值 <=1 时退化为「串行走同一套代码路径」（篇池与块池的 max_workers 都是 1）。

可观测性
--------
模块维护一个**在飞计数**（进入 worker +1 / 离开 -1）并记录峰值；块级计数由块池
（`llm_extract.chunk_stats()`）提供。每批结束后打印两行::

    [并发] 逐篇抽取 3 篇 / 12 块 / 生效并发 12 / 同时在飞峰值 12（块级峰值 12 / 篇级峰值 3） / 批总耗时 5.24s（…）
    [耗时] 逐篇（输入顺序，含该篇解析+LLM）: #1 4.21s / #2 4.90s / #3 5.11s

「生效并发」= min(块池上限, 本批块数) —— 2 块就是 2，不会空开 12 个线程；
「同时在飞峰值」是**块级**峰值（真正在执行 LLM 调用的块数），峰值 <= 生效并发
即为「并发确实生效且没超限」的证明。
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Optional, Sequence, Tuple

DEFAULT_CONCURRENCY = 12
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 32

# ---- 在飞计数（仅用于证明并发上限）----
_GAUGE_LOCK = threading.Lock()
_inflight = 0
_peak = 0
_calls = 0


def reset_peak() -> None:
    """清零峰值/调用计数（每批抽取开始前调用，便于每批单独取证）。"""
    global _inflight, _peak, _calls
    with _GAUGE_LOCK:
        _inflight = 0
        _peak = 0
        _calls = 0


def stats() -> dict:
    """当前批次的 `{'peak', 'calls', 'inflight'}`（并发取证用）。"""
    with _GAUGE_LOCK:
        return {'peak': _peak, 'calls': _calls, 'inflight': _inflight}


def concurrency() -> int:
    """生效并发数：读 `EDM_LLM_CONCURRENCY`，非法/缺省按 12，夹紧到 1~32。"""
    raw = (os.environ.get('EDM_LLM_CONCURRENCY') or '').strip()
    if not raw:
        n = DEFAULT_CONCURRENCY
    else:
        try:
            n = int(raw)
        except ValueError:
            n = DEFAULT_CONCURRENCY
    return max(MIN_CONCURRENCY, min(MAX_CONCURRENCY, n))


def _chunk_stats() -> dict:
    """本批的**块级**统计（来自 `ict_parse.llm_extract` 的全局共享块池）。

    取不到（例如纯规则层、或某些单测里没装 ict_parse）时返回 `{}`，
    日志退化为旧的「篇级」口径，不影响并发本身。
    """
    try:
        from ict_parse import llm_extract as _llm_extract   # noqa: PLC0415
        return _llm_extract.chunk_stats()
    except Exception:                                       # noqa: BLE001
        return {}


def _reset_chunk_stats() -> None:
    """每批开始前清零块级计数/峰值（逐批取证用；失败不影响主流程）。"""
    try:
        from ict_parse import llm_extract as _llm_extract   # noqa: PLC0415
        _llm_extract.reset_chunk_stats()
    except Exception:                                       # noqa: BLE001
        pass


def _run_one(fn: Callable[[], object], idx: Optional[int] = None,
             durs: Optional[dict] = None) -> Tuple[object, Optional[BaseException]]:
    """跑一个任务：正常 -> (value, None)；异常 -> (None, exc)。

    进入/离开时更新在飞计数与峰值 —— 峰值就是「同时在飞的抽取请求数」；
    传入 `durs` 时顺便记下本任务耗时（秒），供批次日志给出**每篇耗时**。
    """
    global _inflight, _peak, _calls
    with _GAUGE_LOCK:
        _inflight += 1
        _calls += 1
        if _inflight > _peak:
            _peak = _inflight
    t0 = time.perf_counter()
    try:
        return fn(), None
    except Exception as e:                      # noqa: BLE001 —— 单篇失败隔离
        return None, e
    finally:
        if durs is not None and idx is not None:
            durs[idx] = round(time.perf_counter() - t0, 3)
        with _GAUGE_LOCK:
            _inflight -= 1


def map_ordered(tasks: Sequence[Callable[[], object]],
                on_done: Optional[Callable[[int, object, Optional[BaseException]], None]] = None,
                workers: Optional[int] = None) -> List[Tuple[object, Optional[BaseException]]]:
    """并发执行 `tasks`，**按输入顺序**返回 `[(value, exc)]`。

    * `tasks[i]()` 就是第 i 篇的整段抽取（含该篇的规则层回退），异常被捕获成 exc；
    * `on_done(i, value, exc)` 在**主线程**（as_completed 的消费方）回调，用于
      `ctx.report` —— 因此进度文案不会出现多线程乱序写；
    * 完成后打印 `[并发] …`（**篇数/块数/生效并发/峰值/批总耗时**）与 `[耗时] …`
      （**每篇耗时**）。块级数字来自 `ict_parse.llm_extract` 的共享块池：本批一共提交了
      多少块、同时最多几块在飞。
    """
    n = len(tasks)
    results: List[Tuple[object, Optional[BaseException]]] = [(None, None)] * n
    if n == 0:
        return results
    limit = workers if workers else concurrency()
    limit = max(1, min(limit, n))
    reset_peak()
    _reset_chunk_stats()
    durs: dict = {}
    t_batch = time.perf_counter()
    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix='edm-extract') as ex:
        futs = {ex.submit(_run_one, tasks[i], i, durs): i for i in range(n)}
        for fut in as_completed(futs):
            i = futs[fut]
            value, exc = fut.result()
            results[i] = (value, exc)
            if on_done is not None:
                on_done(i, value, exc)
    batch = round(time.perf_counter() - t_batch, 2)
    s = stats()
    cs = _chunk_stats()
    n_chunks = int(cs.get('tasks') or 0)          # 本批提交到块池的块任务数
    chunk_peak = int(cs.get('peak') or 0)         # 同时在飞的块级峰值
    pool_n = int(cs.get('workers') or limit)      # 块池上限（同 EDM_LLM_CONCURRENCY）
    # 生效并发：取「块池上限」与「本批块数」的较小者 —— 只有 2 块就不会开 12 路
    eff = min(pool_n, n_chunks) if n_chunks else limit
    # 峰值报**块级**（真正在飞的是 LLM 请求；纯规则层没有块统计时退回篇级峰值）
    peak_disp = chunk_peak if n_chunks else s['peak']
    print(f'[并发] 逐篇抽取 {n} 篇 / {n_chunks} 块 / 生效并发 {eff}'
          f' / 同时在飞峰值 {peak_disp}'
          f'（块级峰值 {chunk_peak} / 篇级峰值 {s["peak"]}）'
          f' / 批总耗时 {batch}s（上限 {MAX_CONCURRENCY}，来自 EDM_LLM_CONCURRENCY='
          f'{os.environ.get("EDM_LLM_CONCURRENCY") or "未设置(默认%d)" % DEFAULT_CONCURRENCY}）',
          flush=True)
    per = ' / '.join(f'#{i + 1} {durs.get(i, 0)}s' for i in range(n))
    print(f'[耗时] 逐篇（输入顺序，含该篇解析+LLM）: {per}', flush=True)
    return results
