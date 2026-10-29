"""按原文件删除的自测：在临时沙盒里复制一份真实数据，跑删除并核对每一处痕迹。

用法：python tests/test_purge.py（真实 raw/ 与 wiki/ 不会被改动）
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import config, ingest, purge, validate, wiki  # noqa: E402

FAILS = []


def _skip_if_no(*kinds):
    """库内缺指定类别材料时跳过：删除回归的选料是动态的，类别不齐就跑不动。
    直读真实库清单——pytest 全量下 config 路径可能被先前测试指到临时目录，不可信。"""
    m = ROOT / "data" / "manifest.json"
    try:
        files = json.loads(m.read_text(encoding="utf-8")).get("files") or []
    except (OSError, ValueError):
        files = []
    if not files:
        pytest.skip("库为空：删除回归需要已摄入材料")
    labels = {"reports": "研报", "filings": "财报", "policies": "政策"}
    for k in kinds:
        if not any(str(f.get("path") or "").startswith(f"raw/{k}/") for f in files):
            pytest.skip(f"库内没有{labels.get(k, k)}：本用例需要已摄入的{labels.get(k, k)}")


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
    _skip_if_no("reports", "filings")
    sandbox()
    print("原文件清单：")
    rows = purge.file_entries()
    check("清单非空", len(rows) > 0, True)
    rep = next((r for r in rows if r["kind_label"] == "研报" and not r["missing"]
                and r["statements"] > 0 and (config.ROOT / r["path"]).exists()), None)
    check("按清单能查到被引用的研报", rep is not None, True)
    check("研报条目数大于 0", bool(rep) and rep["entries_count"] > 0, True)
    check("研报被引用条数大于 0", bool(rep) and rep["statements"] > 0, True)
    check("研报不标 missing", bool(rep) and rep["missing"], False)
    check("清单里没有收件台文件", any("inbox" in r["path"] for r in rows), False)
    fil = next((r for r in rows if r["kind_label"] == "财报" and not r["missing"]
                and r["statements"] > 0 and (config.ROOT / r["path"]).exists()), None)
    check("按清单能查到被引用的财报", fil is not None, True)
    check("财报条目数大于 0", bool(fil) and fil["entries_count"] > 0, True)
    check("财报被引用条数大于 0", bool(fil) and fil["statements"] > 0, True)
    # 造两个依赖页验证删空预测不依赖真实材料组合：删空页只有财报一个来源；
    # 保留页另有其他材料的锚点，删掉财报后应留存
    cited = set()
    for p in wiki.load_pages():
        for a in wiki.ANCHOR_RE.findall(p["body"]):
            cited.add(a[0])
    other = next((r for r in rows if r["name"] in cited
                  and r["name"] not in (rep["name"], fil["name"])
                  and not r["missing"] and (config.ROOT / r["path"]).exists()), None)
    probe = config.TYPE_DIRS["company"] / "测试删空公司.md"
    wiki.write_page(probe, {
        "title": "测试删空公司", "type": "company", "sources": [fil["path"]],
        "updated": config.today_str(), "history": ["2026-10-29 测试夹具"],
    }, "# 测试删空公司\n\n## 经营与财务\n\n"
       f"- 依赖财报的条目[[{fil['name']}#p1]]\n- 无锚点条目\n")
    other_ref = f"[[{other['name']}#para1]]" if other["name"].lower().endswith(".docx") \
        else f"[[{other['name']}#p1]]"
    keep = config.TYPE_DIRS["company"] / "测试保留公司.md"
    wiki.write_page(keep, {
        "title": "测试保留公司", "type": "company",
        "sources": [fil["path"], other["path"]],
        "updated": config.today_str(), "history": ["2026-10-29 测试夹具"],
    }, "# 测试保留公司\n\n## 经营与财务\n\n"
       f"- 依赖财报的条目[[{fil['name']}#p1]]\n"
       f"- 依赖其他材料的条目{other_ref}\n")
    rows = purge.file_entries()
    fil2 = next(r for r in rows if r["name"] == fil["name"])
    check("预测会被删空的页面（含 1 条无锚点条目）", "测试删空公司" in fil2["empty_pages"], True)
    check("多来源页面不进删空预测", "测试保留公司" in fil2["empty_pages"], False)


def test_purge_report():
    _skip_if_no("reports")
    print("删一份研报：")
    rows = purge.file_entries()
    rep = next(r for r in rows if r["kind_label"] == "研报" and not r["missing"]
               and r["statements"] > 0 and (config.ROOT / r["path"]).exists())
    name = rep["name"]
    rep_path = config.ROOT / rep["path"]
    parent = rep_path.parent
    only_in_dir = not [p for p in parent.iterdir() if p.name != rep_path.name]
    others = [r for r in rows if Path(r["path"]).parent != parent and not r["missing"]]
    before = page_titles()
    stats = purge.purge_files([name])
    s = stats[0]
    check("无异常", "error" not in s, True)
    check("统计到被删条目", s["statements"] > 0, True)
    check("原文件已删", rep_path.exists(), False)
    check("提取缓存已删", (config.EXTRACTS_DIR / f"{Path(name).stem}.units.json").exists(), False)
    check("entries 缓存已删", (config.EXTRACTS_DIR / f"{Path(name).stem}.entries.json").exists(), False)
    check("来源页已删", (config.TYPE_DIRS["source"] / f"{Path(name).stem}.md").exists(), False)
    check("清单里已无这份文件", name in {f["file"] for f in ingest.load_manifest()["files"]}, False)
    left = [p for p in wiki.load_pages() if cited_in(p["body"], name)]
    check("全库再无指向它的锚点", left, [])
    if only_in_dir:
        check("唯一文件的空子目录已清掉", parent.exists(), False)
    if others:
        check("其他目录的文件不受影响", (config.ROOT / others[0]["path"]).exists(), True)
    last_log = config.LOG_MD.read_text(encoding="utf-8").strip().splitlines()[-1]
    check("日志记了一笔删除", last_log.startswith(f"| {config.today_str()} | delete | {name}"), True)
    check("索引已重建", f"{name}" not in config.INDEX_MD.read_text(encoding="utf-8"), True)
    check("校验无问题", validate.validate_all(), [])
    after = page_titles()
    print(f"    页面 {len(before)} → {len(after)}：{before} → {after}")


def test_purge_filing():
    _skip_if_no("filings")
    print("删一份财报：")
    rows = purge.file_entries()
    fil = next(r for r in rows if r["kind_label"] == "财报" and not r["missing"]
               and r["statements"] > 0 and (config.ROOT / r["path"]).exists())
    name = fil["name"]
    fin_json = config.FIN_DIR / f"{Path(fil['path']).parent.name}.json"
    before = json.loads(fin_json.read_text(encoding="utf-8"))
    check("删前该文件有报告期",
          [k for k, v in (before.get("periods") or {}).items()
           if str(v.get("source") or "") == fil["path"]] != [], True)
    s = purge.purge_files([name])[0]
    check("无异常", "error" not in s, True)
    check("原文件已删", (config.ROOT / fil["path"]).exists(), False)
    if fin_json.exists():
        store = json.loads(fin_json.read_text(encoding="utf-8"))
        check("财报结构化数据已删掉该文件的报告期",
              [k for k, v in (store.get("periods") or {}).items()
               if str(v.get("source") or "") == fil["path"]], [])
    else:
        check("财报结构化数据随最后一期一起删除", True, True)
    check("财报提取转储已删", (config.EXTRACTS_DIR / f"{Path(name).stem}.filing.json").exists(), False)
    check("公司页随数据一起清空移除", (config.TYPE_DIRS["company"] / "测试删空公司.md").exists(), False)
    check("仍有其他来源的公司页保留", (config.TYPE_DIRS["company"] / "测试保留公司.md").exists(), True)
    check("来源页已删", (config.TYPE_DIRS["source"] / f"{Path(name).stem}.md").exists(), False)
    check("清单里已无这份文件", name in {f["file"] for f in ingest.load_manifest()["files"]}, False)
    check("校验无问题", validate.validate_all(), [])


def test_missing_entry():
    _skip_if_no("reports")
    print("清单残留（原文件已不在 raw/）：")
    rows = purge.file_entries()
    remaining = {r["name"] for r in rows}
    m = ingest.load_manifest()
    m["files"].append({"file": "早就没了.pdf", "path": "raw/reports/旧的/早就没了.pdf", "sha256": "x" * 64})
    ingest.save_manifest(m)
    rows = purge.file_entries()
    ghost = next((r for r in rows if r["name"] == "早就没了.pdf"), None)
    check("残留条目会被列出来", ghost is not None, True)
    check("并标为 missing", ghost and ghost["missing"], True)
    s = purge.purge_files(["早就没了.pdf"])[0]
    check("删残留不报错", "error" not in s, True)
    check("清单里已清掉", {r["name"] for r in purge.file_entries()}, remaining)


def test_batch_and_dangling_link():
    _skip_if_no("reports")
    print("整批删除与双链降级：")
    # 重新搭一个干净沙盒，把三份一起删掉
    sandbox()
    rows = purge.file_entries()
    check("沙盒里文件都在", len(rows) > 0, True)
    stats = purge.purge_files([r["name"] for r in rows])
    check("全部无异常", [s.get("error") for s in stats], [None] * len(rows))
    check("清单已空", ingest.load_manifest()["files"], [])
    check("wiki 里已无任何页面", page_titles(), [])
    check("校验无问题", validate.validate_all(), [])
    print(f"    被移除的页面：{sorted({t for s in stats for t in s['pages_deleted']})}")


def test_web_task():
    _skip_if_no("reports")
    print("走网页接口跑删除任务：")
    sandbox()
    from src.web.app import app
    c = app.test_client()
    rows = c.get("/api/files").get_json()["items"]
    check("接口列出文件", len(rows) > 1, True)
    # 挑有知识条目的文件删：raw 里可能躺着尚未摄入的材料，删它们统计不到条目
    picks = ([r for r in rows if r.get("statements")][:2] or rows[:2])
    r = c.post("/api/files/delete", json={"files": [p["name"] for p in picks]})
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
    check("结果带被删条数", all("statements" in x for x in t["result"]) and
          sum(x["statements"] for x in t["result"]) > 0, True)
    check("接口里只剩未删的", len(c.get("/api/files").get_json()["items"]), len(rows) - 2)


def test_append_manifest():
    print("清单追加不覆盖并发删除：")
    ingest.append_manifest([{"file": "新文件.pdf", "sha256": "a" * 64}])
    ingest.append_manifest([{"file": "新文件.pdf", "sha256": "a" * 64}])
    names = [f["file"] for f in ingest.load_manifest()["files"]]
    check("重复追加只留一条", names.count("新文件.pdf"), 1)


if __name__ == "__main__":
    manifest_path = ROOT / "data" / "manifest.json"
    if not manifest_path.exists() or not json.loads(manifest_path.read_text(encoding="utf-8")).get("files"):
        print("真实库为空（manifest 没有已摄入文件），test_purge 跳过：摄入材料后再跑完整删除回归。")
        sys.exit(0)
    real_manifest = manifest_path.read_text(encoding="utf-8")
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
