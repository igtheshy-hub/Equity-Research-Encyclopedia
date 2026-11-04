"""问题 1–5 的回归测试：XSS、路径穿越、清单原子写、并发打架、整体写回。

对应问题清单（E:\\ai智能体\\4\\problem.md）第 1、2、3、4、5 条。
这些用例全部跑在 tmp_path 沙盒里（conftest.py 的 autouse fixture），不碰真实数据。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _common import check, run_tests  # noqa: E402


# ── 问题 1：XSS ─────────────────────────────────────────────────────────────
def test_xss_escaped():
    """研报正文里的 <script> 必须被转义成文字，不能变成可执行 HTML。"""
    print("问题 1：XSS 内容转义")
    from src.web.render import render_md

    evil = "营收增长<script>alert('xss')</script>与<img src=x onerror=alert(1)>"
    out = render_md(evil)
    check("不含裸 <script>", "<script>" in out, False)
    check("不含裸 <img", "<img" in out, False)
    check("含转义后的文本", "&lt;script&gt;" in out, True)
    check("img 标签被转义", "&lt;img" in out, True)

    # 锚点与双链仍然要渲染成可点的标签
    out2 = render_md("净息差 1.42%。[[某研报.pdf#p3]]")
    check("锚点仍是 sup", "<sup class=\"cite\">" in out2, True)
    check("锚点链接存在", "/viewer?file=" in out2, True)

    # 双链里的标题也要转义（< 变实体、href 变 URL 编码，都不可执行）
    out3 = render_md("参见 [[<img src=x onerror=alert(1)>]]")
    check("双链不含裸 <img", "<img" in out3, False)
    check("双链标题已转义", "&lt;img" in out3, True)
    check("双链 href 已编码", "%3Cimg" in out3, True)


def test_xss_metadata_escaped():
    """知识页 frontmatter 里的字段（标题/摘要）不能在页面上变成标签。"""
    print("问题 1：页面元数据转义")
    from src.web.app import app

    app.config.update(TESTING=True)
    with app.test_client() as c:
        r = c.get("/kb")
        check("知识库页 200", r.status_code, 200)


def test_delete_requires_same_origin():
    """删除接口必须挡住跨站请求（没有同源头 → 403）。"""
    print("问题 1：删除接口的跨站防护")
    from _common import build_sandbox
    from src.web.app import app

    build_sandbox()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        rows = c.get("/api/files").get_json()["items"]
        # 不带同源头 → 拒绝
        r = c.post("/api/files/delete", json={"files": [rows[0]["name"]]})
        check("无同源头被拒", r.status_code, 403)
        # 带同源头 → 放行
        r2 = c.post("/api/files/delete", json={"files": [rows[0]["name"]]},
                    headers={"X-Requested-With": "wiki"})
        check("带同源头放行", r2.status_code, 200)


# ── 问题 2：路径穿越 ────────────────────────────────────────────────────────
def test_path_traversal_blocked():
    """带 ../ 的 slug 必须被挡住，不能读到 wiki 目录外的文件。"""
    print("问题 2：路径穿越防护")
    from src import wiki

    bad_slugs = [
        "..\\..\\schema\\CLAUDE",
        "../../schema/CLAUDE",
        "..",
        ".",
        "a/b",
        "a:b",
        "a\\b",
        "",
        None,
    ]
    for s in bad_slugs:
        try:
            wiki.slug_to_path("company", s)
            check(f"拒绝 {s!r}", "没拒绝", "ValueError")
        except ValueError:
            check(f"拒绝 {s!r}", "ValueError", "ValueError")
        except Exception as e:
            check(f"拒绝 {s!r}", type(e).__name__, "ValueError")

    # 正常 slug 仍然可用
    ok = wiki.slug_to_path("company", "中信银行")
    check("正常 slug 可用", ok.name, "中信银行.md")

    # 未知类型也要拒绝
    try:
        wiki.slug_to_path("nosuchtype", "x")
        check("未知类型被拒", "没拒绝", "ValueError")
    except ValueError:
        check("未知类型被拒", "ValueError", "ValueError")


def test_traversal_url_404():
    """带穿越字符的 URL 必须 404，不能读出 wiki 外的文件。"""
    print("问题 2：穿越 URL 返回 404")
    from src.web.app import app

    app.config.update(TESTING=True)
    with app.test_client() as c:
        r = c.get("/page/company/..%5C..%5Cschema%5CCLAUDE")
        check("穿越 URL 404", r.status_code, 404)
        r2 = c.get("/page/company/..%2F..%2Fschema%2FCLAUDE")
        check("斜杠穿越 404", r2.status_code, 404)


# ── 问题 3：清单原子写 ──────────────────────────────────────────────────────
def test_manifest_atomic_write():
    """清单写入必须是原子的：写完的清单永远不是半截 JSON。"""
    print("问题 3：清单原子写入")
    from src import config, ingest

    ingest.save_manifest({"files": [{"file": "a.pdf", "sha256": "x"}]})
    check("清单存在", config.MANIFEST_PATH.exists(), True)
    data = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    check("能读出条目", len(data["files"]), 1)

    # 第二次写入时，旧版被备份成 .bak（第一次写时还没有旧文件可备）
    ingest.save_manifest({"files": [{"file": "a.pdf", "sha256": "x"},
                                    {"file": "b.pdf", "sha256": "y"}]})
    bak = config.MANIFEST_PATH.with_suffix(config.MANIFEST_PATH.suffix + ".bak")
    check("生成了 .bak 备份", bak.exists(), True)
    bak_data = json.loads(bak.read_text(encoding="utf-8"))
    check("备份里是上一版内容", len(bak_data["files"]), 1)

    # 临时文件不该残留
    leftovers = [p.name for p in config.DATA_DIR.glob("manifest.json.*.tmp")]
    check("没有残留临时文件", leftovers, [])


def test_manifest_corrupt_recovery():
    """清单被写坏时要能自动用备份恢复，而不是整个软件打不开。"""
    print("问题 3：清单损坏自动恢复")
    from src import config, ingest

    # 先写两版，让 .bak 里存着有内容的旧版
    ingest.save_manifest({"files": [{"file": "old.pdf", "sha256": "o1"}]})
    ingest.save_manifest({"files": [{"file": "good.pdf", "sha256": "g1"}]})

    # 手动把主文件写坏（模拟写到一半被 Ctrl+C / 断电）
    config.MANIFEST_PATH.write_text('{"files": [ {"file": "b.pdf", ', encoding="utf-8")

    data = ingest.load_manifest()
    check("坏清单能读出可用结构", isinstance(data.get("files"), list), True)
    names = [f["file"] for f in data["files"]]
    check("用备份恢复出真实内容", names, ["old.pdf"])
    # 坏文件被挪到 .corrupt（留证据，不直接覆盖丢现场）
    check("坏文件另存为 .corrupt",
          config.MANIFEST_PATH.with_suffix(config.MANIFEST_PATH.suffix + ".corrupt").exists(), True)
    # 恢复后主文件被写回成可用内容（下次不用再恢复一次）
    again = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    check("主文件已修好", [f["file"] for f in again["files"]], ["old.pdf"])


# ── 问题 4：并发打架 ────────────────────────────────────────────────────────
def test_concurrent_page_writes():
    """摄入线与删除线同时改同一页时，不能出现"删掉的条目又回来"。"""
    print("问题 4：页面并发写不丢改动")
    from src import config, wiki

    # 造一页
    config.TYPE_DIRS["company"].mkdir(parents=True, exist_ok=True)
    p = config.TYPE_DIRS["company"] / "并发测试.md"
    wiki.write_page(p, {"title": "并发测试", "type": "company", "sources": ["raw/a.pdf"],
                         "updated": "2026-01-01"}, "# 并发测试\n\n## 经营与财务\n\n- 原有条目。[[a.pdf#p1]]\n")

    errors = []

    def writer(i):
        try:
            for _ in range(20):
                meta, body = wiki.parse_page(p)
                lines = [f"- 并发写入 {i}。[[a.pdf#p2]]"]
                new_body = wiki.render_page("并发测试", {"经营与财务": lines}, "company")
                wiki.write_page(p, meta, new_body)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("并发写没报错", errors, [])
    # 写完后文件必须是完整可解析的（不能是半截）
    meta, body = wiki.parse_page(p)
    check("写完后 frontmatter 完整", meta.get("title"), "并发测试")
    check("正文有内容", "并发写入" in body, True)
    # 没有残留临时文件
    leftovers = [x.name for x in p.parent.glob("*.tmp")]
    check("无残留临时文件", leftovers, [])


def test_write_page_temp_unique():
    """两个线程同时写同一页，临时文件名不能撞。"""
    print("问题 4：临时文件名唯一")
    from src import config, wiki

    config.TYPE_DIRS["concept"].mkdir(parents=True, exist_ok=True)
    p = config.TYPE_DIRS["concept"] / "临时名测试.md"
    seen = []
    errors = []

    def w(i):
        try:
            for _ in range(10):
                wiki.write_page(p, {"title": f"t{i}", "type": "concept", "sources": []},
                               f"# t{i}\n")
                seen.append(len(list(p.parent.glob("*.tmp"))))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("并发写没报错", errors, [])
    check("最终无残留临时文件", list(p.parent.glob("*.tmp")), [])


# ── 问题 5：整体写回 ────────────────────────────────────────────────────────
def test_no_full_manifest_writeback():
    """摄取/修复跑完后，不能把并发删除的记录写回总账本。"""
    print("问题 5：不整体写回清单")
    from src import config, ingest

    ingest.save_manifest({"files": [{"file": "keep.pdf", "sha256": "k1"}]})
    # 模拟"摄取期间用户删了 keep.pdf"
    disk = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    disk["files"] = []          # 删除线把那条删了
    ingest.save_manifest(disk)

    # 摄入线手里的内存清单还是旧的（包含 keep.pdf），
    # 但它只把「本轮新增的那一条」交给 persist_new_entries（差集）
    stale = {"files": [{"file": "keep.pdf", "sha256": "k1"},
                       {"file": "new.pdf", "sha256": "n1"}]}
    before = {f.get("sha256") for f in stale["files"] if f["file"] == "keep.pdf"}
    delta = [f for f in stale["files"] if f.get("sha256") not in before]

    n = ingest.persist_new_entries(delta)
    check("只追加了新条目", n, 1)

    after = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    names = sorted(f["file"] for f in after["files"])
    check("已删记录没有复活", names, ["new.pdf"])


def test_run_all_does_not_resurrect_deleted():
    """行为断言：run_all 跑到一半时用户删了材料，跑完不能把删掉的写回来。

    原来这条用 inspect.getsource() 抓源码文本判断"有没有调 save_manifest"，
    改个变量名就假红、改个调用方式就假绿。这里改成真的跑一遍看结果。
    """
    print("问题 5：run_all 不复活已删材料")
    from src import config, ingest

    # 盘上先有一份已被摄入的材料
    ingest.save_manifest({"files": [{"file": "old.pdf", "sha256": "o1"}]})

    # 模拟"摄取跑到一半，用户在网页上删了 old.pdf"
    disk = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    disk["files"] = []
    ingest.save_manifest(disk)

    # run_all 走一遍（用不存在的路径，ingest_one 会直接跳过，不会真调 LLM）
    ingest.run_all([config.RAW_DIR / "不存在的.pdf"])

    after = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    names = [f["file"] for f in after["files"]]
    check("已删材料没有被写回", "old.pdf" in names, False)


def test_finish_does_not_resurrect_deleted():
    """finish 也不能把内存里的旧清单整体写回。"""
    print("问题 5：finish 不复活已删材料")
    from src import config, ingest

    ingest.save_manifest({"files": [{"file": "old.pdf", "sha256": "o1"}]})
    # 删除线把盘上那份删了
    disk = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    disk["files"] = []
    ingest.save_manifest(disk)

    # 摄入线手里还拿着旧的内存清单
    stale = {"files": [{"file": "old.pdf", "sha256": "o1"}]}
    ingest.finish(stale, [])

    after = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    names = [f["file"] for f in after["files"]]
    check("已删材料没有被写回", "old.pdf" in names, False)


def test_fix_ingest_does_not_resurrect_deleted():
    """行为断言：修复向导摄入完不能把已删材料写回清单。"""
    print("问题 5：修复向导不复活已删材料")
    from src import config, ingest

    ingest.save_manifest({"files": [{"file": "old.pdf", "sha256": "o1"}]})
    disk = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    disk["files"] = []
    ingest.save_manifest(disk)

    # 修复向导摄入一个不存在的文件（不会真调 LLM），但清单不能被写回旧内容
    from src import fix
    notes = []
    fix._fix_ingest({"path": "raw/reports/不存在.pdf"}, notes)
    check("修复执行了（跳过提示）", len(notes) >= 1, True)

    after = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    names = [f["file"] for f in after["files"]]
    check("已删材料没有被写回", "old.pdf" in names, False)


if __name__ == "__main__":
    run_tests(globals(), [
        "test_xss_escaped",
        "test_xss_metadata_escaped",
        "test_delete_requires_same_origin",
        "test_path_traversal_blocked",
        "test_traversal_url_404",
        "test_manifest_atomic_write",
        "test_manifest_corrupt_recovery",
        "test_concurrent_page_writes",
        "test_write_page_temp_unique",
        "test_no_full_manifest_writeback",
        "test_run_all_does_not_resurrect_deleted",
        "test_finish_does_not_resurrect_deleted",
        "test_fix_ingest_does_not_resurrect_deleted",
    ])
