"""Excel 导出自测（对应问题 8：派生指标表永远是空的 + 所有指标都被当成百分比 ×100）。

- 派生指标表原来恒空：存的是 {"v": 数值, "a": [锚点]} 组合体，导出只认纯数字，全被跳过。
- 单位：百分比类要 ×100；天数/倍数/金额类再 ×100 会差 100 倍（45 天变 4500）。
"""
import io
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from src import excel_export as X  # noqa: E402
from _common import check, run_tests, build_sandbox  # noqa: E402


def _rows():
    out = []
    for st in X._load_fin_stores():
        out.extend(X._iter_indicators(st))
    return {r["indicator"]: r for r in out}


def test_indicator_sheet_not_empty():
    print("派生指标表不再为空：")
    build_sandbox()
    rows = _rows()
    check("指标行数不为 0", len(rows), 4)
    check("组合体里的数值被解出来", rows["净利率"]["value"], 8.0)
    check("锚点也带出来了", rows["净利率"]["anchor"].startswith("[["), True)


def test_units_not_all_multiplied_by_100():
    print("单位对了，不再一律 ×100：")
    build_sandbox()
    rows = _rows()
    check("百分比 ×100", rows["净利率"]["value"], 8.0)
    check("百分比单位", rows["净利率"]["unit"], "%")
    check("倍数不乘 100", rows["流动比率"]["value"], 0.793)
    check("倍数单位", rows["流动比率"]["unit"], "倍")
    check("天数不乘 100（45 天不能变 4500）", rows["DSO(期末)"]["value"], 16.8)
    check("天数单位", rows["DSO(期末)"]["unit"], "天")
    check("金额不乘 100", rows["FCF(标准)"]["value"], -97672309000)
    check("金额单位", rows["FCF(标准)"]["unit"], "元")


def test_workbook_has_rows():
    print("工作簿里派生指标表有数据：")
    build_sandbox()
    from openpyxl import load_workbook
    buf = io.BytesIO(X.build_workbook(sheets=["indicators"]))
    wb = load_workbook(buf)
    check("有派生指标表这张 sheet", "派生指标表" in wb.sheetnames, True)
    ws = wb["派生指标表"]
    hdr = [c.value for c in ws[1]]
    check("表头有数值与单位两列", ("数值" in hdr, "单位" in hdr), (True, True))
    check("表头不再叫「数值(%)」", "数值(%)" in hdr, False)
    check("表里有数据行", ws.max_row > 1, True)
    body = {ws.cell(row=r, column=hdr.index("指标") + 1).value:
            (ws.cell(row=r, column=hdr.index("数值") + 1).value,
             ws.cell(row=r, column=hdr.index("单位") + 1).value)
            for r in range(2, ws.max_row + 1)}
    check("表里净利率是 8.0 %", body["净利率"], (8.0, "%"))
    check("表里 DSO 是 16.8 天", body["DSO(期末)"], (16.8, "天"))


if __name__ == "__main__":
    run_tests(globals(), ["test_indicator_sheet_not_empty",
                          "test_units_not_all_multiplied_by_100",
                          "test_workbook_has_rows"])
