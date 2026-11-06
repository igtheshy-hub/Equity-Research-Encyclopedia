"""取数源解析自测：慧博榜单 HTML、行业别名与冠军匹配、巨潮标题清洗、AKShare 表转行。

用法：python tests/test_fetcher_parse.py（全部离线，不请求网络）
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import fetcher  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


HIBOR_HTML = """
<table>
  <tr><td><b>（Q）汽车和汽车零部件</b></td><td>1</td><td>东吴证券</td><td>张三，李四、王五</td></tr>
  <tr><td></td><td>2</td><td>中信证券</td><td>赵六</td></tr>
  <tr><td></td><td>名次</td><td>华泰证券</td><td>钱七</td></tr>
  <tr><td><b>（Y）食品饮料</b></td><td>1</td><td>招商证券</td><td>孙八</td></tr>
</table>
"""


def main():
    print("== 慧博榜单解析 ==")
    items = fetcher._parse_author_rows(HIBOR_HTML)
    check("解析行数", len(items), 3)
    check("组名清洗", items[0]["industry"], "汽车和汽车零部件")
    check("名次转整数", items[0]["rank"], 1)
    check("机构", items[0]["org"], "东吴证券")
    check("分析师切分", items[0]["analysts"], ["张三", "李四", "王五"])
    check("组名向下延续", items[1]["group"], "（Q）汽车和汽车零部件")
    check("第二组独立", items[2]["industry"], "食品饮料")

    print("== 行业别名与冠军匹配 ==")
    check("组名前缀剥离", fetcher._strip_group_prefix("（Q）汽车和汽车零部件"), "汽车和汽车零部件")
    check("乘用车扩别名", fetcher._industry_terms("乘用车"), ["乘用车", "汽车"])
    check("包含式得分", fetcher._match_score("汽车", "（Q）汽车和汽车零部件"), 102)
    champ = fetcher.match_champion(items, "乘用车", use_llm=False)
    check("乘用车匹配东吴证券", champ["org"], "东吴证券")
    check("匹配行业组", fetcher.match_group(items, "乘用车", use_llm=False), "（Q）汽车和汽车零部件")
    check("不硬配不相干组", fetcher.match_champion(items, "煤炭", use_llm=False), None)

    print("== 巨潮标题清洗 ==")
    check("去 em 标签", fetcher._clean_title("<em>比亚迪</em>：2025年年度报告"), "比亚迪：2025年年度报告")
    check("空值", fetcher._clean_title(None), "")

    print("== AKShare 表转快照行 ==")
    payload = {"series": "中国乘用车销量", "columns": ["日期", "销量"],
               "records": [{"日期": "2026-01", "销量": 123.4},
                           {"日期": "2026-02", "销量": None}]}
    rows = fetcher._akshare_rows(payload)
    check("只保留有效数值", len(rows), 1)
    check("指标名带序列", rows[0]["指标"], "中国乘用车销量·销量")
    check("数值转字符串", rows[0]["数值"], "123.4")
    check("时点", rows[0]["时点"], "2026-01")

    print()
    if FAILS:
        print(f"{len(FAILS)} 项未通过：{'、'.join(FAILS)}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
