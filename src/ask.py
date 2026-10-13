import re

from . import config, llm, wiki
from .ingest import _apply_create, _apply_merge, load_manifest
from .pdf_extract import extract_to_store, load_cached, raw_file_index

REFUSAL = "知识库中未找到与该问题相关的页面，无法回答。"

# GLM 关闭思考后偶发在正文里混入一串无空格 ASCII 乱码（实测一例：<<+?"98?).CF"3-!#%1>'=)%@&）。
# 按特征剔除：连续>=8个可打印 ASCII 且不含空格、非字母数字占比>=0.4、含>=2个正文罕见符号。
# 锚点（#p11 只含1个罕见符号）、markdown 表格线（|-不在罕见集）不受影响。
_GLITCH_RARE = set("<>=?!#%&@~^$\\")


def strip_glitch(text):
    out, pos = [], 0
    for m in re.finditer(r"[\x21-\x7e]{8,}", text):
        seg = m.group()
        punct = sum(1 for c in seg if not c.isalnum())
        rare = sum(1 for c in seg if c in _GLITCH_RARE)
        if punct * 10 >= len(seg) * 4 and rare >= 2:
            out.append(text[pos:m.start()])
            pos = m.end()
    out.append(text[pos:])
    return "".join(out)

INTENT_PROMPT = """判断用户这条消息的意图与领域，只输出 JSON：
{"intent": "chat 或 report", "title": "report 时给一个不超过 16 字的报告标题，chat 给空字符串", "domain": "finstmt 或 general"}

chat = 普通问答、追问、信息咨询，包括「怎么样/多少/为什么/怎么看/什么观点/对比一下」这类问法。
report = 用户明确要求生成一份可保存的成文档交付物，措辞中带有「写一份/生成一份/帮我出一份/做一份报告/对比分析报告/财务报告」等明确的成文动作。只是打听情况、数据或观点，哪怕话题很专业，也算 chat。
finstmt = 问题涉及财务报表数据的分析理解：比率、盈利、毛利、现金流、资产负债、ROE/ROA/ROIC、偿债、周转（应收/存货/应付账款天数）、营收利润增速、财报地雷或造假嫌疑、审计意见等。只要问题需要读报表数字才能回答就算 finstmt，漏判比误判伤害大，拿不准给 finstmt。
general = 其余所有问题。

用户消息：{Q}"""

SELECT_PROMPT = """你是知识库检索员。下面是知识库索引（每行：slug | 类型 | 别名 | 摘要）：
{INDEX}

{HISTORY}用户问题：{Q}

选出回答该问题所需的页面，最多 {MAXP} 页，宁缺毋滥；问题与所有页面主题都不相关时给空数组。slug 必须来自索引。
问题问的是知识库自身整体情况（库里都有什么、是否都属于某行业、覆盖哪些主题、有多少页面），或需要横跨大部分页面做总览、不针对某个具体页面时，use_index 给 true 且 selected 给空数组，这类问题将直接用索引作答。
只输出 JSON：{"selected": ["slug", ...], "use_index": true 或 false, "reason": "一句话理由"}"""

ANSWER_PROMPT = """你是投研知识库的问答员，只能用给定页面内容回答。

规则：
- 只用给定页面内容，禁止使用你自己的知识补全
- 每个陈述句句尾带锚点 [[文件名.pdf#p页码]] 或 [[文件名.docx#para段落号]]，锚点只能引用给定页面中出现过的来源文件
- 页面没有的信息，明说「知识库中没有该信息」
- 不允许出现没有任何锚点的数据性陈述
- 回答用中文，markdown 格式
{FIN}
{HISTORY}--- 页面内容开始 ---
{PAGES}
--- 页面内容结束 ---

用户问题：{Q}"""

# 索引直答：问题针对知识库整体时用，索引无页码故不允许锚点
INDEX_ANSWER_PROMPT = """你是投研知识库的问答员，用户问的是知识库自身的整体情况（覆盖范围、内容构成、页面数量等）或跨页总览，只能用给定的知识库总索引回答（每行：slug | 类型 | 别名 | 摘要）。

规则：
- 只用索引信息回答，禁止使用你自己的知识补全
- 索引没有的信息，明说「知识库索引中看不出」
- 提到具体条目时用页面名（如「比亚迪」「智能汽车」），不用 slug
- 索引不含页码，回答中不允许出现任何 [[文件名.pdf#p页码]] 或 [[文件名.docx#para段落号]] 形式的锚点
- 索引信息不足以回答时，只输出 UNANSWERABLE 一个词，不输出其他内容
- 回答用中文，markdown 格式
{HISTORY}--- 知识库索引开始 ---
{INDEX}
--- 知识库索引结束 ---

用户问题：{Q}"""

