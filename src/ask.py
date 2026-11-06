import re

from . import anchor_verify, config, llm, source_priority, wiki
from .ingest import _apply_create, _apply_merge, load_manifest
from .pdf_extract import extract_to_store, load_cached, raw_file_index

REFUSAL = "知识库中未找到与该问题相关的页面，无法回答。"

# 历史补丁：GLM 关闭思考后偶发在正文里混入一串无空格 ASCII 乱码（实测一例：<<+?"98?).CF"3-!#%1>'=)%@&）。
# 换用其他模型后此问题未复现，但剔除逻辑代价极低，保留作保险。
# 按特征剔除：连续>=8个可打印 ASCII 且不含空格、非字母数字占比>=0.4、含>=2个正文罕见符号。
# 锚点（#p11 只含1个罕见符号）、markdown 表格线（|-不在罕见集）不受影响。
_GLITCH_RARE = set("<>=?!#%&@~^$\\")
# 同环比这类数字对「+34.4%/-4.7%」也满足乱码特征（ASCII、标点多、两个%），
# 不能按乱码删掉；纯数字与千分位、正负号、百分号、斜杠组成的片段一律保留
_NUMERIC_RUN_RE = re.compile(
    r"^[+\-−±]?\d[\d,.]*(?:%|pct|个百分点)?(?:/[+\-−±]?\d[\d,.]*(?:%|pct|个百分点)?)*$")


def strip_glitch(text):
    out, pos = [], 0
    for m in re.finditer(r"[\x21-\x7e]{8,}", text):
        seg = m.group()
        if _NUMERIC_RUN_RE.match(seg.strip("[]")):
            continue
        punct = sum(1 for c in seg if not c.isalnum())
        rare = sum(1 for c in seg if c in _GLITCH_RARE)
        if punct * 10 >= len(seg) * 4 and rare >= 2:
            out.append(text[pos:m.start()])
            pos = m.end()
    out.append(text[pos:])
    return "".join(out)

INTENT_PROMPT = """判断用户这条消息的意图与领域，只输出 JSON：
{"intent": "chat 或 report", "title": "report 时给一个不超过 16 字的报告标题，chat 给空字符串", "domain": "finstmt 或 general", "report_type": "industry、company 或 builtin", "method": "earnings、comps、both 或 none"}

chat = 普通问答、追问、信息咨询，包括「怎么样/多少/为什么/怎么看/什么观点/对比一下」这类问法。
report = 用户明确要求生成一份可保存的成文档交付物，措辞中带有「写一份/生成一份/帮我出一份/做一份报告/对比分析报告/财务报告」等明确的成文动作。只是打听情况、数据或观点，哪怕话题很专业，也算 chat。
finstmt = 问题涉及财务报表数据的分析理解：比率、盈利、毛利、现金流、资产负债、ROE/ROA/ROIC、偿债、周转（应收/存货/应付账款天数）、营收利润增速、财报地雷或造假嫌疑、审计意见等。只要问题需要读报表数字才能回答就算 finstmt，漏判比误判伤害大，拿不准给 finstmt。
general = 其余所有问题。
report_type = 只在 intent=report 时有意义：写某一行业的研报（行业研报、行业深度、行业月报、行业专题）给 industry；写单一一家公司的研究报告（公司深度、公司研究、个股研报）给 company；多公司对比分析、财务分析报告、快速数据底稿、其他报告给 builtin。拿不准给 builtin。
method = 只在 intent=chat 时有意义，判断回答该问题该用哪种分析框架：业绩点评、季报中报年报解读、业绩超预期或不及预期、单期经营数据拆解给 earnings；可比公司、同业估值对比、相对估值、估值溢价折价、对标给 comps；两者都明显涉及给 both；其余给 none。拿不准给 none，不要为了专业感硬套框架。

用户消息：{Q}"""

