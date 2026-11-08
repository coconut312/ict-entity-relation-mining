"""任务一解析层的数据模型（IR）。"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TableGroup:
    """一组「主要标的信息」表：表头 + 数据行（同一张 html 表可能因多包而重复表头）。"""

    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class Attachment:
    """一个附件文件（可能是嵌套压缩包解出的内层文件）。"""

    name: str
    download_id: str | None = None
    rel_path: str | None = None
    ext: str | None = None
    size: int | None = None
    text: str | None = None
    tables: list | None = None
    note: str | None = None


@dataclass
class AnnouncementIR:
    """一篇公告的结构化中间表示。"""

    announcement_id: str
    source_html: str
    article_title: str = ""
    pub_date: str = ""
    summary: dict[str, str] = field(default_factory=dict)
    categories: list[str] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)
    content_text: str = ""
    main_item_tables: list[TableGroup] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    extracted_files: list[Attachment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