# 财报分析类问题的附加纪律，浓缩自 schema/财报问答-skill.md v1.1；{FIN} 占位符按 domain 注入
FIN_ANALYSIS_BLOCK = """
本次问题属财报分析类，再遵守以下纪律（方法论来源：financial-statement-reading skill，张明辉、Berman 等三本书整合）：
- 呈现顺序倒金字塔：第一屏直接回答用户问题。问单一数据或事实时直接给数带口径；问分析、评价、风险类问题时，先给 3-5 条一句话判断（每条独立一行，句后括注一句依据），再给综合结论，六步框架的详细盘点放在其后展开，展开部分不重复抄写判断清单里已给的数字
- 先盘点给定内容中已有的财报数据再下判断：wiki 页面里的【数据】行是报表原值、【指标】行是固定口径的派生值、【管理层】行是原文摘录；原始材料则直接读报表数字。数据缺失的项明说「无法核验」，不用自身知识补
- 引用任何指标必须带口径标签与报告期（如 ROE(归母平均)@2025FY），中途换口径必须说明理由
- 引用判断阈值必须注明出处（张明辉方法），并说明其为台湾市场经验值，在 A 股的适用性需按行业实际分布校准
- 流动性判断先分负债成色再下结论：应付账款/应付票据/其他应付款/合同负债是无息经营占款，短期借款/一年内到期非流动负债才是有息短债。流动比率、速动比率低于阈值时必须补算类现金资产（货币资金+交易性金融资产+应收款项融资）对有息短期负债的覆盖倍数，DPO 高、CCC 为负的占款模式企业禁止只凭未校准的流动比率断言短期偿债压力大
- 分析顺序：资产负债表（结构、周转、杠杆、流动性）→ 损益表（成长、盈利结构分层、费用、非经常性）→ 现金流量表（获现比、FCF 口径声明、三大活动形态）→ 比率（ROE 杜邦拆解、ROIC）
- 三个机械信号必查并给数值：现金存量变动（期末现金及现金等价物较年初增减额与幅度）、利润补助依赖（其他收益 ÷ 营业利润）、藏杠杆工具（其他权益工具/永续债期末余额及本期发行赎回净额），异常项列入风险清单
- 地雷六特征逐项过数并给数值对照：DSO 过高、DIO 过低、应收存货应付同时异常增长、毛利率偏离同业、自由现金流长期为负、OCI 异常大；命中两项以上结论升级。六大警讯（高管与会计师更换等）属库外信息，只列待查项不编造
- 参考阈值：DSO < 90 天、DIO 30-60 天且 < 20 天可疑、流动比率 > 120%（用前须先按负债成色校准）、获现比持续 < 1 是盈利质量警报
- 数字呈现：金额换算为亿元保留两位小数（803,964,958,000 元写作 8039.65 亿元），比率、天数、倍数用原值；每个数据点的锚点只挂在首次出现处，后文再次提到不再重复带锚点
- 结论按证据强度分层：数据支持充分 / 部分支持 / 数据不足不下结论
"""

DISTILL_PROMPT = """判断这次问答是否产生了值得沉淀进知识库的新知识：跨页面综合得出、任何单个页面都没有明确写出的结论才算新知识，单页已有内容的复述不算。

只输出 JSON：
{"new_knowledge": true 或 false, "topic": "主题名（中文，新知识为 true 时给）", "type": "concept|company|industry", "summary": "一句话摘要", "lines": ["可沉淀的结论句，句尾锚点只能原样复用答案中已出现的锚点"]}
lines 规则：答案中属财报分析判断的行以【观点】前缀开头（格式：【观点】报告期 判断句，报告期如 2025FY），普通事实结论不加前缀。

问题：{Q}
答案：{A}"""

