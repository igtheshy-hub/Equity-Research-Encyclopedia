"""导出页改版 + 关注点显示的回归测试。

覆盖：
1. export_preview() 给出每张表的行数与列清单
2. 导出时按勾选的列裁剪（列选择生效）
3. 科目表锚点是列表时不能整坨写进 Excel（曾经报 "Cannot convert [...] to Excel"）
4. 关注点写进 frontmatter 后，/kb 与知识页都要看得见（原来只有首页输入框，写进去却看不见）
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import check, run_tests  # noqa: E402

FIN_STORE = {
    "slug": "测试公司",
    "company": "测试公司",
    "aliases": ["000001"],
    "periods": {
        "2025FY": {
            "period_end": "2025-12-31",
            "audit_opinion": "标准无保留意见",
            "source": "raw/filings/测试公司/测试公司财报.pdf",
            "items": {
                "营业收入": {"v": 12345600000, "a": ["[[测试公司财报.pdf#p3]]"], "statement": "利润表"},
                "归母净利润": {"v": 3600000000, "a": ["[[测试公司财报.pdf#p4]]"], "statement": "利润表"},
            },
            "items_parent": {},
            "quotes": [],
            "indicators": {
                "净利率": {"v": 0.08, "a": ["[[测试公司财报.pdf#p6]]"]},
                "流动比率": {"v": 0.793, "a": ["[[测试公司财报.pdf#p7]]"]},
            },
        }
    },
}


def _sandbox_with_fin():
    """造一个带财报结构化数据的沙盒，返回 (config, 临时目录)。"""
    import json
    import tempfile
    from src import config

    tmp = Path(tempfile.mkdtemp())
    config.DATA_DIR = tmp / "data"
    config.FIN_DIR = config.DATA_DIR / "fin"
    config.MANIFEST_PATH = config.DATA_DIR / "manifest.json"
    config.WIKI_DIR = tmp / "wiki"
    config.TYPE_DIRS = {
        "company": config.WIKI_DIR / "companies",
        "industry": config.WIKI_DIR / "industries",
        "concept": config.WIKI_DIR / "concepts",
        "source": config.WIKI_DIR / "sources",
    }
    config.FIN_DIR.mkdir(parents=True, exist_ok=True)
    for d in config.TYPE_DIRS.values():
        d.mkdir(parents=True, exist_ok=True)
    (config.FIN_DIR / "测试公司.json").write_text(
        json.dumps(FIN_STORE, ensure_ascii=False), encoding="utf-8")
    (config.MANIFEST_PATH).write_text(json.dumps({"files": [{
        "file": "测试公司财报.pdf", "source_type": "filing", "report_date": "2025-12-31",
        "entries_count": 2, "topics_touched": ["测试公司"],
        "path": "raw/filings/测试公司/测试公司财报.pdf",
        "ingested_at": "2026-01-01T00:00:00",
    }]}, ensure_ascii=False), encoding="utf-8")
    return config, tmp


def test_export_preview():
    """导出页要能拿到每张表的行数与列清单（不然用户不知道勾了会导出什么）。"""
    print("导出预览：行数与列清单")
    _sandbox_with_fin()
    from src.excel_export import export_preview

    pv = export_preview()
    counts = pv["counts"]
    check("科目表行数", counts["items"], 2)
    check("指标表行数", counts["indicators"], 2)
    check("来源表行数", counts["sources"], 1)
    check("四张表都有列清单", sorted(pv["columns"].keys()),
          ["entries", "indicators", "items", "sources"])
    cols = pv["columns"]["items"]
    check("列清单带表头", cols[0]["label"], "公司")
    check("列清单带默认标记", any(c["default"] for c in cols), True)


def test_column_selection():
    """勾哪列就导哪列；一列都不勾 = 用默认列。"""
    print("导出：列选择生效")
    _sandbox_with_fin()
    from openpyxl import load_workbook
    from src.excel_export import build_workbook

    data = build_workbook(sheets=["items"], cols={"items": ["company", "item", "value_yi"]})
    wb = load_workbook(io.BytesIO(data))
    ws = wb["科目数据表"]
    hdrs = [c.value for c in ws[1]]
    check("只导勾选的三列", hdrs, ["公司", "科目", "金额(亿元)"])

    # 一列都不勾 → 用默认列
    data2 = build_workbook(sheets=["items"], cols={"items": []})
    wb2 = load_workbook(io.BytesIO(data2))
    hdrs2 = [c.value for c in wb2["科目数据表"][1]]
    check("一列不勾用默认列", "公司" in hdrs2 and "金额(亿元)" in hdrs2, True)
    check("默认列不含未勾的", "审计意见" in hdrs2, False)


def test_clear_columns_via_http_gets_defaults():
    """清空某张表的列后提交，HTTP 层也要回落到默认列，不能导出全部列。

    页面文案是「留空 = 用该表的默认列」，但浏览器不会提交未勾选的复选框；
    app 只有把空列表也收下，build_workbook 才分得清「清空」与「没传」。
    """
    print("导出：HTTP 清空列回落到默认列")
    _sandbox_with_fin()
    from openpyxl import load_workbook
    from src.web.app import app

    app.config.update(TESTING=True)
    with app.test_client() as c:
        # 模拟点「清空」：表单里没有任何 cols_items
        r = c.post("/export", data={"sheets": ["items"]})
        check("导出 200", r.status_code, 200)
        ws = load_workbook(io.BytesIO(r.get_data()))["科目数据表"]
        hdrs = [cell.value for cell in ws[1]]
    check("清空列后用默认列",
          hdrs, ["公司", "报告期", "期末日", "科目", "金额(亿元)", "锚点"])
    check("清空列后不导出未默认列", "审计意见" in hdrs, False)


def test_anchor_list_not_written_raw():
    """锚点存的是列表，不能整坨写进 Excel（曾报 Cannot convert [...] to Excel）。"""
    print("导出：锚点列表安全落盘")
    _sandbox_with_fin()
    from openpyxl import load_workbook
    from src.excel_export import build_workbook

    data = build_workbook(sheets=["items"], cols={"items": ["item", "anchor"]})
    wb = load_workbook(io.BytesIO(data))
    ws = wb["科目数据表"]
    vals = [r for r in ws.iter_rows(min_row=2, values_only=True)]
    check("两行科目", len(vals), 2)
    for row in vals:
        check(f"{row[0]} 的锚点是字符串", isinstance(row[1], str), True)
        check(f"{row[0]} 的锚点非空", bool(row[1]), True)


def test_indicator_units_correct():
    """比率类 ×100；倍数类不 ×100（0.793 不能变 79.3）。"""
    print("导出：派生指标单位")
    _sandbox_with_fin()
    from openpyxl import load_workbook
    from src.excel_export import build_workbook

    data = build_workbook(sheets=["indicators"],
                          cols={"indicators": ["indicator", "value", "unit"]})
    wb = load_workbook(io.BytesIO(data))
    ws = wb["派生指标表"]
    got = {r[0]: (r[1], r[2]) for r in ws.iter_rows(min_row=2, values_only=True)}
    check("净利率 ×100", got["净利率"], (8, "%"))
    check("流动比率不 ×100", got["流动比率"], (0.793, "倍"))


def test_focus_visible_in_kb_and_page():
    """关注点写进 frontmatter 后，/kb 列表与知识页都要显示得出来。"""
    print("关注点：/kb 与知识页可见")
    from src import wiki

    _sandbox_with_fin()
    # 造一页带 focus 的公司页
    from src import config
    p = config.TYPE_DIRS["company"] / "测试公司.md"
    wiki.write_page(p, {
        "title": "测试公司", "type": "company",
        "sources": ["raw/filings/测试公司/测试公司财报.pdf"],
        "updated": "2026-01-01",
        "focus": ["新能源渗透率", "碳酸锂价格传导"],
        "focus_hits": ["渗透率达 47.3%"],
    }, "# 测试公司\n\n## 经营与财务\n\n- 营收 123.46 亿元。[[测试公司财报.pdf#p3]]\n")

    # build_index_entry 要带出 focus
    e = wiki.build_index_entry(wiki.load_pages()[0])
    check("索引条目带出关注点", e["focus"], ["新能源渗透率", "碳酸锂价格传导"])
    check("索引条目带出命中数", e["focus_hits"], 1)

    # 页面渲染要出现关注点标签
    from src.web.app import app
    app.config.update(TESTING=True)
    with app.test_client() as c:
        r = c.get("/page/company/测试公司")
        body = r.get_data(as_text=True)
        check("知识页 200", r.status_code, 200)
        check("知识页显示关注点", "新能源渗透率" in body, True)
        check("知识页显示命中条目", "渗透率达 47.3%" in body, True)

        r2 = c.get("/kb")
        body2 = r2.get_data(as_text=True)
        check("知识库列表显示关注点", "新能源渗透率" in body2, True)


if __name__ == "__main__":
    run_tests(globals(), [
        "test_export_preview",
        "test_column_selection",
        "test_clear_columns_via_http_gets_defaults",
        "test_anchor_list_not_written_raw",
        "test_indicator_units_correct",
        "test_focus_visible_in_kb_and_page",
    ])