SELECT_PROMPT = """你是知识库检索员。下面是知识库索引（每行：slug | 类型 | 别名 | 摘要）：
{INDEX}

{HISTORY}用户问题：{Q}

选出回答该问题所需的页面，{MAXRULE}，宁缺毋滥；问题与所有页面主题都不相关时给空数组。slug 必须来自索引。
问题问的是知识库自身整体情况（库里都有什么、是否都属于某行业、覆盖哪些主题、有多少页面），或需要横跨大部分页面做总览、不针对某个具体页面时，use_index 给 true 且 selected 给空数组，这类问题将直接用索引作答。
只输出 JSON：{"selected": ["slug", ...], "use_index": true 或 false, "reason": "一句话理由"}"""

ANSWER_PROMPT = """你是行业知识库的问答员，只能用给定页面内容回答。

规则：
- 只用给定页面内容，禁止使用你自己的知识补全
- 每个陈述句句尾带锚点 [[文件名.pdf#p页码]] 或 [[文件名.docx#para段落号]]，锚点只能引用给定页面中出现过的来源文件
- 页面没有的信息，明说「知识库中没有该信息」
- 不允许出现没有任何锚点的数据性陈述
- 公司财务数据的来源优先级：财报（年报、半年报、季报）> 公告/招股书 > 券商研报 > 其他材料。同一公司财务指标冲突时，以最新报告期的财报为准；研报数字只能作对照，并写明差异与各自报告期。行业总量数据不套此序，统计局、行业协会、海关等一手来源优先于研报预测
- 回答用中文，markdown 格式
{FIN}
{METHOD}
{HISTORY}--- 页面内容开始 ---
{PAGES}
--- 页面内容结束 ---

用户问题：{Q}

输出前自检：正文每个含数据的句子句尾是否都带了 [[文件名.pdf#p页码]] 或 [[文件名.docx#para段落号]] 锚点？锚点只能取自上方页面里出现过的来源文件。倒金字塔第一屏的判断句同样要带锚点，不能只写「（依据：…）」而不挂锚点。有缺的补上再输出。"""

# 索引直答：问题针对知识库整体时用，索引无页码故不允许锚点
INDEX_ANSWER_PROMPT = """你是行业知识库的问答员，用户问的是知识库自身的整体情况（覆盖范围、内容构成、页面数量等）或跨页总览，只能用给定的知识库总索引回答（每行：slug | 类型 | 别名 | 摘要）。

规则：
- 只用索引信息回答，禁止使用你自己的知识补全
- 提到具体条目时用页面名（如「比亚迪」「智能汽车」），不用 slug
- 索引不含页码，回答中不允许出现任何 [[文件名.pdf#p页码]] 或 [[文件名.docx#para段落号]] 形式的锚点
- 索引信息不足以回答时，只输出 UNANSWERABLE 一个词，不输出其他内容；不要写「索引中看不出」之类的说明句
- 回答用中文，markdown 格式
{HISTORY}--- 知识库索引开始 ---
{INDEX}
--- 知识库索引结束 ---

用户问题：{Q}"""

# 财报分析类问题的附加纪律，浓缩自 .claude/skills/finstmt-qa v1.1；{FIN} 占位符按 domain 注入
FIN_ANALYSIS_BLOCK = """
本次问题属财报分析类，再遵守以下纪律（方法论来源：financial-statement-reading skill，张明辉、Berman 等三本书整合）：
- 公司财务数据的来源优先级：财报（年报、半年报、季报）> 公告/招股书 > 券商研报 > 其他材料。同一公司财务指标冲突时，以最新报告期的财报为准；研报数字只能作对照，并写明差异与各自报告期。行业总量数据不套此序，统计局、行业协会、海关等一手来源优先于研报预测
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
{"new_knowledge": true 或 false, "topic": "主题名（中文，新知识为 true 时给）", "type": "concept|company|industry", "summary": "一句话摘要", "lines": ["可沉淀的结论句"]}
lines 规则：
- 每行句尾必须带至少一个锚点，格式 [[文件名.pdf#p页码]] 或 [[文件名.docx#para段落号]]，只能原样复用答案中已出现的锚点，禁止新造；判断句综合多个数据、无法精确对应单个数据点时，挂到支撑该判断最核心的数据所在锚点
- 答案中属财报分析判断的行以【观点】前缀开头（格式：【观点】报告期 判断句，报告期如 2025FY），普通事实结论不加前缀
- 比较两家及以上公司的结论，type 填 concept（新建对比概念页），不要填 company

问题：{Q}
答案：{A}"""

