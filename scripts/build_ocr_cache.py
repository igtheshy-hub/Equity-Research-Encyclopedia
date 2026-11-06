"""把 MinerU 的 JSON 结果按页拼成 data/extracts/<pdf stem>.units.json。

用法：python scripts/build_ocr_cache.py mapping.json
mapping.json 是 [{"pdf": "raw/reports/.../x.pdf", "mineru_json": "data/_mineru/x.json"}, ...]
生成的缓存带 ocr 标记与原文 SHA256，pdf_extract 优先使用它，锚点仍是原 PDF 页码。
"""
import hashlib
import json
import sys
from html.parser import HTMLParser
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
EXTRACTS = ROOT / "data" / "extracts"

SKIP_TYPES = {"header", "footer", "page_number"}
NOISE = ("慧博投研", "请勿外传", "本报告来源于")


class _TableText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], [], None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row:
            if any(self.row):
                self.rows.append(self.row)
            self.row = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def table_text(html):
    p = _TableText()
    p.feed(html or "")
    return "\n".join(" | ".join(c for c in row) for row in p.rows)


def block_text(b):
    t = b.get("type")
    if t == "table":
        parts = []
        cap = " ".join(b.get("table_caption") or [])
        if cap:
            parts.append(f"[表] {cap}")
        body = table_text(b.get("table_body"))
        if body:
            parts.append(body)
        foot = " ".join(b.get("table_footnote") or [])
        if foot:
            parts.append(f"[注] {foot}")
        return "\n".join(parts)
    if t == "chart":
        cap = " ".join(b.get("chart_caption") or [])
        return f"[图] {cap}" if cap else ""
    if t == "image":
        cap = " ".join(b.get("image_caption") or [])
        return f"[图] {cap}" if cap else ""
    return str(b.get("text") or "").strip()


def build(pdf, mineru_json):
    pdf, mineru_json = Path(pdf), Path(mineru_json)
    with pymupdf.open(pdf) as doc:
        pages = doc.page_count
    blocks = json.loads(mineru_json.read_text(encoding="utf-8"))
    per_page = {i: [] for i in range(pages)}
    for b in blocks:
        if b.get("type") in SKIP_TYPES:
            continue
        idx = b.get("page_idx")
        if not isinstance(idx, int) or idx < 0 or idx >= pages:
            continue
        text = block_text(b)
        if not text or any(n in text for n in NOISE):
            continue
        per_page[idx].append(text)
    units = []
    for i in range(pages):
        text = "\n".join(per_page[i]).strip()
        units.append({"n": i + 1, "text": text})
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    payload = {
        "file": pdf.name,
        "unit": "page",
        "count": pages,
        "scanned": False,
        "ocr": True,
        "sha256": digest,
        "units": units,
    }
    EXTRACTS.mkdir(parents=True, exist_ok=True)
    out = EXTRACTS / (pdf.stem + ".units.json")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    empty = sum(1 for u in units if not u["text"])
    chars = sum(len(u["text"]) for u in units)
    print(f"{pdf.name}: {pages} 页, {chars} 字符, 空页 {empty} -> {out.relative_to(ROOT)}")


def main():
    mapping = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    for item in mapping:
        build(ROOT / item["pdf"] if not Path(item["pdf"]).is_absolute() else item["pdf"],
              ROOT / item["mineru_json"] if not Path(item["mineru_json"]).is_absolute() else item["mineru_json"])


if __name__ == "__main__":
    main()
