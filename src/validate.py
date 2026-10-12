import re

from . import config, wiki
from .ingest import load_manifest
from .pdf_extract import load_cached, raw_file_index

# 财报三类结构化行的行首模板（schema/财报摄取-skill.md 行格式契约）
LINE_PK_RE = re.compile(r"^【(数据|指标|观点)】(\d{4}(?:FY|H1|Q1|Q3)) ")


def _line_format_problems(name, body):
    # 【数据】/【指标】/【观点】行的格式契约检查：报告期前缀、等号数值可解析、口径括号闭合
    out = []
    for ln in body.splitlines():
        ln = ln.strip().removeprefix("- ").strip()
        m = LINE_PK_RE.match(ln)
        if not m:
            continue
        kind = m.group(1)
        if kind in ("数据", "指标"):
            if " = " not in ln:
                out.append(f"{name}: {kind}行缺等号：{ln[:50]}")
                continue
            if re.search(r"= -?[\d,]+(?:\.\d+)?", ln) is None:
                out.append(f"{name}: {kind}行数值不可解析：{ln[:50]}")
            if "(" in ln and ")" not in ln:
                out.append(f"{name}: {kind}行口径括号未闭合：{ln[:50]}")
    return out


def _files_info():
    # 锚点合法性依据：已摄入清单（manifest）∪ 有提取缓存的 raw 文件（raw 深查答案经会话提取入库）
    info = {f["file"]: (f.get("unit", "page"), f.get("count") or f.get("page_count") or 0)
            for f in load_manifest()["files"]}
    for name, path in raw_file_index().items():
        if name not in info:
            cached = load_cached(path)
            if cached:
                info[name] = (cached["unit"], cached["count"])
    return info


def validate_all():
    problems = []
    files_info = _files_info()
    pages = wiki.load_pages()
    titles = {str(p["meta"].get("title") or p["slug"]) for p in pages}
    for p in pages:
        name = p["path"].name
        meta = p["meta"]
        for field in ("title", "type", "sources", "updated"):
            if not meta.get(field):
                problems.append(f"{name}: 缺少必填 front matter 字段 {field}")
        if meta.get("type") and meta["type"] != p["type"]:
            problems.append(f"{name}: front matter type={meta['type']} 与所在目录 {p['type']} 不一致")
        problems.extend(_line_format_problems(name, p["body"]))
        for f, loc in sorted(p["anchors"]):
            info = files_info.get(f)
            if info is None:
                problems.append(f"{name}: 锚点引用了 raw 中不存在的文件 {f}")
            elif wiki.loc_kind(loc) != info[0]:
                problems.append(f"{name}: 锚点 {f}#{loc} 单元类型不对")
            elif not 1 <= wiki.loc_num(loc) <= info[1]:
                problems.append(f"{name}: 锚点 {f}#{loc} 序号越界（该文件共 {info[1]} 单元）")
        for link in sorted(wiki.wikilinks_of(p["body"])):
            if link not in titles:
                problems.append(f"{name}: wikilink 指向不存在的页面 [[{link}]]")
    idx = config.INDEX_MD.read_text(encoding="utf-8") if config.INDEX_MD.exists() else ""
    for p in pages:
        if f"{p['slug']}.md" not in idx:
            problems.append(f"{p['path'].name}: 未收录进 index.md")
    return problems
