"""去重闸门：下载/归名/摄入三层都按内容判重，不产生 -2/-3 副本。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import classify, config, ingest  # noqa: E402


def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    for name in ("REPORTS_DIR", "FILINGS_DIR", "POLICIES_DIR", "OTHERS_DIR", "INBOX_DIR"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(config, name, d)
    return (config.REPORTS_DIR, config.FILINGS_DIR, config.POLICIES_DIR, config.INBOX_DIR)


def test_find_duplicate_checks_archived_but_not_inbox_by_default(tmp_path, monkeypatch):
    reports, _filings, _policies, inbox = _dirs(tmp_path, monkeypatch)
    base = reports / "base.pdf"
    base.write_bytes(b"same")
    inbox_dup = inbox / "dup.pdf"
    inbox_dup.write_bytes(b"same")

    assert classify.find_duplicate(inbox_dup) == base
    assert classify.find_duplicate(inbox_dup, include_inbox=True) == base

    inbox_only = inbox / "only.pdf"
    inbox_only.write_bytes(b"only")
    assert classify.find_duplicate(inbox_only) is None


def test_classify_one_deletes_inbox_duplicate_without_llm(tmp_path, monkeypatch):
    reports, _filings, _policies, inbox = _dirs(tmp_path, monkeypatch)
    base = reports / "base.pdf"
    base.write_bytes(b"same")
    dup = inbox / "dup.pdf"
    dup.write_bytes(b"same")
    monkeypatch.setattr(classify, "first_text",
                        lambda _p: (_ for _ in ()).throw(AssertionError("不应调用 LLM")))

    r = classify.classify_one(dup)
    assert r["duplicate"] is True
    assert r["target"] == base.relative_to(tmp_path).as_posix()
    assert not dup.exists()
    assert base.exists()


def test_dedupe_raw_removes_only_manifest_duplicates(tmp_path, monkeypatch):
    reports, _filings, _policies, _inbox = _dirs(tmp_path, monkeypatch)
    base = reports / "base.pdf"
    base.write_bytes(b"same")
    dup = reports / "base-2.pdf"
    dup.write_bytes(b"same")
    other = reports / "other.pdf"
    other.write_bytes(b"other")

    monkeypatch.setattr(ingest, "iter_raw_files", lambda: [base, dup, other])
    manifest = {"files": [{
        "path": base.relative_to(tmp_path).as_posix(),
        "file": base.name,
        "sha256": classify.sha256_of(base),
    }]}
    removed = ingest.dedupe_raw(manifest)
    assert [x[0] for x in removed] == [dup.relative_to(tmp_path).as_posix()]
    assert base.exists() and not dup.exists() and other.exists()


class _FakeResponse:
    def raise_for_status(self):
        pass

    def iter_bytes(self):
        yield b"%PDF-1.4 same"


class _FakeStream:
    def __enter__(self):
        return _FakeResponse()

    def __exit__(self, *args):
        return False


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, url):
        return _FakeStream()


def test_download_pdf_skips_when_archived_content_exists(tmp_path, monkeypatch):
    from src import fetcher

    reports, _filings, _policies, inbox = _dirs(tmp_path, monkeypatch)
    base = reports / "base.pdf"
    base.write_bytes(b"%PDF-1.4 same")
    monkeypatch.setattr(fetcher, "_client", lambda headers=None: _FakeClient())

    try:
        fetcher._download_pdf("http://example.test/a.pdf", "a")
    except fetcher.FetchError as e:
        assert "内容与已有文件重复" in str(e)
    else:
        raise AssertionError("重复下载应被拒绝")
    assert not [p for p in inbox.iterdir() if p.suffix == ".pdf"]


def test_make_snapshot_reuses_identical_snapshot(tmp_path, monkeypatch):
    from src import fetcher

    reports, _filings, _policies, _inbox = _dirs(tmp_path, monkeypatch)
    snapdir = tmp_path / "snapshots"
    snapdir.mkdir()
    monkeypatch.setattr(config, "SNAPSHOT_DIR", snapdir)

    kwargs = dict(org="测试机构", topic="测试主题", url="http://example.test",
                  rows=[{"指标": "销量", "数值": 1}])
    first = fetcher.make_snapshot(**kwargs)
    second = fetcher.make_snapshot(**kwargs)
    assert first == second
    assert len(list(snapdir.glob("*.docx"))) == 1


def test_ingest_one_deletes_duplicate_but_keeps_manifest_file(tmp_path, monkeypatch):
    reports, _filings, _policies, _inbox = _dirs(tmp_path, monkeypatch)
    base = reports / "base.pdf"
    base.write_bytes(b"same")
    dup = reports / "base-2.pdf"
    dup.write_bytes(b"same")
    manifest = {"files": [{
        "path": base.relative_to(tmp_path).as_posix(),
        "file": base.name,
        "sha256": classify.sha256_of(base),
    }]}

    assert ingest.ingest_one(base, manifest) is None
    assert base.exists()
    assert ingest.ingest_one(dup, manifest) is None
    assert not dup.exists()
