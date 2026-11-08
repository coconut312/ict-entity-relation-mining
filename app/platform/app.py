"""任务三 · 可视化分析检索平台后端（**FastAPI** 版）。

对应赛题任务三要求：
- 整合任务一实体提取与任务二关系建模成果          -> 单一 SQLite 服务库 platform.db
- 标的物**全字段检索、筛选与查看**                 -> GET /api/items、GET /api/notice/{id}
- 任务二**五大业务场景**可视化查询                 -> GET /api/scenario/s1..s5
- 数据集**自动化处理**（重新解析入库，含耗时）      -> POST /api/ingest
- 评审方可登录操作                                 -> POST/GET /api/login + HTTP Bearer
- 接口自描述（FastAPI 自带 OpenAPI/Swagger）       -> /docs、/redoc、/openapi.json

启动：
    uv run python platform/ingest.py                 # 先入库
    uv run python platform/app.py --port 8899        # 或 uvicorn platform.app:app --port 8899
    浏览器 http://127.0.0.1:8899/   账号 judge / ict2026（见 platform/account.json）

> 另保留 `platform/server.py`：标准库 http.server 的**零依赖备用版**，
> 供无法安装第三方包的评审环境使用（功能集相同）。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
DB = os.path.join(APP, "out", "platform", "platform.db")
STATIC = os.path.join(HERE, "static")          # Vue 前端（index.html / app.js / style.css / vue 运行时）
INDEX = os.path.join(STATIC, "index.html")
ACCOUNT = os.path.join(HERE, "account.json")
DEFAULT_ACCOUNT = {"user": "judge", "password": "ict2026"}

ITEM_COLS = ["announcement_id", "item_name", "category", "brand", "spec", "quantity", "unit",
             "unit_price", "total_price", "supplier_name", "package_no", "source", "engine"]
FIELD_MAP = {"all": None, "item_name": "item_name", "brand": "brand", "spec": "spec",
             "category": "category", "supplier_name": "supplier_name"}

app = FastAPI(
    title="招采标讯标的物智能分析平台",
    description="赛题五 · 任务三：整合任务一实体提取 + 任务二关系建模的一体化可视化检索平台",
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
# Vue 前端静态资源（本地内置 Vue 运行时 -> 离线可用）
if os.path.isdir(STATIC):
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
_bearer = HTTPBearer(auto_error=False)
TOKENS: dict[str, dict] = {}


# ---------------- 依赖 ----------------
def _account() -> dict:
    try:
        return json.load(open(ACCOUNT, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return DEFAULT_ACCOUNT


def _rows(sql: str, args=()) -> list[dict]:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _one(sql: str, args=()) -> dict | None:
    r = _rows(sql, args)
    return r[0] if r else None


def require_token(cred: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> dict:
    """校验 Bearer Token（登录后由 /api/login 下发）。"""
    tok = cred.credentials if cred else ""
    info = TOKENS.get(tok)
    if not info:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录或登录已过期，请先 /api/login",
                            headers={"WWW-Authenticate": "Bearer"})
    return info


Auth = Annotated[dict, Depends(require_token)]


# ---------------- 模型（供 OpenAPI 文档） ----------------
class LoginOut(BaseModel):
    ok: bool
    token: str | None = None
    user: str | None = None
    error: str | None = None


class ItemQuery(BaseModel):
    q: str = Field("", description="关键字")
    field: str = Field("all", description="限定字段：all/item_name/brand/spec/category/supplier_name")
    category: str = ""
    supplier: str = ""
    limit: int = 50
    offset: int = 0


class StatsOut(BaseModel):
    meta: dict[str, Any]
    counts: dict[str, int]
    top_purchasers: list[dict]
    top_suppliers: list[dict]


# ---------------- 接口 ----------------
@app.get("/", include_in_schema=False)
def home():
    if os.path.exists(INDEX):
        return FileResponse(INDEX, media_type="text/html; charset=utf-8")
    return {"error": "index.html 缺失"}


@app.get("/api/health", summary="健康检查", tags=["系统"])
def health():
    return {"ok": True, "db": os.path.exists(DB), "time": time.strftime("%F %T"),
            "engine": "fastapi"}


@app.get("/api/login", response_model=LoginOut, summary="评审方登录", tags=["系统"])
def login(user: str = Query(""), password: str = Query("")):
    acc = _account()
    if user == acc.get("user") and password == acc.get("password"):
        tok = secrets.token_urlsafe(18)
        TOKENS[tok] = {"user": user, "at": time.time()}
        return LoginOut(ok=True, token=tok, user=user)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号或密码错误")


@app.get("/api/stats", response_model=StatsOut, summary="规模概览与 TOP 榜", tags=["检索"])
def stats(_: Auth):
    m = {r["k"]: r["v"] for r in _rows("SELECT k,v FROM _meta")}
    return StatsOut(
        meta=m,
        counts={"标的物明细": _one("SELECT COUNT(*) n FROM t1_item")["n"],
                "公告": _one("SELECT COUNT(*) n FROM g_notice")["n"],
                "实体": _one("SELECT COUNT(*) n FROM entity")["n"],
                "参与投标事实": _one("SELECT COUNT(*) n FROM bid")["n"],
                "合作关系": _one("SELECT COUNT(*) n FROM cooperation")["n"],
                "共同竞标关系": _one("SELECT COUNT(*) n FROM cobidding")["n"]},
        top_purchasers=_rows("SELECT purchaser,n_notices,total_amount FROM purchaser_portrait "
                             "ORDER BY n_notices DESC LIMIT 10"),
        top_suppliers=_rows("SELECT supplier,win_notices,win_rate,won_amount,n_purchasers "
                            "FROM supplier_portrait ORDER BY won_amount DESC LIMIT 10"))


@app.get("/api/items", summary="标的物全字段检索与筛选", tags=["检索"])
def items(_: Auth,
          q: str = "", field: str = "all", category: str = "", supplier: str = "",
          limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    where, args = [], []
    if q.strip():
        col = FIELD_MAP.get(field)
        if col:
            where.append(f'"{col}" LIKE ?')
            args.append(f"%{q}%")
        else:
            where.append("(" + " OR ".join(
                f'IFNULL("{c}",\'\') LIKE ?' for c in
                ("item_name", "brand", "spec", "category", "supplier_name", "announcement_id")) + ")")
            args += [f"%{q}%"] * 6
    if category.strip():
        where.append("IFNULL(category,'') LIKE ?")
        args.append(f"%{category}%")
    if supplier.strip():
        where.append("IFNULL(supplier_name,'') LIKE ?")
        args.append(f"%{supplier}%")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = _one(f"SELECT COUNT(*) n FROM t1_item{w}", args)["n"]
    rows = _rows(f'SELECT {",".join(ITEM_COLS)} FROM t1_item{w} '
                 f"ORDER BY announcement_id LIMIT ? OFFSET ?", (*args, limit, offset))
    return {"total": total, "limit": limit, "offset": offset, "rows": rows}


@app.get("/api/notice/{announcement_id}", summary="单公告全部明细", tags=["检索"])
def notice(announcement_id: str, _: Auth):
    n = _one("SELECT * FROM t1_notice WHERE announcement_id=?", (announcement_id,))
    if not n:
        raise HTTPException(404, "公告不存在")
    return {"notice": n,
            "items": _rows(f'SELECT {",".join(ITEM_COLS)} FROM t1_item WHERE announcement_id=?',
                           (announcement_id,))}


@app.get("/api/suggest", summary="主体名候选（自动补全）", tags=["检索"])
def suggest(_: Auth, type: str = "supplier", q: str = ""):
    src = "supplier_portrait" if type == "supplier" else "purchaser_portrait"
    col = "supplier" if type == "supplier" else "purchaser"
    return {"items": [r["name"] for r in
                      _rows(f'SELECT "{col}" name FROM {src} WHERE "{col}" LIKE ? ORDER BY 1 LIMIT 20',
                            (f"%{q}%",))]}


@app.get("/api/scenario/{name}", summary="任务二五大业务场景（s1~s5）", tags=["场景"])
def scenario(name: str, _: Auth,
             purchaser: str = "", supplier: str = "", suppliers: str = ""):
    name = name.lower()
    if name == "s1":
        return {"input": purchaser,
                "summary": _one("SELECT COUNT(*) n_notices, ROUND(SUM(project_amount),2) total_amount "
                                "FROM g_notice WHERE purchaser=?", (purchaser,)),
                "winners": _rows("SELECT supplier,coop_times,coop_amount,first_time,last_time "
                                 "FROM cooperation WHERE purchaser=? "
                                 "ORDER BY coop_times DESC, coop_amount DESC LIMIT 20", (purchaser,)),
                "product_suppliers": _rows(
                    "SELECT product_supplier,src_kind,SUM(notices) notices "
                    "FROM purchaser_product_supplier WHERE purchaser=? "
                    "GROUP BY product_supplier,src_kind ORDER BY notices DESC LIMIT 20", (purchaser,))}
    if name == "s2":
        return {"input": purchaser,
                "top_bidders": _rows("""SELECT b.supplier, COUNT(*) participate_times,
                        SUM(b.is_winner) win_times, SUM(1-b.is_winner) bid_only_times,
                        ROUND(SUM(CASE WHEN b.is_winner=1 THEN COALESCE(b.amount,0) ELSE 0 END),2) won_amount
                    FROM bid b JOIN g_notice n ON n.notice_id=b.notice_id
                    WHERE n.purchaser=? GROUP BY b.entity_id
                    ORDER BY participate_times DESC, win_times DESC LIMIT 5""", (purchaser,)),
                "top_pairs": _rows("""SELECT ea.canonical_name supplier_a, eb.canonical_name supplier_b,
                        COUNT(DISTINCT a.notice_id) co_times
                    FROM bid a JOIN bid b ON a.notice_id=b.notice_id AND a.entity_id<b.entity_id
                    JOIN g_notice n ON n.notice_id=a.notice_id
                    JOIN entity ea ON ea.entity_id=a.entity_id
                    JOIN entity eb ON eb.entity_id=b.entity_id
                    WHERE n.purchaser=? GROUP BY a.entity_id,b.entity_id
                    ORDER BY co_times DESC LIMIT 5""", (purchaser,))}
    if name == "s3":
        return {"input": supplier,
                "profile": _one("SELECT * FROM supplier_portrait WHERE supplier=?", (supplier,)),
                "top_co_bidders": _rows("""SELECT CASE WHEN supplier_a=? THEN supplier_b ELSE supplier_a END co_bidder,
                        co_times, co_project_amount, a_wins, b_wins
                    FROM cobidding WHERE supplier_a=? OR supplier_b=?
                    ORDER BY co_times DESC LIMIT 5""", (supplier, supplier, supplier)),
                "all_co_bidders": _rows("""SELECT CASE WHEN supplier_a=? THEN supplier_b ELSE supplier_a END co_bidder,
                        co_times, co_project_amount
                    FROM cobidding WHERE supplier_a=? OR supplier_b=?
                    ORDER BY co_times DESC""", (supplier, supplier, supplier))}
    if name in ("s4", "s5"):
        sups = [x for x in re.split(r"[|,，;；]", suppliers) if x.strip()]
        if len(sups) < 2:
            raise HTTPException(422, "请提供 2 个及以上主体，用 | 分隔")
        ph = ",".join("?" * len(sups))
        if name == "s4":
            return {"input": sups,
                    "purchasers": _rows(f"""SELECT purchaser, COUNT(DISTINCT supplier) n_suppliers,
                            SUM(coop_times) coop_times, ROUND(SUM(coop_amount),2) coop_amount
                        FROM cooperation WHERE supplier IN ({ph})
                        GROUP BY purchaser HAVING COUNT(DISTINCT supplier)={len(sups)}
                        ORDER BY coop_times DESC LIMIT 20""", sups)}
        sub = " AND ".join([f"notice_id IN (SELECT notice_id FROM bid WHERE supplier=?)" for _ in sups])
        proj = _rows(f"""SELECT notice_id, project_name, purchaser, publish_time, project_amount
                    FROM g_notice WHERE {sub} ORDER BY publish_time""", sups)
        for p in proj:
            p["results"] = _rows(
                f"SELECT supplier, is_winner FROM bid WHERE notice_id=? AND supplier IN ({ph})",
                (p["notice_id"], *sups))
        return {"input": sups, "n_projects": len(proj),
                "total_project_amount": round(sum(float(p["project_amount"] or 0) for p in proj), 2),
                "projects": proj[:50]}
    raise HTTPException(404, f"未知场景 {name}（支持 s1~s5）")


@app.post("/api/ingest", summary="重新入库（返回耗时，即赛题速率口径的入库环节）", tags=["系统"])
def do_ingest(_: Auth):
    import sys
    sys.path.insert(0, HERE)
    from ingest import ingest                     # 同目录模块
    t0 = time.time()
    r = ingest(DB)
    return {"ok": True, "wall_sec": round(time.time() - t0, 2), **r}


if __name__ == "__main__":
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8899)
    a = ap.parse_args()
    if not os.path.exists(DB):
        print(f"[warn] 服务库不存在：{DB}；请先运行 python platform/ingest.py", flush=True)
    print(f"平台已启动（FastAPI）：http://{a.host}:{a.port}/ ｜ 接口文档 /docs ｜ 账号 judge / ict2026",
          flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
