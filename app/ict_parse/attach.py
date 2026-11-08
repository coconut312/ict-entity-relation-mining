"""附件 zip 的递归解压（含 zip 套娃）+ 类型分发 + 文本抽取（PDF/office/图片→Qwen-VL）。"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

from .models import Attachment

ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f]')
MAX_TEXT = 20000
# csv 附件最多读多少行（防止误传的超大 csv 把内存打满；正常报价表远小于此）
MAX_CSV_ROWS = 20000
# Windows 自带 bsdtar(libarchive) 能直接读 rar/7z（实测 bsdtar 3.8.4）。
# 之前 _extract 遇到 .rar/.7z 只记一句「external tool needed」就跳过 —— 实测 23 篇共 34 个
# 嵌套压缩包因此被静默丢弃（内含报价明细/标的介绍等关键表）。
TAR_BIN = shutil.which("tar") or r"C:\Windows\system32\tar.exe"
ARCN_EXT = (".rar", ".7z")
# Windows MAX_PATH=260：套娃 zip 的深层中文目录名很容易撑爆（实测最长 258 字符），
# 一旦超出，PowerShell/Explorer/git/压缩工具都无法遍历，且在未开启长路径的评测机上会直接写失败。
MAX_PATH_LEN = 235

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # 老式 .doc/.xls/.wps
OFFICE_EXT = {".doc", ".docx", ".rtf", ".wps", ".docm"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
_QUOTE_KEYS = ("报价", "一览", "分项", "标的", "开标")

MB = 1048576

# ==================================================================
# 解压预算：**跨递归共享累计**（zip bomb / 套娃压缩包）
# ==================================================================
#
# 已确认的缺陷（P1：套娃 zip 绕过解压上限）：
#   * `pipeline_impl._unzip()` 只统计**最外层**上传 zip 的条目数与字节数；
#   * `attach._extract()` / `_extract_archive()` 递归解内层 zip / rar / 7z 时
#     **完全没有预算** —— 外层 51 KB 的 zip，内层再来一个 zip，内层里放 50 MB
#     的文件，照样被解开落盘（实测）。
#
# 修法：预算做成一**个对象**（`UnzipBudget`），由 `extract_bundle()` 创建后沿
# 递归一路传递，`used / files` 跨层共享；`_unzip()` 与这里用**同一套上限**
# （`unzip_limits()`，pipeline_impl 直接调用本函数，避免两处口径漂移）。


class UnzipBudgetExceeded(RuntimeError):
    """解压后内容超出上限（zip bomb / 套娃压缩包）。

    单独成一个异常类型（而不是 ValueError）：调用链上有好几处
    `except Exception` 的单篇容错，它们**必须**把这个异常重新抛出去 ——
    否则超限只会被记成一篇的 note，job 反而「成功」。
    """


def unzip_limits() -> tuple[int, int, int]:
    """解压上限 -> `(单文件字节, 解压总字节, 文件数)`。

    三个值都可覆盖（与 `pipeline_impl._unzip_limits` 共用本函数）：
      * `EDM_MAX_UNZIP_MB`      解压**总量**上限，默认 `4 × MAX_UPLOAD_MB`；
      * `EDM_MAX_UNZIP_FILE_MB` **单文件**上限，默认 = 总量上限（与改造前一致）；
      * `EDM_MAX_ZIP_FILES`     条目数上限，默认 = 文件夹上传的文件数上限。
    """
    base_mb, files = 2048, 20000
    try:
        from app.config import settings                      # noqa: PLC0415
        base_mb = int(settings.MAX_UPLOAD_MB)
        files = int(settings.MAX_FOLDER_FILES)
    except Exception:                                        # noqa: BLE001
        pass
    total_mb = int(os.getenv("EDM_MAX_UNZIP_MB") or max(1, base_mb) * 4)
    single_mb = int(os.getenv("EDM_MAX_UNZIP_FILE_MB") or total_mb)
    files = int(os.getenv("EDM_MAX_ZIP_FILES") or files)
    return (max(1, single_mb) * MB, max(1, total_mb) * MB, max(1, files))


class UnzipBudget:
    """一次导入任务共享的解压预算（跨 zip / rar / 7z 递归累计）。"""

    __slots__ = ("single", "total", "max_files", "used", "files")

    def __init__(self, single: int | None = None, total: int | None = None,
                 max_files: int | None = None) -> None:
        s, t, f = unzip_limits()
        self.single = int(single or s)          # 单文件上限（字节）
        self.total = int(total or t)            # 累计上限（字节）
        self.max_files = int(max_files or f)    # 累计条目数上限
        self.used = 0
        self.files = 0

    def begin(self, name: str, declared: int = 0) -> None:
        """开始解一个条目：先按**声明大小**判 —— 能拦住的连一个字节都不落盘。"""
        self.files += 1
        if self.files > self.max_files:
            raise UnzipBudgetExceeded(
                f"解压后文件数超过上限 {self.max_files}")
        if declared and declared > self.single:
            raise UnzipBudgetExceeded(
                f"解压后单个文件 {name} 为 {declared / MB:.1f} MB，"
                f"超过上限 {self.single / MB:.0f} MB")

    def add(self, nbytes: int, name: str = "") -> None:
        """累计已写出的字节数（按**实际**字节计：zip 里的声明大小可以撒谎）。"""
        self.used += nbytes
        if self.used > self.total:
            raise UnzipBudgetExceeded(
                f"解压后合计 {self.used / MB:.1f} MB 超过上限 "
                f"{self.total / MB:.0f} MB（{name or '套娃压缩包'}）")

    def reserve(self, nbytes: int, n_files: int, name: str = "") -> None:
        """bsdtar 这类「先整包解到临时目录」的路径：搬入之前把整棵树先记账。"""
        if self.files + n_files > self.max_files:
            raise UnzipBudgetExceeded(
                f"解压后文件数 {self.files + n_files} 超过上限 {self.max_files}")
        if nbytes and self.used + nbytes > self.total:
            raise UnzipBudgetExceeded(
                f"解压后合计 {(self.used + nbytes) / MB:.1f} MB 超过上限 "
                f"{self.total / MB:.0f} MB（{name or '压缩包'}）")
        self.files += n_files
        self.used += nbytes



def _is_quote(name: str) -> bool:
    return any(k in (name or "") for k in _QUOTE_KEYS)


def _sanitize(name: str) -> str:
    parts = name.replace("\\", "/").split("/")
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p in ("", ".", ".."):
            continue
        p = ILLEGAL.sub("_", p)
        out.append(p)
    return os.path.join(*out) if out else "_"


def _fix_zip_name(info: zipfile.ZipInfo) -> str:
    """修正 Windows 生成的 zip 中 GBK 文件名（未置 UTF-8 flag 时）。"""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("gbk")
    except Exception:
        return info.filename


def _sniff(path: str) -> str:
    """按文件头判断真实格式（不信任扩展名）。"""
    try:
        with open(path, "rb") as f:
            h = f.read(8)
    except OSError:
        return ""
    if h[:8] == OLE2_MAGIC:
        return "ole2"
    if h[:5] == b"{\\rtf":
        return "rtf"
    if h[:2] == b"PK":
        return "zip"
    if h[:5] == b"%PDF-":
        return "pdf"
    return "other"


def _fit_path(root_dir: str, rel: str) -> str:
    """路径过长时压缩为「首层目录/_deep/<截断名>-<hash><后缀>」，保证不超 Windows MAX_PATH。

    只在确实超长时改写，正常附件路径原样返回（不影响既有 out/full 结构）。
    用 rel 的 sha1 前 8 位做去重后缀，避免不同深路径截断后互相覆盖。
    """
    if len(os.path.join(root_dir, rel)) <= MAX_PATH_LEN:
        return rel
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    lead = parts[0] if parts else ""
    base = parts[-1] if parts else rel
    stem, dot, ext = base.rpartition(".")
    if not dot:                      # 无后缀
        stem, ext = base, ""
    stem = stem.rstrip(" .") or "f"  # Windows 文件名不允许以空格/点结尾
    tag = hashlib.sha1(rel.encode("utf-8", "replace")).hexdigest()[:8]
    keep = max(12, 46 - len(tag))
    stem = stem[:keep].rstrip(" .") or "f"
    lead = (lead[:16].rstrip(" .") + os.sep) if lead else ""
    name = f"{stem}-{tag}" + (f".{ext}" if ext else "")

    def build(n: str) -> str:
        return lead + "_deep" + os.sep + n

    cand = build(name)
    while len(os.path.join(root_dir, cand)) > MAX_PATH_LEN and len(name) > 16:
        name = name[:-4].rstrip(" .") + (f".{ext}" if ext else "")
        cand = build(name)
    return cand


def _extract_archive(arc_path: str, root_dir: str, prefix: str, files: list[Attachment],
                     depth: int, max_depth: int, archive_kind: str,
                     budget: UnzipBudget | None = None) -> int:
    """用 bsdtar 解 rar/7z，再按 _fit_path 搬进附件树（返回解出的文件数）。

    先解到**短路径临时目录**再搬入：压缩包内部的中文深层目录名常超 Windows 260 字符，
    直接在目标树里解会写失败（libarchive 不做长路径规避）。

    **解压预算**（P1：套娃绕过上限）：bsdtar 解出来的整棵树先 `stat` 总量，
    超限**一个字节都不搬**（抛 `UnzipBudgetExceeded`，临时目录在 finally 里删掉）；
    没超才记账并搬入。预算对象与上层/下层递归共享同一个 `budget`。
    """
    budget = budget if budget is not None else UnzipBudget()
    tmp = tempfile.mkdtemp(prefix="ict_arc_")
    n = 0
    try:
        r = subprocess.run([TAR_BIN, "-xf", arc_path, "-C", tmp],
                           capture_output=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or b"").decode("utf-8", "replace").strip()[:120]
                               or "bsdtar 返回非零")
        # ---- 先 stat 整棵树的总量，**再**决定是否搬入（超限就什么都不落盘）----
        plan: list[tuple[str, str, int]] = []
        total = 0
        for cur, _dirs, fs in os.walk(tmp):
            for f in fs:
                src = os.path.join(cur, f)
                try:
                    sz = os.path.getsize(src)
                except OSError:
                    sz = 0
                plan.append((src, os.path.relpath(src, tmp), sz))
                total += sz
        for src, _rel_in, sz in plan:
            if sz > budget.single:
                raise UnzipBudgetExceeded(
                    f"解压后单个文件 {os.path.basename(src)} 为 {sz / MB:.1f} MB，"
                    f"超过上限 {budget.single / MB:.0f} MB")
        budget.reserve(total, len(plan), os.path.basename(arc_path))
        for src, rel_in, _sz in plan:
            rel = _fit_path(root_dir, os.path.join(prefix, _sanitize(rel_in)) if prefix
                            else _sanitize(rel_in))
            dst = os.path.join(root_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            ext = os.path.splitext(rel)[1].lower()
            att = Attachment(name=os.path.basename(src), rel_path=rel, ext=ext,
                             size=os.path.getsize(dst))
            files.append(att)
            n += 1
            if (ext == ".zip" or ext in ARCN_EXT) and depth < max_depth:
                try:
                    if ext == ".zip":
                        _extract(dst, root_dir, rel + ".d", files, depth + 1, max_depth,
                                 budget=budget)
                    else:
                        _extract_archive(dst, root_dir, rel + ".d", files, depth + 1,
                                         max_depth, ext[1:], budget=budget)
                    att.note = "nested archive extracted"
                except UnzipBudgetExceeded:
                    raise                     # 超限必须冒泡（job 要失败，不能只记 note）
                except Exception as e:  # noqa: BLE001
                    att.note = f"nested extract failed: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return n


def _extract(zip_path: str, root_dir: str, prefix: str, files: list[Attachment],
             depth: int, max_depth: int,
             budget: UnzipBudget | None = None) -> None:
    budget = budget if budget is not None else UnzipBudget()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = _fix_zip_name(info)
            rel = os.path.join(prefix, _sanitize(name)) if prefix else _sanitize(name)
            rel = _fit_path(root_dir, rel)
            dest = os.path.join(root_dir, rel)
            # 先按声明大小判（拦得住就一个字节都不落盘），再按实际写出的字节累计
            budget.begin(name, info.file_size)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                with z.open(info) as src, open(dest, "wb") as dst:
                    while True:
                        chunk = src.read(MB)
                        if not chunk:
                            break
                        budget.add(len(chunk), name)
                        dst.write(chunk)
            except BaseException:
                try:                              # 半成品不留（超限/CRC 错都不留）
                    os.remove(dest)
                except OSError:
                    pass
                raise
            ext = os.path.splitext(rel)[1].lower()
            att = Attachment(name=name, rel_path=rel, ext=ext, size=info.file_size)
            files.append(att)
            if ext == ".zip" and depth < max_depth:
                try:
                    _extract(dest, root_dir, rel + ".d", files, depth + 1, max_depth,
                             budget=budget)
                    os.remove(dest)
                    att.note = "nested archive extracted"
                except UnzipBudgetExceeded:
                    raise                     # 超限必须冒泡（job 要失败，不能只记 note）
                except Exception as e:  # noqa: BLE001
                    att.note = f"nested extract failed: {e}"
            elif ext in ARCN_EXT:
                if depth < max_depth:
                    try:
                        n = _extract_archive(dest, root_dir, rel + ".d", files, depth + 1,
                                             max_depth, ext[1:], budget=budget)
                        os.remove(dest)          # 压缩包本体已解开，不再留在树里
                        att.note = f"nested archive extracted by bsdtar ({n} files)"
                    except UnzipBudgetExceeded:
                        raise
                    except Exception as e:  # noqa: BLE001
                        att.note = f"nested extract failed: {e}"
                else:
                    att.note = f"archive {ext} not extracted (max depth reached)"
            elif ext == ".wps":
                att.note = "legacy wps (OLE2), 依赖文本抽取层"


def extract_bundle(zip_path: str, root_dir: str, max_depth: int = 8,
                   budget: UnzipBudget | None = None) -> list[Attachment]:
    """解一个附件包（递归），返回附件列表。

    `budget` 由调用方传入时**跨调用共享**（同一次导入的多篇公告共用一个预算）；
    不传则本次调用自建一个（上限见 `unzip_limits()`）。

    按扩展名分派：`.zip` 走 zipfile；`.rar/.7z` 走 bsdtar（`_extract_archive`）。
    为什么要有这条分派：上传管线的「同一公告 HTML + 附件包合并解析」会把**配对的压缩包**
    直接交给 `process_one(html, zip)`，而配对基名允许 `.rar/.7z`（见
    `pipeline_impl._base_key`）—— 不在这里分派的话，配对上的 rar/7z 会在
    `zipfile.ZipFile()` 处抛 `BadZipFile`，等于还是没解析。
    """
    files: list[Attachment] = []
    budget = budget if budget is not None else UnzipBudget()
    ext = os.path.splitext(zip_path)[1].lower()
    if ext in ARCN_EXT:
        _extract_archive(zip_path, root_dir, "", files, 0, max_depth, ext[1:],
                         budget=budget)
    else:
        _extract(zip_path, root_dir, "", files, 0, max_depth, budget=budget)
    return files


# ---------- 文本抽取 ----------

def _vlm_ready() -> bool:
    from .vision import available
    return available()


def _merge_note(att: Attachment, extra: str) -> None:
    att.note = f"{att.note}; {extra}" if att.note else extra


def _vlm_from_pdf(att: Attachment, pdf_path: str, work_dir: str, max_pages: int = 4) -> int:
    """把 PDF 渲染成图片，**逐页**交视觉模型（单图更稳），拼接文本。"""
    from .render import pdf_to_images
    from .vision import image_to_text

    imgdir = os.path.join(work_dir, "img_" + _sanitize(att.name)[:60])
    imgs = pdf_to_images(pdf_path, imgdir, scale=2.0, max_pages=max_pages)
    parts = []
    for p in imgs:
        try:
            parts.append(image_to_text(p))
        except Exception as e:  # noqa: BLE001
            parts.append(f"[page VL 失败: {e}]")
    att.text = "\n".join(parts)[:MAX_TEXT]
    return len(imgs)


def _do_pdf(att: Attachment, path: str, work_dir: str, use_vlm: bool) -> None:
    from .pdf_text import pdf_probe

    # 只对“报价类”PDF 抽表格（招标文件/声明函等不需要表格）→ 显著提速
    need_tbl = any(k in (att.name or "") for k in ("报价", "一览", "明细", "标的", "开标"))
    text, tables, meta = pdf_probe(path, need_tables=need_tbl)
    att.text = text[:MAX_TEXT]
    att.tables = tables
    note = f"pdf pages={meta['pages']} scanned_suspect={meta['scanned_suspect']} tables={len(tables)}"
    if meta["scanned_suspect"] and use_vlm and _vlm_ready():
        try:
            n = _vlm_from_pdf(att, path, work_dir)
            note += f"; VLM(扫描件) pages={n}"
        except Exception as e:  # noqa: BLE001
            note += f"; VLM失败:{e}"
    _merge_note(att, note)


def _do_office(att: Attachment, path: str, work_dir: str, use_vlm: bool) -> None:
    """伪docx/.doc/.wps/.rtf → soffice 转 PDF → 文本（扫描页再走 VLM）。"""
    import shutil
    import tempfile

    from .render import doc_to_pdf

    conv = tempfile.mkdtemp(prefix="ict_conv_")   # 转换中间件放临时目录，用完即删
    try:
        pdf = doc_to_pdf(path, conv)
        if not pdf:
            _merge_note(att, "office->pdf 未产出文件")
            return
        from .pdf_text import pdf_extract

        text, meta = pdf_extract(pdf)
        att.text = text[:MAX_TEXT]
        note = f"converted(soffice) pdf pages={meta['pages']} scanned_suspect={meta['scanned_suspect']}"
        if meta["scanned_suspect"] and use_vlm and _vlm_ready():
            try:
                n = _vlm_from_pdf(att, pdf, work_dir)
                note += f"; VLM(扫描件) pages={n}"
            except Exception as e:  # noqa: BLE001
                note += f"; VLM失败:{e}"
        _merge_note(att, note)
    except Exception as e:  # noqa: BLE001
        _merge_note(att, f"office->pdf 失败: {e}")
    finally:
        shutil.rmtree(conv, ignore_errors=True)


def _do_docx(att: Attachment, path: str) -> None:
    import docx

    d = docx.Document(path)
    lines = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    tables: list[list[list[str]]] = []
    for t in d.tables:
        rows: list[list[str]] = []
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells]
            rows.append(cells)
            lines.append(" | ".join(cells))
        if rows:
            tables.append(rows)
    att.text = "\n".join(lines)[:MAX_TEXT]
    att.tables = tables


def _do_xlsx(att: Attachment, path: str) -> None:
    """xlsx 附件 -> 文本 + **表格**（两者都保留，列对齐一致）。

    为什么还要 `att.tables`：`_do_xlsx` 原先只产出文本，于是《开标一览表》《评审结果》
    这类 xlsx 只能被当成「管道符文本」读，**空单元格会被 `str(v) for v in row if v is not None`
    丢掉**，后面的列整体左移（`未通过资格审查原因` 空着时，`未中标原因` 会被读成前一列）。
    保留空串（只去尾部空列）后列位置与原表一一对应；同时给出 `tables`，让抽取层/投标人名单
    都能按列名取值（docx/pdf 早就是这条路）。
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    lines: list[str] = []
    tables: list[list[list[str]]] = []
    for ws in wb.worksheets:
        lines.append(f"[sheet] {ws.title}")
        rows: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            vals = [("" if v is None else str(v)) for v in (row or [])]
            while vals and not vals[-1].strip():      # 只去尾部空列，保住列位置
                vals.pop()
            if not any(v.strip() for v in vals):
                continue                              # 整行空白（含超宽空白行）跳过
            rows.append(vals)
            lines.append(" | ".join(vals))
        if len(rows) >= 2:                            # 表头 + 至少 1 行数据才算一张表
            tables.append(rows)
    att.text = "\n".join(lines)[:MAX_TEXT]
    att.tables = tables
    wb.close()


