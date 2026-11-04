import html
import re
import threading
from urllib.parse import quote

import markdown as md_lib
from markupsafe import Markup

from .. import wiki

TYPE_LABELS = {"company": "公司", "industry": "行业", "concept": "概念", "source": "来源摘要"}

# 问题 1（XSS）：渲染顺序必须是「先转义、再 markdown、最后放回我们自己生成的标签」，
# 否则研报正文里的 <script> 会被 Python-Markdown 当成合法 HTML 原样输出。
# 这里用占位符把锚点/双链先摘出来，剩余文本整体转义后交给 markdown，
# 最后再把（我们自己生成的、安全的）HTML 放回去。
#
# 占位符用纯字母数字，markdown 不会改它；每线程一份存储，避免并发渲染互相串。
_TLS = threading.local()
_TOKEN = "zzPH{}zz"


def _holders():
    h = getattr(_TLS, "holders", None)
    if h is None:
        h = []
        _TLS.holders = h
    return h


def _hold(fragment):
    """把一段安全 HTML 存进本线程的暂存区，返回占位符。"""
    hs = _holders()
    idx = len(hs)
    hs.append(fragment)
    return _TOKEN.format(idx)


def _restore(text):
    """把占位符换回我们自己生成的 HTML 片段。"""
    hs = _holders()
    return re.sub(r"zzPH(\d+)zz", lambda m: hs[int(m.group(1))] if int(m.group(1)) < len(hs) else "", text)


def _anchor_repl(m):
    fname, loc = m.group(1), m.group(2)
    label = html.escape(fname.rsplit(".", 1)[0])
    tail = f"第{wiki.loc_num(loc)}段" if wiki.loc_kind(loc) == "para" else f"第{wiki.loc_num(loc)}页"
    href = f"/viewer?file={quote(fname)}&loc={loc}"
    return _hold(
        f'<sup class="cite"><a href="{html.escape(href, quote=True)}" '
        f'target="_blank" rel="noopener">{label}·{tail}</a></sup>'
    )


def _wikilink_repl(m):
    t = m.group(1).strip()
    return _hold(f'<a class="wikilink" href="/find/{quote(t)}">{html.escape(t)}</a>')


def render_md(text):
    """锚点与双链替换后转 HTML，页面渲染与聊天接口共用。

    安全（问题 1）：用户/研报提供的内容一律走 html.escape；
    Python-Markdown 看不到任何用户给的原始 HTML，因此无法注入 <script>。
    """
    _TLS.holders = []
    src = str(text or "")
    src = wiki.ANCHOR_RE.sub(_anchor_repl, src)
    src = wiki.WIKILINK_RE.sub(_wikilink_repl, src)
    # 关键：先整体转义，markdown 就只会把转义后的文本当纯文字
    src = html.escape(src, quote=False)
    out = md_lib.markdown(src, extensions=["tables"])
    return _restore(out)


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
