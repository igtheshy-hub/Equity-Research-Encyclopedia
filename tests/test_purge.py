"""按原文件删除的自测：在临时沙盒里搭一套最小数据，跑删除并核对每一处痕迹。

用法：python tests/test_purge.py
不读取真实 raw/、wiki/、data/，避免把个人知识库当成测试夹具。
"""
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import config, ingest, purge, validate, wiki  # noqa: E402

FAILS = []

R1 = "raw/reports/汽车/研报甲.pdf"
R2 = "raw/reports/汽车/研报乙.pdf"
F1 = "raw/filings/比亚迪/财报丙.pdf"


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def _write_page(type_, slug, title, sources, sections):
    body = [f"# {title}", ""]
    for name, lines in sections.items():
        body += [f"## {name}", ""]
        body += [f"- {ln}" for ln in lines]
        body.append("")
    meta = {"title": title, "type": type_, "sources": sources, "updated": "2026-10-01"}
    wiki.write_page(config.TYPE_DIRS[type_] / f"{slug}.md", meta, "\n".join(body))


def _seed():
    """三份原文件、四个主题页、三个来源页，覆盖共享锚点/无锚点/纯锚点页。"""
    for rel in (R1, R2, F1):
        p = config.ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF-1.4\n")

    _write_page("company", "比亚迪", "比亚迪", [R1, R2, F1], {
        "经营与财务": [
            "【数据】2025FY 营业收入 = 100 元 [[财报丙.pdf#p1]]",
            "【数据】2025FY 营业收入同比 = 10.00% [[财报丙.pdf#p2]] [[研报甲.pdf#p2]]",
            "【指标】2025FY ROE(加权披露) = 15.31% [[财报丙.pdf#p3]]",
            "管理层说需求好 [[研报甲.pdf#p1]]",
            "这条没有锚点，是人工笔记",
        ],
        "分析观点": ["长期看好 [[研报乙.pdf#p2]]"],
    })
    _write_page("company", "赛力斯", "赛力斯", [F1], {
        "经营与财务": [
            "【数据】2025FY 营业收入 = 50 元 [[财报丙.pdf#p1]]",
            "【指标】2025FY ROE(加权披露) = 5.00% [[财报丙.pdf#p3]]",
        ],
    })
    _write_page("industry", "汽车", "汽车", [R1, R2], {
        "行业概况": [
            "销量增长 [[研报甲.pdf#p1]]",
            "竞争加剧 [[研报乙.pdf#p2]]",
            "相关概念见 [[估值]]",
        ],
    })
    _write_page("concept", "估值", "估值", [R1], {
        "定义与口径": ["[[研报甲.pdf#p1]]"],
    })
    _write_page("concept", "现金流", "现金流", [F1], {
        "定义与口径": ["[[财报丙.pdf#p1]]"],
    })
    _write_page("source", "研报甲", "研报甲", [R1], {
        "材料信息": ["主题：汽车 [[研报甲.pdf#p1]]", "结论：增长 [[研报甲.pdf#p2]]"],
    })
    _write_page("source", "研报乙", "研报乙", [R2], {
        "材料信息": ["主题：汽车 [[研报乙.pdf#p1]]", "结论：竞争 [[研报乙.pdf#p2]]"],
    })
    _write_page("source", "财报丙", "财报丙", [F1], {
        "材料信息": ["主题：财报 [[财报丙.pdf#p1]]", "结论：盈利 [[财报丙.pdf#p3]]"],
    })

    def entry(name, rel, typ, count):
        return {
            "file": name, "path": rel, "sha256": name, "source_type": typ,
            "entries_count": count, "unit": "page", "count": 3,
            "ingested_at": "2026-10-01T10:00:00",
        }

    ingest.save_manifest({"files": [
        entry("研报甲.pdf", R1, "report", 1),
        entry("研报乙.pdf", R2, "report", 2),
        entry("财报丙.pdf", F1, "filing", 3),
    ]})
    config.FIN_DIR.mkdir(parents=True, exist_ok=True)
    (config.FIN_DIR / "比亚迪.json").write_text(json.dumps({
        "slug": "比亚迪", "periods": {"2025FY": {"source": F1}},
    }, ensure_ascii=False), encoding="utf-8")
    for stem in ("研报甲", "研报乙", "财报丙"):
        (config.EXTRACTS_DIR / f"{stem}.units.json").write_text("{}", encoding="utf-8")
        (config.EXTRACTS_DIR / f"{stem}.entries.json").write_text("{}", encoding="utf-8")
    (config.EXTRACTS_DIR / "财报丙.filing.json").write_text("{}", encoding="utf-8")
    wiki.rebuild_index_md(wiki.load_pages())


