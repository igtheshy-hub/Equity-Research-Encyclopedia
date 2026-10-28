import json
import re
import threading
from pathlib import Path

from . import config, llm, wiki
from .classify import sha256_of, sanitize
from .pdf_extract import (SUPPORTED_SUFFIXES, DuplicateFileError, extract_to_store,
                          find_raw_file, unit_kind_of)

# 清单落盘的串行锁：摄入线只追加条目、删除线只删条目，两条线都在锁内 load-modify-save，互不覆盖
MANIFEST_LOCK = threading.RLock()

COMPILE_PROMPT = """你是投研知识编辑，把一份材料的逐单元文本编译成 wiki 知识条目。单元{UNIT_WORD}，每单元文字前有形如「[第3页]」「[第3段]」的序号标记；锚点格式 {ANCHOR_FORM}，序号必须取该内容所在单元的实际序号。

规则：
- 只用给定材料，禁止编造，材料里没有的信息不写
- 每条知识一句话，句尾必须带锚点 {ANCHOR_EXAMPLE}
- 保留原文关键数字（金额、增速、比率、评级、目标价等），不改口径
- 营销表述、免责声明、分析师介绍一律丢弃
- 主题划分：公司给 type=company，行业给 type=industry，估值方法/政策术语等给 type=concept
- 主题名与别名用中文规范名（英文材料也译成中文通用名；证券代码等专有缩写可保留英文）
- 分节只能用这些节名——公司/行业页：公司概况、行业格局、经营与财务、盈利预测、估值、风险提示、其他；概念页：定义与口径、方法要点、政策背景、应用示例、其他
- 每个主题给一句不超过 40 字的 summary
{FOCUS_RULE}
只输出 JSON，不要解释：
{
  "report_date": "材料中的报告日期 YYYY-MM-DD，识别不出给空字符串",
  "topics": [
    {
      "title": "主题名",
      "type": "company|industry|concept",
      "aliases": ["别名或代码，没有给空数组"],
      "summary": "一句话摘要",
      "sections": {"经营与财务": ["某条知识句尾带锚点{ANCHOR_EXAMPLE}", "..."]},
      "focus_hits": ["命中关注点的条目原文片段，没有给空数组"]
    }
  ]
}

--- 材料开始 ---
{TEXT}
--- 材料结束 ---"""

MERGE_PROMPT = """现有知识库索引如下（每行：slug | 类型 | 别名），可能为空：
{INDEX}

新摄入材料提炼出以下主题（每行：标题 | 类型 | 别名）：
{TOPICS}

为每个新主题决定归并方式。只输出 JSON 数组，不要解释：
[{"title": "新主题名", "action": "create 或 merge", "slug": "merge 时必须从上面索引的 slug 里选；create 时给规范 slug（中文可用，不含空格和特殊符号）", "type": "company|industry|concept"}]
拿不准就 create，宁可新建碎页交给审校收编。"""


def iter_raw_files():
    for d in (config.REPORTS_DIR, config.FILINGS_DIR, config.POLICIES_DIR):
        for suf in SUPPORTED_SUFFIXES:
            yield from sorted(d.rglob("*" + suf))


# 兼容旧名（审校未摄入检查等处引用）
iter_pdfs = iter_raw_files


def source_kind(path):
    rel = path.relative_to(config.RAW_DIR).as_posix()
    for kind, prefix in (("report", "reports/"), ("filing", "filings/"), ("policy", "policies/")):
        if rel.startswith(prefix):
            return kind
    return "unknown"


def load_manifest():
    if config.MANIFEST_PATH.exists():
        return json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    return {"files": []}


def save_manifest(m):
    with MANIFEST_LOCK:
        config.MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.MANIFEST_PATH.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")


def append_manifest(entries):
    """只把新条目追加进盘上的清单，不整份覆盖：并发的删除改动不会被冲掉。"""
    if not entries:
        return
    with MANIFEST_LOCK:
        m = load_manifest()
        have = {(f["file"], f["sha256"]) for f in m["files"]}
        for e in entries:
            if (e["file"], e["sha256"]) not in have:
                m["files"].append(e)
        save_manifest(m)


