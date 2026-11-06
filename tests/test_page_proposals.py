"""任务四：待建页提案（摄入发现「该建而没建的页面」，用户勾选拍板后 mini 摄入建页）。

覆盖：计数汇总与阈值过滤、别名过滤（已有页面/别名不产生提案）、
去重（同名处理过不再出现）、defer/dismiss 语义、确认 → mini 摄入全链路
（新页 frontmatter 完整、句句带锚点、log.md 留痕、总索引收录、history 首行）、
缓存缺失时的失败路径（提示重新摄入，不静默降级）、摄入集成（entities → 提案文件）。

pytest 由 conftest.py 的 autouse 沙盒兜底（临时目录，不碰真实数据）。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _common import REPORT_NAME, build_sandbox, check, run_tests  # noqa: E402


def _stub_llm(compile_out, merge_out=None):
    """替身 llm.chat：compile 调用给 compile_out，merge 调用给 merge_out。"""
    import src.llm as llm_mod

    def fake_chat(messages, **kw):
        if kw.get("task") == "compile":
            return compile_out
        return merge_out or [{"title": "汽车", "action": "merge", "slug": "汽车", "type": "industry"}]

    llm_mod.chat = fake_chat


# ── 计数汇总、阈值过滤、别名过滤、去重 ───────────────────────────────────
def test_merge_detected_threshold_and_filters():
    print("page_proposals：阈值过滤、别名过滤、本报告已建主题过滤、同名去重")
    build_sandbox()
    from src import page_proposals, wiki

    # 沙盒已有页面：行业「汽车」、公司「测试公司」；给测试公司页补个别名验证别名过滤
    from src import config
    p = config.TYPE_DIRS["company"] / "测试公司.md"
    meta, body = wiki.parse_page(p)
    meta["aliases"] = ["999888"]
    wiki.write_page(p, meta, body)

    entities = {"新实体A": 4, "新实体B": 2, "999888": 5, "汽车": 6, "本报告已建": 9}
    types = {"新实体A": "company", "新实体B": "concept"}
    added = page_proposals.merge_detected(
        entities, types, REPORT_NAME, decided_titles=("本报告已建",))
    names = [x["name"] for x in page_proposals.load()["proposals"]]
    check("只有过阈值且未建页的进入队列", names, ["新实体A"])
    check("返回新增数", added, 1)
    pr = page_proposals.load()["proposals"][0]
    check("记录次数", pr["count"], 4)
    check("记录来源", pr["source_file"], REPORT_NAME)
    check("记录建议类型", pr["entity_type"], "company")
    # 同名再检出不再重复添加（任意状态都算已有）
    added2 = page_proposals.merge_detected({"新实体A": 7}, {}, REPORT_NAME)
    check("同名去重", added2, 0)


def test_threshold_from_config():
    print("page_proposals：阈值读 config.json 的 ingest.page_proposal_threshold，默认 3")
    build_sandbox()
    from src import config, page_proposals

    check("默认阈值 3（config.json 已配 3）", page_proposals.threshold(), 3)
    config.CFG.setdefault("ingest", {})["page_proposal_threshold"] = 5
    check("改成 5 生效", page_proposals.threshold(), 5)
    config.CFG["ingest"].pop("page_proposal_threshold")
    check("缺配置退回默认 3", page_proposals.threshold(), 3)


def test_defer_dismiss_semantics():
    print("page_proposals：稍后处理 24h 内不弹；不再提醒永久消失；刷新不丢")
    build_sandbox()
    from datetime import datetime, timedelta

    from src import config, page_proposals

    page_proposals.merge_detected({"实体甲": 4, "实体乙": 4, "实体丙": 4}, {}, REPORT_NAME)
    page_proposals.defer("实体甲")
    page_proposals.dismiss("实体乙")
    names = [x["name"] for x in page_proposals.pending_items()]
    check("只有未处理的在队列", names, ["实体丙"])
    # defer 的 24h 后恢复
    pr = next(x for x in page_proposals.load()["proposals"] if x["name"] == "实体甲")
    until = datetime.fromisoformat(pr["snoozed_until"])
    check("推迟约 24 小时", abs((until - datetime.now()).total_seconds() - 24 * 3600) < 60, True)
    data = page_proposals.load()
    next(x for x in data["proposals"] if x["name"] == "实体甲")["snoozed_until"] = (
        (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds"))
    page_proposals.save(data)
    names = [x["name"] for x in page_proposals.pending_items()]
    check("过期后恢复提醒", "实体甲" in names, True)
    check("dismissed 永不恢复", "实体乙" in names, False)
    # 刷新（重新读盘）不丢
    again = page_proposals.pending_items()
    check("重启读盘仍在", len(again) >= 1, True)


# ── 确认 → mini 摄入全链路 ───────────────────────────────────────────────
def test_confirm_builds_real_page():
    print("page_proposals：确认后 mini 摄入 → 新页句句带锚点、log 留痕、索引收录")
    build_sandbox()
    from src import config, page_proposals, wiki

    page_proposals.merge_detected({"达利电子": 3}, {"达利电子": "company"}, REPORT_NAME)
    page_proposals.mark_building("达利电子")
    # 定向编译的替身输出：条目锚点指向沙盒研报缓存里真实存在的页码（共 2 页）
    _stub_llm({
        "report_date": "2026-01-01",
        "entities": [], "entity_type": {},
        "topics": [{"title": "达利电子", "type": "company", "aliases": [], "summary": "电感代工小厂",
                    "sections": {"公司概况": [f"达利电子是全球电感代工前五 [[{REPORT_NAME}#p1]]",
                                             f"行业销量增速 12% [[{REPORT_NAME}#p2]]"]}}],
    })
    ret = page_proposals.build_page("达利电子", "company")
    check("建页返回条数", ret["entries"], 2)

    path = config.TYPE_DIRS["company"] / "达利电子.md"
    check("新页落在公司目录", path.exists(), True)
    meta, body = wiki.parse_page(path)
    check("frontmatter 完整（title）", meta.get("title"), "达利电子")
    check("frontmatter 完整（sources）", meta.get("sources"), [f"raw/reports/汽车/{REPORT_NAME}"])
    lines = [ln.strip() for ln in body.splitlines() if ln.strip().startswith("- ")]
    check("条目句句带锚点", all(wiki.ANCHOR_RE.search(ln) for ln in lines), True)
    check("history 首行记录建页提案确认", "建页提案确认，定向摄入" in (meta.get("history") or [""])[0], True)
    check("log.md 有 propose-page 记录", "propose-page" in config.LOG_MD.read_text(encoding="utf-8"), True)
    check("总索引收录", "达利电子" in config.INDEX_MD.read_text(encoding="utf-8"), True)
    check("resolve_target 能解析新页", wiki.resolve_target("达利电子"), ("company", "达利电子"))
    page_proposals.mark_done("达利电子")
    check("done 后不再出现在待办", [x["name"] for x in page_proposals.pending_items()], [])
    # 已建页的实体再检出时被 resolve_target 过滤，不产生新提案
    added = page_proposals.merge_detected({"达利电子": 9}, {}, REPORT_NAME)
    check("已建实体不再提案", added, 0)


def test_confirm_merge_into_existing():
    print("page_proposals：提案名命中已有页（别名）→ 并入而不是空壳新建")
    build_sandbox()
    from src import config, page_proposals, wiki

    # 测试公司页补别名，提案名用别名（确认时页面以标题命中）
    p = config.TYPE_DIRS["company"] / "测试公司.md"
    meta, body = wiki.parse_page(p)
    meta["aliases"] = ["达利电子"]
    wiki.write_page(p, meta, body)
    page_proposals.save({"proposals": [{"name": "达利电子", "count": 3,
                                        "source_file": REPORT_NAME, "entity_type": "company",
                                        "status": "pending", "first_seen": config.now_iso(),
                                        "snoozed_until": None, "dismissed": False}]})
    page_proposals.mark_building("达利电子")
    _stub_llm({
        "report_date": "", "entities": [], "entity_type": {},
        "topics": [{"title": "达利电子", "type": "company", "aliases": [], "summary": "",
                    "sections": {"经营与财务": [f"达利电子并入条目 [[{REPORT_NAME}#p1]]"]}}],
    })
    ret = page_proposals.build_page("达利电子", "company")
    check("走并入", ret["page"].startswith("并入"), True)
    meta2, body2 = wiki.parse_page(config.TYPE_DIRS["company"] / "测试公司.md")
    check("条目并入已有页", "达利电子并入条目" in body2, True)
    check("history 记录定向摄入", any("建页提案确认，定向摄入" in h for h in meta2.get("history") or []), True)


def test_cache_missing_fails_loudly():
    print("page_proposals：缓存缺失 → 任务失败并提示重新摄入，不静默降级")
    build_sandbox()
    from src import config, page_proposals

    page_proposals.save({"proposals": [{"name": "达利电子", "count": 3,
                                        "source_file": REPORT_NAME, "entity_type": "company",
                                        "status": "pending", "first_seen": config.now_iso(),
                                        "snoozed_until": None, "dismissed": False}]})
    page_proposals.mark_building("达利电子")
    (config.EXTRACTS_DIR / (Path(REPORT_NAME).stem + ".units.json")).unlink()
    try:
        page_proposals.build_page("达利电子", "company")
        raised = None
    except RuntimeError as e:
        raised = str(e)
    check("抛出失败", raised is not None and "重新摄入" in raised, True)
    # 失败回 pending（api 层职责）：附上原始错误原文供用户排查
    page_proposals.mark_pending("达利电子", "建页失败：请对该文件重新摄入后再确认提案")
    pr = next(x for x in page_proposals.load()["proposals"] if x["name"] == "达利电子")
    check("失败回 pending", pr["status"], "pending")
    check("附失败原因", "重新摄入" in (pr.get("last_error") or ""), True)


# ── 摄入集成：编译产出 entities → 摄入收尾生成提案 ───────────────────────
def test_ingest_integration_creates_proposals():
    print("ingest 集成：编译输出 entities → 摄入后提案文件出现；坏字段不影响摄入")
    build_sandbox()
    from mock_pdf import build_pdf

    from src import config, ingest, page_proposals

    # 阈值调低到 2 便于断言（entities 列表里出现两次即计 2）
    config.CFG.setdefault("ingest", {})["page_proposal_threshold"] = 2
    new_name = "新报告_20260202.pdf"
    (config.REPORTS_DIR / "汽车" / new_name).write_bytes(build_pdf([
        ["New Report about DaLi Electronics and passive components supply chain"],
        ["DaLi Electronics revenue grows fast, some filler text to pass scan detection"],
    ]))
    _stub_llm({
        "report_date": "2026-01-01",
        "entities": ["达利电子", "达利电子", "只提一次"],  # 达利电子计 2 ≥ 阈值；只提一次被过滤
        "entity_type": {"达利电子": "company", "只提一次": "不是合法类型"},
        "topics": [{"title": "汽车", "type": "industry", "aliases": [], "summary": "s",
                    "sections": {"行业概况": [f"- 新知识一条 [[{new_name}#p2]]"]}}],
    })
    manifest = ingest.load_manifest()
    r = ingest.ingest_one(config.REPORTS_DIR / "汽车" / new_name, manifest)
    check("摄入照常完成", r.get("entries_count") is not None, True)
    names = [x["name"] for x in page_proposals.load()["proposals"]]
    check("过阈值实体进入提案", "达利电子" in names, True)
    check("不过滤阈值不进", "只提一次" in names, False)
    check("已有页面（汽车）不提案", "汽车" in names, False)
    pr = next(x for x in page_proposals.load()["proposals"] if x["name"] == "达利电子")
    check("提案记录来源文件", pr["source_file"], new_name)
    check("提案文件位置", (config.DATA_DIR / "page_proposals.json").exists(), True)


# ── API：pending 只读 + defer/dismiss 语义 ───────────────────────────────
def test_api_page_proposals():
    print("api：pending 只读放行、defer/dismiss 要同源头、处理后不再出现")
    build_sandbox()
    from _common import api_client
    from src import page_proposals
    from src.web.app import app

    app.config.update(TESTING=True)
    c = api_client(app)
    raw = app.test_client()  # 不带同源头，验证 CSRF 守卫
    page_proposals.merge_detected({"实体甲": 4}, {}, REPORT_NAME)
    check("pending 无需 CSRF 头", raw.get("/api/page-proposals/pending").status_code, 200)
    check("队列里有提案", len(c.get("/api/page-proposals/pending").get_json()["items"]), 1)
    check("defer 缺头被拒", raw.post("/api/page-proposals/defer", json={"name": "实体甲"}).status_code, 403)
    check("dismiss 缺头被拒", raw.post("/api/page-proposals/dismiss", json={"name": "实体甲"}).status_code, 403)
    check("defer 带头放行", c.post("/api/page-proposals/defer", json={"name": "实体甲"}).status_code, 200)
    check("defer 后不在队列", c.get("/api/page-proposals/pending").get_json()["items"], [])
    page_proposals.mark_pending("实体甲")  # 手动取消推迟（只改 snooze 之外的字段无效，这里直接改盘）
    data = page_proposals.load()
    next(x for x in data["proposals"] if x["name"] == "实体甲")["snoozed_until"] = None
    page_proposals.save(data)
    check("confirm 空勾选 400", c.post("/api/page-proposals/confirm", json={"items": []}).status_code, 400)


if __name__ == "__main__":
    run_tests(globals(), [
        "test_merge_detected_threshold_and_filters",
        "test_threshold_from_config",
        "test_defer_dismiss_semantics",
        "test_confirm_builds_real_page",
        "test_confirm_merge_into_existing",
        "test_cache_missing_fails_loudly",
        "test_ingest_integration_creates_proposals",
        "test_api_page_proposals",
    ])
