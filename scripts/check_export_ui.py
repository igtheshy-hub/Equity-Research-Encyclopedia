"""临时验证：造一份 data/fin 数据 → 检查导出页行数/列选择/实际导出。

跑完自动清理，不留数据。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402
from src.web.app import app  # noqa: E402

# 1. 造临时数据目录
tmp = Path(tempfile.mkdtemp())
config.DATA_DIR = tmp / "data"
config.FIN_DIR = config.DATA_DIR / "fin"
config.MANIFEST_PATH = config.DATA_DIR / "manifest.json"
config.WIKI_DIR = tmp / "wiki"
config.TYPE_DIRS = {
    "company": config.WIKI_DIR / "companies",
    "industry": config.WIKI_DIR / "industries",
    "concept": config.WIKI_DIR / "concepts",
    "source": config.WIKI_DIR / "sources",
}
config.FIN_DIR.mkdir(parents=True, exist_ok=True)
for d in config.TYPE_DIRS.values():
    d.mkdir(parents=True, exist_ok=True)

(config.FIN_DIR / "测试公司.json").write_text(json.dumps({
    "slug": "测试公司",
    "company": "测试公司",
    "aliases": ["000001"],
    "periods": {
        "2025FY": {
            "period_end": "2025-12-31",
            "audit_opinion": "标准无保留意见",
            "source": "raw/filings/测试公司/测试公司财报.pdf",
            "items": {
                "营业收入": {"v": 12345600000, "a": ["[[测试公司财报.pdf#p3]]"], "statement": "利润表"},
                "净息差": {"v": 1.42, "a": ["[[测试公司财报.pdf#p5]]"], "statement": "利润表"},
            },
            "items_parent": {},
            "quotes": [],
            "indicators": {
                "净利率": {"v": 0.08, "a": ["[[测试公司财报.pdf#p6]]"]},
                "流动比率": {"v": 0.793, "a": ["[[测试公司财报.pdf#p7]]"]},
            },
        }
    },
}, ensure_ascii=False), encoding="utf-8")

(config.MANIFEST_PATH).write_text(json.dumps({"files": [{
    "file": "测试公司财报.pdf", "source_type": "filing", "report_date": "2025-12-31",
    "entries_count": 2, "topics_touched": ["测试公司"], "path": "raw/filings/测试公司/测试公司财报.pdf",
    "ingested_at": "2026-01-01T00:00:00",
}]}, ensure_ascii=False), encoding="utf-8")

# 2. 检查导出页
app.config.update(TESTING=True)
with app.test_client() as c:
    r = c.get("/export")
    body = r.get_data(as_text=True)
    print("/export ->", r.status_code, len(body), "bytes")
    import re
    print("  行数徽章:", re.findall(r'class="exp-rows">(\d+) 行', body))
    print("  公司 chip:", re.findall(r'name="companies" value="([^"]+)"', body))
    print("  报告期 chip:", re.findall(r'name="periods" value="([^"]+)"', body))

    # 3. POST 导出（只勾 2 张表 + 只勾部分列）
    r2 = c.post("/export", data={
        "sheets": ["items", "indicators"],
        "cols_items": ["company", "item", "value_yi"],
        "cols_indicators": ["company", "indicator", "value", "unit"],
    })
    print("/export POST ->", r2.status_code,
          r2.headers.get("Content-Type", "")[:60],
          len(r2.get_data()), "bytes")

    # 4. 用 openpyxl 读回来核对
    from openpyxl import load_workbook
    import io
    wb = load_workbook(io.BytesIO(r2.get_data()))
    print("  sheets:", wb.sheetnames)
    for name in wb.sheetnames:
        ws = wb[name]
        hdrs = [c.value for c in ws[1]]
        print(f"  [{name}] {ws.max_row} 行, 表头: {hdrs}")
        for row in ws.iter_rows(min_row=2, values_only=True):
            print("     ", row)

    # 5. 全部列 / 全表
    r3 = c.post("/export", data={"sheets": ["items", "indicators", "entries", "sources"]})
    wb3 = load_workbook(io.BytesIO(r3.get_data()))
    print("  全量 sheets:", wb3.sheetnames)
    for name in wb3.sheetnames:
        print(f"  [{name}] {wb3[name].max_row} 行")

print("\n临时目录（自动清理）:", tmp)