MAX_CSV_BYTES = 8 * MB          # 单个 csv 附件最多读多少字节（防误传的超大 csv 打满内存）


def _decode_text(raw: bytes) -> tuple[str, str]:
    """csv 字节流 -> `(文本, 编码)`：utf-8-sig 优先，回退 gbk / gb18030。

    已确认的缺陷（P2）：改造前固定 `encoding="utf-8-sig", errors="replace"`，
    GBK 编码的报价表被读成**一堆乱码**且没有任何提示 —— 投标人/报价全抽错还看不出来。
    """
    for enc in ("utf-8-sig", "gbk", "gb18030"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8-sig", "replace"), "utf-8(替换非法字节)"


def _do_csv(att: Attachment, path: str) -> None:
    """csv 附件 -> 文本 + **表格**（与 `_do_xlsx` 同一形态，供抽取层/投标人名单按列名取值）。

    为什么必须补 `att.tables`（已确认的缺陷）：改造前 `.csv` 只走
    `att.text = f.read()`，于是「用户上传自己的《报价表.csv》」这类数据集里
    的**投标人 / 报价 / 得分**全部抽不出来 —— 投标人名单只读 `att.tables`
    （`ict_parse/bidders.py`），文本再完整也进不了名单。

    编码（P2）：不再写死 utf-8 + replace。先按 utf-8-sig **严格**解，失败回退
    gbk / gb18030；确实全部失败才用 replace，并在 `att.note` 里如实记一句。
    截断（超过 `MAX_CSV_ROWS` 行）与大小超限（`MAX_CSV_BYTES`）同样写进 `note`。
    """
    import csv as _csv
    import io as _io

    notes: list[str] = []
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_CSV_BYTES + 1)
    except OSError as e:
        _merge_note(att, f"csv 读取失败: {e}")
        att.text = ""
        att.tables = []
        return
    if len(raw) > MAX_CSV_BYTES:
        raw = raw[:MAX_CSV_BYTES]
        notes.append(f"csv 超过 {MAX_CSV_BYTES // MB} MB 上限，只解析前 "
                     f"{MAX_CSV_BYTES // MB} MB")
    text, enc = _decode_text(raw)
    if enc not in ("utf-8-sig", "utf-8"):
        notes.append(f"csv 非 UTF-8 编码（已按 {enc} 解码）")

    sample = text[:4096]
    try:
        dialect = _csv.Sniffer().sniff(sample, delimiters=",;\t")
        delim = dialect.delimiter
    except _csv.Error:
        delim = ","

    rows: list[list[str]] = []
    truncated = False
    for row in _csv.reader(_io.StringIO(text, newline=""), delimiter=delim):
        vals = [("" if v is None else str(v)) for v in row]
        while vals and not vals[-1].strip():          # 只去尾部空列，保住列位置
            vals.pop()
        if not any(v.strip() for v in vals):
            continue                                  # 整行空白跳过
        rows.append(vals)
        if len(rows) >= MAX_CSV_ROWS:
            truncated = True
            break
    if truncated:
        notes.append(f"csv 行数超过 {MAX_CSV_ROWS} 行，只取前 {MAX_CSV_ROWS} 行")
    att.text = "\n".join(" | ".join(r) for r in rows)[:MAX_TEXT]
    # 表头 + 至少 1 行数据才算一张表（与 `_do_xlsx` 一致的判据）
    att.tables = [rows] if len(rows) >= 2 else []
    if notes:
        _merge_note(att, "；".join(notes))