def sandbox():
    """新建临时目录并把 config 的全部路径指过去，绝不碰真实数据。"""
    tmp = Path(tempfile.mkdtemp(prefix="purge-test-"))
    for name in ("raw", "wiki", "data"):
        (tmp / name).mkdir(parents=True, exist_ok=True)
    config.ROOT = tmp
    config.RAW_DIR = tmp / "raw"
    config.INBOX_DIR = config.RAW_DIR / "inbox"
    config.REPORTS_DIR = config.RAW_DIR / "reports"
    config.FILINGS_DIR = config.RAW_DIR / "filings"
    config.POLICIES_DIR = config.RAW_DIR / "policies"
    config.WIKI_DIR = tmp / "wiki"
    config.INDEX_MD = config.WIKI_DIR / "index.md"
    config.LOG_MD = config.WIKI_DIR / "log.md"
    config.DATA_DIR = tmp / "data"
    config.EXTRACTS_DIR = config.DATA_DIR / "extracts"
    config.FIN_DIR = config.DATA_DIR / "fin"
    config.MANIFEST_PATH = config.DATA_DIR / "manifest.json"
    config.TYPE_DIRS = {
        "company": config.WIKI_DIR / "companies",
        "industry": config.WIKI_DIR / "industries",
        "concept": config.WIKI_DIR / "concepts",
        "source": config.WIKI_DIR / "sources",
    }
    for d in (config.INBOX_DIR, config.REPORTS_DIR, config.FILINGS_DIR,
              config.POLICIES_DIR, config.DATA_DIR, config.EXTRACTS_DIR,
              config.FIN_DIR, *config.TYPE_DIRS.values()):
        d.mkdir(parents=True, exist_ok=True)
    _seed()
    return tmp


def page_titles():
    return sorted(str(p["meta"].get("title") or p["slug"]) for p in wiki.load_pages())


def page(title):
    return next(p for p in wiki.load_pages() if str(p["meta"].get("title") or p["slug"]) == title)


def cited_in(body, name):
    return [ln for ln in body.splitlines() if any(a[0] == name for a in wiki.ANCHOR_RE.findall(ln))]


def test_fixture():
    print("夹具自检：")
    sandbox()
    check("初始全库校验通过", validate.validate_all(), [])


def test_list():
    print("原文件清单：")
    sandbox()
    rows = purge.file_entries()
    names = [r["name"] for r in rows]
    check("列出三份入库文件", len(rows), 3)
    check("按名字能查到研报", "研报甲.pdf" in names, True)
    rep = next(r for r in rows if r["name"] == "研报甲.pdf")
    check("研报类型与条目数", (rep["kind_label"], rep["entries_count"]), ("研报", 1))
    check("研报被引用条数（只算独占锚点的行）", rep["statements"], 3)
    check("只含锚点的页面会被删空", rep["empty_pages"], ["估值"])
    fil = next(r for r in rows if r["name"] == "财报丙.pdf")
    check("财报条目数与被引用数", (fil["entries_count"], fil["statements"]), (3, 5))
    check("财报会删空的页面", fil["empty_pages"], ["现金流"])
    check("不把有正文的页面算作会删空", "比亚迪" in fil["empty_pages"], False)


