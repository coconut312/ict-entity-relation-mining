import json
import os
import zipfile
import sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.environ.get("ICT_WS") or str(Path(__file__).resolve().parents[2])
HTML = os.path.join(ROOT, "赛题五.基准测试数据_html")
ZIP = os.path.join(ROOT, "赛题五.基准测试数据_file")

for aid in ["t20260311_26261128", "t20260408_26371938"]:
    print("=" * 90)
    print("ID:", aid)
    r = json.load(open(f"out/full/{aid}/ir.json", encoding="utf-8"))
    print("--- content_text ---")
    print((r.get("content_text") or "")[:2500])
    print("--- main_item_tables ---")
    for i, g in enumerate(r.get("main_item_tables", [])):
        print(f"[T{i}]", g.get("headers"))
        for row in g.get("rows", []):
            print("   ", row)
    print("--- extracted_files ---")
    for a in r.get("extracted_files", []):
        print("  ", (a.get("rel_path") or "")[-50:], "ext=", a.get("ext"),
              "tables=", len(a.get("tables") or []), "textlen=", len(a.get("text") or ""))
    zp = os.path.join(ZIP, aid + ".zip")
    print("--- zip 内容:", zp, os.path.exists(zp), "---")
    if os.path.exists(zp):
        with zipfile.ZipFile(zp) as z:
            for n in z.namelist():
                print("   ", n)
