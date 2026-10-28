"""Excel 导出：把知识库 + 财报结构化数据导成可筛选的 xlsx（CSMAR 式）。

设计原则：**只读不写**，不改任何 markdown、不改 data/fin/*.json。

数据源（三张表）：
  - 科目数据表  data/fin/{公司}.json 的 periods[p].items（已由 fin_calc 归一到元）
  - 派生指标表  periods[p].indicators（毛利率/净利率/ROE…，由 fin_calc.compute_indicators 算好）
  - 条目溯源表  wiki/**/*.md 的知识条目（含锚点）
  - 来源汇总    data/manifest.json 每个源文件贡献

导出规格由前端勾选决定，见 SHEETS 定义。
"""
from __future__ import annotations

import io
import json
from typing import List, Optional

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    _OPENPYXL_OK = True
except ImportError:  # 缺依赖时网页给出中文提示，而不是整站导入失败
    Workbook = None
    Alignment = Font = PatternFill = None
    get_column_letter = None
    _OPENPYXL_OK = False

from . import config, fin_calc, wiki


def available():
    return _OPENPYXL_OK

TYPE_LABELS = {"company": "公司", "industry": "行业", "concept": "概念", "source": "来源摘要"}

# ── 列定义：(key, 表头, 是否默认勾选) ──────────────────────────────────────
ITEM_COLUMNS = [
    ("company", "公司", True),
    ("code", "代码", False),
    ("period", "报告期", True),
    ("period_end", "期末日", True),
    ("statement", "报表", False),
    ("item", "科目", True),
    ("value_yi", "金额(亿元)", True),
    ("value_yuan", "金额(元)", False),
    ("anchor", "锚点", True),
    ("source", "来源文件", False),
    ("audit_opinion", "审计意见", False),
]

INDICATOR_COLUMNS = [
    ("company", "公司", True),
    ("code", "代码", False),
    ("period", "报告期", True),
    ("indicator", "指标", True),
    ("value", "数值", True),
    ("unit", "单位", True),
    ("anchor", "锚点", True),
]

ENTRY_COLUMNS = [
    ("page_title", "页面标题", True),
    ("page_type", "类型", True),
    ("section", "小节", True),
    ("text", "条目正文", True),
    ("anchor", "锚点", True),
    ("anchor_file", "来源文件", True),
    ("page_no", "页码", False),
    ("quote", "去锚点正文", False),
    ("updated", "更新时间", False),
]

SOURCE_COLUMNS = [
    ("file", "文件名", True),
    ("source_type", "类型", True),
    ("report_date", "报告日期", True),
    ("entries_count", "条目数", True),
    ("topics_touched", "触及主题", False),
    ("path", "原始路径", False),
    ("ingested_at", "摄入时间", False),
]

SHEETS = {
    "items": {"label": "科目数据表", "columns": ITEM_COLUMNS},
    "indicators": {"label": "派生指标表", "columns": INDICATOR_COLUMNS},
    "entries": {"label": "条目溯源表", "columns": ENTRY_COLUMNS},
    "sources": {"label": "来源汇总", "columns": SOURCE_COLUMNS},
}

_HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
_HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)

# fin_calc 里标记为"绝对值口径"的指标（比率类按 % 存，绝对额按元存）
try:
    from .fin_calc import STATEMENT_LABELS  # noqa: F401  可选，存在就用
except ImportError:
    STATEMENT_LABELS = {}