# 倒金字塔呈现下，答案顶部的综合判断行常不带锚点（锚点只在展开盘点里），沉淀行照抄就会缺锚点；
# 此时把答案中出现过的合法锚点清单交回模型补挂一轮，句子本身不改
ANCHOR_REPAIR_PROMPT = """下面这些结论句要沉淀进知识库，但句尾缺少溯源锚点。给每行句尾补上锚点，只能从清单中原样选用，禁止新造；每行至少一个，可给多个支撑该判断的锚点，判断综合多个数据时挂最核心数据的锚点。句子本身一字不改，只补锚点。只输出 JSON：{"lines": ["补好锚点后的原句", ...]}

可用锚点清单（原样选用）：
{ANCHORS}

结论句：
{LINES}"""

RAW_SUGGEST_PROMPT = """用户在行业知识库里提问，但知识库页面无法回答。下面是 raw 原始资料库的文件清单（每行：文件名 | 相对路径 | 已摄入页的一句话摘要或「未摄入」）：
{FILES}

判断哪些文件可能含有能回答该问题的内容。只依据文件名与摘要判断，宁缺毋滥，最多给 5 个；都不可能就给空数组。
只输出 JSON：{"candidates": [{"file": "文件名", "reason": "一句话依据"}]}

用户问题：{Q}"""

