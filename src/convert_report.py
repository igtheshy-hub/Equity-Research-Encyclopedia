"""报告转换管线：锚点转上标引注加文末注释表，转 Word 后美化表格与图片。

网页管线直接调 convert(src, out)；命令行用法：
  python -m src.convert_report outputs/report-乘用车行业-20261028.md outputs/report-乘用车行业-20261028.docx
"""
import re
import subprocess
import sys
from pathlib import Path

from . import config

SKILL = config.ROOT / ".claude" / "skills" / "md-to-docx" / "convert_md_to_docx.py"


SOURCE_RE = re.compile(
    r"\[\[([^\[\]]+?)\]\]"
    r"|（((?:乘联会|巨潮资讯|百度股市通|国家统计局|中国人民银行|中汽协|海关总署)[^）]{0,80})）")


def _src_key(content):
    """同一机构同一日期归一化，容忍简称与带书名号全称两种写法。"""
    m = re.match(
        r"([^，,《]*?)(?:《[^》]*》)?[，,]\s*((?:19|20)\d{2}(?:-\d{2})?(?:-\d{2})?)", content)
    return f"{m.group(1).strip()}|{m.group(2)}" if m else content


def anchors_to_notes(text):
    """PDF 锚点与外部来源括号引用统一编号，文末生成注释表。相邻多编号合并为一个上标。"""
    order = []  # 按文本首次出现顺序
    index = {}
    display = {}

    def reg(key, disp):
        if key not in index:
            order.append(key)
            index[key] = len(order)
            display[key] = disp
        elif len(disp) > len(display[key]):
            display[key] = disp  # 同一来源保留信息最全的写法
        return index[key]

    def repl(m):
        if m.group(1):
            if "#" not in m.group(1):
                return m.group(0)  # [[页面名]] 是知识库页链接，不是来源锚点，原样留着
            return f"⟦{reg('PDF:' + m.group(1), m.group(1))}⟧"
        return f"⟦{reg('SRC:' + _src_key(m.group(2)), m.group(2))}⟧"

    text = SOURCE_RE.sub(repl, text)
    # 相邻编号合并：⟦1⟧⟦2⟧⟦3⟧ -> ^[1,2,3]^
    text = re.sub(r"(?:⟦\d+⟧)+", lambda m: "^\\[" + ",".join(
        str(n) for n in sorted({int(x) for x in re.findall(r"\d+", m.group(0))})) + "\\]^", text)

    lines = []
    for i, key in enumerate(order, 1):
        if key.startswith("SRC:"):
            lines.append(f"[{i}] {display[key]}")
        else:
            fname, loc = display[key].split("#", 1)
            if loc.startswith("para"):
                lines.append(f"[{i}] {fname}，第 {loc[4:]} 段")
            else:
                lines.append(f"[{i}] {fname}，第 {loc[1:]} 页")
    notes = "## 注释\n\n" + "\n".join(lines)
    return text, notes


def _abs_images(text):
    """图片相对路径改成绝对路径：display.md 在 data/ 下，pandoc 按输入文件目录解析图片。"""
    charts = (config.OUTPUTS_DIR / "charts").as_posix()
    return re.sub(r"\]\((charts/[^)]+)\)", lambda m: f"]({charts}/{m.group(1)[7:]})", text)


def prettify(docx_path):
    """表格底纹与边框、字号、图片居中。"""
    from docx import Document
    from docx.shared import Pt, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    doc = Document(docx_path)

    # 图片等比缩进版心：matplotlib 原始像素宽常超 A4 版心，直接插入会撑破页面
    sec = doc.sections[0]
    avail = sec.page_width - sec.left_margin - sec.right_margin
    for shape in doc.inline_shapes:
        if shape.width > avail:
            ratio = avail / shape.width
            shape.width = avail
            shape.height = int(shape.height * ratio)

    def cell_bg(cell, color):
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:fill"), color)
        tcPr.append(shd)

    def table_borders(table):
        tblPr = table._tbl.tblPr
        borders = OxmlElement("w:tblBorders")
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            el = OxmlElement(f"w:{edge}")
            el.set(qn("w:val"), "single")
            el.set(qn("w:sz"), "4")
            el.set(qn("w:color"), "A6A6A6")
            borders.append(el)
        tblPr.append(borders)

    for table in doc.tables:
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table_borders(table)
        for cell in table.rows[0].cells:
            cell_bg(cell, "D9E1F2")
            for p in cell.paragraphs:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for r in p.runs:
                    r.font.bold = True
        for row in table.rows[1:]:
            for cell in row.cells:
                txt = cell.text.strip()
                numeric = bool(re.match(r"^[-−]?\d[\d,.]*(%|万辆|万|倍)?$", txt))
                for p in cell.paragraphs:
                    p.paragraph_format.line_spacing = 1.0
                    if numeric:
                        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                    for r in p.runs:
                        r.font.size = Pt(10.5)

    for p in doc.paragraphs:
        if "graphicData" in p._p.xml:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        txt = p.text.strip()
        # 注释表与来源说明顶格
        if re.match(r"^\[\d+\]", txt) or txt.startswith("数据来源"):
            p.paragraph_format.first_line_indent = Cm(0)
        # 题注居中加粗
        if "caption" in (p.style.name or "").lower() and txt:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for r in p.runs:
                r.font.bold = True

    doc.save(docx_path)


def convert(src, out):
    """报告 md 转 docx，返回输出路径。中间稿写 data/_display-<名>.md，不弄脏 outputs/。"""
    src, out = Path(src), Path(out)
    text = src.read_text(encoding="utf-8")

    lines = text.splitlines()
    title = ""
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        del lines[0]
        # 紧随其后的副标题 H2 降为加粗段落，避免混进目录
        for i, ln in enumerate(lines[:4]):
            if ln.startswith("## ") and ln[3:].strip():
                lines[i] = "**" + ln[3:].strip() + "**"
                break
        text = "\n".join(lines).lstrip("\n")

    text, notes = anchors_to_notes(text)
    marker = "## 数据来源与口径说明"
    if marker in text:
        text = text.replace(marker, notes + "\n\n" + marker)
    else:
        text += "\n\n" + notes
    text = _abs_images(text)

    out.parent.mkdir(parents=True, exist_ok=True)
    display = config.DATA_DIR / f"_display-{out.stem}.md"
    display.write_text(text, encoding="utf-8")

    flags = ["--toc", "--no-superscript-citations"]
    if title:
        flags += ["--title", title]
    r = subprocess.run(
        [sys.executable, str(SKILL), str(display), str(out)] + flags,
        cwd=str(config.ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("转 Word 失败：" + (r.stderr or r.stdout or "未知错误")[-400:])

    prettify(out)
    return out


def main():
    if len(sys.argv) != 3:
        raise SystemExit("用法：python -m src.convert_report <报告.md> <输出.docx>")
    out = convert(sys.argv[1], sys.argv[2])
    print(f"完成：{out}（注释表已并入，表格图片已美化）")


if __name__ == "__main__":
    main()
