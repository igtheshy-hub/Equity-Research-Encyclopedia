from pathlib import Path

from . import config, llm, wiki
from .ingest import _apply_create, _apply_merge, load_manifest

REFUSAL = "知识库中未找到与该问题相关的页面，无法回答。"

SELECT_PROMPT = """你是知识库检索员。下面是知识库索引（每行：slug | 类型 | 别名 | 摘要）：
{INDEX}

用户问题：{Q}

选出回答该问题所需的页面，最多 {MAXP} 页，宁缺毋滥；问题与所有页面主题都不相关时给空数组。slug 必须来自索引。
只输出 JSON：{"selected": ["slug", ...], "reason": "一句话理由"}"""

ANSWER_PROMPT = """你是投研知识库的问答员，只能用给定页面内容回答。

规则：
- 只用给定页面内容，禁止使用你自己的知识补全
- 每个陈述句句尾带锚点 [[文件名.pdf#p页码]]，锚点只能引用给定页面中出现过的来源文件
- 页面没有的信息，明说「知识库中没有该信息」
- 不允许出现没有任何锚点的数据性陈述
- 回答用中文，markdown 格式

--- 页面内容开始 ---
{PAGES}
--- 页面内容结束 ---

用户问题：{Q}"""

DISTILL_PROMPT = """判断这次问答是否产生了值得沉淀进知识库的新知识：跨页面综合得出、任何单个页面都没有明确写出的结论才算新知识，单页已有内容的复述不算。

只输出 JSON：
{"new_knowledge": true 或 false, "topic": "主题名（中文，新知识为 true 时给）", "type": "concept|company|industry", "summary": "一句话摘要", "lines": ["可沉淀的结论句，句尾锚点只能原样复用答案中已出现的锚点"]}

问题：{Q}
答案：{A}"""


def _violations(answer, sel_pages):
    manifest = load_manifest()
    files_info = {f["file"]: f for f in manifest["files"]}
    allowed_files = set()
    for p in sel_pages:
        allowed_files.update(Path(s).name for s in (p["meta"].get("sources") or []))
    bad = []
    for f, pg in wiki.ANCHOR_RE.findall(answer):
        if f not in allowed_files:
            bad.append(f"锚点引用了所选页面之外的文件 {f}")
        elif f not in files_info or not 1 <= int(pg) <= files_info[f]["page_count"]:
            bad.append(f"锚点 {f}#p{pg} 页码不合法")
    if "知识库中没有" in answer and wiki.ANCHOR_RE.search(answer):
        bad.append("答案既称知识库中没有又带锚点，口径混乱")
    return bad


def _index_pages():
    pages = wiki.load_pages()
    entries = [wiki.build_index_entry(p) for p in pages if p["type"] != "source"]
    return pages, entries


def _distill(question, answer):
    try:
        out = llm.chat(
            [{"role": "user", "content": DISTILL_PROMPT.replace("{Q}", question).replace("{A}", answer)}],
            task="audit",
            response_json=True,
        )
    except llm.LLMError:
        return None
    if not out.get("new_knowledge"):
        return None
    lines = [str(x).strip() for x in (out.get("lines") or []) if str(x).strip()]
    valid_anchors = set(wiki.ANCHOR_RE.findall(answer))
    lines = [ln for ln in lines if wiki.ANCHOR_RE.findall(ln) and set(wiki.ANCHOR_RE.findall(ln)) <= valid_anchors]
    if not lines:
        return None
    topic = {
        "title": str(out.get("topic") or "").strip() or "综合结论",
        "type": out.get("type") if out.get("type") in ("company", "industry", "concept") else "concept",
        "aliases": [],
        "summary": str(out.get("summary") or ""),
        "sections": {"其他": lines},
    }
    pages = wiki.load_pages()

    def _hit_score(p):
        t = str(p["meta"].get("title") or p["slug"])
        if p["type"] != topic["type"]:
            return 0
        if t == topic["title"]:
            return 3
        if t in topic["title"] or topic["title"] in t:
            return 2
        return 0

    candidates = [(p, _hit_score(p)) for p in pages]
    hit = next((p for p, s in candidates if s >= 2), None)
    manifest = load_manifest()
    path_of = {f["file"]: f["path"] for f in manifest["files"]}
    anchor_files = {a[0] for ln in lines for a in wiki.ANCHOR_RE.findall(ln)}
    extra_sources = [path_of[f] for f in sorted(anchor_files) if f in path_of]
    if hit:
        _apply_merge(hit["path"], hit["meta"], hit["body"], topic, extra_sources, "问答沉淀")
        return {"page": hit["slug"], "action": "merge", "lines": len(lines)}
    decision = {"action": "create", "type": topic["type"]}
    path, count = _apply_create(decision, topic, extra_sources, "问答沉淀新建")
    return {"page": path.stem, "action": "create", "lines": count}


