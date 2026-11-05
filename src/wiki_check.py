"""wiki 页面校验的共享实现：网页写盘前校验与会话内 page_check 钩子共用一套规则。

校验单页：front matter 必填字段与 type 目录一致性、结构化行格式、锚点真实性（文件存在、
单元类型、序号不越界）、wikilink 指向。index 收录与全库遍历由 validate.validate_all 管。
"""
import re

from . import anchor_match as am
from . import config, wiki
from .ingest import load_manifest
from .pdf_extract import load_cached, raw_file_index

# 财报三类结构化行的行首模板（.claude/skills/finstmt-ingest/SKILL.md 行格式契约）
LINE_PK_RE = re.compile(r"^【(数据|指标|观点)】(\d{4}(?:FY|H1|Q1|Q3)) ")
PERSONAL_VIEW_RE = re.compile(r"^【个人观点】")

DIR_TYPE = {d.name: t for t, d in config.TYPE_DIRS.items()}


def _management_problems(name, ln):
    # 【管理层】行契约：【管理层】「原话」——语境 [[文件#位置]]，锚点在行尾
    body = ln.removeprefix("【管理层】").strip()
    if not body.startswith("「"):
        return [f"{name}: 管理层行缺引述（应以「开头）：{ln[:50]}"]
    close = body.rfind("」")
    if close < 0:
        return [f"{name}: 管理层行引述未闭合：{ln[:50]}"]
    rest = body[close + 1:].strip()
    if not rest.startswith("——"):
        return [f"{name}: 管理层行缺语境分隔（」后应接——）：{ln[:50]}"]
    rest = rest.removeprefix("——").strip()
    m = wiki.ANCHOR_RE.search(rest)
    if m is None:
        return [f"{name}: 管理层行缺合法锚点：{ln[:50]}"]
    if not rest[:m.start()].strip():
        return [f"{name}: 管理层行缺语境：{ln[:50]}"]
    if rest[m.end():].strip():
        return [f"{name}: 管理层行锚点后有多余文字：{ln[:50]}"]
    return []


def _line_format_problems(name, body):
    # 【数据】/【指标】/【观点】/【管理层】行的格式契约检查
    out = []
    for ln in body.splitlines():
        ln = ln.strip().removeprefix("- ").strip()
        if ln.startswith("【管理层】"):
            out.extend(_management_problems(name, ln))
            continue
        if PERSONAL_VIEW_RE.match(ln) and not wiki.ANCHOR_RE.search(ln):
            out.append(f"{name}: 【个人观点】行缺锚点：{ln[:50]}")
            continue
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
        elif kind == "观点" and not wiki.ANCHOR_RE.search(ln):
            out.append(f"{name}: 【观点】行缺锚点：{ln[:50]}")
    return out


def files_info():
    # 锚点合法性依据：已摄入清单（manifest）∪ 有提取缓存的 raw 文件（raw 深查答案经会话提取入库）
    info = {f["file"]: (f.get("unit", "page"), f.get("count") or f.get("page_count") or 0)
            for f in load_manifest()["files"]}
    for name, path in raw_file_index().items():
        if name not in info:
            cached = load_cached(path)
            if cached:
                info[name] = (cached["unit"], cached["count"])
    return info


def file_paths():
    paths = {f["file"]: config.ROOT / f["path"] for f in load_manifest()["files"]}
    for name, path in raw_file_index().items():
        paths.setdefault(name, path)
    return paths


