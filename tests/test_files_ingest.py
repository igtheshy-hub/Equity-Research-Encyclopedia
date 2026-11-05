"""研报选页不限页数、原文件页摄入入口的路径与重复件校验。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import ask as ask_mod  # noqa: E402
from src import purge  # noqa: E402
from src.web import api as api_mod  # noqa: E402


def test_max_pages_rule():
    assert ask_mod._max_pages_rule(0) == "不限页数"
    assert ask_mod._max_pages_rule(-1) == "不限页数"
    assert ask_mod._max_pages_rule(8) == "最多 8 页"
    assert ask_mod._max_pages_rule(0, soft_pages=20) == "不设固定上限，通常不超过 20 页"


def test_select_pages_unlimited_prompt(monkeypatch):
    seen = {}

    def fake_chat(messages, **_kwargs):
        seen["prompt"] = messages[0]["content"]
        return {"selected": [], "use_index": False, "reason": ""}

    monkeypatch.setattr(ask_mod, "_index_pages", lambda: ([], [{"slug": "a"}]))
    monkeypatch.setattr(ask_mod.wiki, "compact_index_text", lambda entries: "a | 行业 | | ")
    monkeypatch.setattr(ask_mod, "_history_block", lambda history: "")
    monkeypatch.setattr(ask_mod.llm, "chat", fake_chat)
    ask_mod._select_pages("客车", None, max_pages=0)
    assert "不限页数" in seen["prompt"]
    ask_mod._select_pages("客车", None, max_pages=0, soft_pages=20)
    assert "通常不超过 20 页" in seen["prompt"]
    ask_mod._select_pages("客车", None)
    assert f"最多 {ask_mod.config.CFG['ask']['max_pages']} 页" in seen["prompt"]


def test_ingest_pick_filters(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    reports = raw / "reports"
    filings = raw / "filings"
    policies = raw / "policies"
    inbox = raw / "inbox"
    for d in (reports, filings, policies, inbox):
        d.mkdir(parents=True, exist_ok=True)
    ok = reports / "研报.pdf"
    ok.write_bytes(b"ok")
    dup = filings / "财报.pdf"
    dup.write_bytes(b"dup")
    inbox_file = inbox / "待分类.pdf"
    inbox_file.write_bytes(b"inbox")
    isolated = filings / "_rejected" / "坏件.pdf"
    isolated.parent.mkdir(exist_ok=True)
    isolated.write_bytes(b"bad")

    monkeypatch.setattr(api_mod, "raw_file_index", lambda: {
        p.name: p for p in (ok, dup, inbox_file, isolated)})
    monkeypatch.setattr(api_mod.config, "RAW_DIR", raw)
    monkeypatch.setattr(api_mod.config, "ROOT", tmp_path)
    monkeypatch.setattr(api_mod.config, "REPORTS_DIR", reports)
    monkeypatch.setattr(api_mod.config, "FILINGS_DIR", filings)
    monkeypatch.setattr(api_mod.config, "POLICIES_DIR", policies)
    monkeypatch.setattr(api_mod.config, "INBOX_DIR", inbox)
    monkeypatch.setattr(api_mod.ingest_mod, "load_manifest",
                        lambda: {"files": [{"file": "已有.pdf", "sha256": "DUPSHA"}]})
    monkeypatch.setattr(api_mod.ingest_mod, "sha256_of",
                        lambda p: "DUPSHA" if p == dup else "NEW")

    paths, rejected = api_mod._ingest_pick(
        ["研报.pdf", "财报.pdf", "待分类.pdf", "坏件.pdf", "不存在.pdf"])
    assert [p.name for p in paths] == ["研报.pdf"]
    reasons = {r["file"]: r["reason"] for r in rejected}
    assert "内容相同" in reasons["财报.pdf"]
    assert "raw/reports、raw/filings、raw/policies" in reasons["待分类.pdf"]
    assert "隔离目录" in reasons["坏件.pdf"]
    assert reasons["不存在.pdf"] == "找不到文件"


def test_file_entries_marks_duplicate(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    filings = raw / "filings"
    filings.mkdir(parents=True, exist_ok=True)
    fresh = filings / "新财报.pdf"
    fresh.write_bytes(b"same")

    monkeypatch.setattr(purge, "raw_file_groups", lambda: {fresh.name: [fresh]})
    monkeypatch.setattr(purge.config, "ROOT", tmp_path)
    monkeypatch.setattr(purge.config, "INBOX_DIR", raw / "inbox")
    monkeypatch.setattr(purge, "load_manifest",
                        lambda: {"files": [{"file": "已有.pdf", "sha256": "SAME"}]})
    monkeypatch.setattr(purge, "sha256_of", lambda p: "SAME")
    monkeypatch.setattr(purge.wiki, "load_pages", lambda: [])

    rows = purge.file_entries()
    row = next(r for r in rows if r["name"] == "新财报.pdf")
    assert row["kind_label"] == "未摄入"
    assert row["duplicate_of"] == "已有.pdf"
