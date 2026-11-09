"""来源优先级：财报 > 公告/招股书 > 研报 > 其他，问答、审校、成文三处同一口径。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import ask, audit, industry_report, report, source_priority  # noqa: E402

MANIFEST = {"files": [
    {"file": "比亚迪_2026年三季报_20261030.pdf",
     "path": "raw/filings/比亚迪/比亚迪_2026年三季报_20261030.pdf", "source_type": "filing"},
    {"file": "中信证券_汽车行业研报_20260901.pdf",
     "path": "raw/reports/汽车行业/中信证券_汽车行业研报_20260901.pdf", "source_type": "report"},
    {"file": "比亚迪_公告_20260901.pdf",
     "path": "raw/reports/汽车行业/比亚迪_公告_20260901.pdf", "source_type": "report"},
    {"file": "汽车产业政策.pdf",
     "path": "raw/policies/汽车产业政策.pdf", "source_type": "policy"},
    {"file": "乘联会_销量快照_20260901.docx",
     "path": "raw/snapshots/乘联会_销量快照_20260901.docx", "source_type": "snapshot"},
    {"file": "乘联会_销量快照_20260801.docx",
     "path": "raw/reports/数据快照/乘联会_销量快照_20260801.docx", "source_type": "report"},
    {"file": "某券商_渠道调研其他材料_20260901.docx",
     "path": "raw/others/某券商_渠道调研其他材料_20260901.docx", "source_type": "other"},
]}

FILING = "[[比亚迪_2026年三季报_20261030.pdf#p12]]"
REPORT = "[[中信证券_汽车行业研报_20260901.pdf#p5]]"
ANNOUNCE = "[[比亚迪_公告_20260901.pdf#p3]]"
POLICY = "[[汽车产业政策.pdf#p1]]"
SNAPSHOT = "[[乘联会_销量快照_20260901.docx#para8]]"
LEGACY_SNAPSHOT = "[[乘联会_销量快照_20260801.docx#para3]]"
OTHER = "[[某券商_渠道调研其他材料_20260901.docx#para2]]"


def test_classify_uses_path_and_name():
    assert source_priority.classify(FILING, MANIFEST) == "filing"
    assert source_priority.classify(REPORT, MANIFEST) == "report"
    assert source_priority.classify(ANNOUNCE, MANIFEST) == "announcement"
    assert source_priority.classify(POLICY, MANIFEST) == "policy"
    assert source_priority.classify(SNAPSHOT, MANIFEST) == "snapshot"
    assert source_priority.classify(LEGACY_SNAPSHOT, MANIFEST) == "snapshot"
    assert source_priority.label(SNAPSHOT, MANIFEST) == "快照"
    assert source_priority.classify(OTHER, MANIFEST) == "other"
    assert source_priority.label(OTHER, MANIFEST) == "其他材料"


def test_rank_order_and_prefer():
    assert source_priority.rank(FILING, MANIFEST) < source_priority.rank(ANNOUNCE, MANIFEST)
    assert source_priority.rank(ANNOUNCE, MANIFEST) < source_priority.rank(REPORT, MANIFEST)
    assert source_priority.prefer(FILING, REPORT, MANIFEST) == "a"
    assert source_priority.prefer(REPORT, FILING, MANIFEST) == "b"
    assert source_priority.prefer(REPORT, ANNOUNCE, MANIFEST) == "b"
    assert source_priority.prefer(REPORT, "[[另一份研报.pdf#p2]]", MANIFEST) == "tie"


def test_page_legend_groups_sources_and_flags_conflict():
    page = {"body": f"- 营收 100 亿元 {FILING}\n- 营收 98 亿元 {REPORT}\n- 政策要求 {POLICY}\n- 销量 {SNAPSHOT}"}
    text = source_priority.page_legend(page, MANIFEST)
    assert "财报（比亚迪_2026年三季报_20261030.pdf）" in text
    assert "研报（中信证券_汽车行业研报_20260901.pdf）" in text
    assert "政策（汽车产业政策.pdf）" in text
    assert "快照（乘联会_销量快照_20260901.docx）" in text
    assert "冲突时以财报为准" in text


def test_conflict_note_picks_filing_side():
    note = source_priority.conflict_note(REPORT, FILING, MANIFEST)
    assert note["source_a"] == "研报"
    assert note["source_b"] == "财报"
    assert note["prefer"] == "b"
    assert "以 B 为准" in note["source_note"]


def test_prompts_carry_priority_rule():
    for text in (ask.ANSWER_PROMPT, ask.FIN_ANALYSIS_BLOCK, ask.DEEP_ANSWER_PROMPT,
                 report.REPORT_PROMPT, industry_report.WRITE_PROMPT, audit.CONFLICT_PROMPT):
        assert "公司财务数据的来源优先级" in text
        assert "以最新报告期的财报为准" in text
