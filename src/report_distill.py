"""成稿后的 A-F 分类与【个人观点】沉淀：无锚点句逐句归类，A/D/E 进候选，用户勾选后写入 wiki。

A 综合判断、D 前瞻预测写成【个人观点】行；E 推导计算写成普通【指标】行并注明口径；
B 复述漏锚不入候选（成文阶段应已补锚），C 口径方法、F 行文框架只在报告里，不写入 wiki。
"""
import json
import re

from . import ask as ask_mod
from . import config, llm, wiki, wiki_check

AF_PROMPT = """你是行业知识库的编辑，给研报里的无锚点句逐句分类，挑出可以沉淀进 wiki 的新知识。

分类：
- A 综合判断：跨材料综合得出、任何单个页面都没有明说的判断，可改写成【个人观点】入库
- B 复述漏锚：某份材料里已有、只是这句没挂锚点（给 anchors 即可，不入候选）
- C 口径方法：方法、口径、定义说明（不入库）
- D 前瞻预测：对未来的判断（入候选，必须写清预测条件与预测日期）
- E 推导计算：用库内数据算出的新数值（入候选，必须写清计算口径与报告期）
- F 行文框架：章节过渡、总结、套话（不入库）

标了 [测算句] 的句子已经有锚点，但仍是 E 类推导值，也要列候选：value 取测算结果，calc 取「（测算：…）」里的公式，period 按句中年份与报告期判断。

对 A/D/E 给出：
- judgment：可入库的完整判断句（A/D）或数据句（E），不得带第一人称与套话
- target：应写入的页面标题（优先用已有页面，没有就按行业给新页名）
- target_type：industry|company|concept
- anchors：从下方锚点清单原样选 1-2 个支撑锚点，禁止新造
- D 另给 condition（预测条件）与 forecast_date（预测日期，YYYY-MM-DD 或 YYYY）
- E 另给 metric、period（YYYYFY/H1/Q1/Q3）、value、unit、calc（计算口径）

已有页面：{PAGES}
可用锚点清单：{ANCHORS}

待分类句子（编号）：
{SENTENCES}

只输出 JSON：{{"items": [{{"i": 1, "class": "A|B|C|D|E|F", "reason": "一句话"}}], "candidates": [{{"i": 1, "class": "A", "judgment": "...", "target": "...", "target_type": "industry", "anchors": ["文件名.pdf#p5"], "condition": "", "forecast_date": "", "metric": "", "period": "", "value": "", "unit": "", "calc": ""}}]}}"""

PERSONAL_VIEW_DATE = re.compile(r"(?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?")


def candidates_path(job_id):
    return config.REPORT_JOBS_DIR / f"{job_id}.candidates.json"


def save_candidates(job_id, candidates):
    path = candidates_path(job_id)
    path.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_candidates(job_id):
    path = candidates_path(job_id)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def blank_sentences(report_md, cap=40):
    """取无锚点的陈述行：跳过标题、表格、图片、引用与很短的行。"""
    out, seen = [], set()
    for ln in report_md.splitlines():
        s = ln.strip()
        if not s or s.startswith(("#", "|", "!", "```", ">", "---")):
            continue
        if s.startswith("- "):
            s = s[2:].strip()
        if wiki.ANCHOR_RE.search(s) or len(s) < 12:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= cap:
            break
    return out


def derived_sentences(report_md, cap=20):
    """带「（测算：公式）」且有锚点的行：已锚也不能漏，它们是 E 类沉淀的主要来源。"""
    out, seen = [], set()
    for ln in report_md.splitlines():
        s = ln.strip()
        if s.startswith(("- ", "* ")):
            s = s[2:].strip()
        if not s or s.startswith(("#", "|", "!", "```", ">", "---")):
            continue
        if not re.search(r"[（(]\s*测算\s*[:：]", s) or not wiki.ANCHOR_RE.search(s):
            continue
        if len(s) < 12 or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= cap:
            break
    return out


def _target_page(title, type_hint, job):
    fallback_title = title or job["title"]
    for p in wiki.load_pages():
        names = [str(p["meta"].get("title") or p["slug"]), p["slug"]]
        names += [str(a) for a in (p["meta"].get("aliases") or [])]
        if fallback_title in names or any(fallback_title and fallback_title in str(n) for n in names):
            return {"slug": p["slug"], "type": p["type"],
                    "title": str(p["meta"].get("title") or p["slug"]), "action": "merge"}
    ptype = type_hint if type_hint in ("industry", "company", "concept") else "industry"
    return {"slug": "", "type": ptype, "title": fallback_title, "action": "create"}