def test_purge_report():
    print("删一份研报：")
    sandbox()
    name = "研报甲.pdf"
    before = page_titles()
    stats = purge.purge_files([name])[0]
    check("无异常", "error" not in stats, True)
    check("统计到被删条目", stats["statements"] > 0, True)
    check("原文件已删", not (config.REPORTS_DIR / "汽车" / name).exists(), True)
    check("提取缓存已删", (config.EXTRACTS_DIR / "研报甲.units.json").exists(), False)
    check("entries 缓存已删", (config.EXTRACTS_DIR / "研报甲.entries.json").exists(), False)
    check("来源页已删", (config.TYPE_DIRS["source"] / "研报甲.md").exists(), False)
    check("清单里已无这份文件", sorted(f["file"] for f in ingest.load_manifest()["files"]),
          ["研报乙.pdf", "财报丙.pdf"])
    check("全库再无指向它的锚点", [p for p in wiki.load_pages() if cited_in(p["body"], name)], [])
    check("空的行业子目录已清掉", (config.REPORTS_DIR / "汽车").exists(), True)
    check("其他文件不受影响", (config.REPORTS_DIR / "汽车" / "研报乙.pdf").exists(), True)
    check("纯锚点页被移除", (config.TYPE_DIRS["concept"] / "估值.md").exists(), False)
    check("共享锚点行保留另一个文件的锚点",
          "[[财报丙.pdf#p2]]" in page("比亚迪")["body"], True)
    check("失去锚点的行保留并标需复核", page("比亚迪")["meta"].get("needs_review"), True)
    check("页面记录了复核原因", "失去锚点" in str(page("比亚迪")["meta"].get("review_reason")), True)
    check("双链降级成纯文字", "[[估值]]" not in page("汽车")["body"], True)
    last_log = config.LOG_MD.read_text(encoding="utf-8").strip().splitlines()[-1]
    check("日志记了一笔删除", last_log.startswith(f"| {config.today_str()} | delete | {name}"), True)
    check("索引已重建", f"{name}" not in config.INDEX_MD.read_text(encoding="utf-8"), True)
    check("校验无问题", validate.validate_all(), [])
    print(f"    页面 {len(before)} → {len(page_titles())}：{before} → {page_titles()}")


def test_purge_filing():
    print("删一份财报：")
    sandbox()
    name = "财报丙.pdf"
    check("删前有报告期", list(json.loads((config.FIN_DIR / "比亚迪.json").read_text(encoding="utf-8"))["periods"]), ["2025FY"])
    stats = purge.purge_files([name])[0]
    check("无异常", "error" not in stats, True)
    check("原文件已删", (config.FILINGS_DIR / "比亚迪" / name).exists(), False)
    check("财报结构化数据已删", (config.FIN_DIR / "比亚迪.json").exists(), False)
    check("财报提取转储已删", (config.EXTRACTS_DIR / "财报丙.filing.json").exists(), False)
    check("纯锚点页随数据一起清空移除", (config.TYPE_DIRS["concept"] / "现金流.md").exists(), False)
    check("有正文的公司页保留", (config.TYPE_DIRS["company"] / "比亚迪.md").exists(), True)
    check("公司页标记需复核", page("比亚迪")["meta"].get("needs_review"), True)
    check("公司页仍有其他文件的来源", sorted(page("比亚迪")["meta"].get("sources") or []),
          sorted([R1, R2]))
    check("失锚的数据行保留", "营业收入 = 100 元" in page("比亚迪")["body"], True)
    check("失锚的指标行保留", "ROE(加权披露) = 15.31%" in page("比亚迪")["body"], True)
    check("来源页已删", (config.TYPE_DIRS["source"] / "财报丙.md").exists(), False)
    check("清单里已无这份文件", [f["file"] for f in ingest.load_manifest()["files"]],
          ["研报甲.pdf", "研报乙.pdf"])
    check("校验无问题（需复核页豁免逐行锚点）", validate.validate_all(), [])


