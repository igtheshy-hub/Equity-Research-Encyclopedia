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


# ---------- 待修清单 48-55：预置指标、级联 spec、月数口径、样板入库勾选 ----------

def test_stats_presets_cover_nationwide_and_declared_freq_matrix():
    for name, p in fetcher.STATS_PRESETS.items():
        assert "全国" in p["regions"], name
        assert p["freqs"], name
        for freqs in p["freqs"].values():
            assert freqs and set(freqs) <= {"月", "季", "年"}, name
    assert fetcher.STATS_PRESETS["制造业采购经理指数（PMI）"]["regions"] == ["全国"]
    assert fetcher.STATS_PRESETS["国内生产总值（GDP）"]["freqs"]["全国"] == ["季", "年"]
    assert len(fetcher.STATS_PRESETS) == 5


def test_stats_preset_rejects_region_and_freq_before_any_request(monkeypatch):
    monkeypatch.setattr(fetcher, "_nbs_one",
                        lambda *a, **k: pytest.fail("校验失败不应发请求"))
    with pytest.raises(fetcher.FetchError, match="没有「火星」口径"):
        fetcher.fetch_stats_series("居民消费价格指数（CPI）", freq="年", region="火星")
    with pytest.raises(fetcher.FetchError, match="只有 年 频"):
        fetcher.fetch_stats_series("居民消费价格指数（CPI）", freq="月", region="全国")
    with pytest.raises(fetcher.FetchError, match="只能是 auto、月、季、年"):
        fetcher.fetch_stats_series("居民消费价格指数（CPI）", freq="周", region="全国")


def test_stats_preset_auto_freq_takes_first_allowed(monkeypatch):
    seen = {}

    def fake_nbs_one(keyword, freq, periods, region, strict_main="", prefer=None):
        seen["args"] = (keyword, freq, strict_main, prefer)
        return {"keyword": keyword, "indicator": "居民消费价格指数(上年=100)", "region": region,
                "dt_type": "YY", "dts": "2021YY-2025YY",
                "rows": [{"指标": "CPI", "数值": "100.2", "单位": "%", "口径": "全国", "时点": "2025"}]}

    monkeypatch.setattr(fetcher, "_nbs_one", fake_nbs_one)
    res = fetcher.fetch_stats_series("居民消费价格指数（CPI）",
                                     freq="auto", periods=5, region="广东")
    assert seen["args"] == ("居民消费价格", "年", "居民消费价格指数", "上年=100")
    assert res["freq"] == "年"


def test_dts_range_month_quarter_year_math():
    assert fetcher._dts_range("202609", "MM", 3) == "202607MM-202609MM"
    assert fetcher._dts_range("20263", "SS", 3) == "202601SS-202603SS"
    assert fetcher._dts_range("202603", "SS", 5) == "202503SS-202603SS"
    assert fetcher._dts_range("2025", "YY", 3) == "2023YY-2025YY"
    assert fetcher._dts_range("2025", "YY", 500) == "1906YY-2025YY"  # 期数封顶 120


def test_report_card_akshare_series_removed_but_ppi_kept():
    gone = {"macro_china_gdp_yearly", "macro_china_cpi_yearly",
            "macro_china_cx_pmi_yearly", "macro_china_cx_services_pmi_yearly"}
    assert not gone & set(fetcher.AKSHARE_SERIES)
    assert "macro_china_ppi_yearly" in fetcher.AKSHARE_SERIES


def test_stamp_norm_covers_all_four_stamp_families():
    assert fetcher._stamp_norm("2026年09月份") == ("2026-09", 2026 * 12 + 9)
    assert fetcher._stamp_norm("2026-9") == ("2026-09", 2026 * 12 + 9)
    assert fetcher._stamp_norm("2026/09/01") == ("2026-09-01", 2026 * 12 + 9)
    assert fetcher._stamp_norm("202609") == ("2026-09", 2026 * 12 + 9)
    assert fetcher._stamp_norm("2026年第1-3季度") == ("2026年第1-3季度", 2026 * 4 + 3)
    assert fetcher._stamp_norm("1735689600000") == ("2025-01-01", 2025 * 12 + 1)
    assert fetcher._stamp_norm("1735689600") == ("2025-01-01", 2025 * 12 + 1)
    assert fetcher._stamp_norm("说不清") == ("说不清", None)


