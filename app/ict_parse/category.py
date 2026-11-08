"""政府采购品目分类目录：装载 + 按名称最长匹配推断品目。"""
from __future__ import annotations

import csv
import os
from functools import lru_cache

_CSV = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "gov_category.csv")


@lru_cache(maxsize=1)
def _load() -> list[tuple[str, str, str, int]]:
    names: list[tuple[str, str, str, int]] = []
    with open(_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            nm = row["name"]
            lv = int(row["level"])
            # 跳过顶层(货物/通用设备…)与过短的通用名(≤2字易误配，如「模型」「机柜」)
            if len(nm) >= 3 and lv >= 2:
                names.append((nm, row["code"], row["path"], lv))
    names.sort(key=lambda x: -len(x[0]))  # 最长名优先
    return names


def infer_category(*texts) -> dict | None:
    """在给定文本里用最长品目名匹配，返回 {code,name,path} 或 None。"""
    text = " ".join(str(t) for t in texts if t)
    if not text:
        return None
    for nm, code, path, _lv in _load():
        if nm in text:
            return {"code": code, "name": nm, "path": path}
    return None


def size() -> int:
    return len(_load())
