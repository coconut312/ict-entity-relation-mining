"""对指定/全量公告跑 LLM 抽取层（Qwen/DeepSeek），写 items_llm.json，并统计。

- 并发：只并发 LLM 调用；读 ir.json / 写 items_llm.json 按篇独立，安全。
- 断点续跑：已有 items_llm.json 的篇默认**跳过**（`FORCE=1` 强制全部重跑）。
  长跑被中断后直接重跑即可，不会重复烧 token。
- 统计：跑完对**全部** ids 统一读 items_llm.json 出篇级 7 字段分布/非空率，
  这样续跑得到的数字也是完整的。

环境变量：OUT(默认 out/sample50)、IDS(默认 sample50.txt)、N(取前 N 篇)、
         CONC(并发数,默认 1)、FORCE(1=重跑已存在的)
"""
import collections
import concurrent.futures as cf
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ict_parse import llm_extract  # noqa: E402

ROOT = os.environ.get("OUT", "out/sample50")
N = int(os.environ.get("N", "0"))
CONC = max(1, int(os.environ.get("CONC", "1")))
FORCE = os.environ.get("FORCE", "").lower() in ("1", "true", "yes", "on")
F7 = ["item_name", "category", "brand", "spec", "unit_price", "quantity", "total_price"]
NF = F7 + ["supplier_name"]


def rate_score(k: int) -> int:
    return 10 if k == 7 else 5 if k >= 5 else 3 if k >= 3 else 0


ids = [ln.strip() for ln in open(os.environ.get("IDS", "sample50.txt"), encoding="utf-8") if ln.strip()]
if N:
    ids = ids[:N]


def process(aid: str):
    """返回 (aid, rule_n, llm_n, engine, skipped)。"""
    p = os.path.join(ROOT, aid, "ir.json")
    if not os.path.exists(p):
        return None
    op = os.path.join(ROOT, aid, "items_llm.json")
    if os.path.exists(op) and not FORCE:
        try:
            ext = json.load(open(op, encoding="utf-8"))
            # 不能用 `items` 非空判「已跑」——有些篇**正确地**没有标的（items 为空，
            # 官方口径③允许），用 items 判会导致它们每次运行都被重调 API、结果还会漂移。
            # 以是否已有 engine 标记为准。
            if ext.get("engine") is not None:
                return aid, None, len(ext.get("items") or []), ext.get("engine"), True
        except Exception:  # noqa: BLE001  损坏则重跑
            pass
    rec = json.load(open(p, encoding="utf-8"))
    ext = llm_extract.extract_with_llm_safe(rec)
    with open(op, "w", encoding="utf-8") as f:
        json.dump(ext, f, ensure_ascii=False, indent=2, default=str)
    rp = os.path.join(ROOT, aid, "items.json")
    rule_n = None
    if os.path.exists(rp):
        try:
            rule_n = len(json.load(open(rp, encoding="utf-8"))["items"])
        except Exception:  # noqa: BLE001
            rule_n = None
    return aid, rule_n, len(ext["items"]), ext.get("engine"), False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(f"共 {len(ids)} 篇 | 并发 {CONC} | 已有则跳过={not FORCE} -> {ROOT}", flush=True)
    t0 = time.time()
    engines = collections.Counter()
    done = skipped = failed = 0
    with cf.ThreadPoolExecutor(max_workers=CONC) as ex:
        futs = {ex.submit(process, aid): aid for aid in ids}
        for fu in cf.as_completed(futs):
            aid = futs[fu]
            done += 1
            try:
                res = fu.result()
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  [!] {aid} 失败: {type(e).__name__}: {e}", flush=True)
                continue
            if not res:
                print(f"  [{done}/{len(ids)}] {aid} 跳过：无 ir.json", flush=True)
                continue
            _, rule_n, llm_n, eng, was_skip = res
            engines[str(eng).split("(")[0]] += 1
            if was_skip:
                skipped += 1
            if done % 25 == 0 or done == len(ids):
                el = time.time() - t0
                print(f"  [{done}/{len(ids)}] ok llm={done - failed} skip={skipped} fail={failed} "
                      f"elapsed={el:.0f}s ({el / max(1, done - skipped):.2f}s/新篇)", flush=True)

    # ---- 统一统计（对全部 ids，包含本次跳过的）----
    dist: dict[int, int] = {}
    nn = {f: 0 for f in NF}
    n_items = 0
    cov_html = cov_att = n_ok = 0
    missing = []
    for aid in ids:
        op = os.path.join(ROOT, aid, "items_llm.json")
        if not os.path.exists(op):
            missing.append(aid)
            continue
        try:
            ext = json.load(open(op, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            missing.append(aid)
            continue
        items = ext.get("items") or []
        n_ok += 1
        n_items += len(items)
        # 覆盖率口径：LLM 条目的 source 统一是 "LLM"，无法按来源区分，
        # 故改看「该篇 IR 里是否存在原文标的表 / 附件表，且本篇确有抽取结果」。
        try:
            rec = json.load(open(os.path.join(ROOT, aid, "ir.json"), encoding="utf-8"))
        except Exception:  # noqa: BLE001
            rec = {}
        if items and any(g.get("rows") for g in rec.get("main_item_tables", [])):
            cov_html += 1
        if items and any((a.get("tables") or []) for a in rec.get("extracted_files", [])):
            cov_att += 1
        present = {f for f in F7 if any(it.get(f) not in (None, "", []) for it in items)}
        dist[len(present)] = dist.get(len(present), 0) + 1
        for f in NF:
            nn[f] += sum(1 for it in items if it.get(f) not in (None, "", []))

    el = time.time() - t0
    print(f"\n=== LLM 全量统计 ===\n有结果 {n_ok} 篇 | 缺 {len(missing)} 篇 {missing[:5]}", flush=True)
    print(f"engine 分布: {dict(engines)}", flush=True)
    print(f"items 总数: {n_items}", flush=True)
    print(f"原文类有标的: {cov_html} 篇 | 附件类有标的: {cov_att} 篇", flush=True)
    if n_items:
        print("字段非空率:", {f: round(nn[f] / n_items, 3) for f in NF}, flush=True)
    print("篇级 7 字段达成:", dict(sorted(dist.items(), reverse=True)), flush=True)
    tot = sum(rate_score(k) * v for k, v in dist.items())
    print(f"平均提取率得分 = {tot}/{n_ok * 10} = {tot / max(1, n_ok):.2f}/10", flush=True)
    print(f"耗时 {el:.0f}s（{el / max(1, len(ids)):.2f}s/篇 墙钟，含本次跳过的）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
