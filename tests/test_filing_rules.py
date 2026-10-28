"""财报补充资料取数与【管理层】行格式校验自测。用法：python tests/test_filing_rules.py"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import filing, validate  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def rules(text, pages=(219,)):
    meta = {"sections": {"cf_supp": list(pages)}}
    units = [{"n": 219, "text": text}]
    return filing._supp_net_profit(units, meta, "千元")


def test_supp_net_profit():
    print("[补充资料起点净利润]")
    check("本期列取第一个数", rules("净利润 33,760,758 41,587,940"), 33_760_758_000.0)
    check("括号负数", rules("净利润 (1,234) 2,345"), -1_234_000.0)
    check("跳过归母行", rules("归属于母公司股东的净利润 9,999\n净利润 100 200"), 100_000.0)
    check("净利润率不误认", rules("净利润率 12.5%"), None)
    check("无补充资料页跳过", filing._supp_net_profit(
        [{"n": 1, "text": "净利润 100 200"}], {"sections": {"cf_supp": []}}, "千元"), None)
    check("单位未识别跳过", filing._supp_net_profit(
        [{"n": 219, "text": "净利润 100 200"}], {"sections": {"cf_supp": [219]}}, None), None)


def line_problems(ln):
    return len(validate._line_format_problems("测试页.md", ln))


def test_management_lines():
    print("[管理层行格式]")
    good = "【管理层】「本公司经营稳定」——经营说明 [[a.pdf#p3]]"
    check("完整行通过", line_problems(good), 0)
    check("引述内含」仍通过", line_problems("【管理层】「他说「好」」——风险提示 [[a.pdf#p3]]"), 0)
    check("缺「开头", line_problems("【管理层】本公司经营稳定——经营说明 [[a.pdf#p3]]"), 1)
    check("引述未闭合", line_problems("【管理层】「本公司经营稳定——经营说明 [[a.pdf#p3]]"), 1)
    check("缺语境分隔", line_problems("【管理层】「本公司经营稳定」经营说明 [[a.pdf#p3]]"), 1)
    check("缺锚点", line_problems("【管理层】「本公司经营稳定」——经营说明"), 1)
    check("锚点格式不合法", line_problems("【管理层】「本公司经营稳定」——经营说明 [[a.txt#p3]]"), 1)
    check("缺语境", line_problems("【管理层】「本公司经营稳定」—— [[a.pdf#p3]]"), 1)
    check("锚点后有多余文字", line_problems("【管理层】「本公司经营稳定」——经营说明 [[a.pdf#p3]] 注"), 1)
    check("数据行校验不受影响", line_problems("【数据】2025FY 合并 营业收入 = abc"), 1)


if __name__ == "__main__":
    test_supp_net_profit()
    test_management_lines()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {'、'.join(FAILS)}")
        sys.exit(1)
    print("ALL PASS")
