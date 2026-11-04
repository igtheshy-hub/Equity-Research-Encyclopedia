"""报告版式机械检查：表格题注、图题错位、全局编号。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import anchor_match as am, industry_report as ir  # noqa: E402

TABLE = "| 环节 | 代表主体 |\n|---|---|\n| 上游 | 甲公司 |\n"


def test_missing_table_caption_generated():
    md = "## 一、产业链\n\n" + TABLE
    got = ir._fix_tables_figs(md)
    assert "**表1 环节与代表主体**" in got


def test_figure_caption_above_table_relabelled():
    md = "## 一、产业链\n\n**图1 产业链结构**\n\n" + TABLE
    got = ir._fix_tables_figs(md)
    assert "**表1 产业链结构**" in got
    assert "**图1" not in got


def test_orphan_figure_caption_removed():
    md = "## 一、产业链\n\n**图1 产业链结构**\n\n正文。\n"
    got = ir._fix_tables_figs(md)
    assert "**图1" not in got
    assert "正文。" in got


def test_plain_table_caption_kept_and_bolded():
    md = "## 一、产业链\n\n表1 产业链结构\n\n" + TABLE
    got = ir._fix_tables_figs(md)
    assert "**表1 产业链结构**" in got
    assert got.count("表1 产业链结构") == 1


def test_global_renumber_across_sections():
    md = ("# 报告\n\n## 一、A\n\n**表1 甲表**\n\n" + TABLE +
          "\n见表1。\n\n## 二、B\n\n**表1 乙表**\n\n" + TABLE + "\n见表1。\n")
    got = ir._renumber_tables_figs(md)
    assert "**表1 甲表**" in got
    assert "**表2 乙表**" in got
    assert got.count("见表1。") == 1
    assert got.count("见表2。") == 1


def test_month_labels_are_not_numbers():
    assert am.line_terms("2024M10") == []
    assert am.line_terms("2024M10–2026M9") == []


def test_placeholder_rows_dropped():
    md = ("## 一、产量\n\n**表1 月度增速（2024M10–2026M9）**\n\n"
          "| 月份 | 同比 | 月份 | 同比 |\n|---|---|---|---|\n"
          "| 2024M10 | — | 2025M10 | — |\n"
          "| 2025M3 | 8.4% | 2026M3 | -0.1% |\n")
    got = ir._fix_tables_figs(md)
    assert "2024M10" not in got
    assert "2025M3" in got
    assert "（2025M3–2026M3）" in got


def test_table_with_only_missing_rows_removed():
    md = ("## 一、产量\n\n**表1 月度增速**\n\n"
          "| 月份 | 同比 |\n|---|---|\n"
          "| 2025M3 | — |\n")
    got = ir._fix_tables_figs(md)
    assert "表1" not in got
    assert "|" not in got


def test_fix_is_idempotent():
    md = "## 一、A\n\n" + TABLE
    once = ir._fix_tables_figs(md)
    twice = ir._fix_tables_figs(once)
    assert once == twice