def ask(question, distill=True):
    result = {
        "question": question,
        "step1": {"selected": [], "reason": ""},
        "answer_md": "",
        "validation": {"ok": True, "violations": []},
        "distill": None,
    }
    pages, entries = _index_pages()
    if not entries:
        result["step1"]["reason"] = "知识库为空"
        result["answer_md"] = REFUSAL
        return result
    idx_text = wiki.compact_index_text(entries)
    sel = llm.chat(
        [{"role": "user", "content": SELECT_PROMPT
          .replace("{INDEX}", idx_text)
          .replace("{Q}", question)
          .replace("{MAXP}", str(config.CFG["ask"]["max_pages"]))}],
        task="select",
        response_json=True,
    )
    valid_slugs = {e["slug"] for e in entries}
    slugs = [s for s in (sel.get("selected") or []) if s in valid_slugs]
    result["step1"] = {"selected": slugs, "reason": str(sel.get("reason") or "")}
    if not slugs:
        result["answer_md"] = REFUSAL
        return result
    sel_pages = [p for p in pages if p["slug"] in slugs and p["type"] != "source"]
    pages_text = "\n\n".join(
        f"### {p['meta'].get('title', p['slug'])}（slug: {p['slug']}）\n{p['body']}" for p in sel_pages
    )
    messages = [{"role": "user", "content": ANSWER_PROMPT.replace("{PAGES}", pages_text).replace("{Q}", question)}]
    answer = llm.chat(messages, task="answer")
    bad = _violations(answer, sel_pages)
    if bad:
        messages.append({"role": "assistant", "content": answer})
        messages.append({"role": "user", "content": (
            "以下锚点违规，请修正后重新输出完整答案（只改锚点相关内容）：\n- " + "\n- ".join(bad)
        )})
        answer = llm.chat(messages, task="answer")
        bad = _violations(answer, sel_pages)
    result["answer_md"] = answer
    result["validation"] = {"ok": not bad, "violations": bad}
    if distill and not bad:
        result["distill"] = _distill(question, answer)
        if result["distill"]:
            wiki.rebuild_index_md(wiki.load_pages())
            wiki.append_log("ask", f"问答沉淀新知识到 {result['distill']['page']}（{result['distill']['lines']} 条）：{question}")
    return result


def save_memo(result, title):
    config.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"memo-{config.today_str().replace('-', '')}-{title}.md"
    path = config.OUTPUTS_DIR / fname
    lines = [
        f"# 研究备忘：{title}",
        "",
        f"- 日期：{config.today_str()}",
        f"- 问题：{result['question']}",
        f"- 选页：{'、'.join(result['step1']['selected']) or '（无）'}（{result['step1']['reason']}）",
        f"- 校验：{'通过' if result['validation']['ok'] else '存在违规：' + '；'.join(result['validation']['violations'])}",
        "",
        "## 回答",
        "",
        result["answer_md"],
    ]
    if result.get("distill"):
        d = result["distill"]
        lines += ["", f"（本次问答沉淀了 {d['lines']} 条新知识到 {d['page']}）"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