DEEP_ANSWER_PROMPT = """你是行业研究助手，只能用给定的原始材料片段回答，这些片段来自尚未入库的 raw 原始文件。

规则：
- 只用给定片段，禁止编造，材料片段没有的信息明说「所给材料中没有该信息」
- 每个陈述句句尾带锚点：PDF 片段用 [[文件名.pdf#p页码]]，Word 片段用 [[文件名.docx#para段落号]]，页码/段落号取片段标记中的序号
- 片段标记形如「[文件名 | 第3页]」，请原样使用其中的文件名与序号
- 不允许出现没有任何锚点的数据性陈述
- 公司财务数据的来源优先级：财报（年报、半年报、季报）> 公告/招股书 > 券商研报 > 其他材料。同一公司财务指标冲突时，以最新报告期的财报为准；研报数字只能作对照，并写明差异与各自报告期。行业总量数据不套此序，统计局、行业协会、海关等一手来源优先于研报预测
- 回答用中文，markdown 格式
{FIN}
{METHOD}
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


# 业绩点评与可比公司两个问答框架，只注入 skill references 里的标记段，不加载全文
QA_REFERENCES_DIR = config.SKILLS_DIR / "industry-report" / "references"
QA_FRAMEWORK_FILES = {
    "earnings": config.SKILLS_DIR / "earnings-review" / "references" / "report-structure.md",
    "comps": QA_REFERENCES_DIR / "comps-analysis.md",
}
QA_FRAMEWORK_RE = re.compile(r"<!-- QA-FRAMEWORK:START -->(.*?)<!-- QA-FRAMEWORK:END -->", re.S)

# 财报纪律现读 finstmt-qa 技能的 ASK-RUNTIME 段；代码常量只作技能缺失时的兜底
FIN_SKILL_PATH = config.SKILLS_DIR / "finstmt-qa" / "SKILL.md"
FIN_SKILL_RE = re.compile(r"<!-- ASK-RUNTIME:START -->(.*?)<!-- ASK-RUNTIME:END -->", re.S)


def _fin_block():
    """财报分析纪律。每次现读技能文件，技能缺失或标记为空时回退内嵌常量。"""
    try:
        raw = FIN_SKILL_PATH.read_text(encoding="utf-8")
    except OSError:
        return FIN_ANALYSIS_BLOCK
    hit = FIN_SKILL_RE.search(raw)
    text = (hit.group(1) if hit else raw).strip()
    if not text:
        return FIN_ANALYSIS_BLOCK
    return "\n" + text + "\n"


_EARNINGS_KEYS = ("业绩点评", "财报点评", "季报点评", "中报点评", "年报点评", "业绩快报",
                  "业绩预告", "三季报", "半年报", "一季报", "超预期", "不及预期",
                  "低于预期", "高于预期")
_COMPS_KEYS = ("可比公司", "同业", "可比", "估值对比", "相对估值", "估值溢价",
               "估值折价", "对标", "peer", "comps")


def _keyword_method(question):
    q = str(question or "").lower()
    earnings = any(k in q for k in _EARNINGS_KEYS)
    comps = any(k in q for k in _COMPS_KEYS)
    if earnings and comps:
        return "both"
    if earnings:
        return "earnings"
    if comps:
        return "comps"
    return ""


def _merge_method(method, question):
    """模型判定的框架与关键词命中合并，宁可多给一个组织顺序，不硬套事实。"""
    kw = _keyword_method(question)
    if not kw:
        return method
    if not method:
        return kw
    return "both" if method != kw else method


def _method_block(method):
    if method not in ("earnings", "comps", "both"):
        return ""
    parts = []
    for name in ("earnings", "comps"):
        if method not in (name, "both"):
            continue
        try:
            raw = QA_FRAMEWORK_FILES[name].read_text(encoding="utf-8")
            hit = QA_FRAMEWORK_RE.search(raw)
            text = hit.group(1).strip() if hit else ""
        except OSError:
            text = ""
        if text:
            parts.append(text)
    if not parts:
        return ""
    return ("本次问题按下面的答题框架组织。框架只给组织顺序，不给事实与数字；"
            "所有事实、数据、评级与目标价仍只能来自给定页面，句尾按上文规则带锚点。\n\n"
            + "\n\n".join(parts))


def _detect_intent(question):
    try:
        out = llm.chat(
            [{"role": "user", "content": INTENT_PROMPT.replace("{Q}", question)}],
            task="select",
            response_json=True,
        )
    except llm.LLMError:
        return "chat", "", "general", "builtin", _keyword_method(question)
    domain = "finstmt" if out.get("domain") == "finstmt" else "general"
    report_type = out.get("report_type") if out.get("report_type") in ("industry", "company") else "builtin"
    if out.get("intent") == "report":
        return "report", str(out.get("title") or "").strip()[:16] or "未命名报告", domain, report_type, ""
    method = out.get("method") if out.get("method") in ("earnings", "comps", "both") else ""
    return "chat", "", domain, "builtin", _merge_method(method, question)


# 带单位的数据陈述（「营收 50 亿元」「净息差 1.42%」这类）——用来判断答案里有没有需要出处的数据
_DATA_STATEMENT_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:%|％|个百分点|百分点|亿元|万元|亿美元|万美元|亿|万|元|倍|天|元/股|万股|辆|台)"
)


def _violations(answer, allowed):
    """锚点合规校验。allowed: {文件名: (unit_kind, count[, shown_locs])}。

    shown_locs 是「本次真的给模型看过的那些单元」（深查是喂进去的原文页，
    问答是选中知识页里出现过的锚点）。带了它才能挡住
    「页码在文件范围内、但那一页模型压根没见过」这种编造（问题 6）。
    """
    bad = []
    for f, loc in wiki.ANCHOR_RE.findall(answer):
        info = allowed.get(f)
        if info is None:
            bad.append(f"锚点引用了未提供的文件 {f}")
            continue
        unit_kind, count = info[0], info[1]
        shown = info[2] if len(info) > 2 else None
        if wiki.loc_kind(loc) != unit_kind:
            bad.append(f"锚点 {f}#{loc} 单元类型不对（应为{'段落' if unit_kind == 'para' else '页'}）")
        elif shown is not None and loc not in shown:
            ok = "、".join(sorted(shown)[:10]) or "（本次没有可用的锚点）"
            bad.append(f"锚点 {f}#{loc} 指向的单元没有出现在本次提供给模型的原文里，只允许用这些：{ok}")
        elif not 1 <= wiki.loc_num(loc) <= count:
            bad.append(f"锚点 {f}#{loc} 序号超出范围")
    # 长提示词下模型偶发整篇漏挂锚点（实测同一提示词时而 0 个时而 41 个），此处补一道存在性检查触发既有重试。
    # 判据从「数字个数 ≥ 3」改成「有没有带单位的数据陈述」（问题 21）：
    # 原来只要 1~2 个数字就整条不检查，编造的小数据照样放行；
    # 现在「营收 50 亿元」这种一句话一个数也会被拦，
    # 而「3 个问题」「2026 年」这类不带单位的计数不会被误伤。
    if allowed and not wiki.ANCHOR_RE.search(answer) and _DATA_STATEMENT_RE.search(answer):
        bad.append("全文没有任何锚点，含数据的陈述句句尾必须带锚点")
    return bad


def _is_refusal(answer) -> bool:
    """判断这是不是"拒答"。

    原来用 `"UNANSWERABLE" in answer` 判断——正常回答里只要提到这个词
    （比如讨论模型输出规范）就被当成拒答，整条答案被扔掉（问题 21）。
    现在只在**整条回答就是这一个词**（或只有它加少量标点/空白）时才算拒答。
    """
    a = str(answer or "").strip()
    if not a:
        return False
    # 去掉 markdown 强调、代码围栏、空白与常见标点
    t = a.strip().strip("`*_ ").strip()
    t = t.strip("。．.!！?？:：,，、;； ")
    t = "".join(t.split())   # 去掉所有空白（含制表符与换行）
    return t.upper() == "UNANSWERABLE"


def _index_violations(answer):
    return [f"出现了索引回答不允许的锚点 {f}#{loc}" for f, loc in wiki.ANCHOR_RE.findall(answer)]


def _attach_doubts(result, question, answer):
    """出处文本抽查：答案里的关键数字在锚点页原文里找不到的，记进审校清单待人工复核。

    这是「抽查」不是「判错」——页码定位、表格抽取、单位换算都可能让原文与答案
    字面不一致，所以只记存疑、不拦答案（问题 6）。
    """
    try:
        doubts = anchor_verify.check(answer)
    except Exception:
        doubts = []
    if doubts:
        try:
            anchor_verify.append_doubts(doubts, question=question, answer=answer)
        except Exception:
            pass
    result.setdefault("validation", {})["doubts"] = doubts
    return doubts


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
    if _is_refusal(answer):
        return None
    bad = _index_violations(answer)
    if bad:
        messages.append({"role": "assistant", "content": answer})
        messages.append({"role": "user", "content": (
            "以下内容违规，索引回答不允许出现任何锚点，请修正后重新输出完整答案：\n- " + "\n- ".join(bad)
        )})
        answer = strip_glitch(llm.chat(messages, task="answer"))
        if _is_refusal(answer):
            return None
        bad = _index_violations(answer)
    return answer, bad


def _index_pages():
    pages = wiki.load_pages()
    entries = [wiki.build_index_entry(p) for p in pages if p["type"] != "source"]
    return pages, entries


def _allowed_from_pages(sel_pages):
    """选中知识页 → 允许引用的锚点。

    只认「这些页正文里真的出现过的锚点」：模型看得见的就这么多，
    页面里没露过面的页码不许引（问题 6：只查页码在不在文件范围内太松）。
    """
    manifest = load_manifest()
    files_info = {f["file"]: (f.get("unit", "page"), f.get("count") or f.get("page_count") or 0)
                  for f in manifest["files"]}
    allowed = {}
    for p in sel_pages:
        for f, loc in wiki.ANCHOR_RE.findall(p.get("body") or ""):
            if f not in files_info:
                continue
            kind, count = files_info[f]
            prev = allowed.get(f)
            locs = (prev[2] if prev else set()) | {loc}
            allowed[f] = (kind, count, locs)
    return allowed


# 研报选页：提示词给软上限，代码再兜底硬上限。只写「不限页数」会被模型理解成全选，
# 宇通一次选中 261/322 个非来源页，把每节成文提示词撑爆
REPORT_PAGE_SOFT = 20
REPORT_PAGE_HARD = 30


def _max_pages_rule(max_pages=None, soft_pages=None):
    """选页提示词里的页数规则；soft_pages 给软上限，max_pages=0 表示不限。"""
    if soft_pages:
        return f"不设固定上限，通常不超过 {int(soft_pages)} 页"
    limit = config.CFG["ask"]["max_pages"] if max_pages is None else int(max_pages)
    return "不限页数" if limit <= 0 else f"最多 {limit} 页"


def _select_pages(question, history, max_pages=None, soft_pages=None):
    pages, entries = _index_pages()
    if not entries:
        return pages, [], "", False
    idx_text = wiki.compact_index_text(entries)
    sel = llm.chat(
        [{"role": "user", "content": SELECT_PROMPT
          .replace("{INDEX}", idx_text)
          .replace("{HISTORY}", _history_block(history))
          .replace("{Q}", question)
          .replace("{MAXRULE}", _max_pages_rule(max_pages, soft_pages))}],
        task="select",
        response_json=True,
    )
    valid_slugs = {e["slug"] for e in entries}
    slugs = [s for s in (sel.get("selected") or []) if isinstance(s, str) and s in valid_slugs]
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
        if not isinstance(c, dict):
            continue
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
            tag = "段" if cached["unit"] == "para" else "页"
            shown = set()
            for u in cached["units"]:
                if not u["text"]:
                    continue
                if used + len(u["text"]) > char_budget:
                    break
                pieces.append(f"[{p.name} | 第{u['n']}{tag}]\n{u['text']}")
                used += len(u["text"])
                shown.add(f"{'para' if tag == '段' else 'p'}{u['n']}")
            # 只把「真的喂给模型看过的单元」记成可引用范围（问题 6）
            allowed[p.name] = (cached["unit"], cached["count"], shown)
    else:
        scored = []
        keys = _question_keys(question)
        for p in sorted(targets, key=lambda x: x.name):
            cached = load_cached(p) or _extract_payload(p)
            if not cached:
                continue
            for u in cached["units"]:
                if not u["text"]:
                    continue
                score = sum(1 for k in keys if k in u["text"])
                if score:
                    scored.append((score, p.name, cached["unit"], u, cached["count"]))
        scored.sort(key=lambda x: -x[0])
        shown_by_file = {}
        for score, fname, unit, u, count in scored[:60]:
            tag = "段" if unit == "para" else "页"
            text = u["text"]
            if used + len(text) > char_budget:
                continue
            pieces.append(f"[{fname} | 第{u['n']}{tag}]\n{text}")
            used += len(text)
            shown_by_file.setdefault(fname, (unit, count, set()))[2].add(
                f"{'para' if tag == '段' else 'p'}{u['n']}")
        allowed.update(shown_by_file)
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
    _, _, domain, _, method = _detect_intent(question)
    pieces, allowed = _collect_deep_units(question, files=files)
    if not pieces:
        result["answer_md"] = "没有从 raw 文件提取到可用于回答的文字（可能均为扫描版或文件已移走）。"
        return result
    fin = _fin_block() if domain == "finstmt" else ""
    messages = [{"role": "user", "content": DEEP_ANSWER_PROMPT
                 .replace("{FIN}", fin)
                 .replace("{METHOD}", _method_block(method))
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
    _attach_doubts(result, question, answer)
    if not bad:
        result["proposal"] = _propose_distill(question, answer)
    return result


def ask(question, history=None, propose=True):
    """两步问答。问知识库整体的问题走索引直答；选页为空时先以索引兜底，再走 raw 文件名回退；返回 kind=answer/referral/report。"""
    result = {"kind": "answer", "question": question,
              "step1": {"selected": [], "reason": ""},
              "answer_md": "", "validation": {"ok": True, "violations": []}, "proposal": None}
    intent, title, domain, report_type, method = _detect_intent(question)
    if intent == "report":
        return {"kind": "report", "question": question, "title": title, "domain": domain,
                "report_type": report_type}

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
    manifest = load_manifest()
    chunks = []
    for p in sel_pages:
        legend = source_priority.page_legend(p, manifest)
        head = f"### {p['meta'].get('title', p['slug'])}（slug: {p['slug']}）"
        chunks.append("\n".join(x for x in (head, legend, p["body"]) if x))
    pages_text = "\n\n".join(chunks)
    fin = _fin_block() if domain == "finstmt" else ""
    messages = [{"role": "user", "content": ANSWER_PROMPT
                 .replace("{FIN}", fin)
                 .replace("{METHOD}", _method_block(method))
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
    _attach_doubts(result, question, answer)
    if propose and not bad:
        result["proposal"] = _propose_distill(question, answer)
    return result


def _anchor_ok_lines(lines, valid_anchors):
    # 行内至少一个锚点且全部来自答案出现过的锚点集
    return [ln for ln in lines
            if wiki.ANCHOR_RE.findall(ln) and set(wiki.ANCHOR_RE.findall(ln)) <= valid_anchors]


def _repair_anchors(lines, valid_anchors):
    """按答案锚点清单补挂一轮锚点，返回修复后的行（是否合格仍由过滤器把关）。"""
    anchor_list = "、".join(f"[[{f}#{loc}]]" for f, loc in sorted(valid_anchors))
    try:
        out = llm.chat(
            [{"role": "user", "content": ANCHOR_REPAIR_PROMPT
              .replace("{ANCHORS}", anchor_list)
              .replace("{LINES}", "\n".join(lines))}],
            task="audit",
            response_json=True,
        )
    except llm.LLMError:
        return []
    return [str(x).strip() for x in (out.get("lines") or []) if str(x).strip()]


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
    raw_lines = [str(x).strip() for x in (out.get("lines") or []) if str(x).strip()]
    valid_anchors = set(wiki.ANCHOR_RE.findall(answer))
    lines = _anchor_ok_lines(raw_lines, valid_anchors)
    if not lines and raw_lines and valid_anchors:
        lines = _anchor_ok_lines(_repair_anchors(raw_lines, valid_anchors), valid_anchors)
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
    # 命中已有页则改为 merge（应用时按 slug 重新定位，防止这期间页面变化）。
    # 标题同时命中多个页面说明是跨页对比，不能按页序并进其中一家，改新建对比概念页。
    matches = []
    for p in wiki.load_pages():
        t = str(p["meta"].get("title") or p["slug"])
        if p["type"] != ptype:
            continue
        if t == proposal["title"] or t in proposal["title"] or proposal["title"] in t:
            matches.append((p, t))
    if len(matches) == 1:
        p, t = matches[0]
        proposal["action"] = "merge"
        proposal["target_slug"] = p["slug"]
        proposal["target_title"] = t
        proposal["target_type"] = p["type"]
    elif len(matches) > 1:
        proposal["action"] = "create"
        if ptype == "company":
            proposal["type"] = "concept"
    return proposal


def apply_distill(proposal, chosen_lines=None, note_prefix="会话中提取"):
    """把提案（可只取部分行）写入 wiki，返回 {page, action, lines}。

    【观点】/【个人观点】行进「分析观点」节；【数据】/【指标】行进「经营与财务」节；
    note_prefix 区分来源（会话提取 / 报告沉淀）。
    """
    lines = list(proposal["lines"])
    if chosen_lines:
        keep = [ln for ln in chosen_lines if ln in lines]
        if not keep:
            return None
        lines = keep
    sections = {}
    for ln in lines:
        if ln.startswith(("【观点】", "【个人观点】")):
            bucket = "分析观点"
        elif ln.startswith(("【数据】", "【指标】")) and proposal.get("type") in ("company", "industry"):
            bucket = "经营与财务"
        else:
            bucket = "其他"
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
        _apply_merge(hit["path"], hit["meta"], hit["body"], topic, extra_sources, note_prefix)
        ret = {"page": hit["slug"], "action": "merge", "lines": len(lines)}
    else:
        decision = {"action": "create", "type": topic["type"]}
        path, count = _apply_create(decision, topic, extra_sources, f"{note_prefix}新建")
        ret = {"page": path.stem, "action": "create", "lines": count}
    wiki.rebuild_index_md(wiki.load_pages())
    # 沉淀的新行也可能提到其他页面，一并补双链（幂等，无改动时零开销）
    try:
        from .linking import cross_link_all
        cross_link_all()
    except Exception:
        pass
    wiki.append_log("ask", f"{note_prefix}新知识到 {ret['page']}（{ret['lines']} 条）：{question}")
    return ret