def test_missing_entry():
    print("清单残留（原文件已不在 raw/）：")
    sandbox()
    m = ingest.load_manifest()
    m["files"].append({"file": "早就没了.pdf", "path": "raw/reports/旧的/早就没了.pdf",
                       "sha256": "x" * 64, "source_type": "report", "entries_count": 0,
                       "unit": "page", "count": 1, "ingested_at": "2026-10-01T10:00:00"})
    ingest.save_manifest(m)
    ghost = next((r for r in purge.file_entries() if r["name"] == "早就没了.pdf"), None)
    check("残留条目会被列出来", ghost is not None, True)
    check("并标为 missing", ghost and ghost["missing"], True)
    s = purge.purge_files(["早就没了.pdf"])[0]
    check("删残留不报错", "error" not in s, True)
    check("清单里已清掉", "早就没了.pdf" in [f["file"] for f in ingest.load_manifest()["files"]], False)


def test_batch_and_dangling_link():
    print("整批删除与无锚点内容保留：")
    sandbox()
    stats = purge.purge_files([r["name"] for r in purge.file_entries()])
    check("三份全部无异常", [s.get("error") for s in stats], [None, None, None])
    check("清单已空", ingest.load_manifest()["files"], [])
    left = page_titles()
    check("有正文的页面保留", all(t in left for t in ("比亚迪", "赛力斯", "汽车")), True)
    check("纯锚点页已删", "估值" not in left and "现金流" not in left, True)
    check("来源页已删", not any(t.startswith("研报") or t == "财报丙" for t in left), True)
    check("剩余页面都标记需复核", all(page(t)["meta"].get("needs_review") for t in left), True)
    check("全库已无锚点", [p for p in wiki.load_pages() if p["anchors"]], [])
    check("双链已降级", "[[估值]]" not in page("汽车")["body"], True)
    check("校验无问题", validate.validate_all(), [])


