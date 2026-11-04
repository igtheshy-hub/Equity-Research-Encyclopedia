"""审校修正：主要会计数据误取「本报告期」列、现金流量表误取「上期」列。

只改数值与口径标签，锚点不动。改完写 history、记日志、重建索引。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, filing, wiki  # noqa: E402

# 扣非净利润 / 加权平均净资产收益率：本报告期列 -> 年初至报告期末列
FIX = {
    "三花智控": {"扣非": 3165788314.56},
    "中鼎股份": {"扣非": 1148892015.31, "ROE": 6.36},
    "华阳集团": {"扣非": 547350788.84, "ROE": 8.57},
    "双环传动": {"扣非": 845302202.15, "ROE": 8.81},
    "星源卓镁": {"扣非": -78287999.87, "ROE": -7.01},
    "松原安全": {"扣非": 211620900.08},
    "潍柴动力": {"扣非": 9338590640.99},
    "联明股份": {"扣非": 83259480.01},
    "隆鑫通用": {"扣非": 1522085242.10},
    "中原内配": {"扣非": 400707814.25, "ROE": 12.08},
    "光洋股份": {"扣非": -31593043.40, "ROE": 3.39},
    "德赛西威": {"扣非": 1793411602.75, "ROE": 11.63},
    "比亚迪": {"扣非": 21003793000.00, "ROE": 8.72},
    "瑞鹄模具": {"扣非": 325444262.93, "ROE": 13.18},
    "科博达": {"ROE": 7.35},
    "飞龙股份": {"ROE": 3.09},
    "伯特利": {"ROE": 12.68},
    "保隆科技": {"ROE": 7.65},
}

# 本期列为空、误抄了上期列的行：改标（上年同期），不删知识
PRIOR = {
    "三花智控": "吸收投资收到的现金",
    "伯特利": "发行债券收到的现金",
    "春风动力": "收回投资收到的现金",
    "福达股份": "收回投资收到的现金",
    "隆鑫通用": "投资支付的现金",
}


def _fmt_pct(v):
    return f"{v:.2f}"


def _fix_fin(name, spec, prior_item):
    path = config.FIN_DIR / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    period = data["periods"]["2026Q3"]
    items = period["items"]
    changed = []
    if "扣非" in spec:
        key = "扣除非经常性损益的净利润"
        if key in items:
            old = items[key].get("v")
            if abs(float(old) - spec["扣非"]) > 0.01:
                items[key]["v"] = spec["扣非"]
                changed.append(f"扣非 {old} -> {spec['扣非']}")
    if "ROE" in spec:
        key = "加权平均净资产收益率"
        if key in items:
            old = items[key].get("v")
            if abs(float(old) - spec["ROE"]) > 0.01:
                items[key]["v"] = spec["ROE"]
                changed.append(f"ROE {old} -> {spec['ROE']}")
        ind = period.get("indicators", {}).get("ROE(加权披露)")
        if ind is not None:
            ind["v"] = spec["ROE"] / 100
    if prior_item and prior_item in items:
        new_key = prior_item + "（上年同期）"
        items[new_key] = items.pop(prior_item)
        changed.append(f"{prior_item} 改标上年同期")
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return changed


def _fix_body(name, spec, prior_item):
    page = next(p for p in wiki.load_pages() if p["slug"] == name)
    body = page["body"]
    changes = []
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if not ln.strip().startswith("- "):
            continue
        if "扣非" in spec and " 扣除非经常性损益的净利润 = " in ln:
            m = re.search(r"(= )(-?[\d,]+(?:\.\d+)?)( 元)", ln)
            if m and abs(float(m.group(2).replace(",", "")) - spec["扣非"]) > 0.01:
                lines[i] = ln[:m.start(2)] + filing._fmt_value(spec["扣非"]) + ln[m.end(2):]
                changes.append("扣非")
        if "ROE" in spec and " 加权平均净资产收益率 = " in ln:
            m = re.search(r"(= )(-?[\d.]+)(% \[\[)", ln)
            if m and abs(float(m.group(2)) - spec["ROE"]) > 0.01:
                lines[i] = ln[:m.start(2)] + _fmt_pct(spec["ROE"]) + ln[m.end(2):]
                changes.append("ROE")
        if "ROE" in spec and " ROE(加权披露) = " in ln:
            m = re.search(r"(= )(-?[\d.]+)( \[\[)", ln)
            if m and abs(float(m.group(2)) - spec["ROE"] / 100) > 1e-4:
                lines[i] = ln[:m.start(2)] + f"{spec['ROE'] / 100:.4f}" + ln[m.end(2):]
                changes.append("ROE指标")
        if prior_item and f" 合并 {prior_item} = " in ln:
            lines[i] = ln.replace(f" 合并 {prior_item} = ", f" 合并 {prior_item}（上年同期） = ", 1)
            changes.append(prior_item)
    if not changes:
        return []
    meta = page["meta"]
    hist = meta.get("history") or []
    hist.append(f"{config.today_str()} 审校修正：主要指标改取年初至报告期末列 / 上期列改标（{len(changes)} 项）")
    meta["history"] = hist
    meta["updated"] = config.today_str()
    wiki.write_page(page["path"], meta, "\n".join(lines))
    return changes


def main():
    total = 0
    for name in sorted(set(FIX) | set(PRIOR)):
        fin_changes = _fix_fin(name, FIX.get(name, {}), PRIOR.get(name))
        wiki_changes = _fix_body(name, FIX.get(name, {}), PRIOR.get(name))
        if fin_changes or wiki_changes:
            print(f"{name}: fin={fin_changes} wiki={wiki_changes}")
            total += 1
    print(f"修正 {total} 页")
    if total:
        wiki.rebuild_index_md(wiki.load_pages())
        wiki.append_log("fix", f"审校修正：{total} 页主要指标改为累计口径、上期列改标")


if __name__ == "__main__":
    main()
