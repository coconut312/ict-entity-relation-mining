"""任务三 · 可视化检索平台后端（Python 标准库 http.server，零第三方依赖）。

设计要点（对应赛题任务三要求）：
- 整合任务一实体提取与任务二关系建模成果 -> 单一 SQLite 服务库 platform.db
- 标的物**全字段检索、筛选与查看**       -> /api/items、/api/notice/<id>
- 任务二**五大业务场景**可视化查询       -> /api/scenario/s1..s5
- 数据集**自动化处理**（重新解析入库）   -> POST /api/ingest（带耗时，即赛题速率口径）
- 评审方可登录操作                       -> /api/login + Bearer Token（账号见 platform/account.json）

用法:
    python platform/server.py --port 8899            # 启动
    浏览器打开 http://127.0.0.1:8899/
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
DB = os.path.join(APP, "out", "platform", "platform.db")
STATIC = os.path.join(HERE, "static")
INDEX = os.path.join(STATIC, "index.html")
ACCOUNT = os.path.join(HERE, "account.json")
TOKENS: dict[str, dict] = {}
LOCK = threading.Lock()

DEFAULT_ACCOUNT = {"user": "judge", "password": "ict2026"}


def _account() -> dict:
    try:
        return json.load(open(ACCOUNT, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return DEFAULT_ACCOUNT


def _con() -> sqlite3.Connection:
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _rows(sql: str, args=()) -> list[dict]:
    c = _con()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _one(sql: str, args=()):
    r = _rows(sql, args)
    return r[0] if r else None


# ---------------- 业务查询 ----------------
ITEM_COLS = ["announcement_id", "item_name", "category", "brand", "spec", "quantity", "unit",
             "unit_price", "total_price", "supplier_name", "package_no", "source", "engine"]
FIELD_MAP = {"all": None, "item_name": "item_name", "brand": "brand", "spec": "spec",
             "category": "category", "supplier_name": "supplier_name"}


def api_items(q: dict) -> dict:
    kw = (q.get("q") or [""])[0].strip()
    field = (q.get("field") or ["all"])[0]
    cat = (q.get("category") or [""])[0].strip()
    sup = (q.get("supplier") or [""])[0].strip()
    try:
        limit = min(int((q.get("limit") or ["50"])[0]), 500)
        offset = max(int((q.get("offset") or ["0"])[0]), 0)
    except ValueError:
        limit, offset = 50, 0
    where, args = [], []
    if kw:
        col = FIELD_MAP.get(field)
        if col:
            where.append(f'"{col}" LIKE ?')
            args.append(f"%{kw}%")
        else:                       # 全字段检索
            where.append("(" + " OR ".join(
                f'IFNULL("{c}",\'\') LIKE ?' for c in
                ("item_name", "brand", "spec", "category", "supplier_name", "announcement_id")) + ")")
            args += [f"%{kw}%"] * 6
    if cat:
        where.append('IFNULL(category,\'\') LIKE ?')
        args.append(f"%{cat}%")
    if sup:
        where.append('IFNULL(supplier_name,\'\') LIKE ?')
        args.append(f"%{sup}%")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = _one(f"SELECT COUNT(*) n FROM t1_item{w}", args)["n"]
    rows = _rows(f'SELECT {",".join(ITEM_COLS)} FROM t1_item{w} ORDER BY announcement_id LIMIT ? OFFSET ?',
                 (*args, limit, offset))
    return {"total": total, "limit": limit, "offset": offset, "rows": rows}


def api_notice(aid: str) -> dict:
    return {"notice": _one("SELECT * FROM t1_notice WHERE announcement_id=?", (aid,)),
            "items": _rows(f'SELECT {",".join(ITEM_COLS)} FROM t1_item WHERE announcement_id=?', (aid,))}


def api_stats() -> dict:
    m = {r["k"]: r["v"] for r in _rows("SELECT k,v FROM _meta")}
    return {"meta": m,
            "counts": {"标的物明细": _one("SELECT COUNT(*) n FROM t1_item")["n"],
                       "公告": _one("SELECT COUNT(*) n FROM g_notice")["n"],
                       "实体": _one("SELECT COUNT(*) n FROM entity")["n"],
                       "参与投标事实": _one("SELECT COUNT(*) n FROM bid")["n"],
                       "合作关系": _one("SELECT COUNT(*) n FROM cooperation")["n"],
                       "共同竞标关系": _one("SELECT COUNT(*) n FROM cobidding")["n"]},
            "top_purchasers": _rows("SELECT purchaser,n_notices,total_amount FROM purchaser_portrait "
                                    "ORDER BY n_notices DESC LIMIT 10"),
            "top_suppliers": _rows("SELECT supplier,win_notices,win_rate,won_amount,n_purchasers "
                                   "FROM supplier_portrait ORDER BY won_amount DESC LIMIT 10")}


def api_suggest(q: dict) -> dict:
    t = (q.get("type") or ["supplier"])[0]
    kw = (q.get("q") or [""])[0].strip()
    src = "supplier_portrait" if t == "supplier" else "purchaser_portrait"
    col = "supplier" if t == "supplier" else "purchaser"
    rows = _rows(f'SELECT "{col}" name FROM {src} WHERE "{col}" LIKE ? ORDER BY 1 LIMIT 20', (f"%{kw}%",))
    return {"items": [r["name"] for r in rows]}


def api_scenario(name: str, q: dict) -> dict:
    g = lambda k, d="": (q.get(k) or [d])[0].strip()          # noqa: E731
    if name == "s1":
        p = g("purchaser")
        return {"input": p,
                "summary": _one("SELECT COUNT(*) n_notices, ROUND(SUM(project_amount),2) total_amount "
                                "FROM g_notice WHERE purchaser=?", (p,)),
                "winners": _rows("SELECT supplier,coop_times,coop_amount,first_time,last_time "
                                 "FROM cooperation WHERE purchaser=? "
                                 "ORDER BY coop_times DESC, coop_amount DESC LIMIT 20", (p,)),
                "product_suppliers": _rows("SELECT product_supplier,src_kind,SUM(notices) notices "
                                           "FROM purchaser_product_supplier WHERE purchaser=? "
                                           "GROUP BY product_supplier,src_kind "
                                           "ORDER BY notices DESC LIMIT 20", (p,))}
    if name == "s2":
        p = g("purchaser")
        return {"input": p,
                "top_bidders": _rows("""SELECT b.supplier, COUNT(*) participate_times,
                        SUM(b.is_winner) win_times, SUM(1-b.is_winner) bid_only_times,
                        ROUND(SUM(CASE WHEN b.is_winner=1 THEN COALESCE(b.amount,0) ELSE 0 END),2) won_amount
                    FROM bid b JOIN g_notice n ON n.notice_id=b.notice_id
                    WHERE n.purchaser=? GROUP BY b.entity_id
                    ORDER BY participate_times DESC, win_times DESC LIMIT 5""", (p,)),
                "top_pairs": _rows("""SELECT ea.canonical_name supplier_a, eb.canonical_name supplier_b,
                        COUNT(DISTINCT a.notice_id) co_times
                    FROM bid a JOIN bid b ON a.notice_id=b.notice_id AND a.entity_id<b.entity_id
                    JOIN g_notice n ON n.notice_id=a.notice_id
                    JOIN entity ea ON ea.entity_id=a.entity_id
                    JOIN entity eb ON eb.entity_id=b.entity_id
                    WHERE n.purchaser=? GROUP BY a.entity_id,b.entity_id
                    ORDER BY co_times DESC LIMIT 5""", (p,))}
    if name == "s3":
        s = g("supplier")
        return {"input": s,
                "profile": _one("SELECT * FROM supplier_portrait WHERE supplier=?", (s,)),
                "top_co_bidders": _rows("""SELECT CASE WHEN supplier_a=? THEN supplier_b ELSE supplier_a END co_bidder,
                        co_times, co_project_amount, a_wins, b_wins
                    FROM cobidding WHERE supplier_a=? OR supplier_b=?
                    ORDER BY co_times DESC LIMIT 5""", (s, s, s)),
                "all_co_bidders": _rows("""SELECT CASE WHEN supplier_a=? THEN supplier_b ELSE supplier_a END co_bidder,
                        co_times, co_project_amount
                    FROM cobidding WHERE supplier_a=? OR supplier_b=?
                    ORDER BY co_times DESC""", (s, s, s))}
    if name in ("s4", "s5"):
        sups = [x for x in re.split(r"[|,，;；]", g("suppliers")) if x]
        if len(sups) < 2:
            return {"error": "请提供 2 个及以上主体，用 | 分隔", "suppliers": sups}
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
            p["results"] = _rows(f"SELECT supplier, is_winner FROM bid WHERE notice_id=? AND supplier IN ({ph})",
                                 (p["notice_id"], *sups))
        return {"input": sups, "n_projects": len(proj),
                "total_project_amount": round(sum(float(p["project_amount"] or 0) for p in proj), 2),
                "projects": proj[:50]}
    return {"error": "未知场景"}


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ICT-Task3/1.0"

    def log_message(self, fmt, *args):      # 静音默认日志
        pass

    def _send(self, obj, code: int = 200, ctype: str = "application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _auth(self) -> bool:
        h = self.headers.get("Authorization") or ""
        tok = h[7:] if h.lower().startswith("bearer ") else ""
        with LOCK:
            return bool(tok and tok in TOKENS)

    def do_OPTIONS(self):                    # noqa: N802
        self._send({"ok": True})

    def do_GET(self):                        # noqa: N802
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        if path in ("/", "/index.html"):
            try:
                self._send(open(INDEX, "rb").read(), ctype="text/html; charset=utf-8")
            except OSError:
                self._send({"error": "index.html 缺失"}, 500)
            return
        if path.startswith("/static/"):        # Vue 前端静态资源
            fp = os.path.join(STATIC, os.path.basename(path))
            if os.path.isfile(fp):
                ct = ("text/javascript" if fp.endswith(".js") else
                      "text/css" if fp.endswith(".css") else "application/octet-stream")
                self._send(open(fp, "rb").read(), ctype=ct + "; charset=utf-8")
            else:
                self._send({"error": "not found"}, 404)
            return
        if path == "/api/health":
            self._send({"ok": True, "db": os.path.exists(DB), "time": time.strftime("%F %T")})
            return
        if path == "/api/login":
            acc = _account()
            user = (q.get("user") or [""])[0]
            pwd = (q.get("password") or [""])[0]
            if user == acc.get("user") and pwd == acc.get("password"):
                tok = secrets.token_urlsafe(18)
                with LOCK:
                    TOKENS[tok] = {"user": user, "at": time.time()}
                self._send({"ok": True, "token": tok, "user": user})
            else:
                self._send({"ok": False, "error": "账号或密码错误"}, 401)
            return
        # 其余接口需要登录
        if not self._auth():
            self._send({"error": "未登录或登录已过期，请先 /api/login"}, 401)
            return
        try:
            if path == "/api/stats":
                self._send(api_stats())
            elif path == "/api/items":
                self._send(api_items(q))
            elif path.startswith("/api/notice/"):
                self._send(api_notice(path.rsplit("/", 1)[-1]))
            elif path == "/api/suggest":
                self._send(api_suggest(q))
            elif path.startswith("/api/scenario/"):
                self._send(api_scenario(path.rsplit("/", 1)[-1], q))
            else:
                self._send({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._send({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):                       # noqa: N802
        u = urllib.parse.urlparse(self.path)
        if not self._auth():
            self._send({"error": "未登录"}, 401)
            return
        if u.path == "/api/ingest":
            t0 = time.time()
            from ingest import ingest                    # 同目录模块
            r = ingest(DB)
            self._send({"ok": True, "elapsed_sec": round(time.time() - t0, 2), **r})
        else:
            self._send({"error": "not found"}, 404)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    if not os.path.exists(DB):
        print(f"[warn] 服务库不存在：{DB}；请先运行 python platform/ingest.py", flush=True)
    print(f"平台已启动：http://{a.host}:{a.port}/   （账号见 platform/account.json）", flush=True)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
