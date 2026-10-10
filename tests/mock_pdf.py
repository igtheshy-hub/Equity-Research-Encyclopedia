"""生成测试用 PDF（纯 ASCII 文本），验证提取与收件台管道。用法：python tests/mock_pdf.py [输出目录]"""
import sys
from pathlib import Path


def build_pdf(lines_by_page):
    next_id = 3
    page_specs = []
    for _ in lines_by_page:
        content_id = next_id
        next_id += 1
        page_id = next_id
        next_id += 1
        page_specs.append((content_id, page_id))
    font_id = next_id

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}

    def emit(obj_id, body):
        offsets[obj_id] = len(out)
        out.extend(f"{obj_id} 0 obj\n".encode())
        out.extend(body)
        out.extend(b"\nendobj\n")

    emit(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for _, pid in page_specs)
    emit(2, f"<< /Type /Pages /Kids [{kids}] /Count {len(page_specs)} >>".encode())
    for (content_id, page_id), lines in zip(page_specs, lines_by_page):
        ops = []
        y = 700
        for ln in lines:
            safe = ln.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            ops.append(f"BT /F1 14 Tf 72 {y} Td ({safe}) Tj ET")
            y -= 24
        stream = "\n".join(ops).encode("latin-1", errors="replace")
        body = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        emit(content_id, body)
        emit(page_id, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode())
    emit(font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    xref_pos = len(out)
    size = font_id + 1
    out.extend(f"xref\n0 {size}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for i in range(1, size):
        out.extend(f"{offsets[i]:010d} 00000 n \n".encode())
    out.extend(f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode())
    return bytes(out)


if __name__ == "__main__":
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("raw/inbox")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mocktest_report.pdf").write_bytes(build_pdf([
        ["CITIC Securities Research Report", "Baijiu Industry Outlook 2026", "Date: 2026-03-12"],
        ["Revenue forecast: industry +18% YoY", "Top pick: Kweichow Moutai"],
    ]))
    (out_dir / "mocktest_policy.pdf").write_bytes(build_pdf([
        ["CSRC Notice No. 2026-15", "Regulation on Securities Research Reports", "Issued: 2026-02-01"],
    ]))
    print(f"mock PDFs written to {out_dir}")