RAW_SUGGEST_PROMPT = """用户在投研知识库里提问，但知识库页面无法回答。下面是 raw 原始资料库的文件清单（每行：文件名 | 相对路径 | 已摄入页的一句话摘要或「未摄入」）：
{FILES}

判断哪些文件可能含有能回答该问题的内容。只依据文件名与摘要判断，宁缺毋滥，最多给 5 个；都不可能就给空数组。
只输出 JSON：{"candidates": [{"file": "文件名", "reason": "一句话依据"}]}

用户问题：{Q}"""

DEEP_ANSWER_PROMPT = """你是投研助手，只能用给定的原始材料片段回答，这些片段来自尚未入库的 raw 原始文件。

规则：
- 只用给定片段，禁止编造，材料片段没有的信息明说「所给材料中没有该信息」
- 每个陈述句句尾带锚点：PDF 片段用 [[文件名.pdf#p页码]]，Word 片段用 [[文件名.docx#para段落号]]，页码/段落号取片段标记中的序号
- 片段标记形如「[文件名 | 第3页]」，请原样使用其中的文件名与序号
- 不允许出现没有任何锚点的数据性陈述
- 回答用中文，markdown 格式
{FIN}
{HISTORY}--- 材料片段开始 ---
{UNITS}
--- 材料片段结束 ---

用户问题：{Q}"""

REFERRAL_NONE = "根据目前的wiki知识库中无法回答您的问题，并且我搜索了raw中的所有文件名称判断出可能在raw中不存在能回答您问题的依据，是否要进一步查询所有raw中的文件全部内容"
REFERRAL_SOME = "根据目前的wiki知识库中无法回答您的问题，但是我搜索了raw中的所有文件名称判断出{FILES}可能存在着您想要的答案，是否要进一步查询该文件或这些文件"


def _history_block(history):
    if not history:
        return ""
    lines = ["以下是本会话此前的问答摘要（仅供理解当前问题的指代，回答仍以当前问题为准）："]
    for h in history:
        lines.append(f"问：{h['q']}")
        lines.append(f"答：{h['a']}")
    return "\n".join(lines) + "\n\n"


def _detect_intent(question):
    try:
        out = llm.chat(
            [{"role": "user", "content": INTENT_PROMPT.replace("{Q}", question)}],
            task="select",
            response_json=True,
        )
    except llm.LLMError:
        return "chat", "", "general"
    domain = "finstmt" if out.get("domain") == "finstmt" else "general"
    if out.get("intent") == "report":
        return "report", str(out.get("title") or "").strip()[:16] or "未命名报告", domain
    return "chat", "", domain


def _violations(answer, allowed):
    # allowed: {文件名: (unit_kind, count)}
    bad = []
    for f, loc in wiki.ANCHOR_RE.findall(answer):
        info = allowed.get(f)
        if info is None:
            bad.append(f"锚点引用了未提供的文件 {f}")
            continue
        unit_kind, count = info
        if wiki.loc_kind(loc) != unit_kind:
            bad.append(f"锚点 {f}#{loc} 单元类型不对（应为{'段落' if unit_kind == 'para' else '页'}）")
        elif not 1 <= wiki.loc_num(loc) <= count:
            bad.append(f"锚点 {f}#{loc} 序号超出范围")
    if "知识库中没有" in answer and wiki.ANCHOR_RE.search(answer):
        bad.append("答案既称知识库中没有又带锚点，口径混乱")
    return bad


def _index_violations(answer):
    return [f"出现了索引回答不允许的锚点 {f}#{loc}" for f, loc in wiki.ANCHOR_RE.findall(answer)]


def _answer_from_index(question, history):
    """索引直答：以总索引为上下文作答，返回 (answer, violations) 或 None（索引答不了）。"""
    _, entries = _index_pages()
    if not entries:
        return None
    idx_text = wiki.compact_index_text(entries)
    # 页面计数机械算好放头部，不让模型自己数长列表
    counts = {}
    for e in entries:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    parts = "、".join(f"{t} {c}" for t, c in sorted(counts.items()))
    idx_text = f"（索引共 {len(entries)} 页：{parts}，另有来源摘要页未列入本索引）\n" + idx_text
    messages = [{"role": "user", "content": INDEX_ANSWER_PROMPT
                 .replace("{HISTORY}", _history_block(history))
                 .replace("{INDEX}", idx_text)
                 .replace("{Q}", question)}]
    answer = strip_glitch(llm.chat(messages, task="answer"))
    if "UNANSWERABLE" in answer:
        return None
    bad = _index_violations(answer)
    if bad:
        messages.append({"role": "assistant", "content": answer})
        messages.append({"role": "user", "content": (
            "以下内容违规，索引回答不允许出现任何锚点，请修正后重新输出完整答案：\n- " + "\n- ".join(bad)
        )})
        answer = strip_glitch(llm.chat(messages, task="answer"))
        if "UNANSWERABLE" in answer:
            return None
        bad = _index_violations(answer)
    return answer, bad


