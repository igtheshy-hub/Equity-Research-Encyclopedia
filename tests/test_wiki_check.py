"""共享校验层自测：好页零问题，坏页逐条命中（不碰真实 wiki/raw）。

用法：python tests/test_wiki_check.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import wiki_check  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def problems(meta, body, info, titles, expected="industry"):
    return wiki_check.content_problems("t.md", meta, body, expected_type=expected,
                                       info=info, titles=titles)


BASE_META = {"title": "测试页", "type": "industry", "sources": ["x.pdf"], "updated": "2026-10-28"}
BASE_BODY = (
    "- 行业销量同比增长 3% [[x.pdf#p5]]\n"
    "- 【指标】2024FY 销量 = 100 万辆 [[x.pdf#p5]]\n"
    "- 【个人观点】判断句 [[x.pdf#p5]]（个人研报观点，2026-10-28，report-x.md）"
)
INFO = {"x.pdf": ("page", 10)}
TITLES = {"测试页"}


def main():
    print("== 好页 ==")
    check("好页无问题", problems(BASE_META, BASE_BODY, INFO, TITLES), [])

    print("== front matter ==")
    meta = dict(BASE_META)
    meta.pop("title")
    got = problems(meta, "- 一句判断 [[x.pdf#p5]]", INFO, TITLES)
    check("缺 title 命中", any("缺少必填 front matter 字段 title" in x for x in got), True)
    got = problems(BASE_META, "- 一句判断 [[x.pdf#p5]]", INFO, TITLES, expected="company")
    check("type 与目录不一致命中", any("与所在目录应为" in x for x in got), True)

    print("== 结构化行 ==")
    body = "- 【数据】2024FY 销量 100 万辆 [[x.pdf#p5]]"
    got = problems(BASE_META, body, INFO, TITLES)
    check("数据行缺等号命中", any("缺等号" in x for x in got), True)
    body = "- 【指标】2024FY 销量 = 约一百万辆 [[x.pdf#p5]]"
    got = problems(BASE_META, body, INFO, TITLES)
    check("指标行数值不可解析命中", any("数值不可解析" in x for x in got), True)
    body = "- 【个人观点】没有锚点的判断句"
    got = problems(BASE_META, body, INFO, TITLES)
    check("个人观点缺锚点命中", any("【个人观点】行缺锚点" in x for x in got), True)
    body = "- 【观点】2024FY 没有锚点的观点"
    got = problems(BASE_META, body, INFO, TITLES)
    check("观点行缺锚点命中", any("【观点】行缺锚点" in x for x in got), True)

    print("== 数字锚点 ==")
    orig_paths, orig_cached = wiki_check.file_paths, wiki_check.load_cached
    wiki_check.file_paths = lambda: {"x.pdf": ROOT / "x.pdf"}
    wiki_check.load_cached = lambda _p: {
        "unit": "page", "count": 10,
        "units": [{"n": 5, "text": "营业收入 100 亿元"}],
    }
    try:
        got = problems(BASE_META, "- 营收 200 亿元 [[x.pdf#p5]]", INFO, TITLES)
        check("数字不在锚点页命中", any("找不到数字" in x for x in got), True)
        got = problems(BASE_META, "- 【观点】2024FY 净利率 2.48%，高于去年 [[x.pdf#p5]]", INFO, TITLES)
        check("观点行计算数字不误报", any("找不到数字" in x for x in got), False)
    finally:
        wiki_check.file_paths, wiki_check.load_cached = orig_paths, orig_cached

    print("== 锚点与 wikilink ==")
    got = problems(BASE_META, "- 一句判断 [[y.pdf#p1]]", INFO, TITLES)
    check("锚点文件不存在命中", any("raw 中不存在" in x for x in got), True)
    got = problems(BASE_META, "- 一句判断 [[x.pdf#p11]]", INFO, TITLES)
    check("锚点序号越界命中", any("序号越界" in x for x in got), True)
    got = problems(BASE_META, "- 一句判断 [[x.pdf#para3]]", INFO, TITLES)
    check("锚点单元类型不对命中", any("单元类型不对" in x for x in got), True)
    got = problems(BASE_META, "- 一句判断 [[x.pdf#p5]]，另见 [[不存在的页]]", INFO, TITLES)
    check("wikilink 指向不存在页命中", any("wikilink 指向不存在的页面" in x for x in got), True)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项未通过：{'、'.join(FAILS)}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
