"""快照文字稿：只有 paragraphs 没有 rows 时也能生成可摄入 DOCX。"""
import sys
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import snapshot  # noqa: E402


def test_snapshot_builds_from_paragraphs_only(tmp_path):
    out = tmp_path / "中汽协_行业要闻摘录.docx"
    snapshot.build({
        "out": str(out),
        "title": "行业要闻摘录",
        "org": "中国汽车工业协会",
        "url": "http://www.caam.org.cn/chn/1/cate_2/list_1.html",
        "fetched_at": "2026-10-29",
        "paragraphs": ["第一段正文，含数字 123。", "  ", "第二段正文。"],
    })
    assert out.is_file()
    text = "\n".join(p.text for p in Document(str(out)).paragraphs)
    assert "第一段正文" in text and "第二段正文" in text
    assert "中国汽车工业协会" in text
    assert "2026-10-29" in text