def _norm(s):
    return re.sub(r"[\s·・]", "", str(s)).casefold()


def _safe_type(t):
    return t if t in ("company", "industry", "concept") else "concept"


def _valid_lines(lines, fname, unit_kind, count):
    # 锚点校验：每条必须有锚点且全部指向本文件、单元类型与序号范围正确，违规行丢弃
    keep, dropped = [], 0
    for ln in lines:
        ln = str(ln).strip()
        ln = ln.removeprefix("- ").strip()
        if not ln:
            continue
        found = wiki.ANCHOR_RE.findall(ln)
        ok = bool(found) and all(
            a[0] == fname and wiki.loc_kind(a[1]) == unit_kind and 1 <= wiki.loc_num(a[1]) <= count
            for a in found
        )
        if not ok:
            dropped += 1
            continue
        if ln not in keep:
            keep.append(ln)
    return keep, dropped


def _unit_words(unit_kind):
    if unit_kind == "para":
        return {"UNIT_WORD": "为文档块（段落或表格）", "ANCHOR_FORM": "[[文件名.docx#para段落号]]", "ANCHOR_EXAMPLE": "[[{FILE}#para1]]"}
    return {"UNIT_WORD": "为页", "ANCHOR_FORM": "[[文件名.pdf#p页码]]", "ANCHOR_EXAMPLE": "[[{FILE}#p1]]"}


FOCUS_RULE_TEMPLATE = """- 本次重点关注（用户自定义，命中这些主题的条目优先保留，并列进 focus_hits）：
{focus_lines}
  与关注点无关的内容仍正常提炼；focus_hits 只放明确命中关注点的条目原文片段"""


def _focus_rule(focus):
    """按关注点生成 prompt 注入段；无关注点时返回空串（该行在 prompt 里自然消失）。"""
    items = [str(x).strip() for x in (focus or []) if str(x).strip()]
    if not items:
        return ""
    return FOCUS_RULE_TEMPLATE.replace("{focus_lines}", "\n".join(f"  · {x}" for x in items))


