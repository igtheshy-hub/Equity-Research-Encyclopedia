"""缺口去重范围：主体的定期报告在库只影响主体缺口，不波及其他公司。"""
from src import industry_report as ir


def test_other_company_gap_keeps_fetch(monkeypatch):
    """宇通的季报在库，不能把金龙/中通/安凯的季报缺口错标成不可自动补。"""
    job = {"kind": "company", "title": "宇通客车公司研究报告",
           "question": "请写一份宇通客车的公司研报", "gaps": []}
    monkeypatch.setattr(ir, "_periodic_in_library",
                        lambda j: {"quarterly": {"file": "宇通客车_2026年三季报_20261015.pdf"}})
    gaps = [
        {"id": "g4", "item": "金龙汽车2026年三季报（营收、毛利率）", "why": "同业对比",
         "fetch": {"source": "periodic_report",
                   "params": {"company": "金龙汽车", "market": "a", "kind": "q3"}}},
        {"id": "g5", "item": "宇通客车2026年三季报数据补充", "why": "更新",
         "fetch": {"source": "periodic_report",
                   "params": {"company": "宇通客车", "market": "a", "kind": "q3"}}},
    ]
    out = ir._drop_periodic_dupes(gaps, job)
    assert len(out) == 1
    assert out[0]["id"] == "g4"
    assert out[0]["fetch"], "他司缺口应保留取数源"


def test_subject_gap_fetch_cleared_when_in_library(monkeypatch):
    """主体的季报已在库、模型又列了措辞不含报告关键词的主体缺口时，置空取数源并注明。"""
    job = {"kind": "company", "title": "宇通客车公司研究报告",
           "question": "请写一份宇通客车的公司研报", "gaps": []}
    monkeypatch.setattr(ir, "_periodic_in_library",
                        lambda j: {"quarterly": {"file": "宇通客车_2026年三季报_20261015.pdf"}})
    gaps = [{"id": "g5", "item": "宇通客车最新季度业绩数据（更新对比）", "why": "更新",
             "fetch": {"source": "periodic_report",
                       "params": {"company": "宇通客车", "market": "a", "kind": "q3"}}}]
    out = ir._drop_periodic_dupes(gaps, job)
    assert len(out) == 1 and out[0]["fetch"] is None
    assert "库内已有" in out[0]["why"]
