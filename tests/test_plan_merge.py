from src import ingest


def test_same_title_merges_even_when_type_differs():
    """同一标题被 LLM 判成不同类型时仍并入原页，不能再建 xxx-2 平行页。"""
    topics = [{"title": "乘用车渠道库存", "type": "concept", "aliases": []}]
    index = [{"title": "乘用车渠道库存", "aliases": [], "type": "industry",
              "slug": "乘用车渠道库存"}]
    decisions = ingest.plan_merge(topics, index)
    assert len(decisions) == 1
    decision, _ = decisions[0]
    assert decision["action"] == "merge"
    assert decision["slug"] == "乘用车渠道库存"
    assert decision["type"] == "industry"


def test_alias_match_merges_to_existing_page():
    topics = [{"title": "渠道库存水平", "type": "industry", "aliases": ["渠道库存"]}]
    index = [{"title": "乘用车渠道库存", "aliases": ["渠道库存"], "type": "concept",
              "slug": "乘用车渠道库存"}]
    decision, _ = ingest.plan_merge(topics, index)[0]
    assert (decision["action"], decision["slug"]) == ("merge", "乘用车渠道库存")
