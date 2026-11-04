"""行业研报状态机自测：job 落盘、大纲确认、缺口勾选、续跑不动待确认关卡。

用法：python tests/test_industry_report.py（LLM 与 _begin_write 打桩，不起后台任务）
"""
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import config, industry_report  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def make_job(jid, **kw):
    job = {
        "id": jid, "question": "写一份乘用车行业研报", "title": "乘用车行业研报",
        "domain": "汽车", "kind": "industry", "model": None, "status": "outline_pending",
        "outline": "# 大纲：乘用车\n\n## 一、投资要点\n- 论点", "outline_path": "",
        "samples": [], "gaps": [], "pages": [], "report_path": "", "docx_path": "",
        "charts": [], "review": {}, "candidates_path": "", "candidates_count": 0,
        "error": "", "phase": "", "log": [], "created_at": config.now_iso(),
        "updated_at": config.now_iso(),
    }
    job.update(kw)
    return job


def main():
    tmp = Path(tempfile.mkdtemp(prefix="report-job-test-"))
    config.REPORT_JOBS_DIR = tmp

    print("== job 读写 ==")
    industry_report.save_job(make_job("aaa111"))
    loaded = industry_report.load_job("aaa111")
    check("读回标题", loaded["title"], "乘用车行业研报")
    check("updated_at 已写", bool(loaded["updated_at"]), True)
    check("不存在返回 None", industry_report.load_job("nope"), None)
    listed = industry_report.list_jobs()
    check("列表含一条", len(listed), 1)
    check("列表带状态中文", listed[0]["status_label"], "待确认大纲")

    print("== 报告类型 ==")
    industry_report.save_job(make_job("kkk888", kind="company", title="吉利汽车研究报告"))
    cj = industry_report.load_job("kkk888")
    check("公司研报存 kind", cj.get("kind"), "company")
    check("kind 标签", industry_report.kind_label(cj), "公司研报")
    check("缺省 kind 落行业", industry_report.kind_label(make_job("kkk999")), "行业研报")
    listed = [x for x in industry_report.list_jobs() if x["id"] == "kkk888"]
    check("列表带类型", (listed[0]["kind"], listed[0]["kind_label"]), ("company", "公司研报"))

    print("== 公司研报定期报告缺口 ==")
    original_periodic = industry_report._periodic_in_library
    original_stock = industry_report._stock_code
    industry_report._stock_code = lambda job: "0175.HK"
    try:
        company_job = make_job("ann001", kind="company", title="吉利汽车研究报告",
                               question="帮我写一份吉利汽车的研究报告")
        check("公司研报主体", industry_report._subject(company_job), "吉利汽车")
        check("报告期解析三季报",
              industry_report._period_from_text("公司_2026年三季报_20261030.pdf"),
              ("quarterly", "2026Q3"))
        check("报告期解析半年报",
              industry_report._period_from_text("公司_2026年半年度报告_20260830.pdf"),
              ("interim", "2026H1"))
        check("报告期解析年报标记", industry_report._period_from_text("2025FY"),
              ("annual", "2025FY"))
        check("行业研报不列定期报告缺口", industry_report._periodic_gaps(make_job("ind002")), [])
        industry_report._periodic_in_library = lambda job: {}
        gaps = industry_report._periodic_gaps(company_job)
        check("三类报告都缺", [g["id"] for g in gaps], ["annual", "interim", "quarterly"])
        check("年报缺口必选", gaps[0]["required"], True)
        check("港股季报不阻断", gaps[2]["required"], False)
        check("年报取数走港股", gaps[0]["fetch"]["params"]["market"], "hk")
        industry_report._periodic_in_library = lambda job: {
            "annual": {"file": "a.pdf"}, "interim": {"file": "b.pdf"},
            "quarterly": {"file": "c.pdf"}}
        check("覆盖三类后不列缺口", industry_report._periodic_gaps(company_job), [])
    finally:
        industry_report._periodic_in_library = original_periodic
        industry_report._stock_code = original_stock

    print("== 行业研报大纲顺序 ==")
    prompt = industry_report.OUTLINE_PROMPT
    order = ["产业链", "生产端", "需求端", "市场规模", "竞争格局", "增长驱动", "投资要点"]
    pos = [prompt.find(x) for x in order]
    check("六节行业分析在前、投资要点在后", -1 not in pos and pos == sorted(pos), True)
    check("产业链不得被投资要点顶掉", prompt.find("产业链") < prompt.find("投资要点"), True)

    print("== 大纲解析 ==")
    sections = industry_report._parse_outline("# 大纲\n\n## 一、要点\n- a\n\n## 二、格局\n- b\n")
    check("两节", [s["heading"] for s in sections], ["一、要点", "二、格局"])
    check("节内正文", sections[0]["body"], "- a")
    parsed = industry_report._parse_report_sections("## 一、要点\ntext\n\n## 二、格局\nmore\n")
    check("报告节解析", list(parsed.keys()), ["一、要点", "二、格局"])

    print("== 大纲确认 ==")
    started = []
    original_begin = industry_report._begin_write
    original_gaps = industry_report._extract_gaps
    original_pending = industry_report._pending_gap_items
    industry_report._begin_write = lambda job: started.append(job["id"])
    industry_report._pending_gap_items = lambda: []
    try:
        industry_report.save_job(make_job("bbb222", gaps=[]))
        job = industry_report.approve_outline("bbb222")
        check("无缺口直接成文", started, ["bbb222"])
        check("无缺口状态", job["status"] in ("writing", "outline_pending"), True)

        industry_report._extract_gaps = lambda job, outline, titles: [
            {"id": "g1", "item": "月度销量", "why": "规模", "fetch": {"source": "stats", "params": {}}, "status": "pending"}]
        industry_report.save_job(make_job("ccc333", gaps=[]))
        job = industry_report.approve_outline(
            "ccc333", outline_md="# 大纲：乘用车\n\n## 一、投资要点（改）\n- 新论点\n")
        check("编辑版覆盖大纲", "（改）" in job["outline"], True)
        check("有缺口停待勾选", job["status"], "gaps_pending")
        check("缺口落 job", len(job["gaps"]), 1)

        saved = [{"id": "g9", "item": "原有缺口", "why": "", "fetch": None, "status": "pending"}]
        industry_report.save_job(make_job("ddd999", gaps=saved))

        def boom(job, outline, titles):
            raise industry_report.llm.LLMError("模型调用失败")

        industry_report._extract_gaps = boom
        job = industry_report.approve_outline(
            "ddd999", outline_md="# 大纲：乘用车\n\n## 一、投资要点（改）\n- 新论点\n")
        check("列缺口失败不绕过关卡", job["status"], "gaps_pending")
        check("列缺口失败沿用原缺口", [g["id"] for g in job["gaps"]], ["g9"])
        check("列缺口失败不成文", started, ["bbb222"])
    finally:
        industry_report._begin_write = original_begin
        industry_report._extract_gaps = original_gaps
        industry_report._pending_gap_items = original_pending

    print("== 缺口取数源映射 ==")
    original_chat = industry_report.llm.chat
    try:
        industry_report.llm.chat = lambda *a, **k: {"gaps": [
            {"item": "月度销量", "why": "规模", "source": "stats", "params": {"keyword": "汽车产量"}},
            {"item": "宏观序列", "why": "宏观", "source": "akshare_series", "params": {"series": "macro_china_gdp"}},
            {"item": "竞品研报", "why": "格局", "source": "eastmoney", "params": {"industry": "汽车"}},
            {"item": "公司年报", "why": "公司", "source": "cninfo", "params": {"query": "比亚迪"}},
            {"item": "没有源的数据", "why": "无", "source": "none", "params": {}},
        ]}
        gaps = industry_report._extract_gaps(make_job("ggg777"), "# 大纲", ["乘用车"])
        check("别名映射到真实源",
              [g["fetch"]["source"] if g["fetch"] else None for g in gaps],
              ["stats_series", "akshare_series", "eastmoney_reports", "cninfo_announce", "web_search"])
        check("缺口编号连续", [g["id"] for g in gaps], ["g1", "g2", "g3", "g4", "g5"])
    finally:
        industry_report.llm.chat = original_chat

    print("== 缺口勾选 ==")
    started.clear()
    industry_report._begin_write = lambda job: started.append(job["id"])
    try:
        gaps = [
            {"id": "g1", "item": "月度销量", "why": "", "fetch": {"source": "stats", "params": {}}, "status": "pending"},
            {"id": "g2", "item": "没有源的数据", "why": "", "fetch": None, "status": "pending"},
        ]
        industry_report.save_job(make_job("ddd444", status="gaps_pending", gaps=gaps))
        ret = industry_report.approve_gaps("ddd444", [])
        check("全部跳过不起取数", ret, {"started": "write", "fetchable": 0})
        check("跳过后开始成文", started, ["ddd444"])
        job = industry_report.load_job("ddd444")
        check("缺口状态已标记", [g["status"] for g in job["gaps"]], ["skipped", "skipped"])

        required = [{"id": "annual", "item": "吉利汽车最近一期年度报告（财报）", "why": "", "required": True,
                     "fetch": {"source": "annual_report", "params": {"company": "吉利汽车"}}, "status": "pending"}]
        industry_report.save_job(make_job("ddd555", kind="company", title="吉利汽车研究报告",
                                          status="gaps_pending", gaps=required))
        err = ""
        try:
            industry_report.approve_gaps("ddd555", [])
        except ValueError as e:
            err = str(e)
        check("定期报告缺口不能跳过", "定期报告" in err, True)
        check("跳过被拒不成文", started, ["ddd444"])
    finally:
        industry_report._begin_write = original_begin

    print("== 补数未入库停回关卡 ==")
    original_fetch = industry_report.fetcher.run_fetch
    original_start = industry_report.tasks.start
    industry_report.tasks.start = lambda kind, fn, **kw: fn("t1")
    try:
        industry_report._begin_write = lambda job: started.append(job["id"])
        gaps = [{"id": "g1", "item": "竞品毛利率", "why": "",
                 "fetch": {"source": "eastmoney_reports", "params": {}}, "status": "pending"}]
        industry_report.save_job(make_job("hhh888", status="gaps_pending", gaps=gaps))
        industry_report.fetcher.run_fetch = lambda source, params, tid=None, targets=None: {
            "summary": "下载 a.pdf", "ingest_failed": ["a.pdf"]}
        started.clear()
        industry_report.approve_gaps("hhh888", ["g1"])
        job = industry_report.load_job("hhh888")
        check("入库失败缺口置 failed", [g["status"] for g in job["gaps"]], ["failed"])
        check("入库失败不成文", started, [])
        check("入库失败停回关卡", job["status"], "gaps_pending")
        check("错误说明材料没入库", "没入库" in job.get("error", ""), True)

        seen = {}

        def ok_fetch(source, params, tid=None, targets=None):
            seen["targets"] = targets
            return {"summary": "入库 1 份", "entries_count": 1, "files": ["a.pdf"]}

        industry_report.fetcher.run_fetch = ok_fetch
        industry_report.save_job(make_job("iii999", status="gaps_pending", gaps=gaps))
        started.clear()
        industry_report.approve_gaps("iii999", ["g1"])
        job = industry_report.load_job("iii999")
        check("入库成功缺口置 done", [g["status"] for g in job["gaps"]], ["done"])
        check("入库成功继续成文", started, ["iii999"])
        check("数据缺口带进取数", seen.get("targets"), [{"item": "竞品毛利率", "why": ""}])
    finally:
        industry_report.fetcher.run_fetch = original_fetch
        industry_report.tasks.start = original_start
        industry_report._begin_write = original_begin

    print("== 审校辅助 ==")
    check("长报告分段", len(industry_report._report_chunks("a" * 1000 + "\n" + "b" * 1000, 1500)), 2)
    check("亿元对千元命中", industry_report._num_on_page(3452.32, [345232201.0]), True)
    check("无关小数不命中", industry_report._num_on_page(16.61, [1661.0]), False)
    check("中文数字归一", industry_report._normalize_cn_numerals("二零二五年四月十日"), "2025年4月10日")
    check("前导零归一", industry_report._has_num("00175", "00175", "0175"), True)
    fake = {"unit": "page", "units": [
        {"n": 124, "text": "流動負債與非流動負債"},
        {"n": 126, "text": "銀行借款 10,496,181 千元"},
    ]}
    check("挂错页提示", "第 126 页" in industry_report._other_unit_hint(fake, ["104.96"]), True)

    print("== 删除研报 ==")
    saved_root, saved_jobs = config.ROOT, config.REPORT_JOBS_DIR
    saved_data = config.DATA_DIR
    try:
        delroot = tmp / "delroot"
        (delroot / "outputs" / "charts").mkdir(parents=True)
        (delroot / "data" / "report_jobs").mkdir(parents=True)
        config.ROOT = delroot
        config.DATA_DIR = delroot / "data"
        config.REPORT_JOBS_DIR = delroot / "data" / "report_jobs"
        for rel in ("outputs/大纲.md", "outputs/报告.md", "outputs/报告.docx",
                    "data/report_jobs/ddd444.candidates.json", "outputs/charts/图1-a.png",
                    "data/_display-报告.md"):
            p = delroot / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
        industry_report.save_job(make_job(
            "ddd444", status="done", outline_path="outputs/大纲.md",
            report_path="outputs/报告.md", docx_path="outputs/报告.docx",
            candidates_path="data/report_jobs/ddd444.candidates.json",
            charts=[{"path": "outputs/charts/图1-a.png"}]))
        r = industry_report.delete_job("ddd444")
        check("删除产物与任务记录", len(r["removed"]), 7)
        check("任务记录已不在", industry_report.load_job("ddd444"), None)
        check("报告文件已删", (delroot / "outputs" / "报告.md").exists(), False)
        industry_report.save_job(make_job("run777", status="writing"))
        try:
            industry_report.delete_job("run777")
            check("进行中拒绝删除", "未报错", "应报错")
        except ValueError:
            check("进行中拒绝删除", "拒绝", "拒绝")
    finally:
        config.ROOT, config.REPORT_JOBS_DIR = saved_root, saved_jobs
        config.DATA_DIR = saved_data

    print("== 续跑 ==")
    industry_report.save_job(make_job("eee555", status="outline_pending"))
    job = industry_report.continue_job("eee555")
    check("待确认关卡不动", job["status"], "outline_pending")
    industry_report.save_job(make_job("fff666", status="candidates_pending"))
    job = industry_report.continue_job("fff666")
    check("待勾选沉淀不动", job["status"], "candidates_pending")

    print()
    if FAILS:
        print(f"{len(FAILS)} 项未通过：{'、'.join(FAILS)}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