def test_akshare_stale_note_flags_stale_and_passes_recent():
    stale = fetcher._akshare_stale_note([{"时点": "2020-01"}])
    assert "疑似停更" in stale and "2020-01" in stale
    from datetime import datetime
    now = datetime.now()
    recent = f"{now.year}-{now.month:02d}"
    assert fetcher._akshare_stale_note([{"时点": recent}]) == ""
    assert fetcher._akshare_stale_note([{"时点": "2026年第3季度"}]) == ""


def test_eastmoney_stock_code_resolves_only_a_stocks(monkeypatch):
    def no_network(url, params=None):
        raise AssertionError("6 位代码应直接透传，不查 suggest")

    monkeypatch.setattr(fetcher, "_get_json", no_network)
    assert fetcher.eastmoney_stock_code("002594") == ("002594", "")

    payload = {"QuotationCodeTable": {"Data": [
        {"Code": "00285", "Name": "比亚迪电子", "Classify": "HK"},
        {"Code": "002594", "Name": "比亚迪", "Classify": "AStock"},
        {"Code": "300750", "Name": "宁德时代", "Classify": "AStock"},
    ]}}
    monkeypatch.setattr(fetcher, "_get_json", lambda url, params=None: payload)
    assert fetcher.eastmoney_stock_code("比亚迪") == ("002594", "比亚迪")

    empty = {"QuotationCodeTable": {"Data": [{"Code": "00285", "Name": "比亚迪电子", "Classify": "HK"}]}}
    monkeypatch.setattr(fetcher, "_get_json", lambda url, params=None: empty)
    with pytest.raises(fetcher.FetchError, match="请直接填 6 位股票代码"):
        fetcher.eastmoney_stock_code("比亚迪电子")


def test_hkex_pick_filters_category_word_title_and_fileless_rows():
    rows = [
        {"TITLE": "截至2026年6月30日止六個月業績公告", "LONG_TEXT": "", "FILE_LINK": "/a.pdf"},
        {"TITLE": "董事會召開日期", "LONG_TEXT": "", "FILE_LINK": "/b.pdf"},
        {"TITLE": "業績公告（摘要）", "LONG_TEXT": "", "FILE_LINK": "/c.pdf"},
        {"TITLE": "截至2025年業績", "LONG_TEXT": "", "FILE_LINK": ""},
    ]
    assert [r["TITLE"] for r in fetcher._hkex_pick(rows, kw_filter="業績")] == [
        "截至2026年6月30日止六個月業績公告", "業績公告（摘要）"]
    assert [r["TITLE"] for r in fetcher._hkex_pick(rows, kw_filter="業績", title="六個月")] == [
        "截至2026年6月30日止六個月業績公告"]
    patterns = ["一級業績", "三級業績"]
    with_kw = [{"TITLE": "三級業績速覽", "LONG_TEXT": "", "FILE_LINK": "/d.pdf"}]
    assert [r["TITLE"] for r in fetcher._hkex_pick(with_kw, title_any=patterns)] == ["三級業績速覽"]


def test_sample_chain_ingest_false_skips_ingest_inbox(monkeypatch):
    monkeypatch.setattr(fetcher, "fetch_hibor_ranking", lambda **k: {"year": 2025, "items": []})
    monkeypatch.setattr(fetcher, "fetch_eastmoney_reports",
                        lambda *a, **k: {"items": [{"org": "X证券", "title": "t", "file": "x.pdf"}],
                                         "skipped": []})

    def no_ingest(files, tid=None):
        raise AssertionError("ingest=False 不应触发入库")

    monkeypatch.setattr(fetcher, "ingest_inbox", no_ingest)
    res = fetcher.sample_download_chain("乘用车", limit=1, ingest=False)
    assert res["ingest"] == {"classify": [], "ingest": []}
    assert res["items"][0]["file"] == "x.pdf"


def test_skill_source_table_covers_gap_sources():
    rows = fetcher._sources_block("STRUCTURED")
    ids = [r["id"] for r in rows]
    assert len(ids) == 17 and len(set(ids)) == 17
    assert {"stats_series", "cninfo_announce", "annual_report", "web_search"} <= set(ids)
    text = fetcher.gaps_sources_text()
    for r in rows:
        assert f"- {r['id']}：{r['name']}，{r['doc']}" in text
    assert "{{" in text  # 提示词用 replace 填充，双花括号是字面量


