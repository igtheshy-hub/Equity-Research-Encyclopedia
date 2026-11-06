"""审校补录：三季报主要指标表里未被抄录的扣非净利润与加权平均 ROE。

数值与页码均按 PDF 主要会计数据表「年初至报告期末」列核对，锚点指向该页。
只补缺失项，不覆盖已有值。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, filing, wiki  # noqa: E402

# name: (扣非值, 页码, ROE值或 None, ROE页码或 None)
ADD = {
    "上声电子": (65353359.22, 1, 3.85, 2),
    "上汽集团": (4638899773.30, 1, 1.87, 2),
    "中国汽研": (658571524.84, 1, 8.61, 2),
    "亚普股份": (424830379.65, 1, 9.54, 2),
    "伯特利": (953710217.89, 1, None, None),
    "保隆科技": (189798304.36, 1, None, None),
    "北汽蓝谷": (-2929259643.44, 1, -59.56, 1),
    "千里科技": (6792902.32, 1, 0.74, 2),
    "华域汽车": (3804601832.48, 1, 6.35, 1),
    "均胜电子": (870978122.05, 1, 5.61, 2),
    "宇通客车": (3098580729.05, 1, 20.96, 1),
    "岱美股份": (499152149.23, 1, 12.47, 2),
    "广汽集团": (-8603077188.00, 1, -6.88, 2),
    "拓普集团": (1510577593.43, 1, 6.83, 2),
    "新泉股份": (586681451.53, 1, 8.35, 2),
    "旭升集团": (267247839.14, 1, 3.38, 2),
    "易实精密": (42721375.24, 3, 9.74, 3),
    "星宇股份": (881547127.18, 1, 8.17, 1),
    "春风动力": (1559502194.87, 1, 24.32, 1),
    "沪光股份": (60177336.70, 1, 2.4, 2),
    "爱柯迪": (661929541.60, 1, None, None),
    "爱玛科技": (968702387.43, 1, 10.76, 1),
    "福耀玻璃": (5879139817.00, 1, 15.66, 1),
    "福达股份": (221158150.22, 1, 8.40, 2),
    "科博达": (389261655.47, 1, None, None),
    "经纬恒润": (-511879344.25, 1, -9.89, 1),
    "继峰股份": (449528052.66, 1, 9.07, 2),
    "赛力斯": (-3743821120.37, 1, -0.75, 2),
    "金杯汽车": (102125907.74, 1, 6.74, 1),
    "金龙汽车": (186363757.83, 1, 9.74, 1),
    "银轮股份": (657450487.78, 1, 8.11, 1),
    "长城汽车": (2590085114.60, 1, 4.32, 2),
    "长安汽车": (-700902353.29, 1, 0.08, 1),
    "飞龙股份": (103300436.04, 1, None, None),
    "骆驼股份": (344677115.13, 1, 3.52, 2),
    "联明股份": (None, None, 6.65, 2),
    "隆鑫通用": (None, None, 15.91, 2),
}


def _insert(body, lines):
    rows = body.splitlines()
    if "## 经营与财务" in rows:
        start = rows.index("## 经营与财务")
        end = next((i for i in range(start + 1, len(rows)) if rows[i].startswith("## ")), len(rows))
        pos = end
        while pos > start + 1 and not rows[pos - 1].strip():
            pos -= 1
        rows[pos:pos] = lines
    else:
        rows = rows + [""] + lines
    return "\n".join(rows).rstrip() + "\n"


def main():
    changed_pages = 0
    for name, (cf, cf_page, roe, roe_page) in ADD.items():
        page = next((p for p in wiki.load_pages() if p["slug"] == name), None)
        if page is None:
            print(name, "无页面，跳过")
            continue
        pdfs = list((config.FILINGS_DIR / name).glob("*_2026年三季报_*.pdf"))
        if not pdfs:
            print(name, "无三季报，跳过")
            continue
        fname = pdfs[0].name
        fin_path = config.FIN_DIR / f"{name}.json"
        data = json.loads(fin_path.read_text(encoding="utf-8"))
        items = data["periods"]["2026Q3"]["items"]
        ind = data["periods"]["2026Q3"].setdefault("indicators", {})
        body = page["body"]
        new_lines = []
        changed = []

        if cf is not None and "扣除非经常性损益的净利润" not in items:
            anchor = f"[[{fname}#p{cf_page}]]"
            items["扣除非经常性损益的净利润"] = {
                "v": cf, "a": anchor, "statement": "主要会计数据", "listed": True, "unit": "元",
            }
            line = f"- 【数据】2026Q3 合并 扣除非经常性损益的净利润 = {filing._fmt_value(cf)} 元 {anchor}"
            if "扣除非经常性损益的净利润 = " not in body:
                new_lines.append(line)
                changed.append("扣非")

        if roe is not None and "加权平均净资产收益率" not in items:
            anchor = f"[[{fname}#p{roe_page}]]"
            items["加权平均净资产收益率"] = {
                "v": roe, "a": anchor, "statement": "主要会计数据", "listed": True, "unit": "%",
            }
            line = f"- 【数据】2026Q3 合并 加权平均净资产收益率 = {roe:.2f}% {anchor}"
            if "加权平均净资产收益率 = " not in body:
                new_lines.append(line)
                changed.append("ROE")
            if "ROE(加权披露)" not in ind:
                ind["ROE(加权披露)"] = {"v": roe / 100, "a": anchor}
                new_lines.append(f"- 【指标】2026Q3 ROE(加权披露) = {roe / 100:.4f} {anchor}")
                changed.append("ROE指标")

        if not changed:
            continue
        meta = page["meta"]
        meta.setdefault("history", []).append(f"{config.today_str()} 审校补录：主要会计指标（{len(changed)} 项）")
        meta["updated"] = config.today_str()
        wiki.write_page(page["path"], meta, _insert(body, new_lines))
        fin_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(name, changed)
        changed_pages += 1
    print(f"补录 {changed_pages} 页")
    if changed_pages:
        wiki.rebuild_index_md(wiki.load_pages())
        wiki.append_log("fix", f"审校补录：{changed_pages} 页主要会计指标")


if __name__ == "__main__":
    main()
