"""文档 → 图片 渲染：.doc/.rtf/.wps/.docx --(soffice)--> PDF；PDF --(pypdfium2)--> 图片。"""
from __future__ import annotations

import os
import shutil
import subprocess

import pypdfium2 as pdfium

SOFFICE_CANDIDATES = [
    shutil.which("soffice"),
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]


def find_soffice() -> str | None:
    for c in SOFFICE_CANDIDATES:
        if c and os.path.exists(c):
            return c
    return None


def doc_to_pdf(src: str, out_dir: str, timeout: int = 180) -> str | None:
    """用 LibreOffice 无头把办公文档转成 PDF，返回生成的 PDF 路径。"""
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError("未找到 soffice（LibreOffice 未安装）")
    os.makedirs(out_dir, exist_ok=True)
    out_dir = os.path.abspath(out_dir)
    import tempfile
    profile = tempfile.mkdtemp(prefix="lo_prof_")   # profile 放临时目录，用完清理，不污染产出目录
    uri = "file:///" + profile.replace("\\", "/").replace(" ", "%20")
    cmd = [
        soffice, "--headless", "--norestore", "--nolockcheck",
        f"-env:UserInstallation={uri}",
        "--convert-to", "pdf", "--outdir", out_dir, src,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    base = os.path.splitext(os.path.basename(src))[0] + ".pdf"
    out = os.path.join(out_dir, base)
    return out if os.path.exists(out) else None


def pdf_to_images(pdf_path: str, out_dir: str, scale: float = 2.0,
                  max_pages: int = 6, fmt: str = "png") -> list[str]:
    """把 PDF 前 max_pages 页渲染成图片，返回图片路径列表。"""
    os.makedirs(out_dir, exist_ok=True)
    pdf = pdfium.PdfDocument(pdf_path)
    paths: list[str] = []
    for i in range(min(len(pdf), max_pages)):
        img = pdf[i].render(scale=scale).to_pil()
        p = os.path.join(out_dir, f"page{i + 1}.{fmt}")
        img.save(p)
        paths.append(p)
    return paths
