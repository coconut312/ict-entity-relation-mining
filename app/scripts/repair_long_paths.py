"""修复 out/full 中超出 Windows MAX_PATH 的附件目录（历史遗留）。

背景
----
套娃 zip（zip 里再套 zip）里的超长中文目录名，会让单篇的附件树突破 260 字符。
Python(UCRT) 仍能写入，但 PowerShell / Explorer / git / 7-Zip 都无法遍历，
导致统计失真、打包提交失败；在未开启长路径的机器上还可能直接写失败。
`ict_parse.attach._fit_path` 已让**新解析**自动压缩这类路径，
本脚本负责：① 找出历史遗留的超长篇；② 删掉重解析（路径即被压缩）。

用法
----
    uv run python scripts/repair_long_paths.py --dry-run     # 只看哪些篇受影响
    uv run python scripts/repair_long_paths.py               # 修复
    uv run python scripts/repair_long_paths.py --out out/full
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ict_parse.attach import MAX_PATH_LEN  # noqa: E402
from ict_parse.pipeline import process_one  # noqa: E402

WS = os.environ.get("ICT_WS") or str(Path(__file__).resolve().parents[2])
HTML_DIR = os.path.join(WS, "赛题五.基准测试数据_html")
ZIP_DIR = os.path.join(WS, "赛题五.基准测试数据_file")

# 与新逻辑（attach._fit_path 保证 <=235）对齐：凡 >=235 的一律重解析，
# 使其降到 ~158 字符；<235 的路径 Windows 各工具都能正常遍历，无需动。
SCAN_LIMIT = MAX_PATH_LEN
_LP = "\\\\?\\"


def _lp(p: str) -> str:
    """超长路径加 \\\\?\\ 前缀，绕过 MAX_PATH 限制（供本脚本自身遍历/删除用）。"""
    if len(p) >= 240 and not p.startswith(_LP):
        return _LP + os.path.abspath(p)
    return p


def _strip(p: str) -> str:
    return p[4:] if p.startswith(_LP) else p


def longest_in(dirpath: str) -> int:
    """该目录树下最长文件路径的长度（scandir 走 \\\\?\\，避免自身被长路径卡住）。"""
    mx = 0
    stack = [dirpath]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(_lp(d)) as it:
                for e in it:
                    p = os.path.join(d, e.name)
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    if is_dir:
                        stack.append(p)
                    else:
                        mx = max(mx, len(_strip(p)))
        except OSError:
            pass
    return mx


def scan(out_root: str) -> dict[str, int]:
    bad: dict[str, int] = {}
    if not os.path.isdir(out_root):
        return bad
    for aid in sorted(os.listdir(out_root)):
        ad = os.path.join(out_root, aid, "attachments")
        if not os.path.isdir(_lp(ad)):
            continue
        n = longest_in(ad)
        if n > SCAN_LIMIT:          # _fit_path 允许恰好等于上限，故只在“超过”时报
            bad[aid] = n
    return bad


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(WS, "app", "out", "full"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bad = scan(args.out)
    print(f"[扫描] 附件树超长(>{SCAN_LIMIT})的篇：{len(bad)}")
    for aid, n in sorted(bad.items(), key=lambda x: -x[1]):
        print(f"   {aid}  最长 {n} 字符")
    if not bad or args.dry_run:
        print("（dry-run，未做修改）" if args.dry_run else "（无需修复）")
        return 0

    t0 = time.time()
    fixed = failed = 0
    for i, aid in enumerate(sorted(bad), 1):
        html = os.path.join(HTML_DIR, aid + ".html")
        zp = os.path.join(ZIP_DIR, aid + ".zip")
        if not os.path.exists(html):
            print(f"[{i}/{len(bad)}] {aid} 跳过：无 html")
            failed += 1
            continue
        out_dir = os.path.join(args.out, aid)
        # 若原篇走过 VLM，重解析时必须继续带 VLM，否则会丢掉已识别的扫描件文本
        need_vlm = False
        try:
            with open(os.path.join(out_dir, "ir.json"), encoding="utf-8") as f:
                old = json.load(f)
            need_vlm = any("VLM" in (a.get("note") or "") for a in old.get("extracted_files", []))
        except OSError:
            pass
        shutil.rmtree(_lp(out_dir), ignore_errors=True)
        try:
            process_one(aid, html, zp if os.path.exists(zp) else None, args.out, use_vlm=need_vlm)
            fixed += 1
            print(f"[{i}/{len(bad)}] {aid} 已重解析 (vlm={need_vlm}) 用时 {time.time() - t0:.0f}s")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[{i}/{len(bad)}] {aid} 失败: {type(e).__name__}: {e}")

    left = scan(args.out)
    print(f"\n[结果] 重解析 {fixed} 篇 / 失败 {failed} 篇 | 剩余超长篇 {len(left)} | 总耗时 {time.time() - t0:.0f}s")
    if left:
        for aid, n in sorted(left.items(), key=lambda x: -x[1])[:10]:
            print(f"   仍超长 {aid} {n}")
    return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
