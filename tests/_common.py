"""测试公共件：真断言 + 测试自造的沙盒数据。

对应问题 7「自带的测试是假绿灯，跑起来可能把真数据删了」：

- `check()` 不一致时 **抛 AssertionError**，pytest 会亮红灯。
  原来的写法只往 FAILS 里塞字符串，pytest 永远看不到失败，永远全绿。
- 沙盒数据由本模块 **现造**（不是复制仓库里的真实 raw/ wiki/ data/），
  所以断言里的条数、页数不再写死母项目当前有多少文件；
  多摄入一份研报也不会让测试报错。
- `redirect_config()` 把 config 的全部路径指到临时目录，pytest 下由
  `conftest.py` 的 autouse fixture 自动执行，任何用例都不可能碰到真实数据。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


# ── 断言 ────────────────────────────────────────────────────────────────────
def check(label, got, want, tol=1e-9):
    """比对并断言。不一致 → 抛 AssertionError（pytest 红灯）。"""
    if (isinstance(got, (int, float)) and not isinstance(got, bool)
            and isinstance(want, (int, float)) and not isinstance(want, bool)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)
        raise AssertionError(f"{label}: got={got!r} want={want!r}")


def run_tests(namespace: dict, names) -> None:
    """独立运行（python tests/xxx.py）：逐个跑，一个失败不中断，最后统一给退出码。"""
    failed = 0
    for name in names:
        fn = namespace.get(name)
        if fn is None:
            continue
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {name}: 未预期的异常 {type(e).__name__}: {e}")
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {'、'.join(FAILS)}")
    if failed or FAILS:
        sys.exit(1)
    print("ALL PASS")


# ── 沙盒 ────────────────────────────────────────────────────────────────────
def redirect_config(root: Path) -> Path:
    """把 config 的所有路径指到 root（临时目录）。返回 root。"""
    from src import config

    root = Path(root)
    config.ROOT = root
    config.RAW_DIR = root / "raw"
    config.INBOX_DIR = config.RAW_DIR / "inbox"
    config.REPORTS_DIR = config.RAW_DIR / "reports"
    config.FILINGS_DIR = config.RAW_DIR / "filings"
    config.POLICIES_DIR = config.RAW_DIR / "policies"
    config.WIKI_DIR = root / "wiki"
    config.INDEX_MD = config.WIKI_DIR / "index.md"
    config.LOG_MD = config.WIKI_DIR / "log.md"
    config.DATA_DIR = root / "data"
    config.EXTRACTS_DIR = config.DATA_DIR / "extracts"
    config.FIN_DIR = config.DATA_DIR / "fin"
    config.REPORT_JOBS_DIR = config.DATA_DIR / "report_jobs"
    config.MANIFEST_PATH = config.DATA_DIR / "manifest.json"
    config.LLM_LOG_PATH = config.DATA_DIR / "llm_log.jsonl"
    config.OUTPUTS_DIR = root / "outputs"
    config.TYPE_DIRS = {
        "company": config.WIKI_DIR / "companies",
        "industry": config.WIKI_DIR / "industries",
        "concept": config.WIKI_DIR / "concepts",
        "source": config.WIKI_DIR / "sources",
    }
    for d in (config.INBOX_DIR, config.REPORTS_DIR, config.FILINGS_DIR, config.POLICIES_DIR,
              *config.TYPE_DIRS.values(), config.OUTPUTS_DIR, config.EXTRACTS_DIR, config.FIN_DIR,
              config.REPORT_JOBS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    return root


# 测试用的固定素材名
REPORT_NAME = "测试研报_20260101.pdf"
FILING_NAME = "测试公司财报_20260101.pdf"
REPORT_REL = f"raw/reports/汽车/{REPORT_NAME}"
FILING_REL = f"raw/filings/测试公司/{FILING_NAME}"


def api_client(app, csrf: bool = True):
    """返回 Flask 测试客户端。

    问题 1 的 CSRF 防线要求所有"会改状态"的接口带 X-Requested-With: wiki
    （跨站表单发不出自定义头）。真实网页由 app.js 统一注入这个头，
    测试里用这个 helper 保持一致。

    csrf=False 时**不**注入该头——用来验证守卫本身确实会拒跨站请求。
    """
    c = app.test_client()
    if not csrf:
        return c

    def _with_header(orig):
        def call(*args, **kwargs):
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-Requested-With", "wiki")
            kwargs["headers"] = headers
            return orig(*args, **kwargs)
        return call

    # post / put / patch / delete 都要带（都是"会改状态"的请求）
    c.post = _with_header(c.post)
    c.put = _with_header(c.put)
    c.patch = _with_header(c.patch)
    c.delete = _with_header(c.delete)
    return c


def _page(title, type_, summary, sources, body):
    fm = {
        "title": title, "type": type_, "aliases": [], "summary": summary,
        "sources": sources, "updated": "2026-10-28", "history": [],
    }
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines += [f"- {x}" for x in v]
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def build_sandbox(base: Path | None = None) -> Path:
    """造一套测试自用的最小知识库（两份材料 + 三个知识页 + 清单 + 财报数据 + 缓存）。

    刻意不引用仓库里的真实数据：条数、页数都是这里写死的，
    母项目里多一份少一份研报都不会影响测试。
    """
    from src import wiki
    from mock_pdf import build_pdf

    tmp = Path(base or tempfile.mkdtemp(prefix="ere-test-"))
    redirect_config(tmp)
    from src import config

    # 1) 两份原始材料（mock PDF，文本用 ASCII）
    (config.REPORTS_DIR / "汽车").mkdir(parents=True, exist_ok=True)
    (config.FILINGS_DIR / "测试公司").mkdir(parents=True, exist_ok=True)
    (config.REPORTS_DIR / "汽车" / REPORT_NAME).write_bytes(build_pdf([
        ["Test Research Report", "Auto Industry Outlook 2026", "Date: 2026-01-01"],
        ["Industry volume growth 12% YoY", "Top player share 45%"],
    ]))
    (config.FILINGS_DIR / "测试公司" / FILING_NAME).write_bytes(build_pdf([
        ["Annual Report 2025", "Revenue 10000000000 CNY", "Net profit 800000000 CNY"],
    ]))

    # 2) 知识页：行业页挂 3 条（2 条引研报、1 条引财报），公司页挂 2 条引财报
    industry_body = (
        "# 汽车\n\n## 行业概况\n\n"
        f"- 2026 年行业销量预计增长 12% [[{REPORT_NAME}#p1]]\n"
        f"- 头部厂商份额提升到 45% [[{REPORT_NAME}#p2]]\n"
        f"- 测试公司 2025 年营收 100 亿元 [[{FILING_NAME}#p1]]\n\n"
        "## 风险提示\n\n"
        f"- 价格战风险上升 [[{REPORT_NAME}#p2]]\n"
    )
    company_body = (
        "# 测试公司\n\n## 经营与财务\n\n"
        f"- 2025 年营业收入 100 亿元 [[{FILING_NAME}#p1]]\n"
        f"- 2025 年归母净利润 8 亿元 [[{FILING_NAME}#p1]]\n"
    )
    (config.TYPE_DIRS["industry"] / "汽车.md").write_text(
        _page("汽车", "industry", "测试用行业页", [REPORT_REL, FILING_REL], industry_body),
        encoding="utf-8")
    (config.TYPE_DIRS["company"] / "测试公司.md").write_text(
        _page("测试公司", "company", "测试用公司页", [FILING_REL], company_body),
        encoding="utf-8")
    (config.TYPE_DIRS["source"] / f"{Path(REPORT_NAME).stem}.md").write_text(
        _page(f"{Path(REPORT_NAME).stem}", "source", "测试研报摘要", [REPORT_REL],
              f"# {Path(REPORT_NAME).stem}\n\n## 来源摘要\n\n"
              f"- 研报首行：Test Research Report [[{REPORT_NAME}#p1]]\n"),
        encoding="utf-8")
    (config.TYPE_DIRS["source"] / f"{Path(FILING_NAME).stem}.md").write_text(
        _page(f"{Path(FILING_NAME).stem}", "source", "测试财报摘要", [FILING_REL],
              f"# {Path(FILING_NAME).stem}\n\n## 来源摘要\n\n"
              f"- 财报首行：Annual Report 2025 [[{FILING_NAME}#p1]]\n"),
        encoding="utf-8")

    # 3) 清单
    config.MANIFEST_PATH.write_text(json.dumps({"files": [
        {"file": REPORT_NAME, "path": REPORT_REL, "sha256": "a" * 64,
         "source_type": "report", "report_date": "2026-01-01", "entries_count": 3,
         "topics_touched": ["汽车"], "ingested_at": "2026-10-28T10:00:00",
         "unit": "page", "count": 2},
        {"file": FILING_NAME, "path": FILING_REL, "sha256": "b" * 64,
         "source_type": "filing", "report_date": "2026-01-01", "entries_count": 3,
         "topics_touched": ["测试公司"], "ingested_at": "2026-10-28T10:00:00",
         "unit": "page", "count": 1},
    ]}, ensure_ascii=False, indent=1), encoding="utf-8")

    # 4) 财报结构化数据（期间 source 要等于原文件相对路径，删除时才认得出）
    config.FIN_DIR.mkdir(parents=True, exist_ok=True)
    (config.FIN_DIR / "测试公司.json").write_text(json.dumps({
        "company": "测试公司", "slug": "测试公司", "aliases": ["999888"],
        "periods": {"2025FY": {
            "period_end": "2025-12-31", "audit_opinion": "标准无保留意见",
            "source": FILING_REL,
            "items": {"营业收入": {"v": 10000000000,
                                   "a": [f"[[{FILING_NAME}#p1]]"], "statement": "利润表"}},
            "items_parent": {},
            # 四种单位各来一个：百分比 / 倍数 / 天数 / 金额（导出时不能一律 ×100）
            "indicators": {
                "净利率": {"v": 0.08, "a": [f"[[{FILING_NAME}#p1]]"]},
                "流动比率": {"v": 0.793, "a": [f"[[{FILING_NAME}#p1]]"]},
                "DSO(期末)": {"v": 16.8, "a": [f"[[{FILING_NAME}#p1]]"]},
                "FCF(标准)": {"v": -97672309000, "a": [f"[[{FILING_NAME}#p1]]"]},
            },
        }},
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    # 5) 提取缓存（删除时要一并清掉；units 缓存带上真实原文，供锚点抽查比对）
    config.EXTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    (config.EXTRACTS_DIR / (Path(REPORT_NAME).stem + ".units.json")).write_text(json.dumps({
        "file": REPORT_NAME, "unit": "page", "count": 2, "scanned": False,
        "units": [
            {"n": 1, "text": "Test Research Report Auto Industry Outlook 2026 Date 2026-01-01"},
            {"n": 2, "text": "Industry volume growth 12% YoY Top player share 45%"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    for stem, suffix in ((Path(REPORT_NAME).stem, ".entries.json"),
                         (Path(FILING_NAME).stem, ".filing.json")):
        (config.EXTRACTS_DIR / (stem + suffix)).write_text("{}", encoding="utf-8")

    # 6) 索引与日志（validate 会查页面是否收录进 index.md）
    wiki.rebuild_index_md(wiki.load_pages())
    config.LOG_MD.write_text("| 日期 | 操作 | 摘要 |\n", encoding="utf-8")
    return tmp
