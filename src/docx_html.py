import html

from docx.table import Table
from docx.text.paragraph import Paragraph

from .pdf_extract import _iter_docx_blocks


def docx_to_html(path):
    """docx 转带块号锚点的 HTML，块号与提取层的单元序号严格一致，供原文对照定位。"""
    from docx import Document

    doc = Document(str(path))
    parts = []
    for n, block in enumerate(_iter_docx_blocks(doc), 1):
        if isinstance(block, Paragraph):
            text = (block.text or "").strip()
            if not text:
                continue
            style = (getattr(block.style, "name", "") or "").lower()
            tag = "h3" if "heading" in style or "标题" in style else "p"
            parts.append(f'<{tag} data-n="{n}">{html.escape(text)}</{tag}>')
        elif isinstance(block, Table):
            rows = []
            for row in block.rows:
                cells = "".join(f"<td>{html.escape((c.text or '').strip())}</td>" for c in row.cells)
                rows.append(f"<tr>{cells}</tr>")
            if rows:
                parts.append(f'<table data-n="{n}"><tbody>{"".join(rows)}</tbody></table>')
    return "\n".join(parts) or "<p>（文档没有可显示的文字）</p>"
