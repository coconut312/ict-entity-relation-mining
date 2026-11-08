"""验证集精度评测（**命题方指定的二分类精度指标迁移口径**）：gold/<id>.json vs 预测。

=============================== 官方口径 ===============================
命题方原文：
    评估正确率的下限以整数部分为准，建议统一保留两位小数。三个精度参数的计算方式要
    考虑 TP, FP, TN 和 FN。本命题精度计算为二分类精度指标迁移的计算方式，建议按以下
    方式套用计算：有正确字段的记为 P 样本，无字段/缺失的记为 N 样本。TP 为有字段，
    且标注正确的数量。FP 为有字段，但标注错误的数量。N 样本同理。

落到本项目（**槽位 = (公告, 字段)**，官方 7 字段）：
    P 样本：该 (公告, 字段) 槽位在 gold 中有至少 1 个非空值
    N 样本：该槽位在 gold 中无值/缺失（该公告全部条目该字段都为空）

计数规则（**关键，务必看清；输出里也会再打印一遍**）：
    TP   值粒度  ：预测有值且与 gold 匹配的值数            = |gold值集 ∩ pred值集|
    FN   值粒度  ：gold 有值但预测缺失的值数              = |gold值集 - pred值集|
    FP   ① 值粒度：预测有值但 gold 中不存在的值数         = |pred值集 - gold值集|（只出现在 P 样本）
         ② 槽位粒度：N 槽位被预测出值的**槽位数**，一个 N 槽位无论预测出几个值都只记 1
         FP = ① + ②
    TN   槽位粒度：N 槽位且预测为空的槽位数
    ==> **FP/TN 在 N 样本上为槽位粒度**；输出另给一列 `FP值粒度`( = 规则① ) 作诊断，
        并给出 `FP槽位(N)`( = 规则② )，保证 FP = FP值粒度 + FP槽位(N) 可核对。

指标（全部统一保留两位小数）：
    P  = TP/(TP+FP)      R  = TP/(TP+FN)
    F1 = 2PR/(P+R)       A  = (TP+TN)/(TP+TN+FP+FN)
    「下限」= 截断(floor)到两位小数：0.9876 -> 0.98（**不是四舍五入**）
    另给「按百分比整数口径的下限」= floor(值×100)%，如 0.9876 -> 98%

值粒度一律按**集合**判定"存在/不存在"：同一槽位内重复出现的同一个值只算 1 个
（gold=['A','A'] vs pred=['A'] 算该值已命中）。重复值是否计重的影响见文末诊断段。

旧口径（集合 F1：TP=交集/FP=多出/FN=漏掉，不含 TN、无 P/N 样本概念）**已停用**，
仅在文末以「旧口径(参考)」单列对照，绝不出现在【官方口径】段里冒充新口径。

保留的两种比对归一（**归一化/空值语义沿用现有实现，未改**）：
    默认 = 语义口径（数值按数值比、NFKC 全半角归一、去空白）
    --raw = 字面口径（只 strip，格式敏感，仅供诊断）

预测来源：
    --src pred（默认）：LLM 预测（valset/pred/<id>.json，缺失则回退 out/full/<id>/items_llm.json）
    --src rule        ：规则层预测（out/full/<id>/items.json）

其他：
    --json out.json：把完整结果落盘便于复核
    --selftest     ：硬编码小样例自证 P/R/F1/A 与手算一致（assert，必须通过）
    --supplier     ：额外输出 supplier_name（官方 7 字段之外，默认不出）
"""
import argparse
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _metric import (Counter, count_slot, lower_bound, metrics,  # noqa: E402
                     selftest, trunc2)

ROOT = "out/full"
VDIR = "valset"
GOLD = os.path.join(VDIR, "gold")
# 命题方给定 7 个评测字段；supplier_name 为项目扩展字段，用 --supplier 才纳入
FIELDS = ["item_name", "category", "brand", "spec", "unit_price", "quantity", "total_price"]
EXTRA_FIELDS = ["supplier_name"]
NUM_FIELDS = {"unit_price", "total_price", "quantity"}
# 空值/缺失的各种写法（沿用旧口径并补齐；只列明确的“无值”写法，避免误杀真值）
NULLISH = {"", "-", "--", "—", "nan", "none", "null", "n/a", "na",
           "未公示", "未公开", "暂无", "无"}


