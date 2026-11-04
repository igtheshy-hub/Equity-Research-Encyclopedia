"""出处锚点抽查：把答案句里的关键数字与锚点页原文对一遍，对不上的进审校清单。

对应问题 6「答案里的出处页码可能是编的，系统查不出来」：

原来的校验只查「页码没超出这份文件的页数」——
AI 记混页码、或干脆编个在范围内的页号，校验照样显示「通过」。
这里补一层文本级抽查：答案里最关键的那个数字，在锚点页的原文里找不找得到。

抽查结果是**存疑待复核**，不是直接判失败 —— 页码定位、表格抽取、单位换算
都可能让原文与答案的字面不一致，误杀会伤正常流程。
真正拦得住的是范围校验（见 ask._violations）：锚点必须落在
「本次真的喂给模型看过的那些单元」之内。
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from . import config, wiki

# 句中的数值：支持千分位、小数、百分号
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
# 数值后面跟着的单位词：带单位的数字才值得抽查（年份、小计数不算）
# 顺序有意义：长单位必须排在短单位前面（亿元 在 亿 前面、万股 在 万 前面），否则会截短。
# 只收「数量级明确」的单位；"12 家""50 人"这类小计数不算，避免误伤。
_UNIT_SUFFIX_RE = re.compile(
    r"(-?\d[\d,]*(?:\.\d+)?)\s*"
    r"(亿元|万元|百万元|千元|亿美元|万美元|美元|人民币|元/股|个百分点"
    r"|万辆|万台|万套|万件|万吨|万户|万人|万家|万间|万股|万单"
    r"|亿|万|千|百万|%|pct|倍|天|个月|股)"
)

# 单位换算系数：答案写「亿元」而原文写「元」时要能对上
_SCALES = (1.0, 1e4, 1e6, 1e8, 1e-4, 1e-6, 1e-8)
# 比对容差：相对 0.1%。原值 17.74 与答案 17.7 这种合法舍入靠"取整形态"覆盖，
# 这里只留极小的浮点噪声余量，避免把 45 放进 1450 这种子串误判（见 _page_values）。
_REL_TOL = 1e-3

REVIEW_PATH = None  # 运行时按 config.DATA_DIR 求，测试里会改 config


def _review_path() -> Path:
    return config.DATA_DIR / "anchor_review.json"


def _norm(text) -> str:
    """归一：全角转半角、去千分位逗号、去空白，便于子串比对。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = s.replace(",", "").replace("，", "").replace(" ", "").replace("　", "")
    return s


def _key_numbers(sentence: str) -> list[float]:
    """句里所有值得核的数字（带单位词的），按出现顺序去重。

    原来只挑一个（最长的那个），句里其它数字就漏掉了——编造的数字恰好不是最长的
    就检测不出来。现在全部核。
    """
    out: list[float] = []
    seen: set[float] = set()
    for m in _UNIT_SUFFIX_RE.finditer(sentence):
        raw = m.group(1).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        if v not in seen:
            seen.add(v)
            out.append(v)
    if out:
        return out
    # 没有带单位的数字：退而求其次，核有效位最多的那个
    best, best_digits = None, 0
    for m in _NUM_RE.finditer(sentence):
        raw = m.group(0).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        if abs(v) < 1000:          # 年份、小计数不抽查
            continue
        digits = len(re.sub(r"[^0-9]", "", raw))
        if digits > best_digits:
            best, best_digits = v, digits
    return [best] if best is not None else []


def _page_values(text: str) -> set[float]:
    """把一页原文里的所有数字，展开成「可接受的数值集合」。

    对每个数字 v，再考虑：
      - 各种单位换算（v/1e8、v/1e6、v/1e4 及其倒数）
      - 各种精度的舍入形态（round(v, 0/1/2)）
    于是「原文 803,964,958,000 元」能接受答案里的「8039.65 亿元」，
    「原文 17.74%」能接受答案里的「18%」。
    """
    out: set[float] = set()
    for m in _NUM_RE.finditer(_norm(text)):
        raw = m.group(0)
        try:
            v = float(raw)
        except ValueError:
            continue
        for f in _SCALES:
            x = v / f
            if abs(x) > 1e15:
                continue
            out.add(x)
            out.add(round(x, 2))
            out.add(round(x, 1))
            out.add(round(x, 0))
    return out


def _value_found(num: float, values: set[float]) -> bool:
    """答案里的数字能不能在原文的数值集合里找到。"""
    for v in values:
        if abs(v - num) <= abs(v) * _REL_TOL + 1e-9:
            return True
    return False


def _sentences(text: str):
    """按行拆句：知识条目的锚点都在行尾，一行就是一句。"""
    for raw in str(text or "").splitlines():
        ln = raw.strip().removeprefix("- ").strip()
        if ln:
            yield ln


def unit_text_map(files) -> dict:
    """取这些文件各单元的原文（只取有缓存的），返回 {(文件名, 序号): 原文}。"""
    from .pdf_extract import load_cached, raw_file_index
    out = {}
    index = raw_file_index()
    for name in files:
        path = index.get(name)
        if not path:
            continue
        cached = load_cached(path)
        if not cached:
            continue
        for u in cached.get("units") or []:
            out[(name, u.get("n"))] = u.get("text") or ""
    return out


def check(answer: str, texts: dict | None = None) -> list[dict]:
    """抽查答案里的锚点。返回存疑清单 [{file, loc, sentence, number, reason}]。

    比对方式是**数值比对**（含单位换算与舍入形态），不是裸子串：
      - 「原文 803,964,958,000 元」vs「答案 8039.65 亿元」→ 放行（单位换算）
      - 「原文 17.74%」vs「答案 18%」→ 放行（合法舍入）
      - 「原文 1450 万辆」vs「答案 45 万辆」→ 存疑（不是子串误判）
      - 「原文 2.53 元/股」vs「答案 2.55 元/股」→ 存疑
    """
    files = {f for f, _loc in wiki.ANCHOR_RE.findall(answer)}
    if not files:
        return []
    if texts is None:
        texts = unit_text_map(files)
    doubts = []
    for ln in _sentences(answer):
        anchors = wiki.ANCHOR_RE.findall(ln)
        if not anchors:
            continue
        nums = _key_numbers(ln)
        if not nums:
            continue
        for f, loc in anchors:
            n = wiki.loc_num(loc)
            body = texts.get((f, n))
            if body is None:
                continue                    # 没有原文可比（文件已移走/无缓存），跳过
            hay = _norm(body)
            if not hay:
                continue
            values = _page_values(hay)
            if not values:
                continue
            for num in nums:
                if _value_found(num, values):
                    continue                # 数字在这一页里找得到，放行
                doubts.append({
                    "file": f, "loc": loc, "sentence": ln[:160],
                    "number": num,
                    "reason": f"答案里的数字 {num} 在 {f} 的第 {n} 个单元原文里找不到",
                })
    return doubts


# ── 审校清单（存疑待人工复核） ─────────────────────────────────────────────
def load_doubts() -> list[dict]:
    p = _review_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return data.get("items") or []


def append_doubts(doubts, *, question="", answer=""):
    """把存疑项写进审校清单。清单限量，避免无限增长。"""
    if not doubts:
        return 0
    p = _review_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    items = load_doubts()
    ts = config.now_iso()
    for d in doubts:
        items.append({**d, "ts": ts, "question": str(question)[:120]})
    if len(items) > 200:
        items = items[-200:]
    p.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(doubts)