def _norm_anchor(text):
    """接受 [[文件#位置]]、文件#位置、以及已拆好的 (文件, 位置) 三种写法。"""
    if isinstance(text, (tuple, list)):
        if len(text) != 2:
            return None
        f, loc = str(text[0]).strip(), str(text[1]).strip()
        if re.fullmatch(r"[^\[\]#|]+\.(?:pdf|docx)", f) and re.fullmatch(r"p\d+|para\d+", loc):
            return f, loc
        return None
    s = str(text or "").strip()
    if not s.startswith("[["):
        s = f"[[{s}]]"
    m = wiki.ANCHOR_RE.fullmatch(s)
    if m:
        return m.group(1), m.group(2)
    return None


def _anchor_text(anchors):
    return "".join(f"[[{f}#{loc}]]" for f, loc in anchors)


def validate_candidate(cand, report_anchors, info):
    """锚点必须出现在报告里、且在清单/提取缓存里真实存在；D/E 还校验条件与口径字段。"""
    raw = cand.get("anchors") or []
    anchors = [a for a in (_norm_anchor(x) for x in raw) if a]
    if not anchors:
        return False, "没有可用锚点"
    for a in anchors:
        if a not in report_anchors:
            return False, f"锚点 {a[0]}#{a[1]} 不在报告正文里"
        fi = info.get(a[0])
        if not fi:
            return False, f"锚点文件 {a[0]} 不在已摄入清单"
        if wiki.loc_kind(a[1]) != fi[0] or not 1 <= wiki.loc_num(a[1]) <= fi[1]:
            return False, f"锚点 {a[0]}#{a[1]} 越界或单元类型不对"
    if cand["class"] == "D":
        if not str(cand.get("condition") or "").strip():
            return False, "D 类缺预测条件"
        if not PERSONAL_VIEW_DATE.search(str(cand.get("forecast_date") or "")):
            return False, "D 类缺预测日期"
    if cand["class"] == "E":
        if not str(cand.get("metric") or "").strip():
            return False, "E 类缺指标名"
        if not re.fullmatch(r"\d{4}(?:FY|H1|Q1|Q3)", str(cand.get("period") or "")):
            return False, "E 类报告期须为 YYYYFY/H1/Q1/Q3"
        if not str(cand.get("value") or "").strip() or not str(cand.get("calc") or "").strip():
            return False, "E 类缺数值或计算口径"
    if not str(cand.get("judgment") or "").strip():
        return False, "没有判断句"
    return True, ""


def build_distill_line(cand, job):
    anchors = _anchor_text(cand["anchors"])
    report_name = job.get("report_path", "").rsplit("/", 1)[-1]
    date = config.today_str()
    if cand["class"] == "E":
        unit = str(cand.get("unit") or "").strip()
        val = str(cand["value"]).strip()
        return (f"【指标】{cand['period']} {cand['metric']} = {val}{unit} "
                f"({cand['calc']}) {anchors}")
    # 模型有时自带【个人观点】前缀，这里还要再加一次，先剥掉免得写成两遍
    judgment = re.sub(r"^(?:【个人观点】\s*)+", "", str(cand["judgment"]).strip()).rstrip("。；;")
    if cand["class"] == "D":
        cond = str(cand.get("condition") or "").strip()
        when = str(cand.get("forecast_date") or "").strip()
        if cond and cond not in judgment:
            judgment += f"，预测条件：{cond}"
        if when and when not in judgment:
            judgment += f"，预测时点：{when}"
    return f"【个人观点】{judgment} {anchors}（个人研报观点，{date}，{report_name}）"


