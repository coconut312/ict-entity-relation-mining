# -*- coding: utf-8 -*-
"""后端冒烟测试：在后台线程里真起一个 uvicorn，然后逐个打接口。

不依赖 starlette 的 TestClient（它和 httpx 0.28 有兼容问题），
只用标准库 urllib，任何 Python 环境都能跑。

**数据断言**（本次新增）：不再只看「HTTP 200」。平台库里有数据时，必须同时满足
  * `/dataset/stats` 的 n_docs / n_items 与库里 `t1_notice` / `t1_item` 行数一致；
  * `/items` 的 total 与库里 `t1_item` 行数一致；
  * S1（用库里公告最多的采购单位）供应商非空、S3（用库里一条标的的供货方）非空；
  * 返回行的 `source`（= engine）非空，且 `unit_price` 与库里的值一致；
  * 投标人表下载没有「名称空行」（P1-5 回归）。
改造前这套接口 37/37 全过，但 S1 的 suppliers 是 []（数据根本没接上）——
只有「有数据时非空」这类断言才能抓住它。

用法：python backend/smoke_test.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
APP = ROOT.parents[1]                            # app/
try:                                             # Windows 控制台默认 GBK，中文会乱码
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:                                # noqa: BLE001
    pass

# ---- 冒烟测试跑「真实数据后端」：与 run.py 的默认值一致（可用环境变量覆盖）----
# 改造前不设这些变量 -> DATA_BACKEND=empty -> 仓储是空实现 -> 所有接口 200 但数据全空，
# 「37/37 全过而 S1 suppliers=[]」正是这么来的。setdefault 保留调用方的显式覆盖。
os.environ.setdefault('EDM_DATA_BACKEND', 'real')
os.environ.setdefault('EDM_ITEM_REPOSITORY',
                      'integrations.repos:SQLiteItemRepository')
os.environ.setdefault('EDM_GRAPH_REPOSITORY',
                      'integrations.repos:SQLiteGraphRepository')
os.environ.setdefault('EDM_SQLITE_PATH',
                      str(APP / 'out' / 'platform' / 'platform.db'))
# 冒烟测试只读查询 + 不碰共享图库：绝不允许写 Neo4j / 写库
os.environ.setdefault('EDM_GRAPH_SYNC', '0')
# 0 次真实 LLM 调用：模型配置指向不存在的文件（页面上即便填过 key 也不会被读到）
os.environ.setdefault('LLM_SETTINGS_PATH', str(ROOT / 'var' / 'smoke_no_such.json'))

import uvicorn                                  # noqa: E402
from app.main import app                        # noqa: E402

P = '/api/v1'
TOKEN = ''          # 登录后填充，受保护接口自动带上


def free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = free_port()
BASE = f'http://127.0.0.1:{PORT}'

config = uvicorn.Config(app, host='127.0.0.1', port=PORT, log_level='error')
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()

for _ in range(100):
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=0.2):
            break
    except OSError:
        time.sleep(0.05)
else:
    print('服务未能启动')
    sys.exit(1)

ok = fail = 0


def request(name, method, path, params=None, body=None, expect=200,
            with_token=True):
    """发一个请求并断言状态码。"""
    global ok, fail
    url = BASE + urllib.parse.quote(path, safe='/?=&')
    if params:
        url += '?' + urllib.parse.urlencode(params)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if with_token and TOKEN:
        headers['Authorization'] = f'Bearer {TOKEN}'

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            code, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    except Exception as e:
        code, payload = -1, str(e).encode()

    good = code == expect
    ok, fail = ok + good, fail + (not good)

    extra = ''
    if good:
        try:
            d = json.loads(payload)
            if isinstance(d, dict) and 'items' in d:
                extra = f"items={len(d['items'])} total={d.get('meta',{}).get('total')}"
            elif isinstance(d, list):
                extra = f'len={len(d)}'
            elif isinstance(d, dict):
                extra = ','.join(list(d.keys())[:6])
        except Exception:
            extra = f'{len(payload)} bytes'
    print(f"  [{'OK ' if good else 'FAIL'}] {name:<34} {code}  {extra}")
    if not good:
        print(f'         {payload[:200]!r}')
    return payload


def get(name, path, params=None, expect=200, with_token=True):
    return request(name, 'GET', path, params=params, expect=expect,
                   with_token=with_token)


def post(name, path, body=None, expect=200, with_token=True):
    return request(name, 'POST', path, body=body, expect=expect,
                   with_token=with_token)


def parse(payload):
    try:
        return json.loads(payload)
    except Exception:
        return {}


# ==================================================================
print('== 系统 ==')
get('健康检查（公开）', f'{P}/health', with_token=False)
get('OpenAPI 文档（公开）', '/api/openapi.json', with_token=False)

print('== 鉴权 ==')
post('错误口令 -> 401', f'{P}/auth/login',
     {'username': 'reviewer', 'password': 'wrong-password'},
     expect=401, with_token=False)
post('不存在的账号 -> 401', f'{P}/auth/login',
     {'username': 'nobody', 'password': 'x'}, expect=401, with_token=False)
get('未登录访问受保护接口 -> 401', f'{P}/items', expect=401, with_token=False)

_login = parse(post('登录 reviewer', f'{P}/auth/login',
                    {'username': 'reviewer', 'password': 'ict2026'},
                    with_token=False))
TOKEN = _login.get('token', '')
if not TOKEN:
    print('  !! 未能取得令牌，后续受保护接口将全部失败')
else:
    print(f"         令牌已获取（role={_login.get('role')}, "
          f"有效期 {_login.get('expires_in')}s）")
get('当前用户 /auth/me', f'{P}/auth/me')
get('伪造令牌 -> 401', f'{P}/items', expect=401, with_token=False)

print('== 数据集 ==')
get('概览统计', f'{P}/dataset/stats')
get('不存在的导入任务 -> 404', f'{P}/dataset/import/nope', expect=404)

print('== 任务一 标的检索 ==')
get('无条件分页', f'{P}/items', {'page': 1, 'page_size': 5})
get('关键词检索', f'{P}/items', {'keyword': '服务器'})
get('筛选：品牌', f'{P}/items', {'brand': '五舟'})
get('筛选：采购单位', f'{P}/items', {'purchaser': '东莞市人民医院'})
get('价格区间', f'{P}/items', {'price_min': 100000, 'price_max': 500000})
get('排序 desc', f'{P}/items', {'sort_by': 'unit_price', 'sort_order': 'desc',
                                'page_size': 3})
get('筛选器取值域', f'{P}/items/facets')

print('== 任务二 五大场景 ==')
get('场景元定义', f'{P}/scenarios')
get('场景一', f'{P}/scenarios/s1', {'purchaser': '广州市增城区中心医院'})
get('场景一 空参数应降级', f'{P}/scenarios/s1', {'purchaser': ''})
get('场景二', f'{P}/scenarios/s2', {'purchaser': '广州市增城区中心医院'})
get('场景三', f'{P}/scenarios/s3', {'supplier': '联通数智医疗科技有限公司'})
get('场景四（2 个）', f'{P}/scenarios/s4',
    [('suppliers', '中国移动通信集团广东有限公司'),
     ('suppliers', '联通数智医疗科技有限公司')])
get('场景四 只给 1 个应降级', f'{P}/scenarios/s4',
    [('suppliers', '联通数智医疗科技有限公司')])
get('场景五（2 个）', f'{P}/scenarios/s5',
    [('suppliers', '中国电信股份有限公司广东分公司'),
     ('suppliers', '中国移动通信集团广东有限公司')])

# ==================================================================
# 数据断言：**有数据时必须真有数据**（改造前 37/37 全过但 S1 suppliers=[]）
# ==================================================================
print('== 数据一致性（有数据时）==')


def check(name, cond, detail=''):
    """断言型检查（计入 ok/fail，不依赖 HTTP 状态码）。"""
    global ok, fail
    ok, fail = ok + bool(cond), fail + (not cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name:<34} {detail}")
    return bool(cond)


DB = Path(os.environ['EDM_SQLITE_PATH'])


def _db(sql, args=()):
    """只读查平台库：返回一行 dict（库/表不存在返回 {}，不抛错）。"""
    if not DB.exists():
        return {}
    try:
        con = sqlite3.connect(f'file:{DB.as_posix()}?mode=ro', uri=True)
    except sqlite3.Error:
        return {}
    try:
        cur = con.execute(sql, args)
        row = cur.fetchone()
        if row is None:
            return {}
        return dict(zip([d[0] for d in cur.description], row))
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def _count(table):
    r = _db(f'SELECT count(*) AS n FROM "{table}"')
    return int(r.get('n') or 0)


def _has_table(name):
    """库里是否有这张表/视图（空库 = 什么表都没有）。"""
    r = _db("SELECT count(*) AS n FROM sqlite_master "
            "WHERE type IN ('table','view') AND name=?", (name,))
    return int(r.get('n') or 0) > 0


db_notice, db_item, db_bidder = (_count('t1_notice'), _count('t1_item'),
                                 _count('t1_bidder'))
print(f'         库里：t1_notice={db_notice} t1_item={db_item} t1_bidder={db_bidder}')
has_data = db_item > 0

# ---- 空库 / 空数据：**显式失败**（P2：整套数据断言都挂在 has_data 上，
#      于是把 EDM_SQLITE_PATH 指向一个空库时 43 项全过 —— 一个什么都没有的库
#      反而「冒烟通过」，这正是「37/37 全过而 S1 suppliers=[]」的翻版）----
schema_ok = _has_table('t1_notice') and _has_table('t1_item')
check('平台库已建表（t1_notice / t1_item 存在）', schema_ok,
      f'库 {DB}')
if schema_ok:
    check('t1_notice 非空（有表就必须有公告）', db_notice > 0,
          f'{db_notice} 篇')
    check('t1_item 非空（有表就必须有标的）', db_item > 0, f'{db_item} 条')

stats = parse(get('概览统计（应与库一致）', f'{P}/dataset/stats'))
if has_data:
    check('stats.n_docs == t1_notice 行数', stats.get('n_docs') == db_notice,
          f"{stats.get('n_docs')} vs {db_notice}")
    check('stats.n_items == t1_item 行数', stats.get('n_items') == db_item,
          f"{stats.get('n_items')} vs {db_item}")

page = parse(get('标的检索（total 应与库一致）', f'{P}/items',
                 {'page': 1, 'page_size': 1}))
if has_data:
    check('/items total == t1_item 行数',
          (page.get('meta') or {}).get('total') == db_item,
          f"{(page.get('meta') or {}).get('total')} vs {db_item}")

    # ---- engine/source 与数值字段：抽库里一条标的，按名称检索回来逐字段比对 ----
    probe = _db('SELECT * FROM t1_item WHERE item_name IS NOT NULL AND item_name <> "" '
                'ORDER BY length(item_name) DESC LIMIT 1')
    if probe:
        got = parse(get('按名称检索该标的', f'{P}/items',
                        {'keyword': probe.get('item_name'), 'page_size': 50}))
        rows = got.get('items') or []
        # 接口字段名与库列名不同：`item_name` -> `product_name`、`announcement_id` -> `doc_id`
        hit = next((r for r in rows
                    if r.get('doc_id') == probe.get('announcement_id')
                    and r.get('product_name') == probe.get('item_name')), None)
        check('检索命中库里的标的行', hit is not None,
              f"{probe.get('announcement_id')} / {str(probe.get('item_name'))[:18]}…")
        if hit:
            expect_src = str(probe.get('source') or probe.get('engine') or '').strip()
            check('source（=engine）非空且与库一致',
                  bool(str(hit.get('source') or '').strip())
                  and hit.get('source') == expect_src,
                  f"{hit.get('source')!r} vs {expect_src!r}")
            try:
                same = (abs(float(hit.get('unit_price') or 0)
                            - float(probe.get('unit_price') or 0)) < 1e-6)
            except (TypeError, ValueError):
                same = False
            check('unit_price 与库一致', same,
                  f"{hit.get('unit_price')} vs {probe.get('unit_price')}")

    # ---- S1 / S3：**有数据就必须查得到**（P2：改造前只在 cooperation / cobidding /
    #      bid 表非空时才断言，于是「有数据但 S1/S3 全空」也能通过 ——
    #      正是「37/37 全过而 S1 suppliers=[]」那个缺陷的同一个漏洞面）。
    top_purchaser = (_db('SELECT purchaser, count(*) AS n FROM t1_notice '
                         'WHERE purchaser IS NOT NULL AND purchaser <> "" '
                         'GROUP BY purchaser ORDER BY n DESC LIMIT 1')
                     or {}).get('purchaser') or ''
    if top_purchaser:
        s1 = parse(get('场景一（有数据时必须非空）', f'{P}/scenarios/s1',
                       {'purchaser': top_purchaser}))
        n_sup = len(s1.get('suppliers') or [])
        check(f'S1 供应商非空（{top_purchaser[:12]}…）', n_sup > 0, f'{n_sup} 家')
    else:
        check('库里有带采购单位的公告（S1 可查）', False, 't1_notice.purchaser 全空')

    top_supplier = (_db('SELECT supplier_name, count(*) AS n FROM t1_item '
                        'WHERE supplier_name IS NOT NULL AND supplier_name <> "" '
                        'GROUP BY supplier_name ORDER BY n DESC LIMIT 1')
                    or {}).get('supplier_name') or ''
    if top_supplier:
        s3 = parse(get('场景三（有数据时必须非空）', f'{P}/scenarios/s3',
                       {'supplier': top_supplier}))
        n_bid = len(s3.get('all_bidders') or []) + len(s3.get('top_competitors') or [])
        check(f'S3 参与方非空（{top_supplier[:12]}…）', n_bid > 0, f'{n_bid} 个')
    else:
        check('库里有带供货方的标的（S3 可查）', False, 't1_item.supplier_name 全空')

# ---- P1-5 回归：下载的投标人表不许有「名称空行」，且三表键集合仍自洽 ----
bidder_csv = get('投标人表（下载）', f'{P}/export/official/投标人表',
                 expect=200, with_token=True)
try:
    import csv as _csv
    import io as _io
    bidder_rows = list(_csv.DictReader(
        _io.StringIO(bidder_csv.decode('utf-8-sig', 'ignore'))))
except Exception:                                          # noqa: BLE001
    bidder_rows = []
blank = [r for r in bidder_rows if not str(r.get('名称') or '').strip()]
check('投标人表无名称空行（P1-5）', not blank,
      f'{len(bidder_rows)} 行 / 空名 {len(blank)} 行')
item_csv = get('标的表（下载）', f'{P}/export/official/标的表', expect=200)
try:
    item_rows = list(_csv.DictReader(
        _io.StringIO(item_csv.decode('utf-8-sig', 'ignore'))))
except Exception:                                          # noqa: BLE001
    item_rows = []


def _keys(rows):
    return {(str(r.get('UUID') or '').strip(), str(r.get('标段/合同包') or '').strip())
            for r in rows}


_ik, _bk = _keys(item_rows), _keys(bidder_rows)
check('三表键集合自洽（标的表键 ⊆ 投标人表键）', _ik <= _bk,
      f'标的表 {len(_ik)} 键 / 缺 {len(_ik - _bk)}')

print('== 实体 ==')
get('补全（采购单位）', f'{P}/entities/suggest', {'q': '医院', 'type': 'purchaser'})
get('补全（供应商）', f'{P}/entities/suggest', {'q': '移动', 'type': 'supplier'})
print('== 登出 ==')
post('登出', f'{P}/auth/logout')
get('登出后原令牌失效 -> 401', f'{P}/items', expect=401)
# 重新登录，供后续静态资源检查使用
_login2 = parse(post('重新登录', f'{P}/auth/login',
                     {'username': 'reviewer', 'password': 'ict2026'},
                     with_token=False))
TOKEN = _login2.get('token', '')

# ---- 静态前端托管（生产模式：一个进程同时提供 API 与页面）----
static_dir = (ROOT / 'static').resolve()
if (static_dir / 'index.html').exists():
    print('== 静态前端（生产模式）==')
    body = request('GET / 返回 SPA 页面', 'GET', '/', with_token=False)
    if b'<div id="app">' not in body:
        print('         [WARN] 首页内容不是预期的 SPA 挂载点')
    import re as _re
    html = body.decode('utf-8', 'ignore')
    for a in _re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)[:4]:
        request(f'静态资源 {a.split("/")[-1][:32]}', 'GET', a, with_token=False)
    request('SPA 路由回退到 index.html', 'GET', '/overview', with_token=False)
    request('不存在的 API -> 404', 'GET', f'{P}/definitely-not-here',
            expect=404, with_token=False)
else:
    print('== 静态前端：未构建（跳过；开发期请用 npm run dev）==')

body = get('场景一响应结构样例', f'{P}/scenarios/s1',
           {'purchaser': '广州市增城区中心医院'})
try:
    d = json.loads(body)
    print('\n--- scenarios/s1 响应 ---')
    print(json.dumps({k: (v[:1] if isinstance(v, list) else v)
                      for k, v in d.items()}, ensure_ascii=False, indent=2)[:700])
except Exception:
    pass

# 汇总行**必须在最后**（改造前它被放在「响应样例」之前，
# 于是新增的检查若排在它后面就不计入读者看到的数字里）。
print(f'\n通过 {ok} 项，失败 {fail} 项')

server.should_exit = True
sys.exit(1 if fail else 0)
