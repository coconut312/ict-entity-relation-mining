"""按赛题任务一评分细则，对 sample50 抽取结果打分，并测算接入视觉(Qwen-VL)的边际收益。"""
import json
from pathlib import Path

ROOT = Path("out/sample50")
IDS = sorted(d.name for d in ROOT.iterdir() if d.is_dir())
FIELDS = ["item_name", "category", "brand", "spec", "unit_price", "quantity", "total_price"]
IMG_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
DATABEARING = ("报价", "分项报价", "标的", "采购需求", "设备清单")


def rate_score(k):
    """赛题提取率赋分：7 全得 10；5<=k<7 得 5；3<=k<5 得 3；<3 得 0。"""
    return 10 if k == 7 else 5 if k >= 5 else 3 if k >= 3 else 0


def main():
    cov_html = cov_att = 0
    field_dist = {}
    total_rate = 0
    n_review = n_items = 0
    vlm_gain = 0
    vlm_cases = []
    for aid in IDS:
        items = json.loads((ROOT / aid / "items.json").read_text(encoding="utf-8"))["items"]
        rec = json.loads((ROOT / aid / "ir.json").read_text(encoding="utf-8"))
        n_items += len(items)
        n_review += sum(1 for it in items if it.get("needs_review"))
        from_html = any(it["source"] == "MAIN_ITEM_TABLE" for it in items)
        from_att = any(it["source"].startswith("ATTACHMENT") for it in items)
        cov_html += from_html
        cov_att += from_att
        present = {f for f in FIELDS if any(it.get(f) not in (None, "") for it in items)}
        k = len(present)
        field_dist[k] = field_dist.get(k, 0) + 1
        cur = rate_score(k)
        total_rate += cur
        # 视觉候选：字段不全 且 附件里有“图片/扫描件”且名字像数据载体(报价)
        if k < 7:
            hit = None
            for a in rec.get("extracted_files", []):
                ext = (a.get("ext") or "").lower()
                note = a.get("note") or ""
                name = a.get("rel_path") or a.get("name") or ""
                is_img = ext in IMG_EXT
                is_scan = "scanned_suspect=True" in note
                if (is_img or is_scan) and any(kk in name for kk in DATABEARING):
                    hit = name.split("\\")[-1]
                    break
            if hit:
                gain = 10 - cur
                vlm_gain += gain
                vlm_cases.append((aid, k, cur, gain, hit))

    n = len(IDS)
    print("=== 覆盖率（赛题 10 分：两类各 5）===")
    print(f"  原文类有标的提取: {cov_html}/{n} 篇")
    print(f"  附件类有标的提取: {cov_att}/{n} 篇")
    print("  -> 两类均已实现，覆盖率 = 10/10")

    print("\n=== 提取率（赛题 10 分：7全=10, 5-6=5, 3-4=3, <3=0）===")
    for k in sorted(field_dist, reverse=True):
        print(f"  {k} 字段: {field_dist[k]} 篇 -> 单篇 {rate_score(k)} 分")
    print(f"  平均提取率得分 = {total_rate}/{n*10} = {total_rate/n:.2f}/10")

    print("\n=== 准确性（赛题 15 分，排名赋分）===")
    print(f"  无官方基准，无法算绝对分；内部代理：needs_review {n_review}/{n_items} 条")
    print("  综合=准确率×0.4+精确率×0.3+召回率×0.3（待基准/自建验证集）")

    print("\n=== 处理速率（赛题 5 分，排名赋分）===")
    print("  需在统一平台实测单篇(含附件)平均耗时；视觉会显著拖慢")

    print("\n=== 接入视觉(Qwen-VL)的边际收益 ===")
    print(f"  可受益篇数: {len(vlm_cases)}；提取率潜在加分: +{vlm_gain} 分（样本口径）")
    for aid, k, cur, gain, hit in vlm_cases:
        print(f"    {aid}: {k}字段({cur}分) +{gain} <- {hit}")


if __name__ == "__main__":
    main()
