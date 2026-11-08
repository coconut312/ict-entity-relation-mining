import glob
import json
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ict_parse import attach

# 1) 直接验证 _do_docx 现在能取到表格
z = "../赛题五.基准测试数据_file/t20260408_26371938.zip"
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(z) as zf:
    zf.extractall(tmp)
docx = glob.glob(os.path.join(tmp, "**", "*.docx"), recursive=True)[0]
a = attach.Attachment(name=os.path.basename(docx), rel_path=docx, ext=".docx", size=0)
attach._do_docx(a, docx)
print("_do_docx 验:", os.path.basename(docx)[:40], "| tables=", len(a.tables or []),
      "| text_len=", len(a.text or ""))

# 2) 统计有多少篇的 docx 附件名含 报价/明细/标的（会受益）
K = ("报价", "明细", "标的", "一览", "开标")
hit = []
for d in os.listdir("out/full"):
    p = f"out/full/{d}/ir.json"
    if not os.path.exists(p):
        continue
    r = json.load(open(p, encoding="utf-8"))
    for af in r.get("extracted_files", []):
        nm = af.get("rel_path") or ""
        if (af.get("ext") == ".docx") and any(k in nm for k in K):
            hit.append(d)
            break
print("含 docx 报价类附件的篇数:", len(hit), hit[:10])
