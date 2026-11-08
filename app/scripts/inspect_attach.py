import json
import os
import re
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for aid in ["t20260311_26261128", "t20260408_26371938"]:
    print("#" * 90)
    r = json.load(open(f"out/full/{aid}/ir.json", encoding="utf-8"))
    ct = r.get("content_text") or ""
    print(f"[{aid}] content_text len={len(ct)}")
    print("---- content_text ----")
    print(ct[:3000])
    print("---- main_item_tables ----")
    for i, g in enumerate(r.get("main_item_tables", [])):
        print(f"  T{i}", g.get("headers"))
        for row in g.get("rows", [])[:3]:
            print("      ", row)
    print("---- 附件 ----")
    z = f"../赛题五.基准测试数据_file/{aid}.zip"
    if not os.path.exists(z):
        print("  无 zip")
        continue
    tmp = tempfile.mkdtemp()
    with zipfile.ZipFile(z) as zf:
        zf.extractall(tmp)
    for root, _, files in os.walk(tmp):
        for f in files:
            p = os.path.join(root, f)
            ext = f.lower().rsplit(".", 1)[-1]
            print(f"   - {f[:60]}  ({os.path.getsize(p)}B, .{ext})")
            if ext == "docx":
                try:
                    from docx import Document
                    d = Document(p)
                    print(f"       docx: 段落={len(d.paragraphs)} 表格={len(d.tables)}")
                    for ti, t in enumerate(d.tables[:2]):
                        print(f"       表{ti} {len(t.rows)}x{len(t.columns)}:")
                        for row in t.rows[:3]:
                            print("         ", " | ".join(c.text.strip()[:16] for c in row.cells))
                    for para in d.paragraphs:
                        if re.search(r"单价|总价|金额|报价|元", para.text) and para.text.strip():
                            print("        P>", para.text.strip()[:100])
                except Exception as e:
                    print("       docx 打不开:", repr(e)[:120])
