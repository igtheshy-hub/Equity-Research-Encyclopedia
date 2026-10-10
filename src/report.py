from . import config, llm, wiki
from .ask import _allowed_from_pages, _select_pages, _violations
from .classify import sanitize

REPORT_PROMPT = """你是投研分析师，基于知识库页面内容撰写一份结构化报告。

规则：
- 只用给定页面内容，禁止编造，页面没有的信息明确说知识库中没有
- 结构固定：# 标题 / ## 核心结论（3-5 条，每条句尾带锚点）/ ## 分节论述（按主题分节，句句带锚点）/ ## 数据表（如有可成表的数据，用 markdown 表格，表格上方或下方注明出处锚点；没有可成表数据可省略本节）/ ## 风险与局限
- 锚点格式 [[文件名.pdf#p页码]] 或 [[文件名.docx#para段落号]]，只能引用给定页面中出现过的来源文件
- 不允许出现没有任何锚点的数据性陈述
- 用中文，markdown 格式

--- 页面内容开始 ---
{PAGES}
--- 页面内容结束 ---

报告主题：{TITLE}
用户原始请求：{Q}"""


def _tick(task_id, current, detail):
    if task_id:
        from . import tasks
        tasks.update(task_id, current=current, detail=detail)


def generate(title, question, task_id=None):
    """从 wiki 页面生成报告并存 outputs/，返回文件 Path。进度按 0-100 报。"""
    _tick(task_id, 5, "凭索引选页")
    pages, slugs, reason = _select_pages(question, history=None)
    if not slugs:
        raise ValueError(f"知识库中没有与「{title}」相关的页面，无法生成报告（{reason or '索引无匹配'}）")
    sel_pages = [p for p in pages if p["slug"] in slugs and p["type"] != "source"]
    pages_text = "\n\n".join(
        f"### {p['meta'].get('title', p['slug'])}（slug: {p['slug']}）\n{p['body']}" for p in sel_pages
    )
    _tick(task_id, 25, f"已选 {len(sel_pages)} 页，正在撰写")
    messages = [{"role": "user", "content": REPORT_PROMPT
                 .replace("{PAGES}", pages_text)
                 .replace("{TITLE}", title)
                 .replace("{Q}", question)}]
    answer = llm.chat(messages, task="answer")
    _tick(task_id, 75, "锚点校验")
    allowed = _allowed_from_pages(sel_pages)
    bad = _violations(answer, allowed)
    if bad:
        messages.append({"role": "assistant", "content": answer})
        messages.append({"role": "user", "content": (
            "以下锚点违规，请修正后重新输出完整报告（只改锚点相关内容）：\n- " + "\n- ".join(bad)
        )})
        answer = llm.chat(messages, task="answer")
        bad = _violations(answer, allowed)
    if bad:
        answer += "\n\n> 注：本报告部分锚点未通过程序校验，请人工复核。"
    config.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"report-{config.today_str().replace('-', '')}-{sanitize(title) or '未命名'}.md"
    path = config.OUTPUTS_DIR / fname
    i = 2
    while path.exists():
        path = config.OUTPUTS_DIR / f"report-{config.today_str().replace('-', '')}-{sanitize(title) or '未命名'}-{i}.md"
        i += 1
    path.write_text(answer.rstrip() + "\n", encoding="utf-8")
    _tick(task_id, 95, "已存盘")
    wiki.rebuild_index_md(wiki.load_pages())
    wiki.append_log("report", f"生成报告 {path.name}（选题：{title}）")
    return path
