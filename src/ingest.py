import json
import re
from pathlib import Path

from . import config, llm, wiki
from .classify import sha256_of, sanitize
from .pdf_extract import extract_to_store

COMPILE_PROMPT = """你是投研知识编辑，把一份材料的逐页文本编译成 wiki 知识条目。

规则：
- 只用给定材料，禁止编造，材料里没有的信息不写
- 每条知识一句话，句尾必须带锚点 [[{FILE}#p页码]]，页码取该内容所在页的实际页码
- 保留原文关键数字（金额、增速、比率、评级、目标价等），不改口径
- 营销表述、免责声明、分析师介绍一律丢弃
- 主题划分：公司给 type=company，行业给 type=industry，估值方法/政策术语等给 type=concept
- 主题名与别名用中文规范名（英文材料也译成中文通用名；证券代码等专有缩写可保留英文）
- 分节只能用这些节名——公司/行业页：公司概况、行业格局、经营与财务、盈利预测、估值、风险提示、其他；概念页：定义与口径、方法要点、政策背景、应用示例、其他
- 每个主题给一句不超过 40 字的 summary

只输出 JSON，不要解释：
{
  "report_date": "材料中的报告日期 YYYY-MM-DD，识别不出给空字符串",
  "topics": [
    {
      "title": "主题名",
      "type": "company|industry|concept",
      "aliases": ["别名或代码，没有给空数组"],
      "summary": "一句话摘要",
      "sections": {"经营与财务": ["某条知识句尾带锚点[[{FILE}#p5]]", "..."]}
    }
  ]
}

--- 材料开始（每页前有页码标记）---
{TEXT}
--- 材料结束 ---"""

MERGE_PROMPT = """现有知识库索引如下（每行：slug | 类型 | 别名），可能为空：
{INDEX}

新摄入材料提炼出以下主题（每行：标题 | 类型 | 别名）：
{TOPICS}

为每个新主题决定归并方式。只输出 JSON 数组，不要解释：
[{"title": "新主题名", "action": "create 或 merge", "slug": "merge 时必须从上面索引的 slug 里选；create 时给规范 slug（中文可用，不含空格和特殊符号）", "type": "company|industry|concept"}]
拿不准就 create，宁可新建碎页交给审校收编。"""


def iter_pdfs():
    for d in (config.REPORTS_DIR, config.FILINGS_DIR, config.POLICIES_DIR):
        yield from sorted(d.rglob("*.pdf"))


def source_kind(pdf_path):
    rel = pdf_path.relative_to(config.RAW_DIR).as_posix()
    for kind in ("report", "filing", "policy"):
        if rel.startswith(kind + "s/"):
            return kind
    return "unknown"


def load_manifest():
    if config.MANIFEST_PATH.exists():
        return json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    return {"files": []}


def save_manifest(m):
    config.MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.MANIFEST_PATH.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")


def _norm(s):
    return re.sub(r"[\s·・]", "", str(s)).casefold()


def _safe_type(t):
    return t if t in ("company", "industry", "concept") else "concept"


def _valid_lines(lines, fname, page_count):
    # 锚点校验：每条必须有锚点且全部指向本文件、页码在范围内，违规行丢弃
    keep, dropped = [], 0
    for ln in lines:
        ln = str(ln).strip()
        ln = ln.removeprefix("- ").strip()
        if not ln:
            continue
        found = wiki.ANCHOR_RE.findall(ln)
        if not found or not all(a[0] == fname and 1 <= int(a[1]) <= page_count for a in found):
            dropped += 1
            continue
        if ln not in keep:
            keep.append(ln)
    return keep, dropped


def compile_file(pdf_path, pages):
    fname = pdf_path.name
    chunk_size = config.CFG["ingest"]["compile_chunk_pages"]
    chunks = [pages] if len(pages) <= 40 else [
        pages[i:i + chunk_size] for i in range(0, len(pages), chunk_size)
    ]
    topics = {}
    report_date = ""
    total_dropped = 0
    for chunk in chunks:
        text = "\n\n".join(f"[第{p['page']}页]\n{p['text']}" for p in chunk if p["text"])
        out = llm.chat(
            [{"role": "user", "content": COMPILE_PROMPT.replace("{FILE}", fname).replace("{TEXT}", text)}],
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
            })
            ex["aliases"].extend(str(a) for a in (t.get("aliases") or []) if a)
            if not ex["summary"] and t.get("summary"):
                ex["summary"] = str(t["summary"])
            for s, lines in (t.get("sections") or {}).items():
                s = str(s).strip() or "其他"
                keep, dropped = _valid_lines(lines, fname, len(pages))
                total_dropped += dropped
                bucket = ex["sections"].setdefault(s, [])
                for ln in keep:
                    if ln not in bucket:
                        bucket.append(ln)
    topics = {k: v for k, v in topics.items() if any(v["sections"].values())}
    if total_dropped:
        print(f"  [校验] 丢弃 {total_dropped} 条锚点不合规的条目")
    out_path = config.EXTRACTS_DIR / (pdf_path.stem + ".entries.json")
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


