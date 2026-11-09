"""给导出页塞一份演示数据（看得清行数徽章 / 列清单 / chip 的样子）。

用法：python scripts/demo_export_data.py        # 造数据
     python scripts/demo_export_data.py --clean # 清掉
演示数据只写进 data/fin/ 与 wiki/，不碰真实 raw/ 与 data/manifest.json 里的其它条目。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

MARK = ".demo_export"


def clean():
    n = 0
    for p in list(config.FIN_DIR.glob(f"*{MARK}*.json")):
        p.unlink(); n += 1
    for p in list(config.WIKI_DIR.rglob(f"*{MARK}*.md")):
        p.unlink(); n += 1
    print(f"已清理 {n} 个演示文件")


def make():
    config.FIN_DIR.mkdir(parents=True, exist_ok=True)
    periods = {}
    for pk, end, rev in [("2024FY", "2024-12-31", 98765400000),
                         ("2025FY", "2025-12-31", 123456000000)]:
        periods[pk] = {
            "period_end": end,
            "audit_opinion": "标准无保留意见",
            "source": f"raw/filings/演示银行{MARK}/演示银行财报_{pk}.pdf",
            "items": {
                "营业收入": {"v": rev, "a": [f"[[演示银行财报_{pk}.pdf#p3]]"], "statement": "利润表"},
                "归母净利润": {"v": rev * 0.31, "a": [f"[[演示银行财报_{pk}.pdf#p4]]"], "statement": "利润表"},
                "净息差": {"v": 1.42, "a": [f"[[演示银行财报_{pk}.pdf#p5]]"], "statement": "利润表"},
                "资本充足率": {"v": 13.24, "a": [f"[[演示银行财报_{pk}.pdf#p6]]"], "statement": "资产负债表"},
                "不良贷款率": {"v": 1.07, "a": [f"[[演示银行财报_{pk}.pdf#p7]]"], "statement": "资产负债表"},
                "总资产": {"v": rev * 12.5, "a": [f"[[演示银行财报_{pk}.pdf#p8]]"], "statement": "资产负债表"},
            },
            "items_parent": {},
            "quotes": [],
            "indicators": {
                "净利率": {"v": 0.08, "a": [f"[[演示银行财报_{pk}.pdf#p9]]"]},
                "流动比率": {"v": 0.793, "a": [f"[[演示银行财报_{pk}.pdf#p10]]"]},
                "ROE(加权披露)": {"v": 0.1324, "a": [f"[[演示银行财报_{pk}.pdf#p11]]"]},
                "DSO(期末)": {"v": 16.8, "a": [f"[[演示银行财报_{pk}.pdf#p12]]"]},
                "总资产周转率": {"v": 0.0803, "a": [f"[[演示银行财报_{pk}.pdf#p13]]"]},
            },
        }
    (config.FIN_DIR / f"演示银行{MARK}.json").write_text(json.dumps({
        "slug": f"演示银行{MARK}", "company": f"演示银行{MARK}",
        "aliases": ["000002"], "periods": periods,
    }, ensure_ascii=False), encoding="utf-8")

    # 一个带关注点的知识页
    from src import wiki
    d = config.TYPE_DIRS["company"]
    d.mkdir(parents=True, exist_ok=True)
    wiki.write_page(d / f"演示银行{MARK}.md", {
        "title": f"演示银行{MARK}", "type": "company",
        "sources": [f"raw/filings/演示银行{MARK}/演示银行财报_2025FY.pdf"],
        "updated": "2026-01-01",
        "summary": "演示用：看导出页的行数徽章与列清单效果",
        "focus": ["新能源渗透率", "碳酸锂价格传导"],
        "focus_hits": ["新能源车渗透率达 47.3%", "碳酸锂价格环比 -8.2%"],
        "history": ["2026-01-01 演示数据"],
    }, f"# 演示银行{MARK}\n\n## 经营与财务\n\n"
       f"- 营业收入 1234.56 亿元，同比 +12.5%。[[演示银行财报_2025FY.pdf#p3]]\n"
       f"- 净息差 1.42%，环比持平。[[演示银行财报_2025FY.pdf#p5]]\n"
       f"- 资本充足率 13.24%。[[演示银行财报_2025FY.pdf#p6]]\n")
    print("演示数据已生成：")
    print(f"  data/fin/演示银行{MARK}.json（2 个报告期）")
    print(f"  wiki/companies/演示银行{MARK}.md（带关注点）")


if __name__ == "__main__":
    if "--clean" in sys.argv:
        clean()
    else:
        make()
