"""src/validate.py 的测试 —— 全库 lint（项目自己的质量门）。

validate_all() 有 8 条检测规则。之前一条都没被测过，
等于这道门可能早就坏了（改了规则、坏了实现，CI 也不会红）。
这里把每条规则都造一个"应该被抓到"的样本，再造一个"不该误报"的样本。
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


def _write_page(type_, slug, meta, body, *, in_index=True):
    """在沙盒里造一页知识页。"""
    from src import config, wiki

    d = config.TYPE_DIRS[type_]
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{slug}.md"
    wiki.write_page(path, meta, body)
    if in_index:
        with open(config.INDEX_MD, "a", encoding="utf-8") as f:
            f.write(f"- [{slug}]({type_}/{slug}.md)\n")
    return path


def _clear_pages():
    from src import config, wiki
    for d in config.TYPE_DIRS.values():
        for p in d.glob("*.md"):
            p.unlink()


def _problems():
    from src.validate import validate_all
    return validate_all()


def _has(problems, needle):
    return any(needle in p for p in problems)


def _has_anchor_problem(problems):
    """只认锚点类的三条规则（别被页名里的字眼误伤）。"""
    return _has(problems, "raw 中不存在的文件") \
        or _has(problems, "单元类型不对") \
        or _has(problems, "序号越界")


# ── 规则 1：缺必填 front matter 字段 ───────────────────────────────────────
def test_missing_required_fields():
    """title / type / sources / updated 缺一个就要报。"""
    print("lint：缺必填 front matter 字段")
    _clear_pages()
    from src import config
    config.INDEX_MD.write_text("", encoding="utf-8")

    for field in ("title", "type", "sources", "updated"):
        _clear_pages()
        meta = {"title": "T", "type": "company", "sources": ["raw/a.pdf"], "updated": "2026-01-01"}
        meta.pop(field)
        _write_page("company", "缺字段", meta, "# 缺字段\n", in_index=False)
        probs = _problems()
        check(f"缺 {field} 被抓到", _has(probs, f"缺少必填 front matter 字段 {field}"), True)


def test_complete_page_passes():
    """字段齐全的页不该报错。"""
    print("lint：字段齐全不报错")
    _clear_pages()
    from src import config
    config.INDEX_MD.write_text("", encoding="utf-8")

    _write_page("company", "完整页",
                {"title": "完整页", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 完整页\n\n## 公司概况\n\n- 一条知识。[[a.pdf#p1]]\n",
                in_index=False)
    probs = _problems()
    check("没报缺字段", _has(probs, "缺少必填 front matter 字段"), False)


# ── 规则 2：type 与所在目录不一致 ─────────────────────────────────────────
def test_type_mismatch():
    """front matter 的 type 与所在目录不符要报。"""
    print("lint：type 与目录不一致")
    _clear_pages()
    from src import config
    config.INDEX_MD.write_text("", encoding="utf-8")

    _write_page("company", "类型不符",
                {"title": "类型不符", "type": "industry", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 类型不符\n", in_index=False)
    probs = _problems()
    check("抓到 type 不一致", _has(probs, "与所在目录"), True)


# ── 规则 3-5：锚点三类问题 ────────────────────────────────────────────────
def test_anchor_missing_file():
    """锚点指向 raw 里不存在的文件要报。"""
    print("lint：锚点引用不存在的文件")
    _clear_pages()
    from src import config
    config.INDEX_MD.write_text("", encoding="utf-8")

    _write_page("company", "锚点缺文件",
                {"title": "锚点缺文件", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 锚点缺文件\n\n- 知识。[[不存在.pdf#p1]]\n", in_index=False)
    probs = _problems()
    check("抓到不存在的文件", _has(probs, "raw 中不存在的文件"), True)


def test_anchor_unit_kind_mismatch():
    """PDF 锚点写 paraN（或反之）要报。"""
    print("lint：锚点单元类型不对")
    _clear_pages()
    from src import config
    config.INDEX_MD.write_text("", encoding="utf-8")
    from src.ingest import save_manifest
    save_manifest({"files": [{"file": "a.pdf", "sha256": "x", "unit": "page", "count": 5}]})

    _write_page("company", "锚点类型错",
                {"title": "锚点类型错", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 锚点类型错\n\n- 知识。[[a.pdf#para2]]\n", in_index=False)
    probs = _problems()
    check("抓到单元类型不对", _has(probs, "单元类型不对"), True)


def test_anchor_out_of_range():
    """锚点序号超出该文件单元数要报。"""
    print("lint：锚点序号越界")
    _clear_pages()
    from src import config
    config.INDEX_MD.write_text("", encoding="utf-8")
    from src.ingest import save_manifest
    save_manifest({"files": [{"file": "a.pdf", "sha256": "x", "unit": "page", "count": 5}]})

    _write_page("company", "锚点越界",
                {"title": "锚点越界", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 锚点越界\n\n- 知识。[[a.pdf#p99]]\n", in_index=False)
    probs = _problems()
    check("抓到序号越界", _has(probs, "序号越界"), True)


def test_anchor_in_range_ok():
    """范围内的锚点不该报错。"""
    print("lint：合法锚点不报错")
    _clear_pages()
    from src import config
    config.INDEX_MD.write_text("", encoding="utf-8")
    from src.ingest import save_manifest
    save_manifest({"files": [{"file": "a.pdf", "sha256": "x", "unit": "page", "count": 5}]})

    _write_page("company", "合法引用页",
                {"title": "合法引用页", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 合法引用页\n\n- 知识。[[a.pdf#p3]]\n", in_index=False)
    probs = _problems()
    check("没报锚点问题", _has_anchor_problem(probs), False)


# ── 规则 6：wikilink 指向不存在的页面 ─────────────────────────────────────
def test_dangling_wikilink():
    """[[页面标题]] 指向不存在的页面要报。"""
    print("lint：wikilink 指向不存在的页")
    _clear_pages()
    from src import config
    config.INDEX_MD.write_text("", encoding="utf-8")

    _write_page("company", "死链页",
                {"title": "死链页", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 死链页\n\n- 见 [[根本不存在的页面]]。[[a.pdf#p1]]\n", in_index=False)
    probs = _problems()
    check("抓到死链", _has(probs, "wikilink 指向不存在的页面"), True)


def test_valid_wikilink_ok():
    """指向真实页面的 wikilink 不该报错。"""
    print("lint：合法 wikilink 不报错")
    _clear_pages()
    from src import config
    config.INDEX_MD.write_text("", encoding="utf-8")

    _write_page("company", "甲页",
                {"title": "甲页", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 甲页\n", in_index=False)
    _write_page("company", "乙页",
                {"title": "乙页", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 乙页\n\n- 见 [[甲页]]。[[a.pdf#p1]]\n", in_index=False)
    probs = _problems()
    check("没报死链", _has(probs, "wikilink 指向不存在的页面"), False)


# ── 规则 7：未收录进 index.md ─────────────────────────────────────────────
def test_not_in_index():
    """没写进 index.md 的页要报。"""
    print("lint：未收录进 index.md")
    _clear_pages()
    from src import config
    config.INDEX_MD.write_text("", encoding="utf-8")

    _write_page("company", "漏登记页",
                {"title": "漏登记页", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 漏登记页\n", in_index=False)
    probs = _problems()
    check("抓到未收录", _has(probs, "未收录进 index.md"), True)


def test_in_index_ok():
    """写进 index.md 的页不该报这条。"""
    print("lint：已收录不报错")
    _clear_pages()
    from src import config
    config.INDEX_MD.write_text("", encoding="utf-8")

    _write_page("company", "已登记页",
                {"title": "已登记页", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 已登记页\n", in_index=True)
    probs = _problems()
    check("没报未收录", _has(probs, "未收录进 index.md"), False)


# ── 规则 8：【数据】/【指标】行的格式契约 ─────────────────────────────────
def test_data_line_format():
    """财报页的【数据】/【指标】行必须符合格式契约。"""
    print("lint：财报行格式契约")
    _clear_pages()
    from src import config
    config.INDEX_MD.write_text("", encoding="utf-8")

    # 缺等号
    _write_page("company", "行缺等号",
                {"title": "行缺等号", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 行缺等号\n\n- 【数据】2025FY 合并 营业收入 100 元\n", in_index=False)
    check("抓到缺等号", _has(_problems(), "缺等号"), True)

    # 数值不可解析
    _clear_pages()
    _write_page("company", "数值坏",
                {"title": "数值坏", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 数值坏\n\n- 【数据】2025FY 合并 营业收入 = 一百元\n", in_index=False)
    check("抓到数值不可解析", _has(_problems(), "数值不可解析"), True)

    # 括号未闭合（口径括号少一个右括号）
    _clear_pages()
    _write_page("company", "括号坏",
                {"title": "括号坏", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 括号坏\n\n- 【指标】2025FY 毛利率(累计 = 17.74\n", in_index=False)
    check("抓到括号未闭合", _has(_problems(), "括号未闭合"), True)


def test_data_line_format_ok():
    """格式正确的财报行不该报错。"""
    print("lint：合规财报行不报错")
    _clear_pages()
    from src import config
    config.INDEX_MD.write_text("", encoding="utf-8")

    _write_page("company", "行合规",
                {"title": "行合规", "type": "company", "sources": ["raw/a.pdf"],
                 "updated": "2026-01-01"},
                "# 行合规\n\n- 【数据】2025FY 合并 营业收入 = 100,000 元\n"
                "- 【指标】2025FY 毛利率(累计) = 17.74\n"
                "- 【观点】2025FY 管理层称稳健\n", in_index=False)
    probs = _problems()
    check("没报行格式问题", _has(probs, "行缺等号"), False)
    check("没报数值问题", _has(probs, "数值不可解析"), False)
    check("没报括号问题", _has(probs, "括号未闭合"), False)


if __name__ == "__main__":
    run_tests(globals(), [
        "test_missing_required_fields",
        "test_complete_page_passes",
        "test_type_mismatch",
        "test_anchor_missing_file",
        "test_anchor_unit_kind_mismatch",
        "test_anchor_out_of_range",
        "test_anchor_in_range_ok",
        "test_dangling_wikilink",
        "test_valid_wikilink_ok",
        "test_not_in_index",
        "test_in_index_ok",
        "test_data_line_format",
        "test_data_line_format_ok",
    ])
