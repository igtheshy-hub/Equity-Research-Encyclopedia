"""把财报知识行里千元换算留下的浮点尾数收回（金额 2 位、小额原文最多 6 位）。

背景：早期 _fmt_value 统一保留 6 位小数，51,203,109,216.36 被写成
51,203,109,216.360001。只改显示，不改数值语义，也不动锚点。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, wiki  # noqa: E402

LINE_RE = re.compile(r"(=\s*)(-?[\d,]+\.\d{3,})(\s*元)")


def fmt(v):
    return (f"{v:,.2f}" if abs(v) >= 100 else f"{v:,.6f}").rstrip("0").rstrip(".")


def main():
    total = 0
    for p in wiki.load_pages():
        changed = 0

        def repl(m):
            nonlocal changed
            try:
                v = float(m.group(2).replace(",", ""))
            except ValueError:
                return m.group(0)
            new = fmt(v)
            if new != m.group(2):
                changed += 1
            return m.group(1) + new + m.group(3)

        lines = [LINE_RE.sub(repl, ln) for ln in p["body"].splitlines()]
        if not changed:
            continue
        meta = p["meta"]
        hist = meta.get("history") or []
        hist.append(f"{config.today_str()} 数字格式清理（{changed} 行浮点尾数）")
        meta["history"] = hist
        wiki.write_page(p["path"], meta, "\n".join(lines))
        print(f"{p['slug']}: {changed}")
        total += changed
    print(f"合计 {total} 行")
    if total:
        wiki.rebuild_index_md(wiki.load_pages())
        wiki.append_log("fix", f"数字格式清理：{total} 行浮点尾数收回 2 位")


if __name__ == "__main__":
    main()