def test_remember_web_sources_appends_to_skill_table(tmp_path, monkeypatch):
    skill = tmp_path / "sources.md"
    skill.write_text(
        "# t\n\n<!-- SOURCES:WEB:START -->\n"
        '{"title": "旧源", "url": "https://a.example.com/x", "added": "2026-01-01"}\n'
        "<!-- SOURCES:WEB:END -->\n", encoding="utf-8")
    monkeypatch.setattr(fetcher, "SKILL_SOURCES_PATH", skill)
    fetcher._remember_web_sources([
        {"title": "旧源", "url": "https://a.example.com/x"},
        {"title": "新源", "url": "https://b.example.com/y"},
        {"title": "字典页", "url": "https://www.zdic.net/hans/x"},
    ])
    raw = skill.read_text(encoding="utf-8")
    rows = fetcher._sources_block("WEB")
    assert [r["url"] for r in rows] == ["https://a.example.com/x", "https://b.example.com/y"]
    assert raw.count("https://b.example.com/y") == 1
    assert "zdic.net" not in raw


def test_known_web_sources_falls_back_to_json(tmp_path, monkeypatch):
    missing = tmp_path / "missing.md"
    js = tmp_path / "web_sources.json"
    js.write_text('{"sources": [{"title": "t", "url": "https://c.example.com"}]}',
                  encoding="utf-8")
    monkeypatch.setattr(fetcher, "SKILL_SOURCES_PATH", missing)
    monkeypatch.setattr(fetcher, "WEB_SOURCES_PATH", js)
    assert fetcher.known_web_sources() == [{"title": "t", "url": "https://c.example.com"}]


def test_page_source_specs_now_five_with_cascade_metadata():
    specs = {s["id"]: s for s in fetcher.source_specs(only=fetcher.MANUAL_SOURCE_IDS)}
    assert list(specs) == list(fetcher.MANUAL_SOURCE_IDS)
    east = specs["eastmoney_reports"]
    by_name = {p["name"]: p for p in east["params"]}
    assert "rating_change" not in by_name
    assert by_name["qtype"]["options"] == ["1", "0"]
    assert by_name["industry"]["when"] == {"field": "qtype", "in": ["1"]}
    assert by_name["stock_code"]["when"] == {"field": "qtype", "in": ["0"]}
    for sid in ("cninfo_announce", "hkex_announce"):
        names = [p["name"] for p in specs[sid]["params"]]
        assert "days" not in names and "year" not in names, sid
        assert "months" in names, sid
    assert any(p["name"] == "ingest" for p in specs["sample_chain"]["params"])
    stats = specs["stats_series"]
    region = next(p for p in stats["params"] if p["name"] == "region")
    assert region["options_depends"]["fields"] == ["keyword"]
    freq = next(p for p in stats["params"] if p["name"] == "freq")
    assert freq["options_depends"]["fields"] == ["keyword", "region"]


def test_cninfo_pickers_match_spaced_sec_name():
    period = [{"announcementTitle": "2026年半年度报告", "secName": "五 粮 液"}]
    assert fetcher._pick_periodic(period, "五粮液", ("半年度报告", "半年报"))
    annual = [{"announcementTitle": "2025年年度报告", "secName": "五 粮 液"}]
    assert fetcher._pick_cninfo_annual(annual, "五粮液")


def test_fetch_cninfo_annual_prefers_stock_search(monkeypatch):
    calls = []

    monkeypatch.setattr(fetcher, "cninfo_stock",
                        lambda code="", name="": {"code": "000858", "orgId": "gssz0000858"})

    def fake_search(query, **kw):
        calls.append((query, kw))
        if kw.get("stock"):
            return [{"announcementTitle": "2025年年度报告", "secName": "五 粮 液",
                     "adjunctUrl": "https://example.test/a.pdf"}]
        return [{"announcementTitle": "G 五粮液2005年年度报告", "secName": "五 粮 液",
                 "adjunctUrl": "https://example.test/b.pdf"}]

    monkeypatch.setattr(fetcher, "_cninfo_search", fake_search)
    monkeypatch.setattr(fetcher, "_download_pdf", lambda url, stem: Path("x.pdf"))
    res = fetcher.fetch_cninfo_annual("五粮液")
    assert calls[0][1].get("stock") == "000858,gssz0000858"
    assert res["items"][0]["title"] == "2025年年度报告"


