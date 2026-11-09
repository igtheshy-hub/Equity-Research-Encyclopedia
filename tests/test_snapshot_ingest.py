"""快照闭环自测：spec → DOCX → 提取缓存 → para 锚点可核验。

用法：python tests/test_snapshot_ingest.py（临时目录，不碰 raw/ 与 data/）
"""
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import config, pdf_extract, snapshot, wiki  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="snapshot-test-"))
    config.EXTRACTS_DIR = tmp / "extracts"
    config.SNAPSHOT_DIR = tmp / "snapshots"
    config.SNAPSHOT_DIR.mkdir()
    out = config.SNAPSHOT_DIR / "国家统计局_乘用车销量快照_20261028.docx"

    spec = {
        "out": str(out),
        "title": "乘用车月度销量快照",
        "org": "国家统计局",
        "url": "https://data.stats.gov.cn/example",
        "fetched_at": "2026-10-28",
        "note": "口径：月度累计，单位万辆",
        "rows": [
            {"指标": "乘用车销量", "数值": "123.4", "单位": "万辆", "口径": "当月", "时点": "2026-09"},
            {"指标": "乘用车销量", "数值": "999.9", "单位": "万辆", "口径": "累计", "时点": "2026-09"},
        ],
    }
    snapshot.build(spec)
    check("快照文件已生成", out.is_file(), True)
    check("文件扩展名", out.suffix, ".docx")

    units, count, scanned, kind = pdf_extract.extract_to_store(out)
    check("按文档块切分", kind, "para")
    check("不是扫描版", scanned, False)
    check("有提取单元", count > 0, True)
    text = "\n".join(u["text"] for u in units)
    check("标题进正文", "乘用车月度销量快照" in text, True)
    check("来源机构进正文", "国家统计局" in text, True)
    check("取数时点进正文", "2026-10-28" in text, True)
    check("表格数值进正文", "123.4" in text and "999.9" in text, True)
    check("口径注释进正文", "月度累计" in text, True)

    cached = pdf_extract.load_cached(out)
    check("提取缓存可读", bool(cached), True)
    check("缓存单元数与提取一致", cached["count"], count)
    loc = f"para{cached['units'][0]['n']}"
    check("para 锚点类型合法", wiki.loc_kind(loc), "para")
    check("para 锚点序号在范围内", 1 <= wiki.loc_num(loc) <= cached["count"], True)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项未通过：{'、'.join(FAILS)}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
