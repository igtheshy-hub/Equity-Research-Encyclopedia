"""审校证据定位：问题里引用的话常被改写，要能落回报告段落。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import industry_report as ir  # noqa: E402


def test_evidence_located_by_numbers_when_quote_rewritten(monkeypatch):
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {})
    md = ("## 一、产业链\n\n海外销量梯队中奇瑞以134.4万辆、比亚迪以104.9万辆"
          "（同比+145%）位居前列[[某研报.pdf#p23]]。\n")
    finding = {"where": "一、产业链 第1段",
               "problem": "“2024至2025年海外销量梯队中奇瑞以134.4万辆”未写明期间"}
    got = ir._finding_evidence(finding, md)
    assert "134.4万辆" in got
    assert "报告原句" in got


def test_evidence_marks_paragraph_without_anchor():
    md = "## 一、产业链\n\n这一段只有文字没有锚点。\n"
    finding = {"where": "一、产业链", "problem": "“只有文字没有锚点”缺依据"}
    got = ir._finding_evidence(finding, md)
    assert "（该段没有锚点）" in got