def _number_anchor_problems(name, body, info, unit_cache):
    """带数字的知识行，数字必须真在所挂单元的原文里；挂错页直接报出来。"""
    problems = []
    paths = file_paths()
    for ln in body.splitlines():
        anchors = wiki.ANCHOR_RE.findall(ln)
        if not anchors or re.search(r"[（(]\s*测算\s*[:：]", ln):
            continue
        # 指标行由 fin_calc 按已带锚点的科目计算，锚点指向计算所用原始页，结果数字不必在原文出现。
        # 观点行是判断句，句中的比率、差值、增速多为按锚点页原始数据计算所得，同样不逐字出现；
        # 观点行仍须带锚点（由 _line_format_problems 把关），锚点页只需存在且序号合法。
        if ln.lstrip("- ").startswith(("【指标】", "【观点】", "【个人观点】")):
            continue
        terms = am.line_terms(ln)
        if not terms:
            continue
        for f, loc in anchors:
            fi = info.get(f)
            if not fi:
                continue
            if f not in unit_cache:
                p = paths.get(f)
                unit_cache[f] = load_cached(p) if p else None
            cached = unit_cache[f]
            if not cached:
                continue
            unit = next((u for u in cached["units"] if u["n"] == wiki.loc_num(loc)), None)
            if unit is None or any(am.unit_has_term(unit.get("text") or "", t) for t in terms):
                continue
            nums = "、".join(n for n, _u in terms[:3])
            problems.append(f"{name}: 锚点 {f}#{loc} 的原文里找不到数字（{nums}）")
    return problems


def _expected_type(path):
    try:
        parts = path.resolve().relative_to(config.WIKI_DIR.resolve()).parts
    except ValueError:
        return None
    return DIR_TYPE.get(parts[0]) if len(parts) == 2 else None


def content_problems(name, meta, body, expected_type=None, info=None, titles=None,
                     check_wikilinks=True, unit_cache=None):
    """对单页内容跑全部单页校验。path 未知时用 name 标识，expected_type 需显式给。

    程序批量写入时同一批页面可能互相引用、后写的页尚不存在，可按需跳过 wikilink 存在性检查，
    由写盘后的 validate_all 兜底。
    """
    problems = []
    if expected_type and meta.get("type") != expected_type:
        problems.append(f"{name}: front matter type={meta.get('type')!r} 与所在目录应为 {expected_type!r} 不一致")
    for field in ("title", "type", "sources", "updated"):
        if field == "sources" and meta.get("needs_review"):
            continue  # 需复核页（删除原文件后保留的无锚点内容）允许暂无可归属来源
        if not meta.get(field):
            problems.append(f"{name}: 缺少必填 front matter 字段 {field}")
    problems.extend(_line_format_problems(name, body))
    if info is None:
        info = files_info()
    for f, loc in sorted(wiki.anchors_of(body)):
        fi = info.get(f)
        if fi is None:
            problems.append(f"{name}: 锚点引用了 raw 中不存在的文件 {f}")
        elif wiki.loc_kind(loc) != fi[0]:
            problems.append(f"{name}: 锚点 {f}#{loc} 单元类型不对（应为{'段落' if fi[0] == 'para' else '页'}）")
        elif not 1 <= wiki.loc_num(loc) <= fi[1]:
            problems.append(f"{name}: 锚点 {f}#{loc} 序号越界（该文件共 {fi[1]} 单元）")
    problems.extend(_number_anchor_problems(name, body, info, unit_cache if unit_cache is not None else {}))
    if check_wikilinks:
        if titles is None:
            titles = {str(p["meta"].get("title") or p["slug"]) for p in wiki.load_pages()}
        for link in sorted(wiki.wikilinks_of(body)):
            if link not in titles:
                problems.append(f"{name}: wikilink 指向不存在的页面 [[{link}]]")
    return problems


def check_content(meta, body, path=None, expected_type=None, check_wikilinks=True):
    """写盘前校验：meta/body 尚未落盘，path 仅用于取名与目录类型推断。"""
    name = path.name if path is not None else "页面"
    return content_problems(name, meta, body, expected_type or (_expected_type(path) if path else None),
                            check_wikilinks=check_wikilinks)


def check_page(path):
    meta, body = wiki.parse_page(path)
    return content_problems(path.name, meta, body, _expected_type(path))