def compile_file(path, units, unit_kind, focus=None, task_id=None):
    from . import tasks
    fname = path.name
    chunk_size = max(1, int(config.CFG["ingest"]["compile_chunk_pages"]))
    chunks = [units] if len(units) <= chunk_size else [
        units[i:i + chunk_size] for i in range(0, len(units), chunk_size)
    ]
    prompt = COMPILE_PROMPT.replace("{FOCUS_RULE}", _focus_rule(focus))
    words = _unit_words(unit_kind)
    for k, v in words.items():
        prompt = prompt.replace("{" + k + "}", v)
    topics = {}
    report_date = ""
    total_dropped = 0
    for chunk in chunks:
        tasks.check_cancel(task_id)
        tag = "段" if unit_kind == "para" else "页"
        text = "\n\n".join(f"[第{u['n']}{tag}]\n{u['text']}" for u in chunk if u["text"])
        out = llm.chat(
            [{"role": "user", "content": prompt.replace("{FILE}", fname).replace("{TEXT}", text)}],
            task="compile",
            response_json=True,
        )
        rd = str(out.get("report_date") or "").strip()
        if rd and not report_date:
            report_date = rd
        for t in out.get("topics") or []:
            title = str(t.get("title") or "").strip()
            if not title:
                continue
            ex = topics.setdefault(title, {
                "title": title, "type": _safe_type(t.get("type")),
                "aliases": [], "summary": str(t.get("summary") or ""), "sections": {},
                "focus_hits": [],
            })
            ex["aliases"].extend(str(a) for a in (t.get("aliases") or []) if a)
            if not ex["summary"] and t.get("summary"):
                ex["summary"] = str(t["summary"])
            ex["focus_hits"].extend(str(x).strip() for x in (t.get("focus_hits") or []) if str(x).strip())
            for s, lines in (t.get("sections") or {}).items():
                s = str(s).strip() or "其他"
                keep, dropped = _valid_lines(lines, fname, unit_kind, len(units))
                total_dropped += dropped
                bucket = ex["sections"].setdefault(s, [])
                for ln in keep:
                    if ln not in bucket:
                        bucket.append(ln)
    topics = {k: v for k, v in topics.items() if any(v["sections"].values())}
    if total_dropped:
        print(f"  [校验] 丢弃 {total_dropped} 条锚点不合规的条目")
    out_path = config.EXTRACTS_DIR / (path.stem + ".entries.json")
    out_path.write_text(json.dumps({"file": fname, "report_date": report_date, "topics": list(topics.values())}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"report_date": report_date, "topics": list(topics.values())}


def plan_merge(topics, index_entries):
    decisions = []
    pending = []
    by_key = {}
    for e in index_entries:
        for key in [e["title"]] + e["aliases"]:
            by_key.setdefault(_norm(key), e)
    for t in topics:
        hit = None
        for key in [t["title"]] + [str(a) for a in t.get("aliases") or []]:
            hit = by_key.get(_norm(key))
            if hit:
                break
        if hit and hit["type"] == t["type"]:
            decisions.append(({"action": "merge", "slug": hit["slug"], "type": hit["type"]}, t))
        else:
            pending.append(t)
    if not pending:
        return decisions
    if not index_entries:
        return decisions + [({"action": "create", "type": t["type"]}, t) for t in pending]
    index_text = "\n".join(
        f"{e['slug']} | {e['type']} | {'、'.join(e['aliases'])}" for e in index_entries
    ) or "（空）"
    topics_text = "\n".join(
        f"{t['title']} | {t['type']} | {'、'.join(t['aliases'])}" for t in pending
    )
    out = llm.chat(
        [{"role": "user", "content": MERGE_PROMPT.replace("{INDEX}", index_text).replace("{TOPICS}", topics_text)}],
        task="merge",
        response_json=True,
    )
    items = out if isinstance(out, list) else out.get("decisions") or []
    by_title = {t["title"]: t for t in pending}
    slug_index = {e["slug"]: e for e in index_entries}
    for d in items:
        t = by_title.get(str(d.get("title") or ""))
        if t is None:
            continue
        action = d.get("action")
        if action == "merge" and d.get("slug") in slug_index and slug_index[d["slug"]]["type"] == t["type"]:
            decisions.append(({"action": "merge", "slug": d["slug"], "type": t["type"]}, t))
        else:
            decisions.append(({"action": "create", "type": _safe_type(d.get("type") or t["type"])}, t))
    decided = {id(t) for _, t in decisions}
    for t in pending:
        if id(t) not in decided:
            decisions.append(({"action": "create", "type": t["type"]}, t))
    return decisions


def _apply_merge(page_path, meta, body, topic, extra_sources, note_prefix):
    sections = wiki.body_to_sections(body)
    old_anchors = wiki.anchors_of(body)
    added = 0
    for s, lines in topic["sections"].items():
        bucket = sections.setdefault(s, [])
        for ln in lines:
            if ln not in bucket:
                bucket.append(ln)
                added += 1
    title = str(meta.get("title") or page_path.stem)
    new_body = wiki.render_page(title, sections, _dir_type(page_path))
    new_anchors = wiki.anchors_of(new_body)
    if not old_anchors <= new_anchors:
        raise RuntimeError(f"{page_path.name}: 合并后锚点集合减少，拒绝写盘")
    srcs = meta.get("sources") or []
    for s in extra_sources:
        if s not in srcs:
            srcs.append(s)
    meta["sources"] = srcs
    meta["updated"] = config.today_str()
    aliases = meta.get("aliases") or []
    for a in topic["aliases"]:
        if a not in aliases:
            aliases.append(a)
    if aliases:
        meta["aliases"] = aliases
    hist = meta.get("history") or []
    hist.append(f"{config.today_str()} {note_prefix}（+{added} 条）")
    meta["history"] = hist
    wiki.write_page(page_path, meta, new_body)
    return added


def _dir_type(page_path):
    for type_, d in config.TYPE_DIRS.items():
        if page_path.parent == d:
            return type_
    return "concept"


def _apply_create(decision, topic, extra_sources, note_prefix):
    type_ = decision["type"]
    slug = sanitize(topic["title"]) or sanitize(decision.get("slug") or "") or "未命名"
    path = wiki.slug_to_path(type_, slug)
    i = 2
    while path.exists():
        path = wiki.slug_to_path(type_, f"{slug}-{i}")
        i += 1
    count = sum(len(v) for v in topic["sections"].values())
    meta = {
        "title": topic["title"],
        "type": type_,
        "sources": list(extra_sources),
        "updated": config.today_str(),
        "history": [f"{config.today_str()} {note_prefix}（{count} 条）"],
    }
    aliases = [a for a in topic["aliases"] if _norm(a) != _norm(topic["title"])]
    if aliases:
        meta["aliases"] = aliases
    if topic["summary"]:
        meta["summary"] = topic["summary"]
    body = wiki.render_page(topic["title"], topic["sections"], type_)
    wiki.write_page(path, meta, body)
    return path, count


def _write_source_page(path, topic_titles, report_date, entries_count, extra=None):
    title = path.stem
    out_path = config.TYPE_DIRS["source"] / f"{title}.md"
    rel = path.relative_to(config.ROOT).as_posix()
    unit_word = "文档块" if unit_kind_of(path) == "para" else "页"
    info = [
        f"文件：{path.name}",
        f"类型：{source_kind(path)}",
        f"报告日期：{report_date or '未识别'}",
        f"知识条目：{entries_count} 条",
    ]
    info.extend(extra or [])
    links = "、".join(f"[[{t}]]" for t in topic_titles)
    sections = {"材料信息": info, "贡献": [f"贡献主题页：{links}" if links else "贡献主题页：无"]}
    meta = {"title": title, "type": "source", "sources": [rel], "updated": config.today_str()}
    wiki.write_page(out_path, meta, wiki.render_page(title, sections, "source"))


def _tag_focus_pages(touched_titles, focus, focus_hits):
    """把关注点与命中条目写进主题页 frontmatter（页级字段，不动正文锚点）。"""
    targets = {str(t) for t in touched_titles}
    for p in wiki.load_pages():
        if p["type"] == "source":
            continue
        title = str(p["meta"].get("title") or p["slug"])
        if title not in targets:
            continue
        meta = dict(p["meta"])
        old_focus = list(meta.get("focus") or [])
        for f in focus:
            if f not in old_focus:
                old_focus.append(f)
        meta["focus"] = old_focus
        old_hits = list(meta.get("focus_hits") or [])
        for h in focus_hits:
            if h not in old_hits:
                old_hits.append(h)
        meta["focus_hits"] = old_hits[:50]
        meta["updated"] = config.today_str()
        wiki.write_page(p["path"], meta, p["body"])
        print(f"  [关注] {title} 标注 {len(old_hits)} 条命中")


def ingest_one(path, manifest, focus=None, task_id=None):
    focus = focus or []
    # 锚点只记文件名：同名多份时整份拒绝摄入，避免旧知识条目指向错误文件
    try:
        found = find_raw_file(path.name)
    except DuplicateFileError as e:
        return {"file": path.name, "error": str(e)}
    if found != path:
        return {"file": path.name, "error": f"资料库里存在同名文件（{found}），本份拒绝摄入，请先改名"}
    sha = sha256_of(path)
    if any(f["sha256"] == sha for f in manifest["files"]):
        print("  已摄入过（SHA256 一致），跳过")
        return None
    dup_name = next((f for f in manifest["files"] if f["file"] == path.name), None)
    if dup_name:
        return {"file": path.name, "error": "清单里已有同名文件但内容不同；锚点只记文件名，为免指错，本份拒绝摄入"}
    units, count, scanned, unit_kind = extract_to_store(path)
    if scanned:
        print("  [拒] 疑似扫描版（无文本层），不编译")
        return None
    if not any(u["text"] for u in units):
        print("  [拒] 未提取到任何文字，不编译")
        return None
    if source_kind(path) == "filing":
        # 财报走专用流程（结构化科目 + 勾稽 + 派生指标），见 schema/财报摄取-skill.md
        from . import filing as filing_mod
        return filing_mod.ingest_filing(path, units, unit_kind, manifest, sha, task_id=task_id)
    compiled = compile_file(path, units, unit_kind, focus=focus, task_id=task_id)
    topics = compiled["topics"]
    existing = wiki.load_pages()
    index_entries = [wiki.build_index_entry(p) for p in existing if p["type"] != "source"]
    decisions = plan_merge(topics, index_entries)
    by_slug = {p["slug"]: p for p in existing}
    created, merged, touched_titles = [], [], []
    total_added = 0
    for decision, topic in decisions:
        rel = path.relative_to(config.ROOT).as_posix()
        if decision["action"] == "merge" and decision["slug"] in by_slug:
            p = by_slug[decision["slug"]]
            added = _apply_merge(p["path"], p["meta"], p["body"], topic, [rel], f"并入 {path.name}")
            total_added += added
            title = str(p["meta"].get("title") or decision["slug"])
            merged.append(title)
            touched_titles.append(title)
            print(f"  并入 {decision['slug']}（+{added} 条）")
        else:
            new_path, cnt = _apply_create(decision, topic, [rel], f"新建页面，摄入 {path.name}")
            total_added += cnt
            created.append(topic["title"])
            touched_titles.append(topic["title"])
            print(f"  新建 {new_path.relative_to(config.WIKI_DIR).as_posix()}（{cnt} 条）")
    entries_count = sum(sum(len(v) for v in t["sections"].values()) for t in topics)
    if touched_titles:
        _write_source_page(path, touched_titles, compiled["report_date"], entries_count)
    focus_hits_all = [h for t in topics for h in (t.get("focus_hits") or [])]
    if focus and focus_hits_all:
        _tag_focus_pages(touched_titles, focus, focus_hits_all)
    manifest["files"].append({
        "file": path.name,
        "path": path.relative_to(config.ROOT).as_posix(),
        "sha256": sha,
        "unit": unit_kind,
        "count": count,
        "report_date": compiled["report_date"] or None,
        "source_type": source_kind(path),
        "ingested_at": config.now_iso(),
        "topics_touched": touched_titles,
        "entries_count": entries_count,
        "focus": list(focus),
        "focus_hits": focus_hits_all,
    })
    log = []
    if created:
        log.append("新建 " + "、".join(created))
    if merged:
        log.append("并入 " + "、".join(merged))
    return {
        "file": path.name,
        "entries_count": entries_count,
        "log": "；".join(log) or "未产出条目",
    }


def finish(manifest, results):
    """摄入收尾：落盘清单、重建索引、写日志、全库校验。逐份流水线每轮结束后调用一次。"""
    save_manifest(manifest)
    wiki.rebuild_index_md(wiki.load_pages())
    if results:
        summary = "；".join(f"{r['file']}（{r['entries_count']} 条）：{r['log']}" for r in results)
        wiki.append_log("ingest", summary)
    from . import validate
    problems = validate.validate_all()
    if problems:
        print(f"\n[校验] 全库存在 {len(problems)} 个问题：")
        for x in problems:
            print(f"  - {x}")
    else:
        print("\n[校验] 全库校验通过")
    return problems


def run_all(paths, progress=None, focus=None):
    manifest = load_manifest()
    results = []
    paths = list(paths)
    focus = focus or []
    if focus:
        print(f"[关注点] {len(focus)} 项：{'、'.join(focus)}")
    for i, p in enumerate(paths, 1):
        print(f"== 摄入 {p.name} ==")
        if progress:
            progress(i, p.name)
        try:
            r = ingest_one(p, manifest, focus=focus)
        except Exception as e:
            print(f"  失败: {e}")
            continue
        if r:
            results.append(r)
        save_manifest(manifest)  # 逐文件落盘，中途失败不丢已完成的部分
    finish(manifest, results)
    return results
