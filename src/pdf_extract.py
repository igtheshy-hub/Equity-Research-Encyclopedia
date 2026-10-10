import json

import pdfplumber
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from . import config

SUPPORTED_SUFFIXES = (".pdf", ".docx")


def _iter_docx_blocks(doc):
    # 按文档顺序产出段落与表格，编号与查看器渲染保持一致
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _docx_units(path):
    from docx import Document

    doc = Document(str(path))
    units = []
    for n, block in enumerate(_iter_docx_blocks(doc), 1):
        if isinstance(block, Paragraph):
            text = (block.text or "").strip()
        else:
            rows = []
            for row in block.rows:
                cells = [(c.text or "").strip() for c in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            text = "\n".join(rows)
        if text:
            units.append({"n": n, "text": text})
    return units


def extract_units(path):
    # 统一产出 [{“n”: 序号, “text”: 文本}]：pdf 按页（序号=页码），docx 按文档块（段落/表格，序号=块号）
    if path.suffix.lower() == ".docx":
        return _docx_units(path)
    units = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = (page.extract_text() or "").strip()
            units.append({"n": i, "text": text})
    return units


def unit_kind_of(path):
    return "para" if path.suffix.lower() == ".docx" else "page"


def anchor_of(path, n):
    # 该文件第 n 个单元的锚点写法
    return f"[[{path.name}#{'para' if unit_kind_of(path) == 'para' else 'p'}{n}]]"


def first_text(path, max_chars=2600):
    # 收件台归类用：开头部分文字
    texts = []
    total = 0
    for u in extract_units(path):
        if not u["text"]:
            continue
        texts.append(u["text"])
        total += len(u["text"])
        if total >= max_chars:
            break
    return "\n\n".join(texts)


def avg_valid_chars(units, min_valid=20):
    valid = [len(u["text"]) for u in units if len(u["text"]) >= min_valid]
    if not valid:
        return 0.0
    return sum(valid) / len(valid)


def cache_path(path):
    return config.EXTRACTS_DIR / (path.stem + ".units.json")


def load_cached(path):
    f = cache_path(path)
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if data.get("file") != path.name:
        return None
    return data


def extract_to_store(path):
    # 提取全部单元文本并落盘缓存；扫描版检测仅适用于 pdf
    units = extract_units(path)
    unit = unit_kind_of(path)
    scanned = unit == "page" and avg_valid_chars(units) < config.CFG["ingest"]["min_chars_per_page"]
    config.EXTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "file": path.name,
        "unit": unit,
        "count": len(units),
        "scanned": scanned,
        "units": units,
    }
    cache_path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return units, len(units), scanned, unit


def raw_file_index():
    # 文件名 → 路径（收件台保证文件名全库唯一）；深查与原文查看都靠它定位
    out = {}
    for d in (config.REPORTS_DIR, config.FILINGS_DIR, config.POLICIES_DIR, config.INBOX_DIR):
        for suf in SUPPORTED_SUFFIXES:
            for p in d.rglob("*" + suf):
                out[p.name] = p
    return out
