"""strip_glitch 自测：乱码段剔除、锚点与表格线不误伤。用法：python tests/test_strip_glitch.py"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ask import strip_glitch  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r}")
    if not ok:
        FAILS.append(label)


def test_real_glitch():
    print("实测乱码样本被剔除：")
    text = '基于<<+?"98?).CF"3-!#%1>\'=)%@&比亚迪2025年财报能够反应出有什么不好的地方吗\n\n正文开始。'
    check("剔除乱码", strip_glitch(text), "基于比亚迪2025年财报能够反应出有什么不好的地方吗\n\n正文开始。")


def test_anchor_kept():
    print("锚点不受影响：")
    text = "毛利率 0.1774 [[比亚迪财报_20260300.pdf#p126]]，纯 ASCII 文件名 [[report_final.pdf#p11]] 同样保留。"
    check("中文锚点保留", strip_glitch(text), text)


def test_table_kept():
    print("markdown 表格线不受影响：")
    text = "| 指标 | 数值 |\n|------|------|\n| ROE | 15.31% |"
    check("表格线保留", strip_glitch(text), text)


def test_clean_text():
    print("正常文本原样返回：")
    for t in ["", "DSO 16.80 天，低于 90 天阈值。", "803,964,958,000 元与 ROE(加权披露)@2025FY = 0.1531"]:
        check(f"不变 {t[:20]!r}", strip_glitch(t), t)


if __name__ == "__main__":
    test_real_glitch()
    test_anchor_kept()
    test_table_kept()
    test_clean_text()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {'、'.join(FAILS)}")
        sys.exit(1)
    print("ALL PASS")
