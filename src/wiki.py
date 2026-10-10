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
        raise ValueError(f"{path.name} 缺少 front matter")
    return yaml.safe_load(m.group(1)) or {}, text[m.end():]


def slug_to_path(type_, slug):
    d = config.TYPE_DIRS.get(type_)
    if d is None:
        raise ValueError(f"未知页面类型: {type_}")
    return d / f"{slug}.md"


def load_pages():
    pages = []
    for type_, d in config.TYPE_DIRS.items():
        for p in sorted(d.glob("*.md")):
            meta, body = parse_page(p)
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
    return pages


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
    with config.LOG_MD.open("a", encoding="utf-8") as f:
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
