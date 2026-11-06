"""审校未摄入统计：重复下载件按 SHA256 单列，不混进真未摄入。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import audit  # noqa: E402
from src.classify import sha256_of  # noqa: E402


def test_audit_raw_files_separates_duplicates_changed_and_missing(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    base = raw / "a.pdf"
    base.write_bytes(b"A")
    dup = raw / "a-2.pdf"
    dup.write_bytes(b"A")
    changed = raw / "d.pdf"
    changed.write_bytes(b"new")
    missing = raw / "c.pdf"
    missing.write_bytes(b"C")

    manifest = {"files": [
        {"path": "raw/a.pdf", "file": "a.pdf", "sha256": sha256_of(base)},
        {"path": "raw/d.pdf", "file": "d.pdf", "sha256": "old"},
    ]}
    monkeypatch.setattr(audit.config, "ROOT", tmp_path)
    monkeypatch.setattr(audit, "iter_pdfs", lambda: [base, dup, missing, changed])

    miss, dups, chg = audit.audit_raw_files(manifest)
    assert miss == ["raw/c.pdf"]
    assert dups == [{"path": "raw/a-2.pdf", "duplicate_of": "raw/a.pdf"}]
    assert [c["path"] for c in chg] == ["raw/d.pdf"]
