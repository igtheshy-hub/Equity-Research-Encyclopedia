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
