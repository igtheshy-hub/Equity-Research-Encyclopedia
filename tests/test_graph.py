"""任务三：/api/graph 图谱数据接口。

覆盖：节点数 = 非损坏页面数；边与手算一致（含去重、自引用剔除）；
解析失败的链接丢弃并计数；入链数（按引用页面数去重）正确。

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

from _common import api_client, check, run_tests  # noqa: E402


def _client(csrf=True):
    from src.web.app import app

    app.config.update(TESTING=True)
    return api_client(app, csrf=csrf)


def _page(title, type_, body, aliases=(), slug=None):
    from src import config, wiki

    meta = {"title": title, "type": type_, "sources": ["raw/a.pdf"], "updated": "2026-01-01"}
    if aliases:
        meta["aliases"] = list(aliases)
    d = config.TYPE_DIRS[type_]
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{slug or title}.md"
    if path.exists():
        path.unlink()
    wiki.write_page(path, meta, f"# {title}\n\n{body}")
    return path


def test_graph_nodes_and_edges():
    print("graph：节点数=页面数；边与手算一致；自引用与重复边不画")
    c = _client()
    # P1 → P2、P2（重复，应只算一条边）、自身、不存在页；P2 → P1；P3（孤悬无边）
    _page("页一", "company", body="- 见 [[页二]] 和 [[页二]]，还有 [[页一]] 与 [[不存在页]]。\n")
    _page("页二", "industry", body="- 回指 [[页一]]。\n")
    _page("页三", "concept", body="- 孤悬一条。\n")

    d = c.get("/api/graph").get_json()
    check("节点数 = 页面数", len(d["nodes"]), 3)
    edges = {(l["src"], l["dst"]) for l in d["links"]}
    check("边数与手算一致", len(edges), 2)
    check("含 P1→P2", ("company/页一", "industry/页二") in edges, True)
    check("含 P2→P1", ("industry/页二", "company/页一") in edges, True)
    check("自引用不成边", ("company/页一", "company/页一") in edges, False)
    check("解析失败计数", d["unresolved"], 1)
    check("孤悬页在图上", any(n["slug"] == "页三" for n in d["nodes"]), True)


def test_graph_inbound_counts():
    print("graph：入链数按引用页面数去重；别名入链计入")
    c = _client()
    _page("枢纽", "industry", body="- 一条。\n", aliases=("电感",))
    _page("甲", "company", body="- 见 [[枢纽]] [[枢纽]]。\n")       # 同页引用 2 次仍算 1
    _page("乙", "concept", body="- 见 [[电感]]。\n")                # 别名入链
    d = c.get("/api/graph").get_json()
    hub = next(n for n in d["nodes"] if n["slug"] == "枢纽")
    check("枢纽入链数（去重+别名）", hub["inbound"], 2)
    check("枢纽有 title", hub["title"], "枢纽")


def test_graph_readonly_needs_no_csrf():
    print("graph：只读接口不要求 CSRF 头")
    c = _client(csrf=False)
    check("/api/graph 无头可访问", c.get("/api/graph").status_code, 200)


def test_graph_broken_page_skipped():
    print("graph：损坏页面不进图（节点数=非损坏页面数）")
    c = _client()
    _page("好页", "company", body="- 一条。\n")
    from src import config

    bad = config.TYPE_DIRS["industry"] / "坏页.md"
    bad.write_text("没有 front matter 的坏文件\n", encoding="utf-8")
    d = c.get("/api/graph").get_json()
    check("坏页不进图", [n["slug"] for n in d["nodes"]], ["好页"])


def test_graph_consistent_with_list():
    print("graph：图谱节点与知识库列表页数据一致（同一批页面）")
    c = _client()
    _page("列表页", "company", body="- 一条。\n")
    graph = c.get("/api/graph").get_json()
    kb_html = c.get("/kb").get_data(as_text=True)
    g = next(n for n in graph["nodes"] if n["slug"] == "列表页")
    check("列表页含同一页面", "列表页" in kb_html, True)
    check("图谱含同一页面", g["title"], "列表页")


if __name__ == "__main__":
    run_tests(globals(), [
        "test_graph_nodes_and_edges",
        "test_graph_inbound_counts",
        "test_graph_readonly_needs_no_csrf",
        "test_graph_broken_page_skipped",
        "test_graph_consistent_with_list",
    ])
