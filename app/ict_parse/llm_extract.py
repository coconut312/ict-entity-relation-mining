"""LLM 抽取层（Qwen/DeepSeek，OpenAI 兼容接口）。

对每篇公告：组装上下文(概要+原文标的表+附件报价表/文本) -> 调 LLM -> JSON -> 后处理为 items。
超长公告按「表组/附件表」**分块**抽取（`build_chunks`）再按块序合并去重。

并发粒度 = **一个 chunk 的一次 LLM 调用**（不是「一篇」）
---------------------------------------------------------
进程内有一张**全局共享**的块池（`chunk_pool()`），`_call_plan()` 把该篇的所有块
一次性提交进池、按 `chunk_index` 顺序收集。于是：
  * 跨篇并行：多篇的块同时在一张池里跑（池是全局的，不是每篇一张）；
  * 篇内并行：一篇的 2~8 块不再串行等待（附件路线的 12~16s 主因）；
  * 结果与串行逐字一致：合并仍按块序（`purchaser` 取首个非空、items 按块序去重），
    失败隔离仍按块（单块 `LLMError` -> 跳过；全失败 -> 交给上层回退/细切）。
块池的并发上限 = `EDM_LLM_CONCURRENCY`（默认 12，夹紧 1~32，与
`integrations.llm_parallel.concurrency()` 同一口径）；线程按需创建，不会空开。

环境变量：LLM_API_KEY / DEEPSEEK_API_KEY（必填）；LLM_BASE_URL、LLM_MODEL、
         LLM_TIMEOUT、LLM_RETRIES、LLM_MAX_TOKENS、LLM_MAX_CTX、LLM_APP_RETRIES、
         EDM_LLM_CONCURRENCY（可选）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config
from .extract import (build as rule_build, norm_money, norm_qty, _apply_pricing,
                      _fill_prices_from_siblings, clean_specs, _canon_num)

DEFAULT_BASE = config.DEEPSEEK_BASE
DEFAULT_MODEL = config.TEXT_MODEL
DEFAULT_MAX_CTX = 40000  # 上下文预算（字符；按行/按块，不切断行）


class LLMError(RuntimeError):
    """可预期的 LLM 调用/解析失败——仅供回退，不掩盖代码 bug。"""


SYSTEM = "你是政府采购招投标信息抽取专家。严格依据给定内容抽取，不臆造；不要输出推理或解释，只输出 JSON。"

PROMPT = """下面是某政府采购中标/成交公告的结构化内容。请抽取**标的物明细**与公告级字段，输出 JSON。

要求：
- 逐条标的物一行；同一格内用“；”分隔的多值请拆成多行（如“光跳纤架；光熔纤柜”拆两行）。
- 金额一律换算成**元**（“￥349.9 万元”→“3499000”）。
- 服务类若无品牌/型号/数量/单价，对应字段置 null，不要编造。
- **多包/多标段**：每个包/标段各输出一行，并填入**该包自己的**成交金额
  （金额见“[正文·中标(成交)信息]”块，按包号/标段名对应；不要把所有包写成同一个金额）。
- 只摘取真正的采购标的，排除“序号/供应商名称/评审表/联系方式/下载清单”等非标的行。
- supplier_name 填中标供应商；winners/bidders 填公告里的中标方与参与投标方。

只输出如下 JSON（无则 null）：
{
  "items": [
    {"item_name":"","category":"","brand":null,"spec":null,
     "quantity":null,"unit":null,"unit_price":null,"total_price":null,"supplier_name":null}
  ],
  "purchaser":"","project_amount":null,"winners":[],"bidders":[]
}

