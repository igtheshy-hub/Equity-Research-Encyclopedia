import re
from urllib.parse import quote

import markdown as md_lib
from markupsafe import Markup

from .. import wiki

TYPE_LABELS = {"company": "公司", "industry": "行业", "concept": "概念", "source": "来源摘要"}


def _anchor_repl(m):
    fname, loc = m.group(1), m.group(2)
    label = fname.rsplit(".", 1)[0]
    tail = f"第{wiki.loc_num(loc)}段" if wiki.loc_kind(loc) == "para" else f"第{wiki.loc_num(loc)}页"
    href = f"/viewer?file={quote(fname)}&loc={loc}"
    return f'<sup class="cite"><a href="{href}" target="_blank" rel="noopener">{label}·{tail}</a></sup>'


def _wikilink_repl(m):
    t = m.group(1).strip()
    return f'<a class="wikilink" href="/find/{quote(t)}">{t}</a>'


def render_md(text):
    """锚点与双链替换后转 HTML，页面渲染与聊天接口共用。"""
    text = wiki.ANCHOR_RE.sub(_anchor_repl, str(text or ""))
    text = wiki.WIKILINK_RE.sub(_wikilink_repl, text)
    return md_lib.markdown(text, extensions=["tables"])


def anchor_pretty(line):
    """提案行展示用：[[文件#pN]] 转成（出处：文件·第N页）纯文本。"""
    def repl(m):
        fname, loc = m.group(1), m.group(2)
        label = fname.rsplit(".", 1)[0]
        tail = f"第{wiki.loc_num(loc)}段" if wiki.loc_kind(loc) == "para" else f"第{wiki.loc_num(loc)}页"
        return f"（出处：{label}·{tail}）"
    text = wiki.ANCHOR_RE.sub(repl, str(line or ""))
    text = re.sub(r"\[\[([^\[\]#|.]+)\]\]", r"\1", text)
    return text.strip().lstrip("- ").strip()


def md_filter(text):
    return Markup(render_md(text))