def build_candidates(job):
    path = config.ROOT / job["report_path"]
    report_md = path.read_text(encoding="utf-8")
    plain = blank_sentences(report_md)
    derived = [s for s in derived_sentences(report_md) if s not in plain]
    sentences = [(s, False) for s in plain] + [(s, True) for s in derived]
    if not sentences:
        return []
    report_anchor_pairs = set(wiki.ANCHOR_RE.findall(report_md))
    anchors_text = "、".join(f"[[{f}#{loc}]]" for f, loc in sorted(report_anchor_pairs))[:4000] or "（无）"
    pages = "、".join(str(p["meta"].get("title") or p["slug"]) for p in wiki.load_pages()
                      if p["type"] != "source")
    prompt = (AF_PROMPT
              .replace("{PAGES}", pages[:2000])
              .replace("{ANCHORS}", anchors_text)
              .replace("{SENTENCES}", "\n".join(
                  f"{i}. {('[测算句] ' if d else '')}{s}" for i, (s, d) in enumerate(sentences, 1))))
    out = llm.chat([{"role": "user", "content": prompt}], task="af_classify", response_json=True)
    info = wiki_check.files_info()
    cands = []
    for k, it in enumerate(out.get("candidates") or [], 1):
        cls = str(it.get("class") or "").strip().upper()
        if cls not in ("A", "D", "E"):
            continue
        idx = int(it.get("i") or 0)
        sentence = sentences[idx - 1][0] if 1 <= idx <= len(sentences) else ""
        raw_anchors = [a for a in (_norm_anchor(x) for x in (it.get("anchors") or [])) if a]
        cand = {
            "id": f"c{k}",
            "class": cls,
            "source_sentence": sentence,
            "judgment": str(it.get("judgment") or sentence).strip(),
            "reason": str(it.get("reason") or "").strip(),
            "anchors": raw_anchors,
            "target": _target_page(str(it.get("target") or "").strip(), str(it.get("target_type") or ""), job),
            "condition": str(it.get("condition") or "").strip(),
            "forecast_date": str(it.get("forecast_date") or "").strip(),
            "metric": str(it.get("metric") or "").strip(),
            "period": str(it.get("period") or "").strip(),
            "value": str(it.get("value") or "").strip(),
            "unit": str(it.get("unit") or "").strip(),
            "calc": str(it.get("calc") or "").strip(),
            "chosen": False,
        }
        ok, reason = validate_candidate(cand, report_anchor_pairs, info)
        if not ok:
            cand["invalid_reason"] = reason
            continue
        cand["distill_line"] = build_distill_line(cand, job)
        cands.append(cand)
    return cands


def apply(job_id, chosen_ids, load_job, save_job):
    """按勾选写入 wiki：按目标页分组，A/D 进分析观点，E 进经营与财务。"""
    job = load_job(job_id)
    if not job:
        raise KeyError("报告任务不存在")
    cands = load_candidates(job_id)
    chosen = [c for c in cands if c["id"] in set(chosen_ids or [])]
    if not chosen:
        return {"ok": 0, "errors": []}
    groups = {}
    for c in chosen:
        t = c["target"]
        key = (t["action"], t["type"], t["slug"], t["title"])
        groups.setdefault(key, []).append(c)
    results, errors = [], []
    for (action, ptype, slug, title), items in groups.items():
        lines = [c["distill_line"] for c in items]
        proposal = {
            "question": job["question"],
            "title": title,
            "type": ptype,
            "summary": f"研报《{job['title']}》成稿后勾选沉淀",
            "lines": lines,
            "action": action,
            "target_slug": slug,
            "target_title": title,
            "target_type": ptype,
        }
        try:
            ret = ask_mod.apply_distill(proposal, chosen_lines=lines, note_prefix="报告沉淀")
        except Exception as e:
            errors.append(f"{title}：{e}")
            continue
        if ret:
            results.append({"title": title, **ret})
            for c in items:
                c["chosen"] = True
        else:
            errors.append(f"{title}：没有可写入的行")
    save_candidates(job_id, cands)
    job = load_job(job_id)
    if job:
        job["status"] = "done" if results and not errors else "candidates_pending"
        msg = f"勾选沉淀 {sum(1 for c in chosen if c['chosen'])} 条：{'；'.join(r['title'] for r in results) or '无'}"
        if errors:
            msg += f"；失败：{'；'.join(errors)}"
        job.setdefault("log", []).append({"ts": config.now_iso(), "msg": msg})
        job["log"] = job["log"][-120:]
        save_job(job)
    return {"ok": sum(1 for c in chosen if c["chosen"]), "results": results, "errors": errors}
