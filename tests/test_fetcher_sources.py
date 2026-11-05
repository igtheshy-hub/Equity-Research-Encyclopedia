"""取数源解析回归：乘联会块结构、人行单位与跨列月份、IMF 国家过滤、来源清单完整性。"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import fetcher  # noqa: E402


def _month_items():
    items = []
    for m in range(1, 13):
        item = {"month": f"{m}月", "2025年": [100 + m, 90 + m, 80 + m, 10 + m]}
        if m <= 9:
            item["2026年"] = [110 + m, 100 + m, 90 + m, 12 + m]
            item["同比"] = [5.0, 4.0, 3.0, 2.0]
        items.append(item)
    return items


def test_cpca_month_rows_take_latest_year_and_emit_yoy_once():
    data = [{"category": "整体市场", "dataList": _month_items()},
            {"category": "整体市场", "dataList": _month_items()}]
    rows = fetcher._cpca_rows(data, 1, months=3)
    assert len(rows) == 72  # 2 个口径 × 3 个月 ×（2 年 × 4 指标 + 4 同比）
    stamps = {r["时点"] for r in rows}
    assert stamps == {f"2026年{m}月" for m in (7, 8, 9)} | {f"2025年{m}月" for m in (7, 8, 9)}
    yoy = [r for r in rows if r["指标"] == "狭义乘用车·产量同比" and r["时点"] == "2026年7月"]
    assert len(yoy) == 1


def test_cpca_secondary_blocks_keep_sales_and_share_metrics():
    data = [
        {"category": "整体市场", "dataList": _month_items()[:3]},
        {"category": "", "dataList": [
            {"月份": "2026-1月", "BEV": [1, 2, 3.5, 4.5], "PHEV": [5, 6, 7.5, 8.5]}]},
        {"category": "", "dataList": [
            {"月份": "2026-1月", "ICE": [1, 2, 3.5, 4.5], "NEV": [5, 6, 7.5, 8.5]}]},
    ]
    rows = fetcher._cpca_rows(data, 6, months=3)
    index = {(r["指标"], r["时点"]): r["数值"] for r in rows}
    assert index[("新能源车型·BEV·当月份额", "2026年1月")] == 3.5
    assert index[("动力类型·NEV·累计份额", "2026年1月")] == 8.5


def test_cpca_ratio_block_sorts_periods_before_slicing():
    data = [{"category": "占比", "dataList": [
        {"月份": "2026-1月", "MPV": [1, 2, 3, 4]},
        {"月份": "2025-12月", "MPV": [5, 6, 7, 8]},
        {"月份": "2026-2月", "MPV": [9, 10, 11, 12]},
    ]}]
    rows = fetcher._cpca_rows(data, 3, months=1)
    assert {r["时点"] for r in rows} == {"2026年2月"}


def test_cpca_maker_stamp_normalized_and_yoy_once():
    data = [{"category": "Man market", "dataList": [{
        "厂商": "比亚迪汽车", "2026年 1-9月": [307.5, 169.5],
        "2025年 1-9月": [321.8, 254.2], "同比": [-4.4, -33.3]}]}]
    rows = fetcher._cpca_rows(data, 2, top=3)
    stamps = {r["时点"] for r in rows}
    assert stamps == {"2026年1-9月", "2025年1-9月"}
    assert len([r for r in rows if r["指标"].endswith("批发同比")]) == 1


def test_pbc_units_below_header_and_month_spans_two_columns():
    df = pd.DataFrame([
        ["官方储备资产", None, None, None],
        [None, None, None, None],
        ["项目 Item", "2026.01", None, "2026.02"],
        [None, "亿美元", "亿SDR", "亿美元"],
        ["外汇储备", 33990.78, 24597.67, 34278.07],
    ])
    rows = fetcher._pbc_rows_from_df(df, "官方储备资产", months=2)
    assert [(r["时点"], r["单位"]) for r in rows] == [
        ("2026-01", "亿美元"), ("2026-01", "亿SDR"), ("2026-02", "亿美元")]


def test_pbc_unit_above_header_and_skips_empty_future_months():
    df = pd.DataFrame([
        ["货币当局资产负债表", None, None, None],
        [None, None, None, None],
        [None, None, None, "单位：亿元人民币"],
        ["项目 Item", "2026.01", "2026.02", "2026.03"],
        ["国外资产", 1.5, 2.5, None],
    ])
    rows = fetcher._pbc_rows_from_df(df, "货币当局资产负债表", months=3)
    assert [r["时点"] for r in rows] == ["2026-01", "2026-02"]
    assert {r["单位"] for r in rows} == {"亿元人民币"}


def test_imf_filters_to_requested_countries(monkeypatch):
    payload = {"values": {"NGDP_RPCH": {
        "CHN": {"2024": 5.0, "2025": 4.5},
        "USA": {"2024": 2.0},
        "SDN": {"2024": 1.0},
    }}}
    monkeypatch.setattr(fetcher, "_get_json", lambda url, params=None: payload)
    res = fetcher.fetch_imf_series("NGDP_RPCH", "CHN,USA", periods=24)
    assert {r["指标"] for r in res["rows"]} == {"NGDP_RPCH·CHN", "NGDP_RPCH·USA"}
    assert len(res["rows"]) == 3


def test_source_specs_ids_unique_and_required_params_declared():
    specs = fetcher.source_specs()
    ids = [s["id"] for s in specs]
    assert len(ids) == len(set(ids))
    assert {"cpca_sales", "pbc_stat", "imf_series", "caam_article"} <= set(ids)
    for spec in specs:
        names = [p["name"] for p in spec["params"]]
        assert len(names) == len(set(names)), spec["id"]
        for p in spec["params"]:
            assert p["type"] in {"text", "number", "select", "checkbox"}, (spec["id"], p["name"])
            if p.get("required"):
                assert "default" in p, (spec["id"], p["name"])


def test_eastmoney_qtype_zero_is_not_treated_as_missing(monkeypatch):
    seen = {}

    def fake(industry=None, broker=None, limit=3, months=24, brokers_only=False,
             qtype=1, stock_code="", industry_code="", org_code="",
             rating_change="", page=1):
        seen["qtype"] = qtype
        return {"items": [], "skipped": []}

    monkeypatch.setattr(fetcher, "fetch_eastmoney_reports", fake)
    fetcher.run_fetch("eastmoney_reports",
                      {"qtype": 0, "stock_code": "002594", "ingest": False})
    assert seen["qtype"] == 0


def test_page_parser_keeps_article_and_drops_nav():
    page = """
    <html><body>
      <nav><a>首页</a><a>财经</a><a>汽车</a></nav>
      <div class="article-content">
        <h1>11月乘用车零售份额</h1>
        <p>德系零售份额18.7%，日系15.5%，美系7.9%。</p>
        <p>自主品牌份额66.9%。</p>
      </div>
      <footer>版权所有 2026 京ICP备</footer>
    </body></html>
    """
    parser = fetcher._PageTextParser()
    parser.feed(page)
    text = parser.text()
    assert "18.7" in text and "66.9" in text
    assert "首页" not in text
    assert "版权所有" not in text


def test_page_parser_falls_back_when_no_content_container():
    page = "<html><body><p>第一条 2 万辆</p><p>第二条 3 万辆</p></body></html>"
    parser = fetcher._PageTextParser()
    parser.feed(page)
    text = parser.text()
    assert "2 万辆" in text and "3 万辆" in text


def test_page_date_prefers_url_then_meta_then_visible():
    assert fetcher._page_date("<html></html>", "https://x.com/news/20251215/abc.html").isoformat() == "2025-12-15"
    meta = '<meta property="article:published_time" content="2026-03-08T10:00:00+08:00">'
    assert fetcher._page_date(meta, "https://x.com/a.html").isoformat() == "2026-03-08"
    assert fetcher._page_date("<p>发布于 2024年11月5日 09:12</p>").isoformat() == "2024-11-05"
    assert fetcher._page_date("<html>无日期</html>", "https://x.com/a.html") is None


def test_query_earliest_uses_year_in_query():
    assert fetcher._query_earliest("乘联会 2025 2026 同比数据").isoformat() == "2025-01-01"
    from datetime import datetime, timedelta
    expected = (datetime.now() - timedelta(days=730)).date()
    assert fetcher._query_earliest("乘用车分阵营渗透率").isoformat() == expected.isoformat()


def test_web_gap_drops_older_pages(monkeypatch):
    from datetime import date

    pages = {
        "https://old.example.com/2022.html": ("德系份额20.5% 日系23.1%", date(2022, 2, 1)),
        "https://new.example.com/2026.html": ("德系份额18.7% 日系15.5%", date(2026, 3, 1)),
    }
    monkeypatch.setattr(fetcher, "_registry_hits", lambda q, limit=2: [])
    monkeypatch.setattr(fetcher, "fetch_web_search", lambda q, limit=4: {"items": [
        {"title": "旧文", "url": "https://old.example.com/2022.html", "snippet": ""},
        {"title": "新文", "url": "https://new.example.com/2026.html", "snippet": ""},
    ], "skipped": []})
    monkeypatch.setattr(fetcher, "_page_excerpt",
                        lambda url, query, **kw: pages.get(url, ("", None)))
    captured = {}

    class FakePath:
        name = "fake.docx"

        def relative_to(self, root):
            return self

        def as_posix(self):
            return "raw/reports/数据快照/fake.docx"

    def fake_snapshot(org, topic, url, rows, note=""):
        captured["rows"] = rows
        captured["note"] = note
        return FakePath()

    monkeypatch.setattr(fetcher, "make_snapshot", fake_snapshot)
    monkeypatch.setattr(fetcher, "ingest_snapshot", lambda p, tid=None, targets=None: {"entries_count": 2})
    monkeypatch.setattr(fetcher, "_remember_lesson", lambda *a, **k: None)
    monkeypatch.setattr(fetcher, "_remember_web_sources", lambda items: None)

    res = fetcher.fetch_web_gap("乘联会 2026 年乘用车分阵营份额")
    urls = [r["网址"] for r in captured["rows"]]
    assert urls == ["https://new.example.com/2026.html"]
    assert captured["rows"][0]["发布日期"] == "2026-03-01"
    assert "剔除 1 个" in captured["note"]
    assert res["entries_count"] == 2


def test_page_date_reads_late_publish_field_and_title_month():
    padding = "<p>正文</p>" * 5000
    page = padding + '<script>var x={publishTime:"2024-10-12 12:16:51"}</script>'
    assert fetcher._page_date(page, "https://x.com/a.html").isoformat() == "2024-10-12"
    assert fetcher._page_date("<html>无日期</html>", "https://x.com/a.html",
                              "协会发布|2023年6月份全国乘用车市场分析").isoformat() == "2023-06-01"
    assert fetcher._page_date("<html>无日期</html>", "https://x.com/a.html",
                              "9月零售210.9万辆") is None


def test_excerpt_year_takes_latest_year_and_quarter_labels():
    assert fetcher._excerpt_year("2023年6月零售189.4万辆") == 2023
    assert fetcher._excerpt_year("26Q3出口272万辆，较2023年增长") == 2026
    assert fetcher._excerpt_year("没有年份") is None


def test_web_gap_drops_undated_page_with_old_excerpt(monkeypatch):
    monkeypatch.setattr(fetcher, "_registry_hits", lambda q, limit=2: [])
    monkeypatch.setattr(fetcher, "fetch_web_search", lambda q, limit=4: {"items": [
        {"title": "协会发布|2023年6月份全国乘用车市场分析",
         "url": "https://mp.weixin.qq.com/s/old", "snippet": ""},
        {"title": "2026年10月乘用车市场分析",
         "url": "https://new.example.com/2026.html", "snippet": ""},
    ], "skipped": []})
    monkeypatch.setattr(fetcher, "_page_excerpt", lambda url, query, **kw: (
        ("2023年6月乘用车零售189.4万辆", None) if "old" in url
        else ("2026年10月自主品牌零售份额66.9%", None)))
    captured = {}

    class FakePath:
        name = "fake.docx"

        def relative_to(self, root):
            return self

        def as_posix(self):
            return "raw/reports/数据快照/fake.docx"

    monkeypatch.setattr(fetcher, "make_snapshot",
                        lambda org, topic, url, rows, note="": captured.update(
                            rows=rows, note=note) or FakePath())
    monkeypatch.setattr(fetcher, "ingest_snapshot",
                        lambda p, tid=None, targets=None: {"entries_count": 1})
    monkeypatch.setattr(fetcher, "_remember_lesson", lambda *a, **k: None)
    monkeypatch.setattr(fetcher, "_remember_web_sources", lambda items: None)

    fetcher.fetch_web_gap("乘联会 2026 年 乘用车 零售份额")
    assert [r["网址"] for r in captured["rows"]] == ["https://new.example.com/2026.html"]
    assert "剔除 1 个" in captured["note"]