def _index_pages():
    pages = wiki.load_pages()
    entries = [wiki.build_index_entry(p) for p in pages if p["type"] != "source"]
    return pages, entries


def _allowed_from_pages(sel_pages):
    manifest = load_manifest()
    files_info = {f["file"]: (f.get("unit", "page"), f.get("count") or f.get("page_count") or 0)
                  for f in manifest["files"]}
    allowed = {}
    for p in sel_pages:
        for s in (p["meta"].get("sources") or []):
            name = s.rsplit("/", 1)[-1]
            if name in files_info:
                allowed[name] = files_info[name]
    return allowed


def _select_pages(question, history):
    pages, entries = _index_pages()
    if not entries:
        return pages, [], "", False
    idx_text = wiki.compact_index_text(entries)
    sel = llm.chat(
        [{"role": "user", "content": SELECT_PROMPT
          .replace("{INDEX}", idx_text)
          .replace("{HISTORY}", _history_block(history))
          .replace("{Q}", question)
          .replace("{MAXP}", str(config.CFG["ask"]["max_pages"]))}],
        task="select",
        response_json=True,
    )
    valid_slugs = {e["slug"] for e in entries}
    slugs = [s for s in (sel.get("selected") or []) if s in valid_slugs]
    return pages, slugs, str(sel.get("reason") or ""), bool(sel.get("use_index"))


def _suggest_raw_files(question):
    idx = raw_file_index()
    if not idx:
        return []
    summaries = {}
    for p in wiki.load_pages():
        if p["type"] == "source":
            name = str(p["meta"].get("title") or "")
            summary = str(p["meta"].get("summary") or "")
            summaries.setdefault(name, summary)
    manifest = {f["file"] for f in load_manifest()["files"]}
    lines = []
    for name in sorted(idx):
        note = "未摄入" if name not in manifest else (summaries.get(_stem(name), "") or "已摄入")
        lines.append(f"{name} | {idx[name].relative_to(config.RAW_DIR).as_posix()} | {note}")
    try:
        out = llm.chat(
            [{"role": "user", "content": RAW_SUGGEST_PROMPT
              .replace("{FILES}", "\n".join(lines[:80]))
              .replace("{Q}", question)}],
            task="select",
            response_json=True,
        )
    except llm.LLMError:
        return []
    res = []
    for c in (out.get("candidates") or [])[:5]:
        f = str(c.get("file") or "").strip()
        if f in idx:
            res.append({"file": f, "reason": str(c.get("reason") or "")})
    return res


def _stem(name):
    # 来源摘要页页名 = 文件名去扩展名
    return name.rsplit(".", 1)[0]


def _collect_deep_units(question, files=None, char_budget=20000):
    """提取候选文件单元文本。指定 files 用其全部单元；否则全库文件先按关键词粗筛再取片段。"""
    idx = raw_file_index()
    targets = []
    if files:
        targets = [idx[f] for f in files if f in idx]
    else:
        targets = list(idx.values())
    pieces = []
    allowed = {}
    used = 0
    if files:
        for p in sorted(targets, key=lambda x: x.name):
            cached = load_cached(p) or _extract_payload(p)
            if not cached:
                continue
            allowed[p.name] = (cached["unit"], cached["count"])
            for u in cached["units"]:
                if not u["text"]:
                    continue
                if used + len(u["text"]) > char_budget:
                    break
                tag = "段" if cached["unit"] == "para" else "页"
                pieces.append(f"[{p.name} | 第{u['n']}{tag}]\n{u['text']}")
                used += len(u["text"])
    else:
        scored = []
        keys = _question_keys(question)
        for p in sorted(targets, key=lambda x: x.name):
            cached = load_cached(p) or _extract_payload(p)
            if not cached:
                continue
            allowed[p.name] = (cached["unit"], cached["count"])
            for u in cached["units"]:
                if not u["text"]:
                    continue
                score = sum(1 for k in keys if k in u["text"])
                if score:
                    scored.append((score, p.name, cached["unit"], u))
        scored.sort(key=lambda x: -x[0])
        for score, fname, unit, u in scored[:60]:
            tag = "段" if unit == "para" else "页"
            text = u["text"]
            if used + len(text) > char_budget:
                continue
            pieces.append(f"[{fname} | 第{u['n']}{tag}]\n{text}")
            used += len(text)
    return pieces, allowed