def test_web_task():
    print("走网页接口跑删除任务：")
    sandbox()
    from src.web.app import app
    c = app.test_client()
    rows = c.get("/api/files").get_json()["items"]
    check("接口列出三份", len(rows), 3)
    r = c.post("/api/files/delete", json={"files": ["研报甲.pdf", "财报丙.pdf"]})
    check("起任务返回 200", r.status_code, 200)
    tid = r.get_json()["task_id"]
    t = None
    for _ in range(100):
        t = next((x for x in c.get("/api/tasks").get_json()["tasks"] if x["id"] == tid), None)
        if t and t["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.1)
    check("任务跑完", t and t["status"], "done")
    check("任务类型与标签", (t["kind"], t["kind_label"]), ("delete", "删除原文件"))
    check("进度到 100", t["percent"], 100)
    check("结果两条无错", [x.get("error") for x in t["result"]], [None, None])
    check("结果带被删条数", all(x["statements"] > 0 for x in t["result"]), True)
    check("接口里只剩一份", len(c.get("/api/files").get_json()["items"]), 1)
    health = c.get("/api/health").get_json()
    check("健康接口可用", health.get("ok") in (True, False), True)
    check("健康接口带模型列表", isinstance(health.get("models"), list), True)


def test_duplicate_name():
    print("同名多份拒绝删除：")
    sandbox()
    dup = config.REPORTS_DIR / "其他" / "研报乙.pdf"
    dup.parent.mkdir(parents=True, exist_ok=True)
    dup.write_bytes(b"%PDF-1.4\n")
    rows = {r["name"]: r for r in purge.file_entries()}
    check("同名文件被标出", rows["研报乙.pdf"]["duplicate"], True)
    check("列出全部路径", len(rows["研报乙.pdf"]["paths"]), 2)
    res = purge.purge_files(["研报乙.pdf"])
    check("拒绝并报错", bool(res[0].get("error")), True)
    check("错误说明同名多份", "多份" in res[0]["error"], True)
    check("两份原件都还在", all(p.exists() for p in (config.REPORTS_DIR / "汽车" / "研报乙.pdf",
                                                     dup)), True)


def test_rollback():
    print("删除中途失败回滚：")
    sandbox()
    before = page("比亚迪")["body"]
    with mock.patch.object(purge, "_drop_fin_periods", side_effect=RuntimeError("模拟落库失败")):
        res = purge.purge_files(["研报甲.pdf"])
    check("返回失败", bool(res[0].get("error")), True)
    check("整批中止标记", res[-1].get("batch_aborted"), True)
    check("原文件已回滚", (config.REPORTS_DIR / "汽车" / "研报甲.pdf").exists(), True)
    check("被删页面已回滚", (config.TYPE_DIRS["concept"] / "估值.md").exists(), True)
    check("正文已回滚", page("比亚迪")["body"], before)
    check("双链没有被误降级", "[[估值]]" in page("汽车")["body"], True)
    check("清单条目仍在", "研报甲.pdf" in [f["file"] for f in ingest.load_manifest()["files"]], True)
    check("回滚后校验通过", validate.validate_all(), [])


def test_bad_page():
    print("坏页面不拖垮整站：")
    sandbox()
    bad = config.TYPE_DIRS["company"] / "坏页.md"
    bad.write_text("没有 front matter 的正文\n", encoding="utf-8")
    check("解析时不抛异常", len(wiki.load_pages()) >= 5, True)
    errs = wiki.page_errors()
    check("坏页被记录", any(p == bad for p, _ in errs), True)
    problems = validate.validate_all()
    check("校验单独报告坏页", any("缺少 front matter" in x for x in problems), True)
    check("其他页面照常校验", any("坏页.md" in x for x in problems), True)


def test_merge_dangling_link():
    print("合并页面后清理双链：")
    sandbox()
    from src import fix
    notes = []
    fix._fix_merge({"from": "估值", "into": "汽车"}, notes)
    check("原页已删", (config.TYPE_DIRS["concept"] / "估值.md").exists(), False)
    check("指向原页的双链已降级", "[[估值]]" not in page("汽车")["body"], True)
    check("内容并入了目标页", "[[研报甲.pdf#p1]]" in page("汽车")["body"], True)
    check("合并有说明", bool(notes), True)
    check("合并后校验通过", validate.validate_all(), [])


def test_unique_target():
    print("取名时全库去重：")
    sandbox()
    from src.classify import unique_target
    check("同名 PDF 自动加序号", unique_target(config.REPORTS_DIR, "研报甲.pdf"), "研报甲-2.pdf")
    check("不同扩展名但同 stem 也避开", unique_target(config.REPORTS_DIR, "研报甲.docx"), "研报甲-2.docx")
    check("新名字原样返回", unique_target(config.REPORTS_DIR, "全新研报.pdf"), "全新研报.pdf")


def test_append_manifest():
    print("清单追加不覆盖并发删除：")
    sandbox()
    ingest.append_manifest([{"file": "新文件.pdf", "sha256": "a" * 64}])
    ingest.append_manifest([{"file": "新文件.pdf", "sha256": "a" * 64}])
    names = [f["file"] for f in ingest.load_manifest()["files"]]
    check("重复追加只留一条", names.count("新文件.pdf"), 1)


if __name__ == "__main__":
    test_fixture()
    test_list()
    test_purge_report()
    test_purge_filing()
    test_missing_entry()
    test_batch_and_dangling_link()
    test_web_task()
    test_duplicate_name()
    test_rollback()
    test_bad_page()
    test_merge_dangling_link()
    test_unique_target()
    test_append_manifest()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {'、'.join(FAILS)}")
        sys.exit(1)
    print("ALL PASS")
