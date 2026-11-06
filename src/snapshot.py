"""外部取数快照：把网页/API 取到的数字落成可摄入的 DOCX。

用法：python -m src.snapshot <spec.json>

spec.json 字段：
  out         输出 docx 路径（相对项目根），写 raw/reports/数据快照/<机构>_<主题>快照_<取数日期>.docx
              快照不走 raw/inbox 与 manage.py sort（sort 只有研报/财报/政策三类）
  title       快照标题，如 国家统计局宏观月度数据快照
  org         来源机构，如 国家统计局
  url         取数网页或接口地址
  fetched_at  取数时点，YYYY-MM-DD
  note        口径或使用说明，可空
  rows        [{指标, 数值, 单位, 口径, 时点}]，每行一条数据
  paragraphs  文字稿段落列表，与 rows 二选一或同时给（中汽协这类以文字为主的来源）

文件名、来源机构、取数时点、口径都写进正文，摄入后锚点可追溯到块号。
"""
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docx import Document  # noqa: E402

from src import config  # noqa: E402


def _clean(value):
    """去掉 XML 不接受的控制字符（网页正文常带 \x08、\x0b 等），否则 python-docx 直接报错。"""
    text = str(value if value is not None else "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = text.replace("​", "").replace("﻿", "")
    return re.sub(r"\s+", " ", text).strip()


def build(spec):
    out = Path(spec["out"])
    if not out.is_absolute():
        out = config.ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    doc.add_heading(_clean(spec["title"]), level=0)
    doc.add_paragraph(f"出具机构：{_clean(spec['org'])}")
    doc.add_paragraph(f"数据来源：{_clean(spec['org'])}")
    doc.add_paragraph(f"来源地址：{_clean(spec.get('url') or '未记录')}")
    doc.add_paragraph(f"取数时点：{_clean(spec['fetched_at'])}")

    rows = spec.get("rows") or []
    paragraphs = spec.get("paragraphs") or []
    if not rows and not paragraphs:
        raise SystemExit("rows 与 paragraphs 都为空，没有可写入的数据")
    if rows:
        cols = list(rows[0].keys())
        table = doc.add_table(rows=1, cols=len(cols))
        table.style = "Table Grid"
        for i, c in enumerate(cols):
            table.rows[0].cells[i].text = _clean(c)
        for r in rows:
            cells = table.add_row().cells
            for i, c in enumerate(cols):
                cells[i].text = _clean(r.get(c, ""))
    for p in paragraphs:
        text = _clean(p)
        if text:
            doc.add_paragraph(text)

    if spec.get("note"):
        doc.add_paragraph(_clean(spec["note"]))

    doc.save(str(out))
    print(f"快照已生成：{out}")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("用法：python -m src.snapshot <spec.json>")
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    build(spec)


if __name__ == "__main__":
    main()