def _question_keys(question):
    # 关键词粗筛：取问题里长度>=2的连续片段（去标点），命中越多越相关
    import re as _re

    words = _re.split(r"[^\w一-鿿]+", question)
    keys = set()
    for w in words:
        if len(w) >= 2:
            keys.add(w)
            if len(w) >= 4:  # 长词再补二元切分，提高召回
                keys.update(w[i:i + 2] for i in range(len(w) - 1))
    return keys or {question}


def _extract_payload(path):
    try:
        units, count, scanned, unit = extract_to_store(path)
    except Exception:
        return None
    if scanned:
        return None
    return {"unit": unit, "count": count, "units": units}


def deep_ask(question, history=None, files=None):
    """raw 原文深查。files=None 对全库文件关键词粗筛后作答；给文件名列表则只查这些文件。"""
    if files:
        reason = "raw 原文深查：" + "、".join(files)
    else:
        reason = "raw 原文深查（全库关键词粗筛）"
    result = {"kind": "answer", "deep": True, "question": question,
              "step1": {"selected": [], "reason": reason},
              "answer_md": "", "validation": {"ok": True, "violations": []}, "proposal": None}
    _, _, domain = _detect_intent(question)
    pieces, allowed = _collect_deep_units(question, files=files)
    if not pieces:
        result["answer_md"] = "没有从 raw 文件提取到可用于回答的文字（可能均为扫描版或文件已移走）。"
        return result
    fin = FIN_ANALYSIS_BLOCK if domain == "finstmt" else ""
    messages = [{"role": "user", "content": DEEP_ANSWER_PROMPT
                 .replace("{FIN}", fin)
                 .replace("{HISTORY}", _history_block(history))
                 .replace("{UNITS}", "\n\n".join(pieces))
                 .replace("{Q}", question)}]
    answer = strip_glitch(llm.chat(messages, task="answer"))
    bad = _violations(answer, allowed)
    if bad:
        messages.append({"role": "assistant", "content": answer})
        messages.append({"role": "user", "content": (
            "以下锚点违规，请修正后重新输出完整答案（只改锚点相关内容）：\n- " + "\n- ".join(bad)
        )})
        answer = strip_glitch(llm.chat(messages, task="answer"))
        bad = _violations(answer, allowed)
    result["answer_md"] = answer
    result["validation"] = {"ok": not bad, "violations": bad}
    if not bad:
        result["proposal"] = _propose_distill(question, answer)
    return result


