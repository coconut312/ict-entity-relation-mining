# data/ —— 品目分类词表

`gov_category.csv` 是 `ict_parse/category.py` 依赖的政府采购品目分类词表
（约 4,178 条，含 `code` / `name` / `path` / `level` 四列）。

**本仓库未包含该文件**（属可再生产物），缺失时 `category.py` 会直接抛 `FileNotFoundError`。

## 如何生成

1. 下载《政府采购品目分类目录》PDF（财政部公开文件）
2. 放到任意位置，然后：

```bash
cd app
uv run python scripts/build_catalog.py --src <你的PDF路径>
# 产物：data/gov_category.csv
```

`scripts/build_catalog.py` 里的默认输入路径需要按实际位置修改（该脚本原本指向作者的开发机路径）。

## 生成后的格式

UTF-8 BOM（`utf-8-sig`），表头：

```csv
code,name,path,level
A,货物,,1
A0201,信息化设备,货物/信息化设备,2
...
```

`category.py` 只取 `len(name) >= 3` 且 `level >= 2` 的条目做最长匹配，避免过短通用名误配。
