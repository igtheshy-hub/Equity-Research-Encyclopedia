"""src/ai_review.py（AI 初审）的测试。

核心约束逐条验证：
- 不删减：筛查出的每一条都必须出现在初审报告里，一条不能少
- 排序：高 → 中 → 低；同一严重性里矛盾类排最前
- AI 挂了不能连累报告：判断失败的条目标「无法判断/中」，报告照常生成
- data/audit-ai-*.json 不能被 /api/audit/latest 误认成「最新审校」
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _common import api_client, check, run_tests  # noqa: E402

TS = "20260101-000000"


def _make_base_audit(ts=TS):
    """造一份「审校脚本已跑完」的结构化结果（data/audit-<ts>.json），条条有代表。"""
    from src import config

    data = {
        "ts": ts,
        "report": f"audit-{ts}.md",
        "counts": {"矛盾": 3, "时点差异": 1, "过期": 2, "未摄入文件": 1, "内容缺口页": 1,
                   "孤立": 1, "格式损坏": 1, "需复核": 1, "锚点存疑": 1},
        "conflicts": [
            {"kind": "矛盾", "page": "汽车", "slug": "汽车", "note": "仓位说法不一",
             "quote_a": "公司产能已满仓", "anchor_a": "[[研报A.pdf#p1]]",
             "quote_b": "公司产能已基本满仓", "anchor_b": "[[研报A.pdf#p2]]"},
            {"kind": "矛盾", "page": "汽车", "slug": "汽车", "note": "毛利率对不上",
             "quote_a": "主业毛利率 20%", "anchor_a": "[[研报A.pdf#p3]]",
             "quote_b": "毛利率 25%", "anchor_b": "[[研报B.pdf#p1]]"},
            {"kind": "矛盾", "page": "测试公司", "slug": "测试公司", "note": "份额打架",
             "quote_a": "市场份额 45%", "anchor_a": "[[研报A.pdf#p1]]",
             "quote_b": "市场份额 30%", "anchor_b": "[[研报B.pdf#p2]]"},
        ],
        "time_diffs": [
            {"kind": "时点差异", "page": "汽车", "slug": "汽车", "note": "不同月份的销量预测",
             "quote_a": "2026 年销量 3000 万台", "anchor_a": "[[研报A.pdf#p4]]",
             "quote_b": "2026 年销量 3100 万台", "anchor_b": "[[研报B.pdf#p3]]"},
        ],
        "stale": [
            {"slug": "汽车", "title": "汽车"},
            {"slug": "电池", "title": "电池", "note": "观点依据期早于最新数据期，待复核：……"},
        ],
        "needs_review_pages": [{"slug": "旧页", "title": "旧页", "reason": "原文件已删除，条目失去锚点"}],
        "missing_files": ["raw/inbox/新报告_20260101.pdf"],
        "gaps_by_page": [{"page": "汽车", "gaps": [{"point": "市占率预测", "why": "投研常用"}]}],
        "orphans": ["某概念"],
        "bad_pages": [{"path": "wiki/companies/坏页.md", "error": "frontmatter 缺 ---"}],
        "merge_suggestions": [{"kind": "pair",
                               "a": {"slug": "汽车行业", "title": "汽车行业"},
                               "b": {"slug": "汽车", "title": "汽车"},
                               "note": "标题互为包含，疑似应合并"}],
        "anchor_doubts": [{"file": "研报A.pdf", "loc": "p1", "reason": "数字在原文页找不到",
                           "sentence": "净利润 8 亿元 [[研报A.pdf#p1]]",
                           "number": 70.0, "question": "头部公司有哪些"}],
    }
    (config.DATA_DIR / f"audit-{ts}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def _stub_judge(verdicts):
    """桩掉 llm.chat：按**全局**条目序号（跨批累计）查 verdicts；记录每次调用，方便断言分批。"""
    import src.ai_review as ar

    calls = []
    counter = [0]

    def fake(messages, *, task, response_json=False, model=None, json_any=False):
        calls.append(messages[0]["content"])
        nums = [int(m.group(1)) for m in re.finditer(r"(?m)^(\d+)\. ", messages[0]["content"])]
        items = []
        for k, _n in enumerate(nums, 1):
            counter[0] += 1                      # 全局序号，跨批累计，用于查 verdicts
            v = verdicts.get(counter[0])
            if v is None:
                continue
            items.append({"i": k, "verdict": v[0], "sev": v[1], "why": v[2]})  # i 用批内序号
        return {"items": items}

    ar.llm.chat = fake
    return calls


# ── 主流程 ──────────────────────────────────────────────────────────────────
def test_ai_review_keeps_all_and_sorts():
    from _common import build_sandbox
    from src import ai_review, config

    print("ai_review：全部问题保留 + 按严重性排序")
    build_sandbox()
    _make_base_audit()
    calls = _stub_judge({
        1: ("表述严谨问题", "低", "程度词差异，实质一致"),
        2: ("口径不同", "中", "主业毛利率与毛利率统计范围不同"),
        3: ("实质矛盾", "高", "同口径份额数值不同"),
    })
    r = ai_review.run(ts=TS)
    check("调用 LLM 1 批", len(calls), 1)
    check("总数 13 条", r["total"], 13)
    check("高 3", r["sev_counts"]["高"], 3)
    check("中 5", r["sev_counts"]["中"], 5)
    check("低 5", r["sev_counts"]["低"], 5)
    md = (config.OUTPUTS_DIR / f"audit-ai-{TS}.md").read_text(encoding="utf-8")
    # 一条不删：矛盾双方原文、时点差异、缺口、孤立……全部要在报告里
    for frag in ("已基本满仓", "主业毛利率 20%", "市场份额 30%", "3100 万台",
                 "市占率预测", "某概念", "坏页.md", "新报告_20260101.pdf", "净利润 8 亿元"):
        check(f"报告保留：{frag}", frag in md, True)
    # 锚点存疑要「说人话」：哪个数字、哪次问答、标注了哪页、剥掉句内锚点
    check("存疑行说出数字与问答来源",
          "问答「头部公司有哪些」里的数字 70.0" in md, True)
    check("存疑行说清标注页码", "研报A.pdf 第1页" in md, True)
    check("存疑行剥掉句内锚点", "[[研报A.pdf#p1]]净利润" not in md, True)
    check("高/中/低 章节顺序", md.index("## 高") < md.index("## 中") < md.index("## 低"), True)
    check("高严重性里矛盾排第一", md.index("实质矛盾") < md.index("锚点存疑"), True)
    data = json.loads((config.DATA_DIR / f"audit-ai-{TS}.json").read_text(encoding="utf-8"))
    check("JSON 首条是高严重性矛盾", (data["items"][0]["sev"], data["items"][0]["category"]),
          ("高", "矛盾"))
    check("JSON 记录模型", bool(data.get("model")), True)


def test_ai_review_survives_llm_failure():
    from _common import build_sandbox
    from src import ai_review, config, llm

    print("ai_review：LLM 全挂时报告照常生成")
    build_sandbox()
    _make_base_audit()

    def boom(messages, *, task, response_json=False, model=None, json_any=False):
        raise llm.LLMError("缺少 API Key")

    ai_review.llm.chat = boom
    r = ai_review.run(ts=TS)
    check("3 条矛盾全部标未判", r["unjudged"], 3)
    md = (config.OUTPUTS_DIR / f"audit-ai-{TS}.md").read_text(encoding="utf-8")
    check("报告仍在", (config.OUTPUTS_DIR / f"audit-ai-{TS}.md").is_file(), True)
    check("有未判警示", "AI 未能判断" in md, True)
    # 只数矛盾小标题里的「无法判断」（警示句里还有一处该词，不能直接 count 全文）
    check("矛盾落「无法判断」", len(re.findall(r"(?m)^### \d+\. \[[^\]]*\] 无法判断", md)), 3)
    # 高=锚点存疑+格式损坏=2；中=3 矛盾(未判)+需复核+合并+未摄入+带 note 过期=7；低=4
    check("高 2", r["sev_counts"]["高"], 2)
    check("中 7", r["sev_counts"]["中"], 7)
    check("低 4", r["sev_counts"]["低"], 4)


def test_ai_review_partial_reply_defaults():
    from _common import build_sandbox
    from src import ai_review, config

    print("ai_review：AI 漏答的条目落「无法判断」，不丢条目")
    build_sandbox()
    _make_base_audit()
    _stub_judge({2: ("口径不同", "中", "口径不同")})   # 只答第 2 条
    r = ai_review.run(ts=TS)
    check("未判 2 条", r["unjudged"], 2)
    data = json.loads((config.DATA_DIR / f"audit-ai-{TS}.json").read_text(encoding="utf-8"))
    unjudged = [x for x in data["items"] if x["category"] == "矛盾" and x.get("verdict") == "无法判断"]
    check("缺答的两条被兜住", len(unjudged), 2)
    check("兜底严重性为中", all(x["sev"] == "中" for x in unjudged), True)


def test_ai_review_batches_large_lists():
    from _common import build_sandbox
    from src import ai_review, config

    print("ai_review：45 条矛盾分 3 批判")
    build_sandbox()
    data = {"ts": TS, "report": f"audit-{TS}.md", "counts": {"矛盾": 45},
            "conflicts": [{"kind": "矛盾", "page": "汽车", "slug": "汽车", "note": "n",
                           "quote_a": f"说法A{i}", "anchor_a": "[[研报A.pdf#p1]]",
                           "quote_b": f"说法B{i}", "anchor_b": "[[研报B.pdf#p2]]"}
                          for i in range(45)]}
    (config.DATA_DIR / f"audit-{TS}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    calls = _stub_judge({1: ("实质矛盾", "高", "真矛盾")})
    r = ai_review.run(ts=TS)
    check("分 3 批", len(calls), 3)
    check("全部条目都在", r["total"], 45)
    check("高 1 中 44", (r["sev_counts"]["高"], r["sev_counts"]["中"]), (1, 44))


def test_ai_review_requires_base():
    from _common import build_sandbox
    from src import ai_review

    print("ai_review：没有审校结果时报错提示")
    build_sandbox()
    try:
        ai_review.run(ts="20990101-000000")
        check("应抛 FileNotFoundError", False, True)
    except FileNotFoundError as e:
        check("报错带指引", "请先执行审校" in str(e), True)


# ── API 层 ──────────────────────────────────────────────────────────────────
def test_latest_endpoint_ignores_ai_files():
    from _common import build_sandbox
    from src import config
    from src.web.app import app

    print("api：latest 不把 AI 初审产物当「最新审校」")
    build_sandbox()
    _make_base_audit()
    (config.DATA_DIR / f"audit-ai-{TS}.json").write_text(
        json.dumps({"counts": {"高": 3, "中": 5, "低": 5},
                    "items": [{"category": "矛盾", "sev": "高", "why": "w"}]}, ensure_ascii=False),
        encoding="utf-8")
    (config.OUTPUTS_DIR / f"audit-ai-{TS}.md").write_text("# AI 初审报告\n", encoding="utf-8")
    c = api_client(app)
    d = c.get("/api/audit/latest").get_json()
    check("最新仍是审校结果", d.get("ts"), TS)
    check("带 AI 报告入口", d.get("ai_report"), f"audit-ai-{TS}.md")
    check("带 AI 计数", d.get("ai_counts"), {"高": 3, "中": 5, "低": 5})
    # 修复向导按 AI 初判生成选项的依据：items 原样带回前端
    check("带 AI 条目清单", d.get("ai_items"), [{"category": "矛盾", "sev": "高", "why": "w"}])


def test_reports_endpoint_lists_and_orders():
    from _common import build_sandbox
    from src import config
    from src.web.app import app

    print("api：报告清单含审校与 AI 初审，同 ts 成对相邻、时间倒序")
    build_sandbox()
    for ts in ("20260101-000000", "20260102-000000"):
        (config.OUTPUTS_DIR / f"audit-{ts}.md").write_text("# a\n", encoding="utf-8")
        (config.DATA_DIR / f"audit-{ts}.json").write_text(
            json.dumps({"counts": {"矛盾": 2}}, ensure_ascii=False), encoding="utf-8")
    (config.OUTPUTS_DIR / "audit-ai-20260102-000000.md").write_text("# ai\n", encoding="utf-8")
    (config.DATA_DIR / "audit-ai-20260102-000000.json").write_text(
        json.dumps({"sev_counts": {"高": 1, "中": 2, "低": 3}}, ensure_ascii=False), encoding="utf-8")
    c = api_client(app)
    d = c.get("/api/audit/reports").get_json()
    items = d["items"]
    check("3 份报告", len(items), 3)
    check("排序：时间倒序、AI 初审紧随其审校",
          [(x["file"], x["kind"]) for x in items],
          [("audit-20260102-000000.md", "audit"),
           ("audit-ai-20260102-000000.md", "ai_review"),
           ("audit-20260101-000000.md", "audit")])
    check("审校版带类别计数", items[0]["counts"], {"矛盾": 2})
    check("AI 版带严重性计数", items[1]["counts"], {"高": 1, "中": 2, "低": 3})
    check("时间格式化", items[-1]["ts_pretty"], "2026-01-01 00:00")


def test_ai_review_endpoint_guards():
    from src.web.app import app

    print("api：AI 初审接口的 CSRF 与参数校验")
    c = api_client(app, csrf=False)
    r = c.post("/api/audit/ai-review", json={"ts": TS})
    check("缺同源头被拒", r.status_code, 403)
    c = api_client(app)
    r = c.post("/api/audit/ai-review", json={"ts": "../evil"}, headers={"X-Requested-With": "wiki"})
    check("非法时间戳被拒", r.status_code, 400)


if __name__ == "__main__":
    run_tests(globals(), [
        "test_ai_review_keeps_all_and_sorts",
        "test_ai_review_survives_llm_failure",
        "test_ai_review_partial_reply_defaults",
        "test_ai_review_batches_large_lists",
        "test_ai_review_requires_base",
        "test_latest_endpoint_ignores_ai_files",
        "test_reports_endpoint_lists_and_orders",
        "test_ai_review_endpoint_guards",
    ])