def test_fetch_cninfo_annual_rejects_stale_fulltext(monkeypatch):
    monkeypatch.setattr(fetcher, "cninfo_stock", lambda code="", name="": None)
    monkeypatch.setattr(fetcher, "_cninfo_search", lambda *a, **k: [
        {"announcementTitle": "G 五粮液2005年年度报告", "secName": "五 粮 液",
         "adjunctUrl": "https://example.test/b.pdf"}])
    with pytest.raises(fetcher.FetchError, match="2005"):
        fetcher.fetch_cninfo_annual("五粮液")


def test_fetch_cninfo_periodic_prefers_stock_search(monkeypatch):
    calls = []
    monkeypatch.setattr(fetcher, "cninfo_stock",
                        lambda code="", name="": {"code": "000858", "orgId": "gssz0000858"})

    def fake_search(query, **kw):
        calls.append((query, kw))
        if kw.get("stock"):
            return [{"announcementTitle": "2026年半年度报告", "secName": "五 粮 液",
                     "adjunctUrl": "https://example.test/a.pdf"}]
        return []

    monkeypatch.setattr(fetcher, "_cninfo_search", fake_search)
    monkeypatch.setattr(fetcher, "_download_pdf", lambda url, stem: Path("x.pdf"))
    res = fetcher.fetch_cninfo_periodic("五粮液", kind="interim", code="000858.SZ")
    assert calls[0][1].get("stock") == "000858,gssz0000858"
    assert res["items"][0]["title"] == "2026年半年度报告"


def test_fetch_cninfo_periodic_maps_quarterly_to_latest_a_quarter(monkeypatch):
    q = fetcher._latest_a_quarter()
    title = {"q1": "2026年第一季度报告", "q3": "2026年第三季度报告"}[q]
    cats = []
    monkeypatch.setattr(fetcher, "cninfo_stock",
                        lambda code="", name="": {"code": "000858", "orgId": "gssz0000858"})

    def fake_search(query, **kw):
        cats.append(kw.get("category"))
        return [{"announcementTitle": title, "secName": "五 粮 液",
                 "adjunctUrl": "https://example.test/q.pdf"}]

    monkeypatch.setattr(fetcher, "_cninfo_search", fake_search)
    monkeypatch.setattr(fetcher, "_download_pdf", lambda url, stem: Path("q.pdf"))
    res = fetcher.fetch_cninfo_periodic("五粮液", kind="quarterly", code="000858")
    assert cats[0] == fetcher.CNINFO_PERIODIC[q][0]
    assert res["items"][0]["category"] == fetcher.CNINFO_PERIODIC[q][2]


def test_csindex_valuation_matches_by_index_code(monkeypatch):
    def fake_get(path, params=None):
        if "index-basic-info" in path:
            return {"indexShortNameCn": "中证白酒", "indexFullNameCn": "中证白酒指数"}
        if "indexValuation" in path:
            return {"tradeDate": "20261106", "indexValuations": [
                {"indexCode": "399997", "indexName": "中证白酒指数",
                 "pe": 21.6, "pb": 5.1, "dp": 4.45}]}
        raise fetcher.FetchError("接口不可用")

    monkeypatch.setattr(fetcher, "_csindex_get", fake_get)
    res = fetcher.fetch_csindex_snapshot("399997")
    index = {r["指标"]: r["数值"] for r in res["rows"]}
    assert index["市盈率 PE"] == 21.6
    assert index["股息率"] == 4.45

def test_dynamic_shell_detects_js_placeholder_page():
    shell = ("首页 酒价格 今日酒价 白酒价格倒挂 茅台年份酒 品牌大全 登录 注册 "
             "最新价 - 加载中......")
    assert fetcher._dynamic_shell(shell)
    loaded = "贵州茅台 最新价 1427 元，总市值 17908 亿元，市盈率 19.4 倍"
    assert not fetcher._dynamic_shell(loaded)
    assert not fetcher._dynamic_shell("白酒行业深度调整，正文没有占位符")
