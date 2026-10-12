"""引文解析与原文回查（系统诚信底盘）。

整套 wiki 的承诺是"每句话都可被人工核对到原文页码"。该模块做三件事：

1. 解析：从正文里识别 `[[文件名.pdf#p页码]]` / `[[文件名.docx#para段落号]]`
2. 关联：根据文件名查 Report id，按 (report_id, page_no) 取 Page.text
3. 验证：把 token 之前最近"完整一句"作为 quote，与 Page.text 做归一化模糊匹配

锚点格式跟 Igshy 主仓对齐（见 schema/CLAUDE.md §1）；模糊匹配做两层：
- 严格：归一化空白后子串相等 → 1.0
- 宽松：归一化后子串匹配；或 Jaccard 相似度 ≥ 0.7 → 0.85 / 0.6

未通过的 token 不会清掉，但会进"待人工复核"通道。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 正则常量（与 schema/CLAUDE.md §1 对齐）
# ---------------------------------------------------------------------------
# [[文件名.{pdf|docx}#{pN|paraN}]]
ANCHOR_RE = re.compile(
    r"\[\[([^\[\]#|]+\.(?:pdf|docx))#(p\d+|para\d+)\]\]",
    flags=re.UNICODE,
)

# 仅 [[页面标题]]（没有 # 和扩展名）
WIKILINK_RE = re.compile(
    r"\[\[([^\[\]#|.\n]+)\]\]",
    flags=re.UNICODE,
)

# 句号 / 分号 / 感叹 / 问号（中英文）/ 换行
_TERMINATORS = set("。；！？!？\n\r")


@dataclass
class CitationRef:
    """一个 token：出现在 body_md 哪一段 + 指向的源。"""

    filename: str
    page_no: Optional[int] = None           # PDF 用
    para_no: Optional[int] = None           # Word 用
    span_start: int = 0
    span_end: int = 0
    preceding_text: str = ""
    raw_token: str = ""

    @property
    def locator(self) -> str:
        """统一返回 'pN' / 'paraN' 形式的锚点后缀。"""
        if self.page_no is not None:
            return f"p{self.page_no}"
        if self.para_no is not None:
            return f"para{self.para_no}"
        return ""

    def quote_hash(self) -> str:
        return hashlib.sha256(_norm(self.preceding_text).encode("utf-8")).hexdigest()[:16]


def loc_kind(loc: str) -> str:
    """'p3' → 'page'，'para2' → 'para'。"""
    s = str(loc)
    return "para" if s.startswith("para") else "page"


def loc_num(loc: str) -> int:
    """'p3' → 3，'para2' → 2。"""
    return int(str(loc).replace("para", "").replace("p", ""))


def anchors_of(text: str) -> List[Tuple[str, str]]:
    """返回文档里所有锚点 (filename, locator) 的列表，顺序匹配。"""
    return [(m.group(1), m.group(2)) for m in ANCHOR_RE.finditer(text or "")]


def wikilinks_of(text: str) -> List[str]:
    """返回文档里所有 [[页面标题]] 文本列表。"""
    return [m.group(1).strip() for m in WIKILINK_RE.finditer(text or "")]


# ---------------------------------------------------------------------------
# Token 提取（兼容两种单元类型）
# ---------------------------------------------------------------------------
def extract_citations(body_md: str) -> list[CitationRef]:
    refs: list[CitationRef] = []
    if not body_md:
        return refs
    for m in ANCHOR_RE.finditer(body_md):
        filename = m.group(1).strip()
        loc = m.group(2).strip()
        start = m.start()
        is_para = loc_kind(loc) == "para"

        # 1) 向最近终止符 k 切
        k = start
        for i in range(start - 1, -1, -1):
            if body_md[i] in _TERMINATORS:
                k = i
                break
        # 2) 在 k 之前找上一个终止符 j
        j = -1
        for i in range(k - 1, -1, -1):
            if body_md[i] in _TERMINATORS:
                j = i
                break
        preceding = body_md[j + 1:k].strip(" 　，,、；;·. \t")

        ref = CitationRef(
            filename=filename,
            span_start=start,
            span_end=m.end(),
            preceding_text=preceding,
            raw_token=m.group(0),
        )
        if is_para:
            ref.para_no = loc_num(loc)
        else:
            ref.page_no = loc_num(loc)
        refs.append(ref)
    return refs


# ---------------------------------------------------------------------------
# 文本归一化与匹配
# ---------------------------------------------------------------------------
_WS_RE = re.compile(r"\s+", flags=re.UNICODE)
_FULLWIDTH_MAP = str.maketrans({
    "，": ",", "；": ";", "：": ":", "（": "(", "）": ")",
    "。": ".", "！": "!", "？": "?",
    "　": " ",
})


def _norm_locs(locs: dict) -> dict[str, str]:
    """把 `{3: text}` 或 `{'p3': text}` 都归一到 locator-string 键。"""
    out: dict[str, str] = {}
    for k, v in (locs or {}).items():
        if isinstance(k, int):
            out[f"p{k}"] = v
        else:
            out[str(k)] = v
    return out


def _norm(text: str) -> str:
    """归一空白 + 全角符号 → 半角，便于跨页比对。"""
    if not text:
        return ""
    t = text.translate(_FULLWIDTH_MAP)
    t = _WS_RE.sub(" ", t).strip()
    return t


def quote_in_page(page_text: str, quote: str) -> float:
    """返回 0.0–1.0 相似度；1.0 = 严格子串匹配。

    LLM 输出有时是「近逐字」改写而非严格复制，比如在事实前加「同时强调...」，
    所以：qn 任何 25 字切片能命中 pn，就给高置信度。
    """
    if not page_text or not quote:
        return 0.0
    pn = _norm(page_text)
    qn = _norm(quote)
    if not pn or not qn:
        return 0.0
    # 严格子串
    if qn in pn:
        return 1.0
    # 滑动窗口：qn 任意 25 字切片出现在 pn 即视为命中（兼容「近逐字」改写）
    if len(qn) > 25:
        for start in range(0, len(qn) - 25 + 1, 2):
            chunk = qn[start:start + 25]
            if chunk in pn:
                return 0.9
    # 退一步：qn 中段 30 字
    inner = _mid(qn, 30)
    if inner and inner in pn:
        return 0.85
    # 字符集 Jaccard
    j = _jaccard(pn, qn)
    if j >= 0.5:
        return max(0.6, j)
    return 0.0


def _mid(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    half = n // 2
    start = max(0, (len(s) // 2) - half)
    return s[start:start + n]


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


# ---------------------------------------------------------------------------
# 校验链路
# ---------------------------------------------------------------------------
@dataclass
class VerifiedCitation:
    ref: CitationRef
    report_id: Optional[int]
    page_text: Optional[str]
    match_score: float
    verified: bool            # score >= 0.85 自动通过
    needs_review: bool        # 0 < score < 0.85 标为人工复核
    notes: list[str] = field(default_factory=list)


@dataclass
class FilenameResolver:
    """把 token 里的 filename 解析为 report_id + 配套 page 文本。

    `by_filename` 是规范化 filename → report_id；
    `page_index` 是 report_id → {locator → text}；locator 是 'p3' 或 'para2'。
    """

    by_filename: dict[str, int]
    page_index: dict[int, dict[str, str]]   # report_id → {locator → text}

    @classmethod
    def build(cls, report_index: Iterable[tuple[int, str, dict]]) -> "FilenameResolver":
        """从 `(report_id, filename, {locator: text})` 元组构建。

        locs 支持两种 key 形态：
          - 字符串 locator：`{'p3': text, 'para2': text}`（推荐）
          - 整数页号：`{3: text}`（兼容老调用方）—— 会归一为 'pN'
        """
        by_filename: dict[str, int] = {}
        page_index: dict[int, dict[str, str]] = {}
        for report_id, filename, locs in report_index:
            key = _norm(filename).lower()
            by_filename[key] = report_id
            page_index[report_id] = _norm_locs(locs)
            import os
            base = os.path.basename(filename).lower()
            if base != key:
                by_filename.setdefault(base, report_id)
        return cls(by_filename=by_filename, page_index=page_index)

    def resolve_id(self, filename: str) -> Optional[int]:
        k = _norm(filename).lower()
        rid = self.by_filename.get(k)
        if rid is not None:
            return rid
        import os
        base = os.path.basename(filename).lower()
        for n, rid in self.by_filename.items():
            n_base = os.path.basename(n)
            if n_base == base:
                return rid
        return None


def verify_body_md(
    body_md: str,
    *,
    resolver: FilenameResolver,
) -> list[VerifiedCitation]:
    """对一个 body_md 跑完整链路：抽 token → 找 report → 比原文。"""
    refs = extract_citations(body_md)
    out: list[VerifiedCitation] = []
    for r in refs:
        rid = resolver.resolve_id(r.filename)
        notes: list[str] = []
        if rid is None:
            out.append(
                VerifiedCitation(
                    ref=r, report_id=None, page_text=None,
                    match_score=0.0, verified=False, needs_review=True,
                    notes=[f"文件名未注册: {r.filename}"],
                )
            )
            continue

        loc = r.locator
        pages = resolver.page_index.get(rid, {})
        page_text = pages.get(loc)
        if not page_text:
            out.append(
                VerifiedCitation(
                    ref=r, report_id=rid, page_text=None,
                    match_score=0.0, verified=False, needs_review=True,
                    notes=[f"{loc} 文本未找到"],
                )
            )
            continue

        score = quote_in_page(page_text, r.preceding_text)
        verified = score >= 0.85
        needs_review = 0.0 < score < 0.85
        if not verified and not needs_review:
            notes.append("原文比对未命中（OCR 或排版差异？人工复核）")
        out.append(
            VerifiedCitation(
                ref=r,
                report_id=rid,
                page_text=page_text,
                match_score=score,
                verified=verified,
                needs_review=needs_review,
                notes=notes,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def split_into_sentences(body_md: str) -> list[str]:
    """中文友好的"句"切分：把孤立的 `[[来源:...]]` 并回上一句（兼容期）。"""
    if not body_md:
        return []
    parts = re.split(r"(?<=[。；！？!?])\s*|(?<=\n)\s*", body_md)
    raw = [p.strip() for p in parts if p.strip()]
    out: list[str] = []
    for p in raw:
        if (p.startswith("[来源:") or p.startswith("[[")) and out:
            out[-1] = (out[-1] + " " + p).strip()
        else:
            out.append(p)
    return out


def every_nontrivial_sentence_has_citation(body_md: str) -> tuple[bool, list[str]]:
    """返回 (是否全部带引用, 未带引用的句子列表)。"""
    issues: list[str] = []
    # 同时认两种 token：
    anchor_alt = re.compile(r"\[\[[^\[\]#|]+\.(?:pdf|docx)#(?:p|para)\d+\]\]")
    bracket_alt = re.compile(r"\[\s*来源:[^\[\]:\n]+:\d+\s*\]")  # 旧版，兼容期
    pattern = re.compile(rf"(?:{anchor_alt.pattern}|{bracket_alt.pattern})")
    for sent in split_into_sentences(body_md):
        s = sent.strip()
        if not s:
            continue
        if s.startswith(("#", "-", "*", ">")):
            continue
        if len(s) < 4:
            continue
        if pattern.search(s) is None:
            issues.append(s)
    return (len(issues) == 0), issues