def _do_image(att: Attachment, path: str, use_vlm: bool) -> None:
    """图片附件（png/jpg…）→ Qwen3-VL 识别为文本。"""
    if use_vlm and _vlm_ready():
        from .vision import image_to_text

        try:
            att.text = image_to_text(path)[:MAX_TEXT]
        except Exception as e:  # noqa: BLE001
            _merge_note(att, f"VLM(图片)失败: {e}")
    else:
        _merge_note(att, "image (需 --vlm 识别)")


def extract_text_for(att: Attachment, root_dir: str, use_vlm: bool = False,
                     work_dir: str | None = None) -> None:
    path = os.path.join(root_dir, att.rel_path or "")
    ext = att.ext or ""
    work_dir = work_dir or os.path.join(root_dir, "_work")
    try:
        real = _sniff(path)
        if ext == ".pdf" or real == "pdf":
            _do_pdf(att, path, work_dir, use_vlm)
        elif ext == ".docx" and real == "zip":
            _do_docx(att, path)
        elif ext in OFFICE_EXT or real in ("ole2", "rtf"):
            # 伪docx（OLE2/RTF）与 .doc/.wps/.rtf 统一走转换
            _do_office(att, path, work_dir, use_vlm)
        elif ext in (".xlsx", ".xlsm"):
            _do_xlsx(att, path)
        elif ext in (".txt", ".csv"):
            if ext == ".csv":
                _do_csv(att, path)            # 文本 + 表格（投标人名单要靠表格）
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    att.text = f.read()[:MAX_TEXT]
        elif ext == ".doc":
            _do_office(att, path, work_dir, use_vlm)
        elif ext in IMG_EXT:
            _do_image(att, path, use_vlm)
    except Exception as e:  # noqa: BLE001
        _merge_note(att, f"text extract failed: {e}")
