"""按原文件删除的自测：在临时沙盒里复制一份真实数据，跑删除并核对每一处痕迹。

用法：python tests/test_purge.py（真实 raw/ 与 wiki/ 不会被改动）
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import config, ingest, purge, validate, wiki  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def sandbox():
    """把 raw/wiki/data 复制到临时目录，并把 config 的所有路径指过去。"""
    tmp = Path(tempfile.mkdtemp(prefix="purge-test-"))
    for name in ("raw", "wiki", "data"):
        shutil.copytree(ROOT / name, tmp / name)
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
    return tmp


def page_titles():
    return sorted(str(p["meta"].get("title") or p["slug"]) for p in wiki.load_pages())


def cited_in(body, name):
    return [ln for ln in body.splitlines() if any(a[0] == name for a in wiki.ANCHOR_RE.findall(ln))]


def test_list():
    print("原文件清单：")
    rows = purge.file_entries()
    names = [r["name"] for r in rows]
    check("列出三份入库文件", len(rows), 3)
    check("按名字能查到研报", "东吴证券_智能汽车研报_20260921.pdf" in names, True)
    rep = next(r for r in rows if r["name"] == "东吴证券_智能汽车研报_20260921.pdf")
    check("研报类型与条目数", (rep["kind_label"], rep["entries_count"]), ("研报", 33))
    check("研报被引用条数大于 0", rep["statements"] > 0, True)
    check("研报不标 missing", rep["missing"], False)
    check("清单里没有收件台文件", any("inbox" in r["path"] for r in rows), False)
    fil = next(r for r in rows if r["name"] == "比亚迪财报_20260300.pdf")
    check("财报条目数与被引用数", (fil["entries_count"], fil["statements"]), (175, 174))
    check("预测会被删空的页面（含 1 条无锚点条目）", fil["empty_pages"], ["比亚迪"])


def test_purge_report():
    print("删一份研报：")
    name = "东吴证券_智能汽车研报_20260921.pdf"
    before = page_titles()
    stats = purge.purge_files([name])
    s = stats[0]
    check("无异常", "error" not in s, True)
    check("统计到被删条目", s["statements"] > 0, True)
    check("原文件已删", not (config.REPORTS_DIR / "智能汽车" / name).exists(), True)
    check("提取缓存已删", (config.EXTRACTS_DIR / f"{Path(name).stem}.units.json").exists(), False)
    check("entries 缓存已删", (config.EXTRACTS_DIR / f"{Path(name).stem}.entries.json").exists(), False)
    check("来源页已删", (config.TYPE_DIRS["source"] / f"{Path(name).stem}.md").exists(), False)
    check("清单里已无这份文件", sorted(f["file"] for f in ingest.load_manifest()["files"]),
          ["中国汽车流通协会乘用车市场信息联席分会_乘用车市场研报_20260717.pdf", "比亚迪财报_20260300.pdf"])
    left = [p for p in wiki.load_pages() if cited_in(p["body"], name)]
    check("全库再无指向它的锚点", left, [])
    check("空的行业子目录已清掉", (config.REPORTS_DIR / "智能汽车").exists(), False)
    check("其他文件不受影响", (config.REPORTS_DIR / "汽车").exists(), True)
    last_log = config.LOG_MD.read_text(encoding="utf-8").strip().splitlines()[-1]
    check("日志记了一笔删除", last_log.startswith(f"| {config.today_str()} | delete | {name}"), True)
    check("索引已重建", f"{name}" not in config.INDEX_MD.read_text(encoding="utf-8"), True)
    check("校验无问题", validate.validate_all(), [])
    after = page_titles()
    print(f"    页面 {len(before)} → {len(after)}：{before} → {after}")


def test_purge_filing():
    print("删一份财报：")
    name = "比亚迪财报_20260300.pdf"
    before = json.loads((config.FIN_DIR / "比亚迪.json").read_text(encoding="utf-8"))
    check("删前有报告期", list(before["periods"]), ["2025FY"])
    s = purge.purge_files([name])[0]
    check("无异常", "error" not in s, True)
    check("原文件已删", (config.FILINGS_DIR / "比亚迪" / name).exists(), False)
    check("财报结构化数据已删", (config.FIN_DIR / "比亚迪.json").exists(), False)
    check("财报提取转储已删", (config.EXTRACTS_DIR / f"{Path(name).stem}.filing.json").exists(), False)
    check("公司页随数据一起清空移除", (config.TYPE_DIRS["company"] / "比亚迪.md").exists(), False)
    check("来源页已删", (config.TYPE_DIRS["source"] / f"{Path(name).stem}.md").exists(), False)
    check("清单里已无这份文件", [f["file"] for f in ingest.load_manifest()["files"]], ["中国汽车流通协会乘用车市场信息联席分会_乘用车市场研报_20260717.pdf"])
    check("校验无问题", validate.validate_all(), [])


def test_missing_entry():
    print("清单残留（原文件已不在 raw/）：")
    rows = purge.file_entries()
    check("只剩最后一份", [r["name"] for r in rows], ["中国汽车流通协会乘用车市场信息联席分会_乘用车市场研报_20260717.pdf"])
    m = ingest.load_manifest()
    m["files"].append({"file": "早就没了.pdf", "path": "raw/reports/旧的/早就没了.pdf", "sha256": "x" * 64})
    ingest.save_manifest(m)
    rows = purge.file_entries()
    ghost = next((r for r in rows if r["name"] == "早就没了.pdf"), None)
    check("残留条目会被列出来", ghost is not None, True)
    check("并标为 missing", ghost and ghost["missing"], True)
    s = purge.purge_files(["早就没了.pdf"])[0]
    check("删残留不报错", "error" not in s, True)
    check("清单里已清掉", [f["file"] for f in ingest.load_manifest()["files"]], ["中国汽车流通协会乘用车市场信息联席分会_乘用车市场研报_20260717.pdf"])


def test_batch_and_dangling_link():
    print("整批删除与双链降级：")
    # 重新搭一个干净沙盒，把三份一起删掉
    sandbox()
    rows = purge.file_entries()
    check("沙盒里三份都在", len(rows), 3)
    stats = purge.purge_files([r["name"] for r in rows])
    check("三份全部无异常", [s.get("error") for s in stats], [None, None, None])
    check("清单已空", ingest.load_manifest()["files"], [])
    check("wiki 里已无任何页面", page_titles(), [])
    check("校验无问题", validate.validate_all(), [])
    print(f"    被移除的页面：{sorted({t for s in stats for t in s['pages_deleted']})}")


def test_web_task():
    print("走网页接口跑删除任务：")
    sandbox()
    from src.web.app import app
    c = app.test_client()
    rows = c.get("/api/files").get_json()["items"]
    check("接口列出三份", len(rows), 3)
    r = c.post("/api/files/delete", json={"files": [rows[0]["name"], rows[1]["name"]]})
    check("起任务返回 200", r.status_code, 200)
    tid = r.get_json()["task_id"]
    t = None
    for _ in range(100):
        t = next((x for x in c.get("/api/tasks").get_json()["tasks"] if x["id"] == tid), None)
        if t and t["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    check("任务跑完", t and t["status"], "done")
    check("任务类型与标签", (t["kind"], t["kind_label"]), ("delete", "删除原文件"))
    check("进度到 100", t["percent"], 100)
    check("结果两条无错", [x.get("error") for x in t["result"]], [None, None])
    check("结果带被删条数", all(x["statements"] > 0 for x in t["result"]), True)
    check("接口里只剩一份", len(c.get("/api/files").get_json()["items"]), 1)


def test_append_manifest():
    print("清单追加不覆盖并发删除：")
    ingest.append_manifest([{"file": "新文件.pdf", "sha256": "a" * 64}])
    ingest.append_manifest([{"file": "新文件.pdf", "sha256": "a" * 64}])
    names = [f["file"] for f in ingest.load_manifest()["files"]]
    check("重复追加只留一条", names.count("新文件.pdf"), 1)


if __name__ == "__main__":
    real_manifest = (ROOT / "data" / "manifest.json").read_text(encoding="utf-8")
    sandbox()
    test_list()
    test_purge_report()
    test_purge_filing()
    test_missing_entry()
    test_batch_and_dangling_link()
    test_web_task()
    test_append_manifest()
    check("真实数据未被改动", (ROOT / "data" / "manifest.json").read_text(encoding="utf-8") == real_manifest, True)
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {'、'.join(FAILS)}")
        sys.exit(1)
    print("ALL PASS")
