"""收件台第四类：其他材料归入 raw/others/，摄入走通用编译管线（source_kind=other）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import classify, config, ingest, purge  # noqa: E402


def test_classify_one_files_other_into_others_dir(monkeypatch):
    src = config.INBOX_DIR / "会议纪要.docx"
    src.write_bytes(b"dummy")
    monkeypatch.setattr(classify, "first_text", lambda p: "渠道调研会议纪要正文")
    monkeypatch.setattr(classify.llm, "chat", lambda *a, **k: {
        "kind": "other", "broker": "", "topic": "渠道调研", "industry": "",
        "date": "20261109", "reason": "会议纪要，不属于研报、财报或政策"})

    r = classify.classify_one(src)

    assert r["ok"] is True and r["kind"] == "other"
    dest = config.OTHERS_DIR / "渠道调研其他材料_20261109.docx"
    assert dest.is_file() and not src.exists()
    assert r["target"] == dest.relative_to(config.ROOT).as_posix()
    assert ingest.source_kind(dest) == "other"
    assert purge.KIND_LABELS["other"] == "其他材料"


def test_other_filename_falls_back_to_original_stem():
    name = classify.build_filename(
        {"kind": "other", "broker": "", "topic": "", "date": "20261109"}, "晨会纪要", ".docx")
    assert name == "晨会纪要其他材料_20261109.docx"


def test_filing_filename_uses_period_when_given():
    name = classify.build_filename(
        {"kind": "filing", "broker": "", "topic": "赛力斯", "period": "2025年年报",
         "date": "20260330"}, "原名", ".pdf")
    assert name == "赛力斯_2025年年报_20260330.pdf"
    # 模型没给报告期时退回旧格式（主题+类型词+日期）
    name2 = classify.build_filename(
        {"kind": "filing", "broker": "", "topic": "赛力斯", "date": "20260330"}, "原名", ".pdf")
    assert name2 == "赛力斯财报_20260330.pdf"
