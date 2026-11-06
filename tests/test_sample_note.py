from src import industry_report


def test_sample_broker_falls_back_to_filename():
    """补位样板没有 broker 字段时，从文件名认出券商，不能一律算非前 3。"""
    brokers = ["东吴证券", "长江证券", "民生证券"]
    assert industry_report._sample_broker({"file": "东吴证券_汽车行业研报_20261102.pdf"}, brokers) == "东吴证券"
    assert industry_report._sample_broker({"file": "某券商_汽车月报.pdf"}, brokers) == ""


def test_sample_top3_note_counts_filename_broker():
    job = {
        "top3": {"group": "（Q）汽车和汽车零部件",
                 "brokers": ["东吴证券", "长江证券", "民生证券"]},
        "samples": [
            {"file": "东吴证券_汽车行业研报_20261102.pdf", "broker": ""},
            {"file": "某券商_汽车月报.pdf"},
        ],
    }
    note = industry_report._sample_top3_note(job)
    assert "2 份中仅 1 份" in note
    assert "东吴证券" in note


def test_sample_top3_note_silent_when_three_hits():
    job = {
        "top3": {"group": "汽车", "brokers": ["东吴证券", "长江证券", "民生证券"]},
        "samples": [{"broker": "东吴证券"}, {"broker": "长江证券"}, {"broker": "民生证券"}],
    }
    assert industry_report._sample_top3_note(job) == ""
