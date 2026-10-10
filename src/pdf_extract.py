import json

import pdfplumber

from . import config


def extract_pages(pdf_path):
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = (page.extract_text() or "").strip()
            pages.append({"page": i, "text": text})
    return pages


def first_pages_text(pdf_path, n=3):
    with pdfplumber.open(pdf_path) as pdf:
        return [(page.extract_text() or "").strip() for page in pdf.pages[:n]]


def avg_valid_chars(pages, min_valid=20):
    valid = [len(p["text"]) for p in pages if len(p["text"]) >= min_valid]
    if not valid:
        return 0.0
    return sum(valid) / len(valid)


def extract_to_store(pdf_path):
    # 提取全部分页文本并落盘 data/extracts/<名>.pages.json
    pages = extract_pages(pdf_path)
    page_count = len(pages)
    scanned = avg_valid_chars(pages) < config.CFG["ingest"]["min_chars_per_page"]
    out = config.EXTRACTS_DIR / (pdf_path.stem + ".pages.json")
    payload = {
        "file": pdf_path.name,
        "page_count": page_count,
        "scanned": scanned,
        "pages": pages,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return pages, page_count, scanned
