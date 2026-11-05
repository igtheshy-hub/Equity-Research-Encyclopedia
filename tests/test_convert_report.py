import base64

from docx import Document
from docx.shared import Cm

from src import convert_report, charts


PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def test_prettify_widens_page_and_fits_image_to_text_width(tmp_path):
    """A4 版心放宽后超宽图应缩到版心内，不能溢出页面。"""
    img = tmp_path / "wide.png"
    img.write_bytes(PNG_1PX)
    doc = Document()
    doc.add_picture(str(img), width=Cm(20))
    src = tmp_path / "wide.docx"
    doc.save(src)

    convert_report.prettify(src)

    got = Document(src)
    sec = got.sections[0]
    avail = sec.page_width - sec.left_margin - sec.right_margin
    assert round(sec.page_width / Cm(1), 1) == 21.0
    assert round(sec.page_height / Cm(1), 1) == 29.7
    assert got.inline_shapes[0].width <= avail


def test_label_rotation_for_long_chinese_labels():
    assert charts._label_rotation(["2024A", "2025A"], 2) == 0
    assert charts._label_rotation(["整车制造（2025年）", "上游"], 2) == 45
