"""自动补双链（src/linking.py）的测试：内容页之间真正连动的地基。

覆盖：标题/别名精确匹配插入、长名优先、锚点与已有双链保护、自引用不链、
跨页重名不链、ASCII 数字边界、幂等、锚点集合不变、history/log 留痕、
validate 无死链、摄入 finish 集成。

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


def wiki_norm(s):
    from src import wiki

    return wiki._norm_name(s)


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


# ── 单行匹配规则 ──────────────────────────────────────────────────────────
def test_link_line_basic_rules():
    print("link_line：标题与别名都链；锚点保护；长名优先；自引用排除")
    from src.linking import build_candidates, link_line

    pages = [
        {"type": "industry", "slug": "行业页", "meta": {"title": "AI芯片电感行业", "aliases": ["电感行业"]}, "body": ""},
        {"type": "concept", "slug": "概念页", "meta": {"title": "AI芯片电感", "aliases": []}, "body": ""},
        {"type": "company", "slug": "公司页", "meta": {"title": "铂科新材", "aliases": ["300811"]}, "body": ""},
    ]
    cands = build_candidates(pages)
    n2t = {wiki_norm(d): k for d, k in cands}
    # 长名优先：句首的「AI芯片电感行业」整体链行业，后文独立的「AI芯片电感」链概念
    ln, n = link_line("AI芯片电感行业景气上行，利好 AI芯片电感 与铂科新材。", cands, n2t)
    check("插了三处", n, 3)
    check("长名优先", "[[AI芯片电感行业]]景气上行" in ln, True)
    check("独立出现的短名照链", "利好 [[AI芯片电感]]" in ln, True)
    check("正文原词保留", "[[铂科新材]]" in ln, True)
    # 别名链（文字不变）
    ln2, n2 = link_line("300811 份额提升", cands, n2t)
    check("别名也链", (n2, ln2), (1, "[[300811]] 份额提升"))
    # 锚点保护区
    ln3, n3 = link_line("提到铂科新材 [[报告.pdf#p2]]", cands, n2t)
    check("锚点原样", "[[报告.pdf#p2]]" in ln3, True)
    check("锚点外才插", ln3, "提到[[铂科新材]] [[报告.pdf#p2]]")
    # 已有双链不重复插
    ln4, n4 = link_line("已有 [[铂科新材]] 的行", cands, n2t)
    check("已有双链不动", (n4, ln4), (0, "已有 [[铂科新材]] 的行"))
    # 同一目标一行只链首次
    ln5, n5 = link_line("铂科新材和铂科新材的子公司", cands, n2t)
    check("一行一目标只链首次", (n5, ln5.count("[[铂科新材]]")), (1, 1))
    # 行里已有指向该目标的双链时，同目标的其他名字也不插（一遍幂等）
    ln5b, n5b = link_line("对标[[铂科新材]]和 300811", cands, n2t)
    check("同目标已链不再插", (n5b, ln5b), (0, "对标[[铂科新材]]和 300811"))
    # ASCII 数字边界：别名不吃进更长的数字串
    ln6, n6 = link_line("营收 13008112 元", cands, n2t)
    check("数字串内不链", n6, 0)
    # 自己名字的出现处是保护区：自己标题里的子串不链给别人
    ln7, n7 = link_line("AI芯片电感是三层电源的核心器件", cands, n2t,
                        protected_extra=[(0, 6)])
    check("保护区内不链", n7, 0)
    # 空行/短于 2 字的名字不进候选（build_candidates 侧保证）
    check("候选非空", len(cands) >= 3, True)


# ── 全库补链 ──────────────────────────────────────────────────────────────
def test_cross_link_all_end_to_end():
    print("cross_link_all：真实页面互链、自链排除、跨页重名跳过、留痕、幂等")
    from src import config, validate, wiki
    from src.linking import cross_link_all

    _page("铂科", "company", body="- 行业景气带动 [[铂科]] 自身与 300811 的份额提升。\n", aliases=("300811",))
    _page("行业页", "industry", body="- 行业格局里铂科新材份额 7.6% [[报告.pdf#p1]]。\n- TLVR电感放量。\n")
    _page("TLVR电感", "concept", body="- 铂科新材深度绑定。\n")
    _page("甲乙页", "company", body="- 一条。\n", aliases=("重名别名",))
    _page("丙丁页", "company", body="- 一条。\n", aliases=("重名别名",))

    st = cross_link_all()
    check("有插入", st["inserts"] >= 3, True)
    # 行业页 → 公司页（标题「铂科」在「铂科新材」里逐字出现）与概念页
    meta, body = wiki.parse_page(config.TYPE_DIRS["industry"] / "行业页.md")
    check("行业页链到公司", "[[铂科]]新材" in body, True)
    check("行业页链到概念", "[[TLVR电感]]" in body, True)
    check("锚点纹丝不动", "[[报告.pdf#p1]]" in body, True)
    check("history 留痕", any("自动补双链" in h for h in meta.get("history") or []), True)
    # 铂科页：正文 [[铂科]] 是自引用不链；300811 是自己的别名也不链 → 整页零改动
    _, body_pk = wiki.parse_page(config.TYPE_DIRS["company"] / "铂科.md")
    check("自引用与自别名不链", body_pk.count("[[") == 1 and "300811]]" not in body_pk, True)
    # 跨页重名的别名谁都不链
    _, body_a = wiki.parse_page(config.TYPE_DIRS["company"] / "甲乙页.md")
    _, body_b = wiki.parse_page(config.TYPE_DIRS["company"] / "丙丁页.md")
    check("重名别名不链", "重名别名]]" not in body_a + body_b, True)
    # 幂等：再跑一遍 0 改动
    st2 = cross_link_all()
    check("第二次幂等", st2["inserts"], 0)
    # 补出的链全部可解析，validate 无死链
    wiki.rebuild_index_md(wiki.load_pages())
    check("validate 无死链", [p for p in validate.validate_all() if "指向不存在的页面" in p], [])
    check("log.md 有 link-cross", "link-cross" in config.LOG_MD.read_text(encoding="utf-8"), True)


def test_ingest_finish_triggers_linking():
    print("ingest.finish：摄入收尾自动补链")
    import json as _json

    from src import config, ingest, wiki

    # 造两页：行业页正文提到公司名（无双链），摄入收尾后应自动连上
    _page("某公司", "company", body="- 一条。\n")
    _page("某行业", "industry", body="- 某公司份额领先 [[b.pdf#p2]]。\n")
    config.MANIFEST_PATH.write_text(_json.dumps({"files": []}, ensure_ascii=False), encoding="utf-8")
    ingest.finish(ingest.load_manifest(), [])
    _, body = wiki.parse_page(config.TYPE_DIRS["industry"] / "某行业.md")
    check("摄入收尾补上双链", "[[某公司]]" in body, True)


def test_source_pages_untouched():
    print("cross_link_all：来源摘要页不加双链（贡献区本就是链接）")
    from src import config, wiki
    from src.linking import cross_link_all

    _page("TLVR电感", "concept", body="- 一条。\n")
    _page("铂科", "company", body="- 一条提到 TLVR电感。\n")
    _page("来源甲", "source", body="- 提到铂科与 TLVR电感。\n")
    cross_link_all()
    _, body_src = wiki.parse_page(config.TYPE_DIRS["source"] / "来源甲.md")
    check("来源页未被改写", "[[" not in body_src, True)
    _, body_pk = wiki.parse_page(config.TYPE_DIRS["company"] / "铂科.md")
    check("非来源页正常补链", "[[TLVR电感]]" in body_pk, True)


def test_dry_run_no_write():
    print("cross_link_all：dry-run 只预览不写盘")
    from src import config, wiki
    from src.linking import cross_link_all

    _page("公司甲", "company", body="- 提到公司乙。\n")
    _page("公司乙", "company", body="- 一条。\n")
    st = cross_link_all(dry_run=True)
    check("预览到插入", st["inserts"] >= 1, True)
    check("带样例", len(st["samples"]) >= 1, True)
    _, body = wiki.parse_page(config.TYPE_DIRS["company"] / "公司甲.md")
    check("盘上未写", "[[公司乙]]" not in body, True)


if __name__ == "__main__":
    run_tests(globals(), [
        "test_link_line_basic_rules",
        "test_cross_link_all_end_to_end",
        "test_ingest_finish_triggers_linking",
        "test_source_pages_untouched",
        "test_dry_run_no_write",
    ])
