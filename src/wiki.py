import re
from pathlib import Path

import yaml

from . import config

# 锚点两种形态：[[文件.pdf#p页码]]、[[文件.docx#para段落号]]（Word 无固定页码，按段落定位）
ANCHOR_RE = re.compile(r"\[\[([^\[\]#|]+\.(?:pdf|docx))#(p\d+|para\d+)\]\]")
WIKILINK_RE = re.compile(r"\[\[([^\[\]#|.]+)\]\]")
FRONT_SPLIT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def loc_kind(loc):
    return "para" if str(loc).startswith("para") else "page"


def loc_num(loc):
    return int(str(loc).replace("para", "").replace("p", ""))


def anchors_of(text):
    return {(m.group(1), m.group(2)) for m in ANCHOR_RE.finditer(text)}


def wikilinks_of(text):
    return {m.group(1).strip() for m in WIKILINK_RE.finditer(text)}


def parse_page(path):
    text = path.read_text(encoding="utf-8")
    m = FRONT_SPLIT_RE.match(text)
    if not m:
        raise ValueError(f"{path.name} 缺少 front matter（文件开头的 --- 元数据块）")
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise ValueError(f"{path.name} 的 front matter 不是合法 YAML：{e}") from e
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ValueError(f"{path.name} 的 front matter 不是键值对")
    return meta, text[m.end():]


def slug_to_path(type_, slug):
    d = config.TYPE_DIRS.get(type_)
    if d is None:
        raise ValueError(f"未知页面类型: {type_}")
    return d / f"{slug}.md"


# 最近一次 load_pages 跳过的坏页面；validate / audit / 网页都能读到，不静默丢
_PAGE_ERRORS = []


def load_pages(skip_bad=True):
    """读取 wiki 页面。坏页面（缺 front matter、YAML 写坏）默认跳过并记录，
    不让整站 500；validate 等需要严格模式时传 skip_bad=False 直接抛出。"""
    pages = []
    errors = []
    for type_, d in config.TYPE_DIRS.items():
        for p in sorted(d.glob("*.md")):
            try:
                meta, body = parse_page(p)
            except (ValueError, OSError) as e:
                if not skip_bad:
                    raise
                errors.append({"path": p, "type": type_, "error": str(e)})
                continue
            statements = sum(1 for line in body.splitlines() if line.strip().startswith("- "))
            pages.append({
                "path": p,
                "type": type_,
                "slug": p.stem,
                "meta": meta,
                "body": body,
                "anchors": anchors_of(body),
                "statements": statements,
            })
    _PAGE_ERRORS[:] = errors
    return pages


def page_errors():
    """返回最近一次 load_pages 跳过的坏页面，[(Path, 中文错误说明)]。"""
    return [(e["path"], e["error"]) for e in _PAGE_ERRORS]


def check_pages():
    """主动扫一遍并返回坏页面清单，不改 _PAGE_ERRORS。"""
    errors = []
    for type_, d in config.TYPE_DIRS.items():
        for p in sorted(d.glob("*.md")):
            try:
                parse_page(p)
            except (ValueError, OSError) as e:
                errors.append((p, str(e)))
    return errors


def build_index_entry(page):
    meta = page["meta"]
    aliases = meta.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    return {
        "slug": page["slug"],
        "type": page["type"],
        "title": str(meta.get("title") or page["slug"]),
        "aliases": [str(a) for a in aliases],
        "summary": str(meta.get("summary") or ""),
        "source_files": sorted({Path(s).name for s in (meta.get("sources") or [])}),
        "statements": page["statements"],
        "updated": str(meta.get("updated") or ""),
    }


def compact_index_text(entries):
    lines = []
    for e in entries:
        parts = [e["slug"], e["type"]]
        if e["aliases"]:
            parts.append("/".join(e["aliases"]))
        if e["summary"]:
            parts.append(e["summary"])
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def rebuild_index_md(pages):
    labels = {"company": "公司", "industry": "行业", "concept": "概念", "source": "来源摘要"}
    out = ["# 研报百科 · 总索引", ""]
    for type_, label in labels.items():
        tp = [p for p in pages if p["type"] == type_]
        out += [f"## {label}（{len(tp)}）", ""]
        if not tp:
            out += ["（暂无）", ""]
            continue
        out += ["| 页面 | 别名 | 摘要 | 来源数 | 条目数 | 更新 |", "|---|---|---|---|---|---|"]
        for p in tp:
            e = build_index_entry(p)
            rel = p["path"].relative_to(config.WIKI_DIR).as_posix()
            aliases = "、".join(e["aliases"]) or "-"
            out.append(
                f"| [{e['title']}]({rel}) | {aliases} | {e['summary'] or '-'} "
                f"| {len(e['source_files'])} | {e['statements']} | {e['updated']} |"
            )
        out.append("")
    config.INDEX_MD.write_text("\n".join(out) + "\n", encoding="utf-8")


def append_log(op, summary):
    config.WIKI_DIR.mkdir(parents=True, exist_ok=True)
    new = not config.LOG_MD.exists()
    with config.LOG_MD.open("a", encoding="utf-8") as f:
        if new:
            f.write("# 操作日志\n\n| 日期 | 操作 | 说明 |\n|---|---|---|\n")
        f.write(f"| {config.today_str()} | {op} | {summary} |\n")


def body_to_sections(body):
    sections = {}
    current = "其他"
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
        elif line.strip().startswith("- "):
            sections.setdefault(current, []).append(line.strip()[2:])
    return sections


def render_page(title, sections, type_):
    enum = config.SECTION_ENUMS.get(type_) or []
    ordered = [s for s in enum if s in sections]
    extras = [s for s in sections if s not in enum]
    out = [f"# {title}", ""]
    for name in ordered + extras:
        out += [f"## {name}", ""]
        out += [f"- {ln}" if not ln.startswith("- ") else ln for ln in sections[name]]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_page(path, meta, body):
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
    text = f"---\n{fm}---\n{body}"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