def norm(v, field=None, raw=False):
    """值归一。raw=True 时只 strip（原始口径）。

    默认口径做**语义等价归一**，避免把「相等的值」判成错：
    - 数值字段：按数值比较（264800.0 == 264800）；
    - 文本字段：NFKC 全/半角统一（；vs ;、（vs (）+ 去空白；
    - category：去尾部「类」+ 品目路径分隔符归一 + **父子级等价**（截到前 2 段），
      例如 `服务类`≡`服务`、`货物/图书和档案/期刊`≡`货物/图书和档案/期刊/其他期刊`。
      注意：不做宽泛同义词合并（`信息技术服务` 与 `服务` 仍判为不同，属真实差异）；
    - item_name：去编号前缀与项目符号（`●汽车电商…` ≡ `汽车电商…`）；
    - spec：分隔符归一（`;；,，、` → `;`）；
    - brand：清掉「详见…/招标文件…」类**非品牌文案**（与 ict_parse/clean.py 同口径）。
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if raw:
        return s
    if field in NUM_FIELDS:
        try:
            return "%.2f" % float(s)
        except ValueError:
            pass
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", "", s)
    if field == "category":
        s = re.sub(r"类$", "", s)
        s = re.sub(r"[/>＞、]+", "/", s)
        segs = [x for x in s.split("/") if x]
        s = "/".join(segs[:2]) if len(segs) >= 2 else (segs[0] if segs else "")
    elif field == "item_name":
        s = re.sub(r"^[（(]?\d{1,2}[）)、.．]\s*", "", s)
        s = re.sub(r"^[●○·•*★☆\-–—>》]+", "", s)
    elif field == "spec":
        s = re.sub(r"[;；,，、]+", ";", s)
    elif field == "brand":
        if re.search(r"(详见|招标文件|投标文件|响应文件|满足.*要求)", s):
            return None
    return s or None


def is_null(v):
    """判断原始值是否为空/缺失：None、纯空白、'nan'、'None'、'未公示' 等。"""
    if v is None:
        return True
    s = str(v).strip()
    if not s:
        return True
    return s.lower() in NULLISH or s in NULLISH


def ok(v, field=None, raw=False):
    """判空 + 归一 一步到位：返回归一化后的非空值，空/缺失返回 None。"""
    if is_null(v):
        return None
    return norm(v, field, raw)


def slot_vals(items, f, raw=False):
    """取某公告在字段 f 上的**全部非空归一值**（该槽位的值集来源）。

    槽位 = (公告, 字段)：一篇公告有 N 个条目时，同字段的 N 个值共同构成该槽位的
    值集，这正是官方"多值字段按值粒度计数"的落点。
    """
    out = []
    for it in items:
        x = ok(it.get(f), f, raw)
        if x is not None:
            out.append(x)
    return out


def set_prf(gs, ps):
    """旧「集合 F1」口径（TP=交集/FP=多出/FN=漏掉），仅历史对照，已停用。"""
    tp = len(gs & ps)
    fp = len(ps - gs)
    fn = len(gs - ps)
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return tp, fp, fn, p, r, f1


def legacy_count(gold, pred, fields, raw):
    """历史脚本口径：只用 norm() 判空（不认 'nan'/'未公示'），且逐篇取集合比对。

    这样算出的才是 docs/ 里引用的旧 F1（0.989 / 0.547 / …），用于「旧口径(参考)」段。
    """
    agg = {f: {"tp": 0, "fp": 0, "fn": 0} for f in fields}
    for f in fields:
        gs = {norm(it.get(f), f, raw) for it in gold if norm(it.get(f), f, raw)}
        ps = {norm(it.get(f), f, raw) for it in pred if norm(it.get(f), f, raw)}
        tp, fp, fn, _, _, _ = set_prf(gs, ps)
        agg[f]["tp"] += tp
        agg[f]["fp"] += fp
        agg[f]["fn"] += fn
    return agg


def run(ids, src, raw=False, fields=FIELDS):
    """逐篇逐槽位按官方口径计数。

    返回 (用上的 id 列表, 条目数, {字段: Counter}, {字段: Counter 多重集诊断},
          旧口径7字段, 旧口径8字段)。
    """
    agg = {f: Counter() for f in fields}                 # 官方口径（集合去重）
    agg_ms = {f: Counter() for f in fields}              # 诊断：多重集（重复值计重）
    legacy7 = {f: {"tp": 0, "fp": 0, "fn": 0} for f in FIELDS}
    legacy8 = {f: {"tp": 0, "fp": 0, "fn": 0} for f in FIELDS + EXTRA_FIELDS}
    used, n_items = [], 0
    for aid in ids:
        gp = os.path.join(GOLD, aid + ".json")
        if src == "rule":
            pp = os.path.join(ROOT, aid, "items.json")
        else:
            pp = os.path.join(VDIR, "pred", aid + ".json")
            if not os.path.exists(pp):
                pp = os.path.join(ROOT, aid, "items_llm.json")
        if not os.path.exists(gp) or not os.path.exists(pp):
            continue
        gold = json.load(open(gp, encoding="utf-8"))["items"]
        pred = json.load(open(pp, encoding="utf-8"))["items"]
        used.append(aid)
        n_items += max(len(gold), len(pred))
        for f in fields:
            # 一个 (公告, 字段) 槽位：把该公告所有条目的值收成一个值集
            gs_all = slot_vals(gold, f, raw)
            ps_all = slot_vals(pred, f, raw)
            agg[f].merge(count_slot(gs_all, ps_all))
            agg_ms[f].merge(count_slot(gs_all, ps_all, multiset=True))
        # 旧口径(参考)：固定 7 字段 + 8 字段两套，便于与 docs 历史值对齐
        for f, acc in ((FIELDS, legacy7), (FIELDS + EXTRA_FIELDS, legacy8)):
            for k, v in legacy_count(gold, pred, f, raw).items():
                for kk in ("tp", "fp", "fn"):
                    acc[k][kk] += v[kk]
    return used, n_items, agg, agg_ms, legacy7, legacy8


def fmt2(x):
    return f"{x:.2f}"


def pct_line(name, value, tp, fp):
    """打印一个指标的 =公式= 值、两位小数、截断下限、百分比整数下限。"""
    print(f"  {name:<4} = {tp}/({tp}+{fp}) = {fmt2(value)}   下限 {trunc2(value):.2f}"
          f"   （按百分比整数口径的下限 {lower_bound(value)}%）")


def main():
    # Windows 控制台默认 GBK，emoji/特殊符号会 UnicodeEncodeError；这里兜底
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--src", choices=["pred", "rule"], default="pred",
                    help="pred=LLM 预测(默认)；rule=规则层 out/full/<id>/items.json")
    ap.add_argument("--raw", action="store_true",
                    help="只 strip（**格式敏感**：gold 的数值多为字符串如 '120000.0'，"
                         "此口径会把同值不同格式判成错，仅供诊断，不可作为精度结论）")
    ap.add_argument("--json", metavar="PATH", default=None, help="把完整结果落盘到该 json 文件")
    ap.add_argument("--selftest", action="store_true", help="硬编码小样例自证算术后退出")
    ap.add_argument("--supplier", action="store_true", help="额外评测 supplier_name（官方 7 字段之外）")
    args = ap.parse_args()

    # ---------------- 自证算术 ----------------
    if args.selftest:
        r = selftest()
        print("== --selftest 算术自证（硬编码小样例，不读 valset 数据）==")
        print("样例 3 个槽位：gold=[A] vs pred=[A] ／ gold=[B,C] vs pred=[B,X] ／ gold=[] vs pred=[]")
        print(f"  官方计数：TP={r['tp']} FP={r['fp']}（=FP值粒度 {r['fp_val']} + FP槽位(N) "
              f"{r['fp_nslot']}） TN={r['tn']} FN={r['fn']} | P 样本={r['np']} N 样本={r['nn']}")
        print(f"  P  = TP/(TP+FP) = {r['tp']}/({r['tp']}+{r['fp']}) = {r['P']:.4f} → 两位 {fmt2(r['P'])}")
        print(f"  R  = TP/(TP+FN) = {r['tp']}/({r['tp']}+{r['fn']}) = {r['R']:.4f} → 两位 {fmt2(r['R'])}")
        print(f"  F1 = 2PR/(P+R)  = {r['F1']:.4f} → 两位 {fmt2(r['F1'])}")
        print(f"  A  = (TP+TN)/(TP+TN+FP+FN) = {r['tp'] + r['tn']}/{r['tp'] + r['tn'] + r['fp'] + r['fn']}"
              f" = {r['A']:.4f} → 两位 {fmt2(r['A'])}")
        print(f"  下限（截断两位）：P {trunc2(r['P']):.2f} / R {trunc2(r['R']):.2f} / "
              f"F1 {trunc2(r['F1']):.2f} / A {trunc2(r['A']):.2f}")
        print("SELFTEST OK（全部 assert 通过：计数粒度 + P/R/F1/A 手算 + 截断下限）")
        return 0

    fields = FIELDS + (EXTRA_FIELDS if args.supplier else [])
    ids_file = os.environ.get("VALSET_IDS", os.path.join(VDIR, "valset_ids.txt"))
    ids = [ln.strip() for ln in open(ids_file, encoding="utf-8") if ln.strip()]

    used, n_items, agg, agg_ms, legacy7, legacy8 = run(ids, args.src, args.raw, fields)
    tot = Counter()
    for f in fields:
        tot.merge(agg[f])
    tot_ms = Counter()
    for f in fields:
        tot_ms.merge(agg_ms[f])
    P, R, F1, A = metrics(tot)

    l7 = {k: sum(legacy7[f][k] for f in FIELDS) for k in ("tp", "fp", "fn")}
    l8 = {k: sum(legacy8[f][k] for f in FIELDS + EXTRA_FIELDS) for k in ("tp", "fp", "fn")}
    lP7 = l7["tp"] / (l7["tp"] + l7["fp"]) if (l7["tp"] + l7["fp"]) else 0.0
    lR7 = l7["tp"] / (l7["tp"] + l7["fn"]) if (l7["tp"] + l7["fn"]) else 0.0
    lF17 = 2 * lP7 * lR7 / (lP7 + lR7) if (lP7 + lR7) else 0.0
    lP8 = l8["tp"] / (l8["tp"] + l8["fp"]) if (l8["tp"] + l8["fp"]) else 0.0
    lR8 = l8["tp"] / (l8["tp"] + l8["fn"]) if (l8["tp"] + l8["fn"]) else 0.0
    lF18 = 2 * lP8 * lR8 / (lP8 + lR8) if (lP8 + lR8) else 0.0
    mP, mR, mF1, mA = metrics(tot_ms)

    if args.raw:
        print("！！注意 raw（字面）口径 = 逐字符比对，数值格式不同即判错（gold 内 str/int 混用："
              "total_price str 383 / int 14）。这不是抽取质量差异，不可作为精度结论。")
    print(f"评测篇数 {len(used)} | 条目数 {n_items} | 槽位数 {tot.n}（= 篇数 × {len(fields)} 字段）"
          f" | 预测来源 = {'规则层 out/full' if args.src == 'rule' else 'LLM'}"
          f" | 比对归一 = {'字面(仅 strip, 格式敏感)' if args.raw else '语义等价归一（数值按数值、全半角标点归一）'}")
    if args.supplier:
        print("！！已启用 --supplier：总体合计额外含 supplier_name（**不属于官方 7 字段**），"
              "该总体的 P/R/F1/A 不得当作官方口径结论；官方结论请用不加 --supplier 的运行。")

    # ================== 【官方口径】总体 ==================
    print("\n" + "=" * 86)
    print("【官方口径】二分类精度指标迁移（P 样本 = gold 有字段；N 样本 = gold 无字段/缺失）")
    print("=" * 86)
    print("  槽位 = (公告, 字段)。计数规则：")
    print("    TP 值粒度 = 预测有值且与 gold 匹配的值数")
    print("    FN 值粒度 = gold 有值但预测缺失的值数")
    print("    FP = ①值粒度(预测有值但 gold 中不存在的值数) + ②槽位粒度(N 槽位被预测出值的槽位数)")
    print("    TN 槽位粒度 = N 槽位且预测为空的槽位数")
    print("  ==> **FP/TN 在 N 样本上为槽位粒度**（一个 N 槽位无论预测出几个值都只记 FP=1）；")
    print("      P 样本侧为值粒度。下表的 `FP值粒度` 就是规则①，`FP槽位(N)` 是规则②，")
    print(f"      两者之和恒等于官方 FP（本运行 {tot.fp} = {tot.fp_val} + {tot.fp_nslot}）。")
    print("-" * 86)
    print(f"总体（{len(fields)} 字段合计）   P 样本数 {tot.np} | N 样本数 {tot.nn}"
          f" | 槽位合计 {tot.n} | 条目录入 {n_items}")
    print(f"  TP={tot.tp}  FN={tot.fn}  FP={tot.fp}（FP值粒度 {tot.fp_val} + FP槽位(N) {tot.fp_nslot}）"
          f"  TN={tot.tn}")
    print(f"  精确率 P  = TP/(TP+FP) = {tot.tp}/({tot.tp}+{tot.fp}) = {fmt2(P)}"
          f"   下限 {trunc2(P):.2f}   （按百分比整数口径的下限 {lower_bound(P)}%）")
    print(f"  召回率 R  = TP/(TP+FN) = {tot.tp}/({tot.tp}+{tot.fn}) = {fmt2(R)}"
          f"   下限 {trunc2(R):.2f}   （按百分比整数口径的下限 {lower_bound(R)}%）")
    print(f"  F1        = 2PR/(P+R)                    = {fmt2(F1)}"
          f"   下限 {trunc2(F1):.2f}   （按百分比整数口径的下限 {lower_bound(F1)}%）")
    print(f"  准确率 A  = (TP+TN)/(TP+TN+FP+FN) = {tot.tp + tot.tn}/{tot.tp + tot.tn + tot.fp + tot.fn}"
          f" = {fmt2(A)}   下限 {trunc2(A):.2f}   （按百分比整数口径的下限 {lower_bound(A)}%）")

    # ================== 【官方口径】分字段 ==================
    print("\n【官方口径】分字段 · 计数（FP 拆成值粒度① + 槽位粒度②）")
    h1 = (f"{'字段':<14}{'P样本':>6}{'N样本':>6}{'TP':>6}{'FN':>6}{'FP':>6}"
          f"{'FP值粒度':>9}{'FP槽位(N)':>10}{'TN':>6}")
    print(h1)
    print("-" * len(h1))
    for f in fields:
        c = agg[f]
        print(f"{f:<14}{c.np:>6}{c.nn:>6}{c.tp:>6}{c.fn:>6}{c.fp:>6}"
              f"{c.fp_val:>9}{c.fp_nslot:>10}{c.tn:>6}")
    print("-" * len(h1))
    print(f"{'合计':<14}{tot.np:>6}{tot.nn:>6}{tot.tp:>6}{tot.fn:>6}{tot.fp:>6}"
          f"{tot.fp_val:>9}{tot.fp_nslot:>10}{tot.tn:>6}")

    print("\n【官方口径】分字段 · 指标（统一两位小数；下限=截断到两位）")
    h2 = (f"{'字段':<14}{'P':>7}{'P下限':>7}{'R':>7}{'R下限':>7}{'F1':>7}{'F1下限':>7}"
          f"{'A':>7}{'A下限':>7}{'下限%':>7}")
    print(h2)
    print("-" * len(h2))
    for f in fields:
        c = agg[f]
        p, r, f1, a = metrics(c)
        print(f"{f:<14}{p:>7.2f}{trunc2(p):>7.2f}{r:>7.2f}{trunc2(r):>7.2f}"
              f"{f1:>7.2f}{trunc2(f1):>7.2f}{a:>7.2f}{trunc2(a):>7.2f}{lower_bound(a):>6}%")
    print("-" * len(h2))
    print(f"{'合计':<14}{P:>7.2f}{trunc2(P):>7.2f}{R:>7.2f}{trunc2(R):>7.2f}"
          f"{F1:>7.2f}{trunc2(F1):>7.2f}{A:>7.2f}{trunc2(A):>7.2f}{lower_bound(A):>6}%")
    print("（末列「下限%」= 准确率 A 的百分比整数下限，即 floor(A×100)；"
          "P/R/F1 的百分比整数下限见上方总体段。）")

    # ================== 历史公式行 ==================
    print("\n【历史公式行·按官方口径重算，非结论】赛题综合 = 准确率×0.4 + 精确率×0.3 + 召回率×0.3")
    print(f"  准确率取 A（官方口径）        : {0.4 * A + 0.3 * P + 0.3 * R:.3f}")
    print(f"  准确率取 P（旧 docs 写法）    : {0.4 * P + 0.3 * P + 0.3 * R:.3f}")
    print("  注：公式仅为兼容 docs 历史引用而保留，数值已按官方口径重算；正式结论只看【官方口径】段。")

    # ================== 旧口径(参考) ==================
    print("\n【旧口径(参考)·已停用】集合 F1：TP=每篇值集合交集 / FP=预测多出 / FN=gold 漏掉（不含 TN）")
    print(f"  7 字段（官方字段集，可直接对照）：TP={l7['tp']} FP={l7['fp']} FN={l7['fn']} → "
          f"集合P={fmt2(lP7)} 集合R={fmt2(lR7)} 集合F1={fmt2(lF17)}")
    print(f"  8 字段（历史脚本含 supplier_name）：TP={l8['tp']} FP={l8['fp']} FN={l8['fn']} → "
          f"集合P={fmt2(lP8)} 集合R={fmt2(lR8)} 集合F1={fmt2(lF18)}")
    print("  说明：旧口径**已停用**（命题方指定二分类迁移口径；旧口径没有 TN、没有 P/N 样本概念，")
    print("        且 FP 全按值粒度累加，与官方 TP/FP/TN/FN 定义不同）。此处仅作历史对照，")
    print("        行内已带「旧口径(参考)」标签，不参与【官方口径】任何数值。")
    if not args.supplier:
        print("  另：supplier_name 不在官方 7 字段内，默认不计入；加 --supplier 可单列。")

    # ================== 多值/重复值诊断 ==================
    print("\n【多值字段诊断】同一槽位内重复出现的相同值是否计重（官方口径按集合去重）")
    print(f"  集合去重（官方口径，本次采用）：TP={tot.tp} FP={tot.fp} TN={tot.tn} FN={tot.fn}"
          f" → P={fmt2(P)} R={fmt2(R)} F1={fmt2(F1)}")
    print(f"  多重集计重（仅诊断）          ：TP={tot_ms.tp} FP={tot_ms.fp} TN={tot_ms.tn} FN={tot_ms.fn}"
          f" → P={fmt2(mP)} R={fmt2(mR)} F1={fmt2(mF1)}")
    print("  规则依据：官方原文是「gold 中不存在的值数」「预测缺失的值数」，属于「值是否存在」的判定，")
    print("            故同一槽位内重复值只按 1 个值计；上面的差异即该约定带来的影响量级。")
    print(f"  另：若把 N 槽位的每个预测值也按值粒度展开（放弃槽位粒度），官方 FP 会由 {tot.fp} 变成"
          f" {tot.fp_val_expanded}（诊断，非官方口径）。")

    # 结构性恒等式自检：任一断言失败即说明计数/聚合有 bug，不允许静默通过
    assert tot.fp == tot.fp_val + tot.fp_nslot, "FP 必须 = 值粒度分量① + 槽位粒度分量②"
    assert tot.nn == tot.tn + tot.fp_nslot, "N 样本必须恰好分为 TN 或 FP(槽位)"
    assert tot.n == len(used) * len(fields), "槽位数必须 = 篇数 × 字段数"
    assert tot.fp_val_expanded >= tot.fp, "全展开值粒度 FP 不应小于官方 FP"
    print(f"  结构自检：FP={tot.fp}=FP值粒度{tot.fp_val}+FP槽位(N){tot.fp_nslot} ✓ ｜ "
          f"N样本{tot.nn}=TN{tot.tn}+FP槽位(N){tot.fp_nslot} ✓ ｜ "
          f"槽位{tot.n}=篇数{len(used)}×字段{len(fields)} ✓")

    # ================== 落盘 ==================
    if args.json:
        art = {
            "config": {"src": args.src, "raw": args.raw, "fields": fields,
                       "ids_file": ids_file, "papers_used": len(used),
                       "items": n_items, "slots": tot.n},
            "definitions": {
                "slot": "(announcement_id, field)",
                "P_sample": "gold 该槽位有至少一个非空值",
                "N_sample": "gold 该槽位无值/缺失",
                "TP": "值粒度：预测有值且与 gold 匹配的值数",
                "FN": "值粒度：gold 有值但预测缺失的值数",
                "FP": "①值粒度(预测有值但 gold 中不存在的值数) + ②槽位粒度(N 槽位被预测出值的槽位数)",
                "TN": "槽位粒度：N 槽位且预测为空的槽位数",
                "granularity_note": "FP/TN 在 N 样本上为槽位粒度；P 样本侧为值粒度",
                "value_equality": "集合去重：同一槽位内重复出现的相同值只算 1 个",
                "lower_bound": "截断(floor)到两位小数；百分比整数下限 = floor(值×100)%",
            },
            "official": {
                "total": dict(tp=tot.tp, fn=tot.fn, tn=tot.tn, fp=tot.fp,
                              fp_val=tot.fp_val, fp_nslot=tot.fp_nslot,
                              fp_val_expanded=tot.fp_val_expanded,
                              n_p=tot.np, n_n=tot.nn, slots=tot.n,
                              P=fmt2(P), R=fmt2(R), F1=fmt2(F1), A=fmt2(A),
                              P_lower=trunc2(P), R_lower=trunc2(R),
                              F1_lower=trunc2(F1), A_lower=trunc2(A),
                              P_pct_lower=lower_bound(P), R_pct_lower=lower_bound(R),
                              F1_pct_lower=lower_bound(F1), A_pct_lower=lower_bound(A)),
                "per_field": {f: dict(
                    tp=agg[f].tp, fn=agg[f].fn, tn=agg[f].tn, fp=agg[f].fp,
                    fp_val=agg[f].fp_val, fp_nslot=agg[f].fp_nslot,
                    fp_val_expanded=agg[f].fp_val_expanded,
                    n_p=agg[f].np, n_n=agg[f].nn,
                    P=fmt2(metrics(agg[f])[0]), R=fmt2(metrics(agg[f])[1]),
                    F1=fmt2(metrics(agg[f])[2]), A=fmt2(metrics(agg[f])[3]),
                    P_lower=trunc2(metrics(agg[f])[0]),
                    R_lower=trunc2(metrics(agg[f])[1]),
                    F1_lower=trunc2(metrics(agg[f])[2]),
                    A_lower=trunc2(metrics(agg[f])[3])) for f in fields},
                "diagnostic_multiset": dict(
                    tp=tot_ms.tp, fn=tot_ms.fn, tn=tot_ms.tn, fp=tot_ms.fp,
                    P=fmt2(mP), R=fmt2(mR), F1=fmt2(mF1), A=fmt2(mA),
                    note="重复值计重的替代口径，仅供诊断，非官方口径"),
            },
            "legacy_setf1_reference_disabled": {
                "fields_7": dict(**l7, P=fmt2(lP7), R=fmt2(lR7), F1=fmt2(lF17)),
                "fields_8": dict(**l8, P=fmt2(lP8), R=fmt2(lR8), F1=fmt2(lF18)),
                "status": "已停用",
                "reason": "命题方指定二分类迁移口径；旧口径无 TN、无 P/N 样本概念，FP 全按值粒度累加",
            },
            "historical_formula": {
                "acc_is_A": round(0.4 * A + 0.3 * P + 0.3 * R, 3),
                "acc_is_P": round(0.4 * P + 0.3 * P + 0.3 * R, 3),
                "note": "公式保留（docs 历史引用），数值按官方口径重算，非结论",
            },
            "ids_used": used,
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(art, fh, ensure_ascii=False, indent=2)
        print(f"\n结果已落盘：{args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