def _write_source_page(pdf_path, topic_titles, report_date, entries_count):
    title = pdf_path.stem
    path = config.TYPE_DIRS["source"] / f"{title}.md"
    rel = pdf_path.relative_to(config.ROOT).as_posix()
    info = [
        f"文件：{pdf_path.name}",
        f"类型：{source_kind(pdf_path)}",
        f"报告日期：{report_date or '未识别'}",
        f"知识条目：{entries_count} 条",
    ]
    links = "、".join(f"[[{t}]]" for t in topic_titles)
    sections = {"材料信息": info, "贡献": [f"贡献主题页：{links}" if links else "贡献主题页：无"]}
    meta = {"title": title, "type": "source", "sources": [rel], "updated": config.today_str()}
    wiki.write_page(path, meta, wiki.render_page(title, sections, "source"))


def ingest_one(pdf_path, manifest):
    sha = sha256_of(pdf_path)
    if any(f["sha256"] == sha for f in manifest["files"]):
        print("  已摄入过（SHA256 一致），跳过")
        return None
    dup_name = next((f for f in manifest["files"] if f["file"] == pdf_path.name), None)
    if dup_name:
        print(f"  [警告] 与已摄入的 {dup_name['file']} 同名但内容不同，旧锚点将指向新文件内容")
    pages, page_count, scanned = extract_to_store(pdf_path)
    if scanned:
        print("  [拒] 疑似扫描版（无文本层），不编译")
        return None
    compiled = compile_file(pdf_path, pages)
    topics = compiled["topics"]
    existing = wiki.load_pages()
    index_entries = [wiki.build_index_entry(p) for p in existing if p["type"] != "source"]
    decisions = plan_merge(topics, index_entries)
    by_slug = {p["slug"]: p for p in existing}
    created, merged, touched_titles = [], [], []
    total_added = 0
    for decision, topic in decisions:
        rel = pdf_path.relative_to(config.ROOT).as_posix()
        if decision["action"] == "merge" and decision["slug"] in by_slug:
            p = by_slug[decision["slug"]]
            added = _apply_merge(p["path"], p["meta"], p["body"], topic, [rel], f"并入 {pdf_path.name}")
            total_added += added
            title = str(p["meta"].get("title") or decision["slug"])
            merged.append(title)
            touched_titles.append(title)
            print(f"  并入 {decision['slug']}（+{added} 条）")
        else:
            path, count = _apply_create(decision, topic, [rel], f"新建页面，摄入 {pdf_path.name}")
            total_added += count
            created.append(topic["title"])
            touched_titles.append(topic["title"])
            print(f"  新建 {path.relative_to(config.WIKI_DIR).as_posix()}（{count} 条）")
    entries_count = sum(sum(len(v) for v in t["sections"].values()) for t in topics)
    if touched_titles:
        _write_source_page(pdf_path, touched_titles, compiled["report_date"], entries_count)
    manifest["files"].append({
        "file": pdf_path.name,
        "path": pdf_path.relative_to(config.ROOT).as_posix(),
        "sha256": sha,
        "page_count": page_count,
        "report_date": compiled["report_date"] or None,
        "source_type": source_kind(pdf_path),
        "ingested_at": config.now_iso(),
        "topics_touched": touched_titles,
        "entries_count": entries_count,
    })
    log = []
    if created:
        log.append("新建 " + "、".join(created))
    if merged:
        log.append("并入 " + "、".join(merged))
    return {
        "file": pdf_path.name,
        "entries_count": entries_count,
        "log": "；".join(log) or "未产出条目",
    }


def run_all(paths):
    manifest = load_manifest()
    results = []
    for p in paths:
        print(f"== 摄入 {p.name} ==")
        try:
            r = ingest_one(p, manifest)
        except Exception as e:
            print(f"  失败: {e}")
            continue
        if r:
            results.append(r)
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
    return results