# ── 数据准备 ────────────────────────────────────────────────────────────────
def _load_fin_stores():
    """读 data/fin/*.json。"""
    out = []
    if not config.FIN_DIR.exists():
        return out
    for f in sorted(config.FIN_DIR.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    return out


def _iter_items(store):
    """摊平 store → 科目行。"""
    company = store.get("company") or store.get("slug") or ""
    code = ""
    al = store.get("aliases") or []
    if al:
        code = str(al[0])
    for pk, pd_ in (store.get("periods") or {}).items():
        src = pd_.get("source") or ""
        audit = pd_.get("audit_opinion") or ""
        items = pd_.get("items") or {}
        parents = pd_.get("items_parent") or {}
        for name, spec in items.items():
            if not isinstance(spec, dict):
                continue
            v = spec.get("v")
            anchors = spec.get("a") or ""
            statement = spec.get("statement") or parents.get(name) or ""
            yield {
                "company": company,
                "code": code,
                "period": pk,
                "period_end": pd_.get("period_end") or "",
                "statement": statement,
                "item": name,
                "value_yi": round(v / 1e8, 4) if isinstance(v, (int, float)) else "",
                "value_yuan": v if isinstance(v, (int, float)) else "",
                "anchor": anchors,
                "source": src,
                "audit_opinion": audit,
            }


def _iter_indicators(store):
    """摊平 store → 指标行。"""
    company = store.get("company") or store.get("slug") or ""
    code = ""
    al = store.get("aliases") or []
    if al:
        code = str(al[0])
    for pk, pd_ in (store.get("periods") or {}).items():
        inds = pd_.get("indicators") or {}
        for name, spec in inds.items():
            v = spec.get("v") if isinstance(spec, dict) else spec
            if not isinstance(v, (int, float)):
                continue
            unit = fin_calc.indicator_unit(name)
            # 只有比率类乘 100；DSO/CCC 是天、FCF 是元、EPS 是元/股，不能一律当百分比
            value = round(v * 100, 4) if unit == "%" else round(v, 4)
            yield {
                "company": company,
                "code": code,
                "period": pk,
                "indicator": name,
                "value": value,
                "unit": unit,
                "anchor": "".join((spec.get("a") if isinstance(spec, dict) else []) or []),
            }


def _entry_rows(types, keyword="", date_from="", date_to=""):
    kw = (keyword or "").strip().lower()
    rows = []
    for p in wiki.load_pages():
        if types and p["type"] not in types:
            continue
        meta = p.get("meta") or {}
        title = str(meta.get("title") or p.get("slug") or "")
        ptype = p.get("type") or ""
        updated = str(meta.get("updated") or "")
        if date_from and updated and updated < date_from:
            continue
        if date_to and updated and updated > date_to:
            continue
        for section, lines in wiki.body_to_sections(p.get("body") or "").items():
            for ln in lines:
                text = str(ln).strip()
                if kw and kw not in text.lower() and kw not in title.lower():
                    continue
                anchors = wiki.ANCHOR_RE.findall(text)
                fname = anchors[0][0] if anchors else ""
                loc = anchors[0][1] if anchors else ""
                page_no = wiki.loc_num(loc) if (loc and wiki.loc_kind(loc) == "page") else None
                rows.append({
                    "page_title": title,
                    "page_type": TYPE_LABELS.get(ptype, ptype),
                    "section": section,
                    "text": text,
                    "anchor": f"[[{fname}#{loc}]]" if fname else "",
                    "anchor_file": fname,
                    "page_no": page_no,
                    "quote": wiki.ANCHOR_RE.sub("", text).strip(),
                    "updated": updated,
                })
    return rows


def _source_rows():
    if not config.MANIFEST_PATH.exists():
        return []
    m = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = []
    for f in m.get("files") or []:
        rows.append({
            "file": f.get("file") or "",
            "source_type": f.get("source_type") or "",
            "report_date": f.get("report_date") or "",
            "entries_count": f.get("entries_count") or 0,
            "topics_touched": "、".join(f.get("topics_touched") or []),
            "path": f.get("path") or "",
            "ingested_at": f.get("ingested_at") or "",
        })
    return rows


def _filter_fin(rows, keyword="", companies=None, periods=None):
    kw = (keyword or "").strip().lower()
    out = []
    for r in rows:
        if companies and r["company"] not in companies:
            continue
        if periods and r["period"] not in periods:
            continue
        if kw:
            hay = f"{r.get('company','')} {r.get('item','')} {r.get('indicator','')}".lower()
            if kw not in hay:
                continue
        out.append(r)
    return out


# ── 写 Excel ────────────────────────────────────────────────────────────────
def _write_sheet(ws, rows: List[dict], columns):
    keys = [k for k, _h, _d in columns]
    hdrs = [h for _k, h, _d in columns]
    ws.append(hdrs)
    for cell in ws[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"
    for r in rows:
        ws.append([("" if r.get(k) is None else r.get(k, "")) for k in keys])
    # 列宽（只看前 200 行，避免大表变慢）
    for i, (k, h) in enumerate(zip(keys, hdrs), start=1):
        w = len(str(h))
        for r in rows[:200]:
            w = max(w, len(str(r.get(k, "") or "")))
        ws.column_dimensions[get_column_letter(i)].width = min(48, max(10, w + 4))


def build_workbook(
    *,
    sheets: Optional[List[str]] = None,
    types: Optional[List[str]] = None,
    keyword: str = "",
    date_from: str = "",
    date_to: str = "",
    companies: Optional[List[str]] = None,
    periods: Optional[List[str]] = None,
) -> bytes:
    """生成 xlsx 字节流。

    sheets      要导出哪几张：items / indicators / entries / sources
    types       条目表的页面类型：company / industry / concept
    companies   财务表限定公司（空=全部）
    periods     财务表限定报告期（空=全部）
    """
    sheets = sheets or ["items", "indicators", "entries", "sources"]
    types = types if types is not None else ["company", "industry", "concept"]

    wb = Workbook()
    wb.remove(wb.active)

    stores = _load_fin_stores() if ("items" in sheets or "indicators" in sheets) else []

    if "items" in sheets:
        rows = []
        for st in stores:
            rows.extend(_iter_items(st))
        rows = _filter_fin(rows, keyword, companies, periods)
        _write_sheet(wb.create_sheet("科目数据表"), rows, ITEM_COLUMNS)

    if "indicators" in sheets:
        rows = []
        for st in stores:
            rows.extend(_iter_indicators(st))
        rows = _filter_fin(rows, keyword, companies, periods)
        _write_sheet(wb.create_sheet("派生指标表"), rows, INDICATOR_COLUMNS)

    if "entries" in sheets:
        rows = _entry_rows(types, keyword, date_from, date_to)
        _write_sheet(wb.create_sheet("条目溯源表"), rows, ENTRY_COLUMNS)

    if "sources" in sheets:
        _write_sheet(wb.create_sheet("来源汇总"), _source_rows(), SOURCE_COLUMNS)

    if not wb.sheetnames:
        wb.create_sheet("空")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def available_companies():
    """导出页用：列出可筛选的公司与报告期。"""
    comps, periods = [], set()
    for st in _load_fin_stores():
        name = st.get("company") or st.get("slug") or ""
        if name and name not in comps:
            comps.append(name)
        for pk in (st.get("periods") or {}):
            periods.add(pk)
    return comps, sorted(periods)