def ask(question, history=None, propose=True):
    """两步问答。问知识库整体的问题走索引直答；选页为空时先以索引兜底，再走 raw 文件名回退；返回 kind=answer/referral/report。"""
    result = {"kind": "answer", "question": question,
              "step1": {"selected": [], "reason": ""},
              "answer_md": "", "validation": {"ok": True, "violations": []}, "proposal": None}
    intent, title, domain = _detect_intent(question)
    if intent == "report":
        return {"kind": "report", "question": question, "title": title, "domain": domain}

    pages, slugs, reason, use_index = _select_pages(question, history)
    result["step1"] = {"selected": slugs, "reason": reason}
    if use_index:
        got = _answer_from_index(question, history)
        if got is not None:
            answer, bad = got
            result["answer_md"] = answer
            result["validation"] = {"ok": not bad, "violations": bad}
            return result
    if not slugs:
        got = None
        if not use_index:
            got = _answer_from_index(question, history)
        if got is not None:
            answer, bad = got
            result["answer_md"] = answer
            result["validation"] = {"ok": not bad, "violations": bad}
            result["step1"]["reason"] = "选页为空，改用知识库总索引直答"
            return result
        candidates = _suggest_raw_files(question)
        if candidates:
            names = "、".join(f"「{c['file']}」" for c in candidates)
            result["answer_md"] = REFERRAL_SOME.replace("{FILES}", names)
        else:
            result["answer_md"] = REFERRAL_NONE
        result["kind"] = "referral"
        result["candidates"] = candidates
        return result

    sel_pages = [p for p in pages if p["slug"] in slugs and p["type"] != "source"]
    pages_text = "\n\n".join(
        f"### {p['meta'].get('title', p['slug'])}（slug: {p['slug']}）\n{p['body']}" for p in sel_pages
    )
    fin = FIN_ANALYSIS_BLOCK if domain == "finstmt" else ""
    messages = [{"role": "user", "content": ANSWER_PROMPT
                 .replace("{FIN}", fin)
                 .replace("{HISTORY}", _history_block(history))
                 .replace("{PAGES}", pages_text)
                 .replace("{Q}", question)}]
    answer = strip_glitch(llm.chat(messages, task="answer"))
    allowed = _allowed_from_pages(sel_pages)
    bad = _violations(answer, allowed)
    if bad:
        messages.append({"role": "assistant", "content": answer})
        messages.append({"role": "user", "content": (
            "以下锚点违规，请修正后重新输出完整答案（只改锚点相关内容）：\n- " + "\n- ".join(bad)
        )})
        answer = strip_glitch(llm.chat(messages, task="answer"))
        bad = _violations(answer, allowed)
    result["answer_md"] = answer
    result["validation"] = {"ok": not bad, "violations": bad}
    if propose and not bad:
        result["proposal"] = _propose_distill(question, answer)
    return result


def _propose_distill(question, answer):
    """判定并构造沉淀提案，只算不写盘。"""
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
    ptype = out.get("type") if out.get("type") in ("company", "industry", "concept") else "concept"
    proposal = {
        "question": question,
        "title": str(out.get("topic") or "").strip() or "综合结论",
        "type": ptype,
        "summary": str(out.get("summary") or ""),
        "lines": lines,
        "action": "create",
        "target_slug": "",
        "target_title": "",
        "target_type": "",
    }
    # 命中已有页则改为 merge（应用时按 slug 重新定位，防止这期间页面变化）
    for p in wiki.load_pages():
        t = str(p["meta"].get("title") or p["slug"])
        if p["type"] != ptype:
            continue
        if t == proposal["title"] or t in proposal["title"] or proposal["title"] in t:
            proposal["action"] = "merge"
            proposal["target_slug"] = p["slug"]
            proposal["target_title"] = t
            proposal["target_type"] = p["type"]
            break
    return proposal


def apply_distill(proposal, chosen_lines=None):
    """把提案（可只取部分行）写入 wiki，返回 {page, action, lines}。【观点】行进「分析观点」节。"""
    lines = list(proposal["lines"])
    if chosen_lines:
        keep = [ln for ln in chosen_lines if ln in lines]
        if not keep:
            return None
        lines = keep
    sections = {}
    for ln in lines:
        bucket = "分析观点" if ln.startswith("【观点】") else "其他"
        sections.setdefault(bucket, []).append(ln)
    topic = {
        "title": proposal["title"],
        "type": proposal["type"],
        "aliases": [],
        "summary": proposal.get("summary") or "",
        "sections": sections,
    }
    pages = wiki.load_pages()
    hit = None
    if proposal.get("action") == "merge":
        hit = next((p for p in pages
                    if p["type"] == proposal.get("target_type") and p["slug"] == proposal.get("target_slug")), None)
    manifest = load_manifest()
    path_of = {f["file"]: f["path"] for f in manifest["files"]}
    anchor_files = {a[0] for ln in lines for a in wiki.ANCHOR_RE.findall(ln)}
    extra_sources = [path_of[f] for f in sorted(anchor_files) if f in path_of]
    question = proposal.get("question") or ""
    if hit:
        _apply_merge(hit["path"], hit["meta"], hit["body"], topic, extra_sources, "会话中提取")
        ret = {"page": hit["slug"], "action": "merge", "lines": len(lines)}
    else:
        decision = {"action": "create", "type": topic["type"]}
        path, count = _apply_create(decision, topic, extra_sources, "会话中提取新建")
        ret = {"page": path.stem, "action": "create", "lines": count}
    wiki.rebuild_index_md(wiki.load_pages())
    wiki.append_log("ask", f"会话中提取新知识到 {ret['page']}（{ret['lines']} 条）：{question}")
    return ret
