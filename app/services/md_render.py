"""Markdown 渲染 + 锚点 / wikilink 转可悬浮徽章。

锚点格式（与 schema/CLAUDE.md §1 对齐，Igshy 同款）：
  [[file.pdf#pN]]     → <sup class="cite" data-file="..." data-page="N">label·第N页</sup>
  [[file.docx#paraN]] → <sup class="cite" data-file="..." data-para="N">label·第N段</sup>
  [[页面标题]]          → <a class="wikilink" href="/find/...">页面标题</a>

设计：先在 markdown 文本上做 cite / wikilink 替换；markdown 自己的 [link](url) 语法不会和 `[[...]]` 撞。
"""
from __future__ import annotations

import html
import re
from typing import List
from urllib.parse import quote

from markupsafe import Markup

from app.services.citation import (
    ANCHOR_RE,
    WIKILINK_RE,
    loc_kind,
    loc_num,
)


def _cite_label(fname: str) -> str:
    base = fname.rsplit(".", 1)[0]
    if len(base) > 16:
        base = base[:14] + "…"
    return base


def _anchor_to_html(m: re.Match[str]) -> str:
    fname, loc = m.group(1), m.group(2)
    n = loc_num(loc)
    tail = f"第{n}段" if loc_kind(loc) == "para" else f"第{n}页"
    safe_fname = html.escape(fname, quote=True)
    safe_token = html.escape(m.group(0), quote=True)
    # 同时挂 sup.cite（hover 取气泡）+ a.cite-link（点击跳转到 /viewer）
    if loc_kind(loc) == "page":
        href = f"/viewer?file={quote(fname, safe='')}&page={n}"
    else:
        # Word 文件暂未实现 viewer，跳到 KB 自身搜索
        href = f"/find/{quote(_cite_label(fname), safe='')}"
    return (
        f'<a class="cite-link" href="{href}" '
        f'data-file="{safe_fname}" data-loc="{loc}" data-n="{n}" '
        f'data-token="{safe_token}" title="悬浮查看原文 · 点击跳到原文页">'
        f'<sup class="cite">{html.escape(_cite_label(fname))}·{tail}</sup></a>'
    )


def _wikilink_to_html(m: re.Match[str]) -> str:
    t = html.escape(m.group(1).strip(), quote=True)
    href = quote(t, safe="")
    return f'<a class="wikilink" href="/find/{href}">{t}</a>'


# markdown 极简子集：# / ## / ### 标题；- 列表；段落；空行分段
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^[ \t]*[-*]\s+(.*)$")


def _md_pass(text: str) -> str:
    """极简 markdown → html。"""
    out: list[str] = []
    in_list = False

    def flush_p() -> None:
        if not paragraph:
            return
        joined = " ".join(paragraph).strip()
        paragraph.clear()
        if not joined:
            return
        joined = WIKILINK_RE.sub(_wikilink_to_html, joined)
        out.append(f"<p>{joined}</p>")

    def flush_l() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    paragraph: list[str] = []
    for line in text.split("\n"):
        s = line.rstrip()
        if not s.strip():
            flush_p(); flush_l(); continue
        hm = _HEADING_RE.match(s)
        if hm:
            flush_p(); flush_l()
            level = len(hm.group(1))
            content = WIKILINK_RE.sub(_wikilink_to_html, hm.group(2).strip())
            out.append(f"<h{level}>{content}</h{level}>")
            continue
        lm = _LIST_RE.match(s)
        if lm:
            flush_p()
            if not in_list:
                out.append("<ul>")
                in_list = True
            content = WIKILINK_RE.sub(_wikilink_to_html, lm.group(1))
            out.append(f"<li>{content}</li>")
            continue
        flush_l()
        paragraph.append(s)
    flush_p()
    flush_l()
    return "".join(out)


def render(md: str) -> Markup:
    if not md:
        return Markup("")
    # 第一步：在 markdown 文本上把 [[...]] 转成 cite HTML（避开 markdown 的 link 解析）
    text = ANCHOR_RE.sub(_anchor_to_html, md)
    # 第二步：wikilink 在 markdown 处理时手动替换（避免与 cite 撞）
    return Markup(_md_pass(text))


def citations_in(md: str) -> List[dict]:
    """仅返回 token 元信息列表，便于模板列引用清单。"""
    out: List[dict] = []
    for m in ANCHOR_RE.finditer(md or ""):
        fname = m.group(1).strip()
        loc = m.group(2)
        out.append({
            "filename": fname,
            "loc": loc,
            "page": loc_num(loc) if loc_kind(loc) == "page" else None,
            "para": loc_num(loc) if loc_kind(loc) == "para" else None,
        })
    return out
