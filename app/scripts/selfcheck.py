import json
import os

F7 = ["item_name", "category", "brand", "spec", "unit_price", "quantity", "total_price"]


def rate_score(k):
    return 10 if k == 7 else 5 if k >= 5 else 3 if k >= 3 else 0


ids = [ln.strip() for ln in open("all_ids.txt", encoding="utf-8") if ln.strip()]
dist = {}
cov_html = cov_att = n = 0
tot_rate = 0
items_total = 0
missing = []
for aid in ids:
    ip = f"out/full/{aid}/items.json"
    if not os.path.exists(ip):
        missing.append(aid)
        continue
    ext = json.load(open(ip, encoding="utf-8"))
    items = ext["items"]
    n += 1
    items_total += len(items)
    if any(it.get("source") == "MAIN_ITEM_TABLE" for it in items):
        cov_html += 1
    if any((it.get("source") or "").startswith(("ATTACHMENT", "VLTEXT", "ANNTEXT")) for it in items):
        cov_att += 1
    present = {f for f in F7 if any(it.get(f) not in (None, "") for it in items)}
    k = len(present)
    dist[k] = dist.get(k, 0) + 1
    tot_rate += rate_score(k)

print("参与统计篇数:", n, "| 缺 items.json:", len(missing), missing[:5])
print("items 总数:", items_total)
print("原文类有标的:", cov_html, "篇 | 附件类有标的:", cov_att, "篇")
print("字段数分布:", dict(sorted(dist.items(), reverse=True)))
print("平均提取率得分 = %.2f/10" % (tot_rate / n))
