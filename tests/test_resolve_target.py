"""任务一：resolve_target 目标解析（title → slug → aliases 精确匹配）与三处接线。

覆盖：五类解析情形（title/slug/alias/未命中/冲突）、大小写不敏感、
validate 对别名双链不再报死链、audit 孤立检测把别名入链算有效、/find 路由按别名跳转。

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

from _common import check, run_tests  # noqa: E402


def _page(title, type_, body="", aliases=(), slug=None):
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


# ── 五类解析情形 ──────────────────────────────────────────────────────────
def test_resolve_by_title():
    print("resolve_target：按标题命中")
    from src import wiki
    _page("英伟达", "company")
    check("标题命中", wiki.resolve_target("英伟达"), ("company", "英伟达"))


def test_resolve_by_slug():
    print("resolve_target：按 slug 命中")
    from src import wiki
    _page("铂科新材", "company", slug="boko")
    check("slug 命中", wiki.resolve_target("boko"), ("company", "boko"))


def test_resolve_by_alias():
    print("resolve_target：按别名命中（含大小写不敏感）")
    from src import wiki
    _page("铂科新材", "company", aliases=("300811", "NVIDIA"))
    check("别名命中", wiki.resolve_target("300811"), ("company", "铂科新材"))
    check("别名大小写不敏感", wiki.resolve_target("nvidia"), ("company", "铂科新材"))


def test_resolve_miss():
    print("resolve_target：未命中返回 None；模糊/包含匹配不命中（精确匹配）")
    from src import wiki
    _page("TDK与村田", "company", aliases=("村田制作所",))
    check("未命中 None", wiki.resolve_target("根本不存在的页面"), None)
    check("包含不命中（村田 ≠ TDK与村田）", wiki.resolve_target("村田"), None)
    check("空名 None", wiki.resolve_target("  "), None)


def test_resolve_conflict_first_wins():
    print("resolve_target：别名冲突按 load_pages 顺序取先建的那页")
    from src import wiki
    # 用 ASCII 名保证文件名排序确定（中文按码点排序对人不可读）
    _page("A公司", "company", aliases=("共同别名",))
    _page("B公司", "company", aliases=("共同别名",))
    pages = wiki.load_pages()  # companies 目录下按文件名排序：A < B
    check("冲突取第一页", wiki.resolve_target("共同别名", pages), ("company", "A公司"))
    # 标题优先于别名：别处别名撞上已有标题时，跳标题页
    check("标题优先于别名", wiki.resolve_target("A公司", pages), ("company", "A公司"))


# ── 接线一：validate 对别名双链不再报死链 ─────────────────────────────────
def test_validate_accepts_alias_wikilink():
    print("validate：别名双链不报死链，真死链仍报")
    from src.validate import validate_all

    _page("铂科新材", "company", aliases=("300811",))
    _page("引用页", "concept", body="- 见 [[300811]] 和 [[真不存在页]]。\n")
    probs = validate_all()
    check("别名双链不报", any("300811" in p and "指向不存在的页面" in p for p in probs), False)
    check("真死链仍报", any("真不存在页" in p and "指向不存在的页面" in p for p in probs), True)


# ── 接线二：audit 孤立检测把别名入链算有效 ────────────────────────────────
def test_audit_orphan_counts_alias_inlink():
    print("audit：仅被别名引用的页不算孤立")
    import json as _json

    import src.llm as llm_mod

    # 不让审校真的调 LLM（省钱且离线稳定）
    llm_mod.chat = lambda messages, **kw: {"conflicts": [], "gaps": []}

    from src import audit, config

    _page("铂科新材", "company", aliases=("300811",))
    _page("孤立页", "concept", body="- 没有人引用我。\n")
    _page("引用页", "concept", body="- 它家材料见 [[300811]]。\n")
    r = audit.run()
    data = _json.loads((config.DATA_DIR / r["path"].name.replace(".md", ".json")).read_text(encoding="utf-8"))
    check("别名入链页不在孤立清单", "铂科新材" in data["orphans"], False)
    check("无人引用页在孤立清单", "孤立页" in data["orphans"], True)


# ── 接线三：/find 路由按别名跳转 ──────────────────────────────────────────
def test_find_route_via_alias():
    print("/find：别名可跳转，未命中 404，精确匹配不误跳")
    from _common import api_client
    from src.web.app import app

    app.config.update(TESTING=True)
    c = api_client(app)
    _page("铂科新材", "company", aliases=("300811",))
    _page("TDK与村田", "company", aliases=("村田制作所",))
    r = c.get("/find/300811")
    check("别名跳转", r.status_code, 302)
    from urllib.parse import unquote
    check("跳对页面", unquote(r.headers["Location"]).endswith("/page/company/铂科新材"), True)
    r = c.get("/find/村田")
    check("「村田」不误跳（404）", r.status_code, 404)
    r = c.get("/find/TDK与村田")
    check("标题照常跳", r.status_code, 302)


# ── 全库校验的别名冲突提示（非错误） ─────────────────────────────────────
def test_alias_conflict_hint():
    print("validate_hints：重名给提示且不算错误")
    from src import wiki
    from src.validate import validate_all, validate_hints

    _page("A公司", "company", aliases=("共同别名",))
    _page("B公司", "company", aliases=("共同别名",))
    wiki.rebuild_index_md(wiki.load_pages())  # 先收进索引，排除「未收录」干扰
    check("不算错误", validate_all(), [])
    hints = validate_hints()
    check("有冲突提示", any("共同别名" in h for h in hints), True)


if __name__ == "__main__":
    run_tests(globals(), [
        "test_resolve_by_title",
        "test_resolve_by_slug",
        "test_resolve_by_alias",
        "test_resolve_miss",
        "test_resolve_conflict_first_wins",
        "test_validate_accepts_alias_wikilink",
        "test_audit_orphan_counts_alias_inlink",
        "test_find_route_via_alias",
        "test_alias_conflict_hint",
    ])
