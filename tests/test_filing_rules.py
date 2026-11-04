"""财报补充资料取数、目标数据补抄与【管理层】行格式校验自测。用法：python tests/test_filing_rules.py"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import config, filing, fin_calc, industry_report, ingest, validate, wiki  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def rules(text, pages=(219,)):
    meta = {"sections": {"cf_supp": list(pages)}}
    units = [{"n": 219, "text": text}]
    return filing._supp_net_profit(units, meta, "千元")


def test_supp_net_profit():
    print("[补充资料起点净利润]")
    check("本期列取第一个数", rules("净利润 33,760,758 41,587,940"), 33_760_758_000.0)
    check("括号负数", rules("净利润 (1,234) 2,345"), -1_234_000.0)
    check("跳过归母行", rules("归属于母公司股东的净利润 9,999\n净利润 100 200"), 100_000.0)
    check("净利润率不误认", rules("净利润率 12.5%"), None)
    check("无补充资料页跳过", filing._supp_net_profit(
        [{"n": 1, "text": "净利润 100 200"}], {"sections": {"cf_supp": []}}, "千元"), None)
    check("单位未识别跳过", filing._supp_net_profit(
        [{"n": 219, "text": "净利润 100 200"}], {"sections": {"cf_supp": [219]}}, None), None)


def line_problems(ln):
    return len(validate._line_format_problems("测试页.md", ln))


def test_management_lines():
    print("[管理层行格式]")
    good = "【管理层】「本公司经营稳定」——经营说明 [[a.pdf#p3]]"
    check("完整行通过", line_problems(good), 0)
    check("引述内含」仍通过", line_problems("【管理层】「他说「好」」——风险提示 [[a.pdf#p3]]"), 0)
    check("缺「开头", line_problems("【管理层】本公司经营稳定——经营说明 [[a.pdf#p3]]"), 1)
    check("引述未闭合", line_problems("【管理层】「本公司经营稳定——经营说明 [[a.pdf#p3]]"), 1)
    check("缺语境分隔", line_problems("【管理层】「本公司经营稳定」经营说明 [[a.pdf#p3]]"), 1)
    check("缺锚点", line_problems("【管理层】「本公司经营稳定」——经营说明"), 1)
    check("锚点格式不合法", line_problems("【管理层】「本公司经营稳定」——经营说明 [[a.txt#p3]]"), 1)
    check("缺语境", line_problems("【管理层】「本公司经营稳定」—— [[a.pdf#p3]]"), 1)
    check("锚点后有多余文字", line_problems("【管理层】「本公司经营稳定」——经营说明 [[a.pdf#p3]] 注"), 1)
    check("数据行校验不受影响", line_problems("【数据】2025FY 合并 营业收入 = abc"), 1)


def test_hk_statements():
    print("[港股报表口径]")
    check("收入归一", fin_calc.normalize_item("收入")[0], "营业收入")
    check("销售成本归一", fin_calc.normalize_item("销售成本")[0], "营业成本")
    check("本年度利润归一", fin_calc.normalize_item("本年度利潤")[0], "净利润")
    items = {
        "营业收入": {"v": 100.0}, "营业成本": {"v": -60.0}, "研发费用": {"v": -5.0},
        "资产总计": {"v": 200.0}, "负债合计": {"v": -120.0}, "所有者权益合计": {"v": 80.0},
    }
    check("识别港股列示", filing._fix_statement_signs(items), True)
    check("成本翻正", items["营业成本"]["v"], 60.0)
    check("研发翻正", items["研发费用"]["v"], 5.0)
    check("负债翻正", items["负债合计"]["v"], 120.0)
    check("R1 通过", fin_calc.check_reconciliations({k: v["v"] for k, v in items.items()}, 0.005), [])

    a_items = {"营业收入": {"v": 100.0}, "营业成本": {"v": 60.0},
               "资产总计": {"v": 200.0}, "负债合计": {"v": 120.0}}
    check("A 股不误翻", filing._fix_statement_signs(a_items), False)
    check("A 股成本不变", a_items["营业成本"]["v"], 60.0)

    r3 = {"未分配利润": 10.0, "期初未分配利润": 5.0, "净利润": 4.0,
          "提取盈余公积": 0.0, "对所有者或股东的分配": -2.0}
    check("R3 不平会报", fin_calc.check_reconciliations(r3, 0.005),
          ["R3 未分配利润勾稽不平：期末 10 vs 期初+净利+提取+分配(代数和) 7"])
    check("R3 可显式跳过", fin_calc.check_reconciliations(r3, 0.005, skip=("R3",)), [])


def test_targets():
    print("[目标数据补抄]")
    check("目标归一字符串", ingest.normalize_targets(["毛利率"]),
          [{"item": "毛利率", "why": ""}])
    check("目标归一字典", ingest.normalize_targets([{"item": "研发投入", "why": "核心竞争力"}]),
          [{"item": "研发投入", "why": "核心竞争力"}])

    units = [{"n": n, "text": f"[第{n}页] 测试公司 2025 年研发投入 50 亿元"} for n in range(1, 13)]
    meta = {"sections": {"key": [3], "mda": [8]},
            "target_pages": [{"item": "近三年研发投入", "pages": [9]}]}
    orig_chat = filing.llm.chat
    filing.llm.chat = lambda *a, **k: {"data": [
        {"item": "近三年研发投入", "scope": "2023-2025年", "value": "50",
         "unit": "亿元", "page": 9, "quote": "研发投入 50 亿元"},
        {"item": "近三年研发投入", "scope": "2023-2025年", "value": 50,
         "unit": "亿元", "page": 9},
    ]}
    try:
        rows = filing._target_rows(Path("a.pdf"), units, meta,
                                   [{"item": "近三年研发投入", "why": "核心竞争力"}])
    finally:
        filing.llm.chat = orig_chat
    check("按定位页抄录并去重", len(rows), 1)
    check("锚点取实际页", rows[0]["anchor"], "[[a.pdf#p9]]")
    check("数值解析", rows[0]["value"], 50.0)

    line = filing._render_target_lines("2025FY", rows)[0]
    check("目标行", line,
          "【数据】2025FY 目标 近三年研发投入（2023-2025年） = 50 亿元 [[a.pdf#p9]]")
    check("目标行通过格式校验", validate._line_format_problems("测试.md", line), [])

    pct = filing._render_target_lines("2025FY", [{
        "item": "毛利率", "scope": "2025年", "value": 16.61, "unit": "%",
        "page": 8, "anchor": "[[a.pdf#p8]]"}])[0]
    check("百分数行", pct, "【数据】2025FY 目标 毛利率（2025年） = 16.61% [[a.pdf#p8]]")
    check("百分数行通过格式校验", validate._line_format_problems("测试.md", pct), [])

    cons = {"营业收入": {"v": 1e11}}
    dupe = [{"item": "营业收入", "scope": "合并", "value": 1000, "unit": "亿元",
             "page": 6, "anchor": "[[a.pdf#p6]]"}]
    check("与四表重复略过", filing._drop_target_dupes(dupe, cons), ([], 1))

    job = {"gaps": [
        {"status": "selected", "item": "近三年研发投入", "why": "核心竞争力"},
        {"status": "selected", "item": "公司最近一期年度报告（财报）", "why": "年报为锚"},
        {"status": "selected", "item": "公司最近一年内半年报（财报）", "why": "更新数据"},
        {"status": "selected", "item": "未摄入材料：某研报.pdf", "why": "先入库"},
        {"status": "skipped", "item": "毛利率", "why": "盈利预测"},
        {"status": "done", "item": "管理层调整公告（辞任）", "why": "公司概况"},
    ]}
    check("缺口转目标只留数据项", industry_report._gap_targets(job),
          [{"item": "近三年研发投入", "why": "核心竞争力"}])


def test_target_ingest():
    print("[目标数据随财报入库]")
    import src.fetcher as fetcher
    orig_build = fetcher.build_lines
    captured = {}

    def fake_build(names, focus=None, targets=None, force_kind=None):
        captured["targets"] = targets
        return (lambda tid: [], lambda tid: [])

    fetcher.build_lines = fake_build
    try:
        fetcher.ingest_inbox(["a.pdf"], targets=[{"item": "研发投入", "why": "核心竞争力"}])
    finally:
        fetcher.build_lines = orig_build
    check("取数入库带目标", captured["targets"], [{"item": "研发投入", "why": "核心竞争力"}])

    orig_annual = fetcher.fetch_annual_report
    orig_inbox = fetcher.ingest_inbox
    got = {}
    fetcher.fetch_annual_report = lambda company, code=None, market=None, year=None, limit=1: {
        "items": [{"file": "a.pdf"}]}

    def fake_inbox(names, tid=None, targets=None, force_kind=None):
        got.update(names=names, targets=targets, force_kind=force_kind)
        return {"classify": [], "ingest": []}

    fetcher.ingest_inbox = fake_inbox
    try:
        fetcher.run_fetch("annual_report", {"company": "测试"},
                          targets=[{"item": "研发投入", "why": "核心竞争力"}])
    finally:
        fetcher.fetch_annual_report = orig_annual
        fetcher.ingest_inbox = orig_inbox
    check("年报强制归为财报", got.get("force_kind"), "filing")
    check("年报带目标与文件", (got.get("names"), got.get("targets")),
          (["a.pdf"], [{"item": "研发投入", "why": "核心竞争力"}]))

    tmp = Path(tempfile.mkdtemp(prefix="kb_target_test_"))
    keys = ("ROOT", "WIKI_DIR", "TYPE_DIRS", "FIN_DIR", "EXTRACTS_DIR", "MANIFEST_PATH",
            "RAW_DIR", "REPORTS_DIR", "FILINGS_DIR", "POLICIES_DIR", "INBOX_DIR")
    saved = {k: getattr(config, k) for k in keys}
    orig = {k: getattr(filing, k) for k in (
        "_locate", "_transcribe", "_quotes", "_detect_currency", "_detect_unit", "_write_source_page")}
    orig_chat = filing.llm.chat
    orig_check = filing.fin_calc.check_reconciliations
    try:
        wiki_dir = tmp / "wiki"
        type_dirs = {t: wiki_dir / d for t, d in (
            ("company", "companies"), ("industry", "industries"),
            ("concept", "concepts"), ("source", "sources"))}
        for d in type_dirs.values():
            d.mkdir(parents=True)
        (tmp / "data" / "extracts").mkdir(parents=True)
        (tmp / "data" / "fin").mkdir(parents=True)
        (tmp / "raw" / "filings" / "测试公司").mkdir(parents=True)
        pdf = tmp / "raw" / "filings" / "测试公司" / "测试公司财报_20251231.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        config.ROOT = tmp
        config.WIKI_DIR = wiki_dir
        config.TYPE_DIRS = type_dirs
        config.FIN_DIR = tmp / "data" / "fin"
        config.EXTRACTS_DIR = tmp / "data" / "extracts"
        config.MANIFEST_PATH = tmp / "data" / "manifest.json"
        config.RAW_DIR = tmp / "raw"
        config.REPORTS_DIR = tmp / "raw" / "reports"
        config.FILINGS_DIR = tmp / "raw" / "filings"
        config.POLICIES_DIR = tmp / "raw" / "policies"
        config.INBOX_DIR = tmp / "raw" / "inbox"
        for d in (config.REPORTS_DIR, config.POLICIES_DIR, config.INBOX_DIR):
            d.mkdir(parents=True, exist_ok=True)
        rel = "raw/filings/测试公司/测试公司财报_20251231.pdf"
        config.MANIFEST_PATH.write_text(json.dumps({"files": [{
            "file": pdf.name, "path": rel, "sha256": "fake", "unit": "page",
            "count": 20, "source_type": "filing"}]}, ensure_ascii=False), encoding="utf-8")

        units = [{"n": i, "text": f"[第{i}页] 测试公司 2025 年年度报告 营业收入 1,000 亿元"}
                 for i in range(1, 21)]
        meta = {"company": "测试公司", "code": "000001", "pk": "2025FY",
                "period_end": "2025-12-31", "audit_opinion": "标准无保留意见",
                "sections": {"key": [3], "bs": [5], "is": [6], "cf": [7], "eq": [],
                             "cf_supp": [], "mda": [8]},
                "target_pages": [{"item": "近三年研发投入", "pages": [9]}]}
        rows = [
            {"statement": "资产负债表", "scope": "合并", "item": "资产总计", "value": 2000, "unit": "亿元", "page": 5},
            {"statement": "资产负债表", "scope": "合并", "item": "负债合计", "value": 1200, "unit": "亿元", "page": 5},
            {"statement": "资产负债表", "scope": "合并", "item": "所有者权益合计", "value": 800, "unit": "亿元", "page": 5},
            {"statement": "利润表", "scope": "合并", "item": "营业收入", "value": 1000, "unit": "亿元", "page": 6},
            {"statement": "利润表", "scope": "合并", "item": "净利润", "value": 100, "unit": "亿元", "page": 6},
        ]
        filing._locate = lambda path, units, targets=None: dict(meta)
        filing._transcribe = lambda path, units, meta: rows
        filing._quotes = lambda path, units, meta: []
        filing._detect_currency = lambda units, sections: "CNY"
        filing._detect_unit = lambda units, sections: "亿元"
        filing._write_source_page = lambda *a, **k: None
        filing.fin_calc.check_reconciliations = lambda *a, **k: []
        filing.llm.chat = lambda *a, **k: {"data": [{
            "item": "近三年研发投入", "scope": "2023-2025年", "value": 50,
            "unit": "亿元", "page": 9, "quote": "研发投入 50 亿元"}]}

        manifest = ingest.load_manifest()
        r = filing.ingest_filing(pdf, units, "page", manifest, "fake",
                                 targets=[{"item": "近三年研发投入", "why": "核心竞争力"}])
        page = next((wiki_dir / "companies").glob("测试公司*.md"))
        text = page.read_text(encoding="utf-8")
        check("目标数据写入公司页",
              "【数据】2025FY 目标 近三年研发投入（2023-2025年） = 50 亿元 "
              "[[测试公司财报_20251231.pdf#p9]]" in text, True)
        store = json.loads((tmp / "data" / "fin" / "测试公司.json").read_text(encoding="utf-8"))
        check("目标数据进结构化存储", store["periods"]["2025FY"]["targets"][0]["page"], 9)
        check("结果计入目标条数", "目标数据 1 条" in r["log"], True)
    finally:
        filing.llm.chat = orig_chat
        filing.fin_calc.check_reconciliations = orig_check
        for k, v in orig.items():
            setattr(filing, k, v)
        for k, v in saved.items():
            setattr(config, k, v)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_supp_net_profit()
    test_management_lines()
    test_hk_statements()
    test_targets()
    test_target_ingest()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {'、'.join(FAILS)}")
        sys.exit(1)
    print("ALL PASS")
