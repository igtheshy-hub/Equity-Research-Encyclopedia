from src import fetcher, tasks


def test_keyed_locks_are_independent_and_stable():
    """同一份研报的阶段共用一把锁，不同研报各用各的锁。"""
    a1 = tasks._lock_for("report", "job-a")
    a2 = tasks._lock_for("report", "job-a")
    b = tasks._lock_for("report", "job-b")
    assert a1 is a2
    assert a1 is not b


def test_no_key_falls_back_to_kind_lock():
    assert tasks._lock_for("report") is tasks._lock_for("report", None)
    assert tasks._lock_for("report") is tasks._KIND_LOCKS["report"]


def test_manual_source_whitelist_has_only_eight_sources():
    ids = [s["id"] for s in fetcher.source_specs(only=fetcher.MANUAL_SOURCE_IDS)]
    assert ids == list(fetcher.MANUAL_SOURCE_IDS)
    assert "web_search" not in ids
    assert "annual_report" not in ids
