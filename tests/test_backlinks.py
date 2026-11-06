"""任务二：反向链接面板的后端计算 wiki.inbound_links。

覆盖：三条排序规则依次生效、×N 引用次数、空状态、别名入链、自引用不计、
页面详情页渲染出入链卡片（含展开与空状态文案）。

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


def _page(title, type_, body, aliases=(), statements=None):
    from src import config, wiki

    meta = {"title": title, "type": type_, "sources": ["raw/a.pdf"], "updated": "2026-01-01"}
    if aliases:
        meta["aliases"] = list(aliases)
    d = config.TYPE_DIRS[type_]
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{title}.md"
    if path.exists():
        path.unlink()
    wiki.write_page(path, meta, f"# {title}\n\n{body}")
    if statements is not None:
        # statements 由 load_pages 按行数统计，这里不再伪造
        pass
    return path


def _names(lst):
    return [b["title"] for b in lst]


# ── 排序规则三条依次生效 ──────────────────────────────────────────────────
def test_backlinks_sort_rules():
    print("backlinks：主排序入链数（枢纽优先）→ 次排序引用次数 → 末级条目数")
    from src import wiki

    # 目标页
    _page("铂科", "company", body="- 自引用不算 [[铂科]]。\n")
    # ref_hub：行业枢纽，被另外两页引用（inbound=2），引用目标 1 次，3 条
    _page("行业枢纽", "industry", body="- 指向目标 [[铂科]]。\n")
    _page("引用甲", "concept", body="- 见 [[行业枢纽]]。\n- 内容一。\n- 内容二。\n")
    _page("引用乙", "concept", body="- 见 [[行业枢纽]]。\n")
    # ref_a：inbound=0，引用目标 3 次（×3），10 条
    _page("多引页", "company", body="- 一 [[铂科]] [[铂科]] [[铂科]]。\n" + "".join(f"- 填充{i}。\n" for i in range(9)))
    # ref_c：inbound=0，引用目标 1 次，20 条
    _page("厚页", "company", body="- 一 [[铂科]]。\n" + "".join(f"- 填充{i}。\n" for i in range(19)))

    bl = wiki.inbound_links("company", "铂科")
    # 主排序：行业枢纽 inbound=2 最前；多引页与厚页 inbound 都是 0 → 次排序：多引页 ×3 > 厚页 ×1
    check("排序顺序", _names(bl), ["行业枢纽", "多引页", "厚页"])
    hub = bl[0]
    check("枢纽引用次数", hub["count"], 1)
    check("枢纽自身入链", hub["inbound"], 2)
    check("×N 计数", bl[1]["count"], 3)
    # 末级排序：再造两个同 inbound 同次数、条目数不同的引用页
    _page("薄页", "company", body="- 一 [[铂科]]。\n")
    _page("厚页2", "company", body="- 一 [[铂科]]。\n- 填充。\n- 填充。\n")
    bl2 = wiki.inbound_links("company", "铂科")
    tail = [b for b in bl2 if b["title"] in ("薄页", "厚页2")]
    check("末级按条目数", _names(tail), ["厚页2", "薄页"])


def test_backlinks_alias_and_empty():
    print("backlinks：别名入链计入；无人引用返回空（空状态）")
    from src import wiki

    _page("铂科", "company", body="- 一条。\n", aliases=("300811",))
    _page("引用页", "concept", body="- 见 [[300811]]。\n")
    bl = wiki.inbound_links("company", "铂科")
    check("别名入链", _names(bl), ["引用页"])
    check("别名引用次数", bl[0]["count"], 1)

    _page("无人引用", "concept", body="- 孤岛一条。\n")
    check("空状态", wiki.inbound_links("concept", "无人引用"), [])


def test_backlinks_self_link_ignored():
    print("backlinks：页面自引用不算入链")
    from src import wiki

    _page("自恋页", "company", body="- 我引用我自己 [[自恋页]]。\n")
    check("自引用不计", wiki.inbound_links("company", "自恋页"), [])


# ── 页面详情页渲染 ────────────────────────────────────────────────────────
def test_page_renders_backlinks_card():
    print("page.html：meta-card 后出入链卡片；超 5 个折叠展开；空页显示淡字")
    from _common import api_client
    from src.web.app import app

    app.config.update(TESTING=True)
    c = api_client(app)

    _page("目标页", "company", body="- 一条。\n")
    for i in range(7):
        _page(f"引用{i}", "concept", body=f"- 见 [[目标页]]。\n")
    r = c.get("/page/company/目标页")
    check("详情页 200", r.status_code, 200)
    html = r.get_data(as_text=True)
    check("有入链卡片", 'class="meta-card backlinks-card"' in html, True)
    # 7 个 tag 全部渲染：5 个在默认区、2 个在折叠的 details 里（DOM 存在、视觉收起）
    check("默认区 5 个 + 折叠区 2 个", html.count("backlink-tag"), 7)
    check("有展开控件", "展开（还有 2 个）" in html, True)
    check("引用计数提示", "被 7 个页面引用" in html, True)

    _page("孤岛页", "concept", body="- 没人引用。\n")
    r = c.get("/page/concept/孤岛页")
    html = r.get_data(as_text=True)
    check("空状态文案", "暂无其他页面引用此页" in html, True)


def test_backlink_tag_shows_title_not_alias_text():
    print("backlinks：tag 显示引用页规范标题，不显示双链原文（别名）")
    from _common import api_client
    from src.web.app import app

    app.config.update(TESTING=True)
    c = api_client(app)
    _page("目标页", "company", body="- 一条。\n", aliases=("TP-88",))
    _page("引用页", "concept", body="- 见 [[TP-88]]。\n")
    html = c.get("/page/company/目标页").get_data(as_text=True)
    # 用户写的是别名 [[TP-88]]，tag 也显示引用页的规范标题，视觉统一
    check("tag 用规范标题", ">引用页</a>" in html, True)
    check("tag 不含双链别名原文", ">TP-88<" not in html, True)


if __name__ == "__main__":
    run_tests(globals(), [
        "test_backlinks_sort_rules",
        "test_backlinks_alias_and_empty",
        "test_backlinks_self_link_ignored",
        "test_page_renders_backlinks_card",
        "test_backlink_tag_shows_title_not_alias_text",
    ])