=== 公告内容 ===
{context}
"""


def available() -> bool:
    """是否有可用的文本 LLM key。

    走 config.text_config()（会读 Reasonix 的 .env 文件），
    不能只用 os.environ —— 否则 key 在 .env 里时会误判为「不可用」，
    导致 pipeline 的 --llm 静默退回规则层。
    """
    return bool(config.text_config()[2])


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# 进程内客户端缓存：一篇公告内可能分多块调用，整库抽取更是几十上百篇，
# 每篇新建一个 OpenAI/httpx 客户端会反复做 DNS+TLS 握手（平台「上传即解析」路径上尤其明显）。
# 只缓存**最后一个**配置对应的客户端：页面改 key/base 后签名变化会立刻重建，
# 且内存里始终只留一份 key。
_CLIENT_LOCK = threading.Lock()
_CLIENT_CACHE: dict = {}


def _client():
    from openai import OpenAI

    base, model, key = config.text_config()
    if not key:
        raise LLMError("未设置 DEEPSEEK_API_KEY / LLM_API_KEY")
    timeout = _env_float("LLM_TIMEOUT", 120.0)
    retries = _env_int("LLM_RETRIES", 3)      # SDK 对 429/5xx 自动退避重试
    sig = (base, key, timeout, retries)
    with _CLIENT_LOCK:
        client = _CLIENT_CACHE.get(sig)
        if client is None:
            client = OpenAI(api_key=key, base_url=base, timeout=timeout,
                            max_retries=retries)
            _CLIENT_CACHE.clear()             # 配置变了：旧客户端丢弃，只留一份
            _CLIENT_CACHE[sig] = client
    return client, model


def _bname(p: str) -> str:
    return p.split("\\")[-1].split("/")[-1]


# ---------- 上下文：按「表组/附件表」切成自洽块 ----------
def _header_lines(rec: dict) -> list[str]:
    L = ["公告标题：" + (rec.get("article_title") or "")]
    sm = rec.get("summary", {})
    for k in ("采购项目名称", "采购单位", "总中标金额", "品目", "行政区域"):
        if sm.get(k):
            L.append(f"{k}：{sm[k]}")
    return L


def _announcement_block_lines(rec: dict) -> list[str]:
    """把正文里的「中标(成交)信息」成交块 / 「主要标的信息」散文块压成紧凑文本喂给 LLM。

    这些内容**不在 `<table>` 里**（是 key-value 段落），原先完全没进 prompt，
    导致多包公告只抽到包名、抽不到各包成交金额（实测 t20260408 4 包、t20260707 6 包全丢）。
    直接复用规则层的解析，保证两条链路口径一致。
    """
    from .extract import _items_from_announcement_text, _items_from_prose_items

    out: list[str] = []
    t = _items_from_announcement_text(rec)
    if t:
        out.append("[正文·中标(成交)信息] 标段/包名称 | 供应商名称 | 成交金额(元)")
        for it in t[:60]:
            out.append(" | ".join([
                str(it.get("item_name") or ""), str(it.get("supplier_name") or ""),
                "" if it.get("total_price") is None else str(it.get("total_price")),
            ]))
    p = _items_from_prose_items(rec)
    if p:
        out.append("[正文·主要标的信息] 名称 | 品牌 | 规格型号 | 数量 | 单价")
        for it in p[:60]:
            out.append(" | ".join(str(it.get(k) if it.get(k) is not None else "")
                                  for k in ("item_name", "brand", "spec", "quantity", "unit_price")))
    return out


def _unit_lines(rec: dict, max_rows: int = 60) -> list[list[str]]:
    units: list[list[str]] = []
    for i, g in enumerate(rec.get("main_item_tables", [])):
        if not g["headers"]:
            continue
        u = [f"[表{i}] 表头：" + " | ".join(g["headers"])]
        u += [" | ".join((c or "") for c in r) for r in g["rows"][:max_rows]]
        units.append(u)
    blk = _announcement_block_lines(rec)
    if blk:
        units.append(blk)
    for a in rec.get("extracted_files", []):
        # 同 extract.build：rel_path 可能被超长路径压缩，关键字匹配需带上原始文件名
        nm = " ".join(x for x in (a.get("name"), a.get("rel_path")) if x)
        if a.get("tables") and any(k in nm for k in ("报价", "一览", "明细", "标的", "开标")):
            for t in a["tables"][:2]:
                if not t:
                    continue
                u = [f"[附件表] {_bname(nm)} 表头：" + " | ".join(t[0])]
                u += [" | ".join((c or "") for c in r) for r in t[1:max_rows + 1]]
                units.append(u)
        elif a.get("text") and "报价" in nm:
            units.append([f"[附件文本] {_bname(nm)}：", (a.get("text") or "")[:3000]])
    return units


def _pack_chunks(header: list[str], units: list[list[str]], budget: int) -> list[str]:
    """把单元按预算打包成块；每块都带上 header；不切断行。"""
    hlen = sum(len(x) + 1 for x in header)
    chunks: list[str] = []
    cur = list(header)
    n = hlen
    for u in units:
        ulen = sum(len(x) + 1 for x in u)
        if len(cur) > len(header) and n + ulen > budget:  # 当前块已有内容且放不下 -> 收口
            chunks.append("\n".join(cur))
            cur = list(header)
            n = hlen
        cur.extend(u)
        n += ulen
    chunks.append("\n".join(cur))
    return chunks


def build_chunks(rec: dict, budget: int | None = None, max_rows: int = 60) -> list[str]:
    budget = budget if budget is not None else _env_int("LLM_MAX_CTX", DEFAULT_MAX_CTX)
    return _pack_chunks(_header_lines(rec), _unit_lines(rec, max_rows), budget)


def build_context(rec: dict, max_rows: int = 60, budget: int | None = None) -> str:
    """兼容旧接口：返回首块（单块公告即全文）。"""
    return build_chunks(rec, budget, max_rows)[0]


# ---------- LLM 调用 ----------
_JSON_DEC = json.JSONDecoder()


def _parse_json_content(content: str) -> dict:
    content = content.strip()
    if not content:
        raise LLMError("空响应")
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    for m in re.finditer(r"\{", content):
        try:
            obj, _ = _JSON_DEC.raw_decode(content[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("items" in obj or "winners" in obj):
            return obj
    raise LLMError(f"JSON 解析失败: 片段={content[:200]!r}")


def _call_json(client, model: str, context: str, app_retries: int) -> dict:
    """单次调用 + 应用层重试（空响应/JSON 失败）。网络类错误由 SDK 处理，这里包成 LLMError。

    每次调用都会写一条合规留痕到 `out/llm_calls.jsonl`（见 ict_parse/llm_log.py）。
    """
    from openai import APIError

    from .llm_log import log_call

    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": PROMPT.replace("{context}", context)}]
    mt = _env_int("LLM_MAX_TOKENS", 8192)
    mt_cap = _env_int("LLM_MAX_TOKENS_CAP", 32768)
    last: LLMError | None = None
    base = str(getattr(client, "base_url", "") or "")
    for attempt in range(app_retries + 1):
        t0 = time.perf_counter()
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, temperature=0,
                max_tokens=mt,
                response_format={"type": "json_object"},
            )
        except APIError as e:
            log_call("text", base, model, t0, False, err=f"APIError: {str(e)[:160]}",
                     chars_in=len(context), attempt=attempt)
            raise LLMError(f"API 错误: {type(e).__name__}: {str(e)[:160]}") from e
        choice = resp.choices[0]
        content = (choice.message.content or "").strip()
        log_call("text", base, model, t0, bool(content), resp=resp,
                 chars_in=len(context), chars_out=len(content), attempt=attempt)
        if content:
            try:
                data = _parse_json_content(content)
                if (data.get("items") or data.get("winners") or data.get("bidders")
                        or data.get("project_amount") is not None):
                    return data
                last = LLMError("空结果(items 为空)")  # 触发重试；仍空则上层回退规则
            except LLMError as e:
                last = e
        else:
            fr = getattr(choice, "finish_reason", None)
            last = LLMError(f"空响应(finish={fr})")
            if fr == "length" and mt < mt_cap:  # reasoning 占满 token -> 放大重试
                mt = min(mt * 2, mt_cap)
        if attempt < app_retries:
            time.sleep(0.8 * (attempt + 1))
    raise last or LLMError("未知失败")


# ---------- 块级共享并发池（任务粒度 = 一个 chunk 的一次 LLM 调用）----------
# 为什么是「全局一张池」而不是「每篇一张池」：篇级的并发上限只有 12，一篇若有 4 块，
# 它内部 4 次调用是**串行**的 -> 3 篇附件包（12 块）实际只利用了 3 路并发，
# 总耗时 ≈ 4 个响应周期。改成块级任务后，12 块同时在一张池里跑 ≈ 1 个响应周期。
DEFAULT_LLM_CONCURRENCY = 12
MIN_LLM_CONCURRENCY = 1
MAX_LLM_CONCURRENCY = 32

_POOL_LOCK = threading.Lock()
_POOLS: dict[int, ThreadPoolExecutor] = {}

_BATCH_LOCK = threading.Lock()
_stats = {"tasks": 0, "inflight": 0, "peak": 0}


def llm_concurrency() -> int:
    """块级并发上限：读 `EDM_LLM_CONCURRENCY`，非法/缺省按 12，夹紧到 1~32。

    与 `task3/backend/integrations/llm_parallel.concurrency()` **同一口径**（同一个
    环境变量、同一个默认值、同一个夹紧区间）。这里独立实现的原因：`ict_parse` 是纯
    解析层，不能反向 import `task3/backend/integrations`（CLI `scripts/run_llm.py`
    的 sys.path 里没有那个包）；两处都只读同一个变量，取值因此不会分叉。
    """
    raw = (os.environ.get("EDM_LLM_CONCURRENCY") or "").strip()
    if not raw:
        n = DEFAULT_LLM_CONCURRENCY
    else:
        try:
            n = int(raw)
        except ValueError:
            n = DEFAULT_LLM_CONCURRENCY
    return max(MIN_LLM_CONCURRENCY, min(MAX_LLM_CONCURRENCY, n))


def chunk_pool(workers: int | None = None) -> ThreadPoolExecutor:
    """取**进程内唯一**的块池（同一个并发数只建一张，跨篇/跨批复用）。

    `ThreadPoolExecutor` 的工作线程是**按提交惰性创建**的：一篇文章只有 2 块时
    只会创建 2 个线程，不会因为上限 12 就空开 12 个线程。并发数变化（例如 A/B 时
    把 `EDM_LLM_CONCURRENCY` 从 12 改成 1）会另建一张池，旧池没有在跑的任务，
    不动它也不影响正确性。
    """
    n = workers if workers else llm_concurrency()
    n = max(MIN_LLM_CONCURRENCY, min(MAX_LLM_CONCURRENCY, n))
    with _POOL_LOCK:
        pool = _POOLS.get(n)
        if pool is None:
            pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="edm-chunk")
            _POOLS[n] = pool
    return pool


def reset_chunk_stats() -> None:
    """清零「本批」块级计数/峰值（每批抽取开始前调用，便于逐批取证）。"""
    with _BATCH_LOCK:
        _stats["tasks"] = 0
        _stats["inflight"] = 0
        _stats["peak"] = 0


def chunk_stats() -> dict:
    """本批块级统计：`{'tasks','peak','inflight','workers'}`。

    * `tasks`  本批提交到块池的**块任务数**（= 一次 LLM 调用对应一块）；
    * `peak`   同时在飞（真正在执行 `_call_json`）的块数峰值 —— 证明并发上限没被击穿；
    * `workers` 当前生效的块池上限（`EDM_LLM_CONCURRENCY`）。
    """
    with _BATCH_LOCK:
        out = dict(_stats)
    out["workers"] = llm_concurrency()
    return out


def _run_chunk(client, model: str, chunk: str, app_retries: int,
               gauge: dict | None = None):
    """在块池的工作线程里跑**一次**块调用；异常包成 `(None, exc)`，不在池里泄漏。

    `gauge` 是这一篇自己的在飞计数（每篇一行日志用），与全局计数一起在锁内累加。
    """
    with _BATCH_LOCK:
        _stats["inflight"] += 1
        if _stats["inflight"] > _stats["peak"]:
            _stats["peak"] = _stats["inflight"]
        if gauge is not None:
            gauge["inflight"] = gauge.get("inflight", 0) + 1
            if gauge["inflight"] > gauge.get("peak", 0):
                gauge["peak"] = gauge["inflight"]
    try:
        return _call_json(client, model, chunk, app_retries), None
    except Exception as e:                       # noqa: BLE001 —— 单块失败隔离（与串行一致）
        return None, e
    finally:
        with _BATCH_LOCK:
            _stats["inflight"] -= 1
            if gauge is not None:
                gauge["inflight"] -= 1


def _call_chunks(chunks: list[str], client, model: str,
                 app_retries: int) -> list[tuple[dict | None, BaseException | None]]:
    """把**一篇的所有块**一次性提交进共享池，**按块序**收集 `[(data|None, exc|None)]`。

    * 提交顺序 = 块序（`chunk_index` 递增），收集也按块序，因此调用方按块序合并即可，
      结果与「逐块串行」逐字一致；
    * 所有块同时提交（不是一块一等）：这是篇内并行、跨篇并行的关键；
    * 池里抛出的任何异常都被 `_run_chunk` 收成 exc，主循环不会被打断。
    """
    pool = chunk_pool()
    with _BATCH_LOCK:
        _stats["tasks"] += len(chunks)
    gauge = {"inflight": 0, "peak": 0}
    t0 = time.perf_counter()
    futures = [pool.submit(_run_chunk, client, model, chunk, app_retries, gauge)
               for chunk in chunks]
    out: list[tuple[dict | None, BaseException | None]] = []
    for fut in futures:
        try:
            out.append(fut.result())
        except Exception as e:                   # noqa: BLE001 —— 池被关闭等极少见情况
            out.append((None, e))
    if len(chunks) > 1:                          # 篇内并行取证（单块不必刷屏）
        print(f'[并发] 分块 {len(chunks)} 块 / 篇内并发峰值 {gauge["peak"]}'
              f' / 块总耗时 {round(time.perf_counter() - t0, 2)}s', flush=True)
    return out


# ---------- 后处理 ----------
def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return _canon_num(v)
    return norm_money(str(v))


def _qty(v):
    if v is None or v == "":
        return (None, None)
    if isinstance(v, (int, float)):
        return (_canon_num(v), None)
    return norm_qty(str(v))


def _norm_item(it: dict):
    q, u = _qty(it.get("quantity"))
    nm = it.get("item_name")
    if not nm and not it.get("brand") and not it.get("spec"):
        return None
    item = {
        "item_name": nm,
        "category": it.get("category"),
        "brand": it.get("brand"),
        "spec": it.get("spec"),
        "quantity": q,
        "unit": it.get("unit") or u,
        "unit_price": _num(it.get("unit_price")),
        "total_price": _num(it.get("total_price")),
        "supplier_name": it.get("supplier_name"),
        "package_no": None,
        "source": "LLM",
        "derived": False,
        "needs_review": False,
        "item_type": (str(it.get("category")).split("/")[0] if it.get("category") else None),
    }
    if item["total_price"] is None and item["unit_price"] and item["quantity"]:
        item["total_price"] = _canon_num(item["unit_price"] * item["quantity"])
        item["derived"] = True
    if item["unit_price"] is None and item["total_price"] and item["quantity"]:
        item["unit_price"] = _canon_num(item["total_price"] / item["quantity"])  # 总价/数量->单价
        item["derived"] = True
    return item


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = (it.get("item_name"), it.get("spec"), it.get("brand"),
               it.get("quantity"), it.get("unit_price"), it.get("total_price"))
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _merge_ann(rec: dict, win: list[str], bid: list[str], over: dict) -> dict:
    base = rule_build(rec)
    ann = base["announcement"]
    for k in ("purchaser", "project_amount"):
        if over.get(k) is not None:
            ann[k] = over[k]
    if win:
        ann["winners"] = win
    if bid:
        ann["bidders"] = bid
    if ann["winners"] and not ann.get("bidders"):
        ann["bidders"] = ann["winners"]
    return ann


def _apply_discount(rec: dict, ann: dict, items: list[dict]) -> None:
    """按报价方式处理单价/总价（泛化：下浮率/费率/折扣率/框架/据实…）。"""
    _fill_prices_from_siblings(items)
    _apply_pricing(rec, ann, items)


def _dedupe_names(xs: list[str]) -> list[str]:
    seen, out = set(), []
    for x in xs:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_plan(rec: dict) -> list[list[str]]:
    """三种 token 预算下的分块方案，**一次算好**（= 改造前 `extract_with_llm` 的逐级细切阶梯）。

    为什么要在「调用前」算好：上传管线会先只解析「模型看得见的那部分附件」就发请求，
    其余附件在等响应时补解析（见 `extract_with_llm(defer=…)`）。如果把分块推迟到
    重试时再算，重试那一次就会看到「已被补解析的附件」→ prompt 与首次不同 → 结果漂移。
    预先算好三档 chunk，重试只换 chunk，不再重算 prompt，保证**与串行口径逐字一致**。
    """
    base = _env_int("LLM_MAX_CTX", DEFAULT_MAX_CTX)
    return [build_chunks(rec, budget=b)
            for b in (base, max(4000, base // 3), max(2500, base // 6))]


def _call_plan(plan: list[list[str]], app_retries: int) -> dict:
    """只做**网络调用与模型返回值的归一化**，全程不读 `rec`。

    这样调用方可以在「请求已发出、还在等响应」的时间窗里补解析附件（`defer`），
    而不会改变 prompt，也不会与主线程争用 `rec`（无数据竞争、结果可复现）。

    **并发**：本次计划里的所有块一次性提交进全局块池（`_call_chunks`），再按
    `chunk_index` 顺序收集合并 —— 块是并行的，合并顺序与串行完全相同。

    返回值（`raw`）语义与改造前的 `_postprocess` / `_extract_chunked` 前半段逐条对齐：
      * `items` 已经是 `_norm_item` 归一化后的标的（单块不去重、多块由 `_finalize` 去重）；
      * 单块：`purchaser` / `project_amount` 直接取模型返回值（空串也算「有值」）；
      * 多块：只累积真值（与 `_extract_chunked` 一致）。
    """
    client, model = _client()
    last: LLMError | None = None
    for chunks in plan:
        try:
            if len(chunks) <= 1:
                # 单块也走共享池：否则「篇级 12 路」与「块级 12 路」会同时打到服务商，
                # 实际在飞数会翻倍（实测 16 路单次延迟 +9%，多于此反而更慢）。
                data, exc = _call_chunks(chunks, client, model, app_retries)[0]
                if exc is not None:
                    raise exc                          # LLMError -> 换下一档；其它原样抛
                return {
                    "items": [x for x in (_norm_item(it) for it in (data.get("items") or [])) if x],
                    "winners": [w for w in (data.get("winners") or []) if w],
                    "bidders": [b for b in (data.get("bidders") or []) if b],
                    "purchaser": data.get("purchaser"),
                    "project_amount": _num(data.get("project_amount")),
                    "n_chunks": 1,
                    "failed_chunks": 0,
                }
            results = _call_chunks(chunks, client, model, app_retries)
            all_items: list[dict] = []
            win: list[str] = []
            bid: list[str] = []
            over: dict = {}
            errors = 0
            # **按块序合并**（不是按完成顺序）：两个 `purchaser` 都有值时取第一块的那个，
            # 与逐块串行完全相同。
            for data, exc in results:
                if exc is not None:
                    if not isinstance(exc, LLMError):
                        raise exc                      # 代码 bug 原样抛（不被当成「块失败」吞掉）
                    errors += 1
                    continue  # 单块失败不致命，跳过
                all_items.extend(
                    [x for x in (_norm_item(it) for it in (data.get("items") or [])) if x])
                win += [w for w in (data.get("winners") or []) if w]
                bid += [b for b in (data.get("bidders") or []) if b]
                if data.get("purchaser") and not over.get("purchaser"):
                    over["purchaser"] = data["purchaser"]
                if data.get("project_amount") is not None and over.get("project_amount") is None:
                    over["project_amount"] = _num(data.get("project_amount"))
            if errors == len(chunks):          # 全失败 -> 视为可预期失败（交给上层回退/细切）
                raise LLMError(f"全部分块失败({errors}/{len(chunks)})")
            return {
                "items": all_items, "winners": win, "bidders": bid,
                "purchaser": over.get("purchaser"),
                "project_amount": over.get("project_amount"),
                "n_chunks": len(chunks), "failed_chunks": errors,
            }
        except LLMError as e:
            last = e
            continue
    raise last or LLMError("未知失败")


def _finalize(rec: dict, raw: dict) -> dict:
    """`raw`（模型输出）+ **完整** `rec` -> 最终 extraction（与改造前逐字段一致）。

    这一步是唯一读 `rec` 的地方：公告级字段（`_merge_ann` -> 规则层）、报价方式折扣
    （`_apply_pricing`）、`clean_specs` 都在这里做，所以「等 LLM 时补解析的附件」已经
    全部就位，口径与「先解析完所有附件再调 LLM」完全相同。
    """
    n = int(raw.get("n_chunks") or 1)
    items = raw["items"]
    win, bid = raw["winners"], raw["bidders"]
    if n > 1:                                  # 多块：先去重（与 _extract_chunked 一致）
        items = _dedupe(items)
        win, bid = _dedupe_names(win), _dedupe_names(bid)
    ann = _merge_ann(rec, win, bid,
                     {"purchaser": raw.get("purchaser"),
                      "project_amount": raw.get("project_amount")})
    if len(ann["winners"]) == 1:
        for it in items:
            if not it.get("supplier_name"):
                it["supplier_name"] = ann["winners"][0]
    _apply_discount(rec, ann, items)
    out: dict = {"announcement": ann, "items": items}
    if n > 1:
        out["chunk_info"] = {"chunks": n, "failed_chunks": int(raw.get("failed_chunks") or 0)}
        out["engine"] = f"llm(chunked:{n},fail:{out['chunk_info']['failed_chunks']})"
    else:
        clean_specs(items)
        out["engine"] = "llm"
    return out


def _postprocess(rec: dict, data: dict) -> dict:
    """兼容旧接口：单次模型返回 -> extraction（= `_call_plan` 单块 + `_finalize`）。"""
    win = [w for w in (data.get("winners") or []) if w]
    bid = [b for b in (data.get("bidders") or []) if b]
    raw = {
        "items": [x for x in (_norm_item(it) for it in (data.get("items") or [])) if x],
        "winners": win, "bidders": bid,
        "purchaser": data.get("purchaser"),
        "project_amount": _num(data.get("project_amount")),
        "n_chunks": 1, "failed_chunks": 0,
    }
    return _finalize(rec, raw)


def extract_with_llm(rec: dict, app_retries: int | None = None,
                     defer=None) -> dict:
    """单块可容纳则单次；否则分块逐块抽取再合并。可预期失败抛 LLMError，代码 bug 原样抛出。

    `defer`（可选）：在**请求已发出、还在等响应**的时间窗里执行的回调 ——
    上传管线用它把「模型看不见的附件」补解析完（见 `ict_parse/pipeline.py::process_one`）。
    分块方案在调用前就用**当前** rec 算好（`build_plan`），回调里对 rec 的补写
    不会影响已发出的 prompt；后续 `_finalize` 读到的却是补完后的 rec，
    因此最终结果与「先解析完所有附件再调 LLM」逐字段一致。
    """
    app_retries = app_retries if app_retries is not None else _env_int("LLM_APP_RETRIES", 2)
    plan = build_plan(rec)
    if defer is None:
        raw = _call_plan(plan, app_retries)
    else:
        # 请求在 worker 线程里等网络（等待期间释放 GIL），主线程补解析附件 ——
        # 这一段就是「3 篇 3144ms -> 单篇量级」的关键：慢的附件解析不再排在 LLM 前面。
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="edm-llm-defer") as ex:
            fut = ex.submit(_call_plan, plan, app_retries)
            try:
                defer()
            except Exception as e:                     # noqa: BLE001
                # 补解析失败不能让已经发出/成功的抽取白跑：只记一行，缺的附件按「未解析」参与后处理
                print(f"[警告] LLM 等待期间补解析附件失败（已忽略）：{type(e).__name__}: {e}",
                      flush=True)
            raw = fut.result()
    return _finalize(rec, raw)


def extract_with_llm_safe(rec: dict, defer=None) -> dict:
    """仅在可预期的 LLMError 时回退规则；其它异常(代码 bug)向上抛出，不掩盖。"""
    try:
        return extract_with_llm(rec, defer=defer)
    except LLMError as e:
        out = rule_build(rec)
        out["engine"] = f"rule(fallback: {e})"[:220]
        return out
