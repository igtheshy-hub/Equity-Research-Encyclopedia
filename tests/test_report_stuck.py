"""成文卡点检测与记录：破折号误报、卡点状态回写。"""
from src import industry_report as ir


def test_prose_dash_is_not_placeholder():
    """正文里的破折号是正常行文，不能判成占位符。"""
    text = "底盘降幅最小，与工程车抗跌相印证——9月工程车销量0.85万辆。"
    assert ir._placeholder_hits(text) == []


def test_standalone_dash_is_placeholder():
    assert ir._placeholder_hits("| 车企 | 毛利率 |\n| 比亚迪 | —— |") == ["——"]
    assert ir._placeholder_hits("——") == ["——"]


def test_placeholder_words_still_detected():
    assert "数据缺失" in ir._placeholder_hits("本节数据缺失，暂不展开。")
    assert "待补" in ir._placeholder_hits("渠道库存待补。")


def test_unit_has_num_ignores_similar_values():
    """提示只认完整数字，22.9 不能命中 22.98/22.90，否则会把救火模型引错页。"""
    assert not ir._unit_has_num("CS有色金属 22.98 85.98 18.39", "22.9")
    assert not ir._unit_has_num("7—9月产量分别为222.90/234.85/259.04万辆", "22.9")
    assert ir._unit_has_num("SW商用载货车PE 28.2x，处于22.9%分位", "22.9")


def test_mark_stuck_resolved_flips_only_open_entry():
    job = {"stuck": [
        {"where": "第 2 节《生产端》", "outcome": "未解决", "action": "救火代理重写"},
        {"where": "第 6 节《盈利》", "outcome": "未解决", "action": ""},
    ]}
    assert ir._mark_stuck_resolved(job, "第 2 节《生产端》", "续跑后自检通过")
    entry = job["stuck"][0]
    assert entry["outcome"] == "已修复"
    assert "续跑后自检通过" in entry["action"]
    assert entry["fixed_at"]
    assert job["stuck"][1]["outcome"] == "未解决"
    assert not ir._mark_stuck_resolved(job, "第 2 节《生产端》", "重复")
