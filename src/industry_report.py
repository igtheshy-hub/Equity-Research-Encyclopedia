"""研报管线（行业研报与公司研报共用）：样板 → 大纲（网页确认）→ 缺口取数（网页勾选）→ 分节成文 → 图表 → 审校 → Word → A-F 候选。

状态存在 data/report_jobs/<id>.json，每个阶段起一个 report 或 fetch 后台任务，状态机可跨服务重启续跑。
大纲确认（outline_pending）、缺口勾选（gaps_pending）、候选勾选（candidates_pending）是三个网页关卡。
"""
import json
import re
import shutil
import threading
import uuid
from datetime import date, timedelta

from . import anchor_match as am
from . import ask as ask_mod
from . import charts, config, fetcher, ingest as ingest_mod, llm, source_priority, tasks, wiki, wiki_check
from .classify import sanitize
from .pdf_extract import load_cached

JOB_LOCK = threading.RLock()

SKILL_DIR = config.ROOT / ".claude" / "skills"
STRUCTURE_PATH = SKILL_DIR / "industry-report" / "references" / "report-structure.md"
SECTOR_PATH = SKILL_DIR / "industry-report" / "references" / "sector-overview.md"
COMPETITIVE_PATH = SKILL_DIR / "industry-report" / "references" / "competitive-analysis.md"
COMPS_PATH = SKILL_DIR / "industry-report" / "references" / "comps-analysis.md"
COMPANY_SKILL_PATH = SKILL_DIR / "company-report" / "SKILL.md"
COMPANY_STRUCTURE_PATH = SKILL_DIR / "company-report" / "references" / "report-structure.md"
COMPANY_COMPS_PATH = SKILL_DIR / "company-report" / "references" / "comps-analysis.md"
ANNUAL_RE = re.compile(r"(年度报告|年報|annual\s*report)", re.I)
DOC_GAP_RE = re.compile(
    r"未摄入材料|年度报告|年报|年報|半年度报告|半年报|中期报告|季度报告|季报|"
    r"公告|研报|招股书|招股說明書|募集说明|审计报告|全文|PDF", re.I)

RUNNING_STATUSES = ("outlining", "fetching", "writing", "charting", "reviewing", "docxing")

STATUS_LABELS = {
    "outlining": "正在生成大纲",
    "outline_pending": "待确认大纲",
    "gaps_pending": "待勾选缺口",
    "fetching": "正在补缺口",
    "writing": "正在成文",
    "charting": "正在出图",
    "reviewing": "正在审校",
    "docxing": "正在转 Word",
    "candidates_pending": "待勾选沉淀",
    "done": "已完成",
    "fallback": "已改走内置底稿",
    "error": "出错",
}


# ---------- job 文件 ----------

def _job_path(job_id):
    return config.REPORT_JOBS_DIR / f"{job_id}.json"


def load_job(job_id):
    path = _job_path(str(job_id))
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_job(job):
    with JOB_LOCK:
        job["updated_at"] = config.now_iso()
        path = _job_path(job["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def list_jobs():
    out = []
    for p in sorted(config.REPORT_JOBS_DIR.glob("*.json"), reverse=True):
        if p.name.endswith(".candidates.json"):
            continue
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        out.append({
            "id": j.get("id"), "title": j.get("title"), "status": j.get("status"),
            "status_label": STATUS_LABELS.get(j.get("status"), j.get("status")),
            "kind": j.get("kind") or "industry",
            "kind_label": KIND_LABELS.get(j.get("kind"), "行业研报"),
            "updated_at": j.get("updated_at"), "error": j.get("error"),
            "gaps": len(j.get("gaps") or []),
            "candidates": j.get("candidates_count") or 0,
        })
    return out


def delete_job(job_id):
    """删除一份研报任务与它的全部产物。进行中的任务不许删，免得后台阶段把文件写回来。"""
    job = load_job(job_id)
    if not job:
        raise KeyError("报告任务不存在")
    if job.get("status") in RUNNING_STATUSES:
        raise ValueError("任务还在进行中，等它结束或出错后再删")

    removed = []

    def rel_of(p):
        try:
            return p.relative_to(config.ROOT).as_posix()
        except ValueError:
            return str(p)

    def drop(rel):
        if not rel:
            return
        p = config.ROOT / str(rel)
        if p.is_file():
            p.unlink()
            removed.append(rel_of(p))

    drop(job.get("outline_path"))
    drop(job.get("report_path"))
    drop(job.get("docx_path"))
    drop(job.get("fallback_path"))
    drop(job.get("candidates_path"))
    for c in job.get("charts") or []:
        drop(c.get("path") if isinstance(c, dict) else "")
    # 转 Word 的中间稿 data/_display-<docx名>.md
    if job.get("docx_path"):
        stem = (config.ROOT / str(job["docx_path"])).stem
        p = config.DATA_DIR / f"_display-{stem}.md"
        if p.is_file():
            p.unlink()
            removed.append(rel_of(p))

    job_path = _job_path(job_id)
    for p in (job_path.with_name(f"{job_id}.candidates.json"), job_path):
        if p.is_file():
            p.unlink()
            removed.append(rel_of(p))
    return {"ok": True, "title": job.get("title") or "", "removed": removed}


def _log(job, msg):
    job.setdefault("log", []).append({"ts": config.now_iso(), "msg": str(msg)})
    job["log"] = job["log"][-120:]


def _tick(tid, pct, detail):
    if tid:
        tasks.update(tid, current=pct, detail=detail)


def _start(job_id, fn, label):
    """起一个 report 任务跑某阶段；异常落 job.status=error 并记录阶段名，便于 /continue 重试。"""
    def run(tid):
        job = load_job(job_id)
        if not job:
            raise RuntimeError("报告任务不存在")
        try:
            with llm.use_model(job.get("model")):
                fn(job_id, tid)
        except Exception as e:
            job = load_job(job_id) or job
            job["status"] = "error"
            job["error"] = str(e)
            job["phase"] = fn.__name__
            _log(job, f"{fn.__name__} 失败：{e}")
            save_job(job)
            raise
        return {"report_job": job_id}
    return tasks.start("report", run, label=label)


def start_job(question, title, model=None, domain="general", kind="industry"):
    job_id = uuid.uuid4().hex[:10]
    job = {
        "id": job_id,
        "question": question,
        "title": title,
        "domain": domain,
        "kind": kind if kind in KIND_LABELS else "industry",
        "model": model,
        "status": "outlining",
        "outline": "",
        "outline_path": "",
        "samples": [],
        "gaps": [],
        "pages": [],
        "report_path": "",
        "docx_path": "",
        "fallback_path": "",
        "charts": [],
        "review": {},
        "candidates_path": "",
        "candidates_count": 0,
        "error": "",
        "phase": "",
        "log": [],
        "created_at": config.now_iso(),
        "updated_at": config.now_iso(),
    }
    _log(job, f"收到{kind_label(job)}请求：{title}")
    save_job(job)
    _start(job_id, _phase_outline, f"{kind_label(job)}大纲《{title}》")
    return job


# ---------- 取料与样板 ----------

def _topic(title):
    return sanitize(title) or "未命名"


KIND_LABELS = {"industry": "行业研报", "company": "公司研报"}


def kind_label(job):
    return KIND_LABELS.get(job.get("kind"), "行业研报")


def _display_title(job):
    """成稿标题带「个人」标识与成稿日期，和库内机构研报区分开。"""
    return f"个人·{job['title']}（{config.today_str()}）"


def _unique_path(directory, stem, suffix):
    path = directory / f"{stem}{suffix}"
    i = 2
    while path.exists():
        path = directory / f"{stem}-{i}{suffix}"
        i += 1
    return path


def _industry_of(title, question):
    """行业研报主题：标题与提问各剥一遍报告类词，取更短的非空结果。

    标题由意图判别生成、最干净（如「乘用车行业研究报告」→「乘用车」）；直接拼接提问会
    留下「生成一份」「写一份」这类指令词（如「乘用车生成一份乘用车」），榜单匹配与
    东财搜索都会落空，所以分开剥、取短者。
    """
    stop = ("行业深度报告", "行业研究报告", "行业研报", "深度报告", "研究报告", "研报", "行业", "报告")
    cands = []
    for text in (sanitize(title or ""), sanitize(question or "")):
        for word in stop:
            text = text.replace(word, "")
        text = text.strip()
        if text:
            cands.append(text)
    if not cands:
        return sanitize(title) or "该行业"
    return min(cands, key=len)


def _terms(text):
    return ask_mod._question_keys(text)


def _sample_candidates(job):
    manifest = ingest_mod.load_manifest()
    keys = _terms(f"{job['title']} {job['question']}")
    scored = []
    for f in manifest["files"]:
        if f.get("source_type") != "report":
            continue
        blob = f["file"] + " " + " ".join(str(t) for t in (f.get("topics_touched") or []))
        score = sum(1 for k in keys if k and k in blob)
        if score:
            scored.append((score, str(f.get("report_date") or ""), f))
    scored.sort(key=lambda x: x[1] or "", reverse=True)
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, date, f in scored[:2]:
        p = config.ROOT / f["path"]
        if p.exists():
            out.append({"file": f["file"], "path": f["path"], "score": score, "date": date})
    return out


def _sample_text(sample, budget=7000):
    p = config.ROOT / sample["path"]
    cached = load_cached(p)
    if not cached:
        return ""
    parts, used = [], 0
    for u in cached["units"]:
        text = str(u.get("text") or "")
        if not text:
            continue
        parts.append(f"[第{u['n']}{'段' if cached['unit'] == 'para' else '页'}]\n{text}")
        used += len(text)
        if used >= budget:
            break
    return "\n\n".join(parts)[:budget]


def _company_industry_titles(subject):
    """从覆盖该公司的研报主题里找行业页标题，用来匹配新财富行业组。"""
    topics = set()
    for f in ingest_mod.load_manifest()["files"]:
        if f.get("source_type") != "report":
            continue
        blob = f["file"] + " " + " ".join(str(t) for t in (f.get("topics_touched") or []))
        if subject in blob:
            topics |= {str(t) for t in (f.get("topics_touched") or [])}
    if not topics:
        return []
    titles = [str(p["meta"].get("title") or p["slug"]) for p in wiki.load_pages()
              if p["type"] == "industry" and (p["meta"].get("title") or p["slug"]) in topics]
    return sorted(set(titles), key=len)


def _library_top3_samples(terms, brokers):
    """库里已摄入、且出自榜单券商的研报，每家取最新一份；terms 任一词命中文件名或主题即算覆盖。"""
    out, seen = [], set()
    for f in ingest_mod.load_manifest()["files"]:
        if f.get("source_type") != "report":
            continue
        name = str(f.get("file") or "")
        broker = next((b for b in brokers if b["org"] in name), None)
        if not broker or broker["org"] in seen:
            continue
        blob = name + " " + " ".join(str(t) for t in (f.get("topics_touched") or []))
        if not any(t and t in blob for t in terms):
            continue
        path = config.ROOT / f["path"]
        if not path.exists():
            continue
        seen.add(broker["org"])
        out.append({"file": name, "path": f["path"], "score": 0,
                    "date": str(f.get("report_date") or ""), "broker": broker["org"]})
    return out


def _chain_samples(res):
    """把下载链取回并入库的研报映射成样板条目。"""
    samples = []
    for it in res.get("items") or []:
        manifest = ingest_mod.load_manifest()
        hit = next((f for f in manifest["files"] if f["file"] == it["file"]), None)
        if hit:
            samples.append({"file": hit["file"], "path": hit["path"], "score": 0,
                            "date": it.get("date"), "broker": it.get("org") or ""})
    return samples


def _log_manual_broker_gap(job, brokers, samples):
    """前 3 券商还有缺时提醒用户：可自行从慧博终端下 PDF 传入库再重跑。"""
    if not brokers:
        return
    have = {s.get("broker") for s in samples if s.get("broker")}
    missing = [b["org"] for b in brokers if b["org"] not in have]
    if missing:
        _log(job, "新财富前 3 中 " + "、".join(missing)
             + " 在东财没有可用研报，本次未当样板；确需其研报时可自行在慧博终端下载 PDF，"
               "从首页上传或放 raw/inbox 摄入后重新发起研报")


def _sample_top3_note(job):
    """样板前 3 覆盖不全时的审校提示：占比 + 放宽说明 + 手动补齐路径。"""
    top3 = job.get("top3") or {}
    brokers = [str(b) for b in (top3.get("brokers") or [])]
    samples = job.get("samples") or []
    if not brokers or not samples:
        return ""
    have = {str(s.get("broker") or "") for s in samples}
    hit = [b for b in brokers if b in have]
    if len(hit) == len(brokers):
        return ""
    missing = [b for b in brokers if b not in have]
    return (f"样板研报 {len(samples)} 份中仅 {len(hit)} 份出自新财富「{top3.get('group') or ''}」前 3"
            f"（{('、'.join(hit)) or '无'}），缺 {'、'.join(missing)}，本轮已放宽为综合最优研报成文。"
            "补齐方法：在慧博终端下载缺失券商的本主题研报 PDF，从首页上传摄入，"
            "然后删除本报告重新发起，管线会优先用库内新财富前 3 研报当样板")


def _ensure_company_samples(job, tid):
    """公司研报样板：库内新财富前 3 优先，缺的走榜单加东财下载补齐。"""
    subject = _subject(job)
    try:
        ranking = fetcher.fetch_hibor_ranking()
    except fetcher.FetchError as e:
        _log(job, f"慧博榜单取不到（{e}），公司样板退回库内关键词匹配")
        return _sample_candidates(job)[:3]
    group = fetcher.match_group(ranking["items"], subject)
    industry = subject
    if not group:
        for title in _company_industry_titles(subject)[:3]:
            hit = fetcher.match_group(ranking["items"], title)
            if hit:
                group, industry = hit, title
                break
    if not group:
        _log(job, "新财富榜单没有匹配到该公司所属行业组，样板退回库内关键词匹配")
        return _sample_candidates(job)[:3]
    brokers = sorted([it for it in ranking["items"] if it["group"] == group],
                     key=lambda x: x.get("rank") or 99)[:3]
    job["top3"] = {"group": group, "brokers": [b["org"] for b in brokers]}
    hits = _library_top3_samples([subject], brokers)
    if len(hits) >= len(brokers):
        _log(job, f"样板取库内新财富「{group}」前 {len(brokers)} 研报：" + "、".join(s["file"] for s in hits))
        return hits[:3]
    _log(job, f"库内新财富「{group}」前 3 覆盖 {subject} 的研报只有 "
              + ("、".join(s["broker"] for s in hits) or "无")
              + "，缺的走东财下载补齐")
    _tick(tid, 15, f"库内前 3 样板不足，走榜单加东财找「{subject}」")
    try:
        res = fetcher.sample_download_chain(industry, limit=max(1, 3 - len(hits)), tid=tid,
                                            subject=subject, skip_orgs=[s["broker"] for s in hits])
    except fetcher.FetchError as e:
        _log(job, f"样板下载链失败，按公司页面与通用框架合成大纲：{e}")
        samples = (hits + _sample_candidates(job))[:3]
        _log_manual_broker_gap(job, brokers, samples)
        return samples
    notes = "；".join(res.get("notes") or [])
    if notes:
        _log(job, "样板链：" + notes)
    samples = hits + _chain_samples(res)
    if len(samples) < 3:
        have = {s["file"] for s in samples}
        pad = [c for c in _sample_candidates(job) if c["file"] not in have]
        if pad:
            _log(job, "新财富前 3 样板不足 3 份，按库内关键词研报补位")
            samples += [dict(c, broker="") for c in pad]
    samples = samples[:3]
    _log_manual_broker_gap(job, brokers, samples)
    return samples


def _ensure_samples(job, tid):
    """行业研报样板：新财富该行业组前 3 券商研报，每家 1 份、至少 3 份，库内优先、缺的东财下载。"""
    if job.get("kind") == "company":
        return _ensure_company_samples(job, tid)
    industry = _industry_of(job["title"], job["question"])
    keys = [k for k in _terms(f"{job['title']} {job['question']}") if k]
    brokers, group = [], None
    try:
        ranking = fetcher.fetch_hibor_ranking()
        group = fetcher.match_group(ranking["items"], industry)
        if group:
            brokers = sorted([it for it in ranking["items"] if it["group"] == group],
                             key=lambda x: x.get("rank") or 99)[:3]
    except fetcher.FetchError as e:
        _log(job, f"慧博榜单取不到（{e}），样板按东财热度找")
    job["top3"] = {"group": group, "brokers": [b["org"] for b in brokers]}
    samples = []
    if brokers:
        hits = _library_top3_samples(keys, brokers)
        if len(hits) >= len(brokers):
            _log(job, f"样板取库内新财富「{group}」前 {len(brokers)} 研报：" + "、".join(s["file"] for s in hits))
            return hits[:3]
        if hits:
            _log(job, "库内新财富「" + str(group) + "」前 3 样板只有 "
                      + "、".join(s["broker"] for s in hits) + "，缺的走东财下载补齐")
        samples += hits
        _tick(tid, 15, f"库内前 3 样板不足，走榜单加东财找「{industry}」")
    else:
        _tick(tid, 15, f"榜单没有匹配到「{industry}」的行业组，走东财热度找样板")
    try:
        res = fetcher.sample_download_chain(industry, limit=max(1, 3 - len(samples)), tid=tid,
                                            skip_orgs=[s["broker"] for s in samples])
    except fetcher.FetchError as e:
        _log(job, f"样板下载链失败，改用双源合成大纲：{e}")
        samples = samples or _sample_candidates(job)[:3]
        _log_manual_broker_gap(job, brokers, samples)
        return samples
    notes = "；".join(res.get("notes") or [])
    if notes:
        _log(job, "样板链：" + notes)
    samples += _chain_samples(res)
    if len(samples) < 3:
        have = {s["file"] for s in samples}
        pad = [c for c in _sample_candidates(job) if c["file"] not in have]
        if pad:
            _log(job, "新财富前 3 样板不足 3 份，按库内关键词研报补位")
            samples += [dict(c, broker="") for c in pad]
    samples = samples[:3]
    _log_manual_broker_gap(job, brokers, samples)
    return samples


def _fallback_pages(title, question, cap=8):
    keys = _terms(f"{title} {question}")
    scored = []
    for p in wiki.load_pages():
        if p["type"] == "source":
            continue
        blob = str(p["meta"].get("title") or p["slug"]) + " " + str(p["meta"].get("summary") or "")
        score = sum(1 for k in keys if k in blob)
        if score:
            scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:cap]]


def _page_names(page):
    """页面可匹配名集合：标题、slug、别名。"""
    names = {str(page["meta"].get("title") or page["slug"]), page["slug"]}
    names |= {str(a) for a in (page["meta"].get("aliases") or [])}
    return names


def _subject(job):
    """公司研报主体：只从标题里剥掉「研究报告」等词，不掺进原始提问。"""
    text = sanitize(str(job.get("title") or ""))
    for word in ("公司研究", "公司深度", "公司报告", "研究报告", "深度研究", "深度报告",
                 "分析报告", "研报", "报告", "研究", "分析"):
        text = text.replace(word, "")
    text = text.strip()
    return text or _industry_of(job["title"], job["question"])


def _stock_code(job):
    """从公司页别名里取股票代码，A 股如 600104.SH，港股如 0175.HK。"""
    subject = _subject(job)
    for page in wiki.load_pages():
        if page["type"] != "company" or subject not in _page_names(page):
            continue
        for name in _page_names(page):
            m = re.search(r"\b(\d{4,6})\.(HK|SH|SZ|BJ)\b", name, re.I)
            if m:
                return m.group(0).upper()
    return ""


def _annual_in_library(job):
    """库里是否已有该公司的年度报告（文件名或结构化财报里出现 FY 报告期）。"""
    subject = _subject(job)
    for f in ingest_mod.load_manifest()["files"]:
        name = str(f.get("file") or "")
        topics = " ".join(str(t) for t in (f.get("topics_touched") or []))
        if subject not in name and subject not in topics:
            continue
        if ANNUAL_RE.search(name) or re.search(r"财报_\d{4}1231", name):
            return f
    for path in config.FIN_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if subject not in path.stem and subject != str(data.get("company") or ""):
            continue
        if any(str(pk).endswith("FY") for pk in (data.get("periods") or {})):
            return {"file": path.name}
    return None


PERIOD_KEY_RE = re.compile(
    r"(20\d{2})\s*(?:年)?\s*(半年度报告|半年报|中期报告|中报|年度报告|年報|年报|"
    r"第一季度报告|一季度报告|一季报|第三季度报告|三季度报告|三季报|季度报告|季报)")
PERIOD_CODE_RE = re.compile(r"(20\d{2})\s*(FY|H1|Q[1-4])", re.I)
DATE8_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})")
PERIOD_LABELS = {"annual": "年度报告", "interim": "半年报", "quarterly": "季报"}


def _period_from_text(text):
    """从文件名或报告期标记解析 (类型, 报告期标签)，如 2026年三季报 -> (quarterly, 2026Q3)。"""
    s = str(text or "")
    m = PERIOD_KEY_RE.search(s)
    if m:
        year, kw = int(m.group(1)), m.group(2)
        if kw in ("半年度报告", "半年报", "中期报告", "中报"):
            return "interim", f"{year}H1"
        if kw in ("年度报告", "年報", "年报"):
            return "annual", f"{year}FY"
        q = "Q1" if kw in ("第一季度报告", "一季度报告", "一季报") else "Q3"
        return "quarterly", f"{year}{q}"
    m = PERIOD_CODE_RE.search(s)
    if m:
        year, code = int(m.group(1)), m.group(2).upper()
        kind = {"FY": "annual", "H1": "interim"}.get(code, "quarterly")
        return kind, f"{year}{code}"
    return None


def _period_end(kind, label):
    year = int(str(label)[:4])
    if kind == "annual":
        return date(year, 12, 31)
    if kind == "interim":
        return date(year, 6, 30)
    return date(year, 3, 31) if str(label).endswith("Q1") else date(year, 9, 30)


def _filing_date(text):
    """取文件名里的 8 位日期，多个取最后一个；没有返回 None。"""
    for y, m, d in reversed(DATE8_RE.findall(str(text or ""))):
        try:
            return date(int(y), int(m), int(d))
        except ValueError:
            continue
    return None


def _record_date(kind, label, text):
    got = _filing_date(text)
    if got:
        return got
    lag = {"annual": 120, "interim": 62, "quarterly": 31}.get(kind, 31)
    return _period_end(kind, label) + timedelta(days=lag)


def _periodic_records(job):
    """库里该公司的定期报告记录，来源为 manifest 文件名与 data/fin 报告期。"""
    subject = _subject(job)
    out = {}
    for f in ingest_mod.load_manifest()["files"]:
        name = str(f.get("file") or "")
        topics = " ".join(str(t) for t in (f.get("topics_touched") or []))
        if subject not in name and subject not in topics:
            continue
        meta = _period_from_text(name)
        if not meta:
            continue
        kind, label = meta
        out[(kind, label, name)] = {
            "kind": kind, "label": label, "file": name,
            "date": _record_date(kind, label, name),
        }
    for path in config.FIN_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        names = [path.stem, str(data.get("company") or "")]
        names += [str(a) for a in (data.get("aliases") or [])]
        if not any(subject in n for n in names if n):
            continue
        for pk, pv in (data.get("periods") or {}).items():
            meta = _period_from_text(pk)
            if not meta:
                continue
            kind, label = meta
            src = str((pv or {}).get("source") or "")
            out[(kind, label, src)] = {
                "kind": kind, "label": label, "file": src or path.name,
                "date": _record_date(kind, label, src or label),
            }
    return list(out.values())


def _periodic_in_library(job):
    """最近 365 天内披露的定期报告，每类取最新一份。"""
    today = date.today()
    start = today - timedelta(days=365)
    best = {}
    for r in _periodic_records(job):
        if not (start <= r["date"] <= today):
            continue
        cur = best.get(r["kind"])
        if not cur or r["date"] > cur["date"]:
            best[r["kind"]] = r
    return best


def _latest_quarter_kind(today):
    """当前已到披露期的最近一季：1 到 4 月看上年三季报，5 到 10 月看一季报，11 月起看三季报。"""
    if today.month <= 4 or today.month >= 11:
        return "q3"
    return "q1"


def _periodic_gaps(job):
    """公司研报必选缺口：最近一年内的年报、半年报、季报，缺哪类补哪类。"""
    if job.get("kind") != "company":
        return []
    coverage = _periodic_in_library(job)
    subject = _subject(job)
    code = _stock_code(job)
    market = "hk" if code.endswith(".HK") else "a"
    today = date.today()
    specs = [
        ("annual", "公司研报的财务分析与盈利预测必须以最近一年内的年报为锚"),
        ("interim", "最近一年内的半年报用于更新经营数据，按定期报告覆盖规则须补齐"),
        ("quarterly", "最近一年内的季报用于更新最新经营数据，按定期报告覆盖规则须补齐"),
    ]
    out = []
    for kind, why in specs:
        if kind in coverage:
            continue
        if kind == "annual":
            fetch = {"source": "annual_report",
                     "params": {"company": subject, "code": code, "market": market}}
        else:
            pkind = kind
            if kind == "quarterly" and market == "a":
                pkind = _latest_quarter_kind(today)
            fetch = {"source": "periodic_report",
                     "params": {"company": subject,
                                "code": code.split(".")[0] if market == "a" else code,
                                "market": market, "kind": pkind,
                                "days": 400, "limit": 1}}
        out.append({
            "id": kind,
            "item": f"{subject}最近一年内{PERIOD_LABELS[kind]}（财报）",
            "why": why,
            "fetch": fetch,
            "status": "pending",
            "required": not (market == "hk" and kind == "quarterly"),
        })
    return out


def _gap_targets(job, cap=10):
    """缺口清单里要的数据，随取数任务带进财报摄入。

    文档类缺口（年报、公告、研报本身）不是数据目标，不进清单；取到年报后由
    filing.ingest_filing 按这些目标补抄标准科目字典以外的数据。
    """
    out, seen = [], set()
    for g in job.get("gaps") or []:
        if g.get("status") not in ("selected", "done"):
            continue
        item = str(g.get("item") or "").strip()
        if not item or DOC_GAP_RE.search(item) or item in seen:
            continue
        seen.add(item)
        out.append({"item": item, "why": str(g.get("why") or "").strip()})
        if len(out) >= cap:
            break
    return out


def _base_gaps(job):
    """程序生成的缺口：未摄入材料提醒 + 公司定期报告（年报、半年报、季报）。"""
    gaps = _pending_gap_items()
    periodic = _periodic_gaps(job)
    if periodic:
        gaps = periodic + gaps
    return gaps


def _drop_periodic_dupes(gaps, job):
    """模型也列了定期报告时去掉重复项，保留程序生成的必选缺口。

    最近一年内的该类报告已在库的，其余指向它的缺口改成不能自动补：重下同一份文件会被
    SHA256 去重，库里的条目不会增加，标成可取数只会给假的「已补」。
    """
    subject = _subject(job)
    coverage = _periodic_in_library(job)
    out = []
    for g in gaps:
        if g.get("required"):
            out.append(g)
            continue
        text = str(g.get("item") or "")
        if subject in text and any(k in text for k in (
                "年报", "年度报告", "半年报", "半年度报告", "季报", "季度报告")):
            continue
        fetch = g.get("fetch") or {}
        kind = ""
        if fetch.get("source") == "annual_report":
            kind = "annual"
        elif fetch.get("source") == "periodic_report":
            pkind = str((fetch.get("params") or {}).get("kind") or "")
            if pkind == "interim":
                kind = "interim"
            elif pkind in ("q1", "q3", "quarterly"):
                kind = "quarterly"
        if kind and kind in coverage:
            g["fetch"] = None
            note = (f"库内已有最近一年内的{PERIOD_LABELS[kind]}"
                    f" {coverage[kind].get('file') or ''}，重下同一份不会新增条目")
            g["why"] = (g["why"] + "；" if g.get("why") else "") + note
        out.append(g)
    return out


def _pending_gap_items(cap=5):
    """raw 里有、清单里没有的材料：列成不能自动补的缺口，提醒先摄入，否则拿不到锚点。

    重复下载的同内容文件（不同文件名、同一 SHA256）不算未摄入，摄入时本来就会跳过。
    """
    entries = ingest_mod.load_manifest()["files"]
    known = {f["file"] for f in entries}
    known_sha = {f.get("sha256") for f in entries if f.get("sha256")}
    pending = []
    for p in ingest_mod.iter_raw_files():
        if p.name in known:
            continue
        try:
            if ingest_mod.sha256_of(p) in known_sha:
                continue
        except OSError:
            pass
        pending.append(p)
        if len(pending) >= cap:
            break
    return [{
        "id": f"u{i}", "item": f"未摄入材料：{p.name}",
        "why": "先入库才能取页码锚点，到首页上传或跑 manage.py ingest --all",
        "fetch": None, "status": "pending",
    } for i, p in enumerate(pending, 1)]


def _material_base(job):
    """两类研报共用的选页：先问答选页，再用标题关键词兜底。"""
    question = f"{job['title']} {job['question']}"
    pages, slugs, _reason, _use_index = ask_mod._select_pages(question, None)
    sel = [p for p in pages if p["slug"] in slugs and p["type"] != "source"]
    have = {p["slug"] for p in sel}
    for p in _fallback_pages(job["title"], job["question"]):
        if p["slug"] not in have:
            sel.append(p)
            have.add(p["slug"])
    return sel, have


def _material_gap_pages(job, have, cap=4):
    """缺口取数刚落库的文件，其 wiki 页必须进成文上下文，否则取到的数据用不上。"""
    files = set()
    for g in job.get("gaps") or []:
        if g.get("status") == "done":
            files.update(str(x) for x in (g.get("materials") or []))
    if not files:
        return []
    out = []
    for p in wiki.load_pages():
        if len(out) >= cap:
            break
        if p["slug"] in have:
            continue
        if files & set(p["meta"].get("sources") or []):
            out.append(p)
            have.add(p["slug"])
    return out


def _pages_with_legend(sel):
    manifest = ingest_mod.load_manifest()
    chunks = []
    for p in sel:
        head = f"### {p['meta'].get('title', p['slug'])}（slug: {p['slug']}，类型 {p['type']}）"
        legend = source_priority.page_legend(p, manifest)
        chunks.append("\n".join(x for x in (head, legend, p["body"]) if x))
    return "\n\n".join(chunks)


def _material_industry(job):
    """行业研报取料人：行业页与概念页优先，公司页只留少量做竞争格局对照。"""
    sel, have = _material_base(job)
    order = {"industry": 0, "concept": 1, "company": 2}
    sel.sort(key=lambda p: order.get(p["type"], 3))
    extra = _material_gap_pages(job, have)
    sel = (extra + sel)[:10]
    return sel, _pages_with_legend(sel), ask_mod._allowed_from_pages(sel)


def _material_company(job):
    """公司研报取料人：本公司页排最前，年报来源页随后，再补同行与行业页。"""
    sel, have = _material_base(job)
    subject = _industry_of(job["title"], job["question"])
    top = next((p for p in sel if p["type"] == "company" and _page_names(p) & {subject}), None)
    if top:
        sel.remove(top)
        sel.insert(0, top)
    elif subject:
        hit = next((p for p in wiki.load_pages()
                    if p["type"] == "company" and subject in _page_names(p)), None)
        if hit:
            sel.insert(0, hit)
            have.add(hit["slug"])
    annual = _annual_in_library(job)
    rel = (annual or {}).get("path") or ""
    if rel:
        head = sel[:1]
        extra = [p for p in wiki.load_pages()
                 if p["slug"] not in have and rel in (p["meta"].get("sources") or [])][:2]
        sel = head + extra + sel[1:]
        have.update(p["slug"] for p in extra)
    gap_pages = _material_gap_pages(job, have)
    sel = (sel[:1] + gap_pages + sel[1:])[:10]
    return sel, _pages_with_legend(sel), ask_mod._allowed_from_pages(sel)


def _material(job):
    if job.get("kind") == "company":
        return _material_company(job)
    return _material_industry(job)


# ---------- 大纲 ----------

OUTLINE_PROMPT = """你是中文卖方行业研报的主笔，现在只定大纲，不写正文。

报告主题：{TITLE}
用户原始要求：{Q}

可参考的结构文件：
--- 机构研报逐页解剖框架 ---
{STRUCTURE}
--- 行业全景报告标准结构 ---
{SECTOR}
--- 竞对与定位参考 ---
{COMPETITIVE}
--- 可比公司分析框架 ---
{COMPS}
--- 样板研报骨架（开头部分） ---
{SAMPLES}

知识库可用页面：
{PAGES}

要求：
1. 章节顺序固定按行业分析的展开逻辑，最多 {MAXS} 节：产业链（上游、中游、下游与价值分布）→ 生产端（产能、产量、成本与供给格局）→ 需求端（需求结构、下游景气、渗透率）→ 市场规模（量价与规模测算）→ 竞争格局（份额、集中度、玩家分层）→ 增长驱动（政策、技术、渗透率、出口等）→ 盈利与估值 → 投资要点 → 风险提示。产业链必须放在第一节，投资要点必须放在行业分析之后，不得用投资要点开篇；缺料的章节可合并，但产业链、生产端、需求端、市场规模、竞争格局、增长驱动六节必须保留
2. 每节写清：本节论点、需要的数据与表、可画图表的建议（只写数据、图型与口径，不编号、不写图题）、取料页面（写 slug）
3. 只用知识库页面里有的材料定结构；库里没有的数据不要在这里编，缺口由下一道程序另列
4. 输出 markdown：第一行 "# 大纲：{TITLE}"，其后每节 "## 一、标题" 形式，节内用短横线列论点/数据/图表建议/取料页
5. 不写正文，不写套话"""

GAPS_PROMPT = """你在给研报大纲列数据缺口。报告主题：{TITLE}

大纲：
{OUTLINE}

知识库已有页面：{PAGES}

可用的免登录取数源（source / params），source 必须原样使用下面的英文 id：
- stats_series：国家统计局，params {{"keyword": "中文指标名，多个用分号", "region": "全国或省市", "freq": "auto|月|季|年", "periods": 期数}}
- akshare_series：AKShare 宏观序列，params {{"series": "以下之一：{SERIES}", "periods": 期数}}
- eastmoney_reports：东财研报下载，params {{"qtype": "0个股|1行业|2策略", "industry": "行业", "stock_code": "代码", "broker": "券商可空", "limit": 份数}}
- cninfo_announce：巨潮 A 股公告（港股不覆盖），params {{"query": "检索词", "code": "A股代码可空", "category": "分类代码可空", "days": 天数, "limit": 份数}}
- hkex_announce：港交所公告，params {{"company": "公司名", "code": "港股代码", "category": "annual|interim|quarterly|esg|results|all", "limit": 份数}}
- annual_report：公司年报，params {{"company": "公司名", "market": "a|hk"}}
- periodic_report：公司定期报告，params {{"company": "公司名", "code": "代码可空", "market": "a|hk", "kind": "interim|q1|q3|quarterly", "days": 天数}}
- sample_chain：行业样板研报下载链，params {{"industry": "行业", "limit": 份数}}
- cpca_sales：乘联会汽车销量，params {{"charttype": "1整体|2厂商|3车型|4国别|5级别|6新能源", "metric": "产量|批发|零售|出口|当月销量|累计销量|当月份额|累计份额|全部", "months": 月数}}
- csindex_index：中证指数行情估值与权重，params {{"code": "指数代码"}}
- pbc_stat：人民银行统计表，params {{"dataset": "货币统计概览|金融业机构资产负债统计|金融机构信贷收支统计|金融市场统计", "keyword": "表名关键词", "months": 月数}}
- fred_series：FRED 美国宏观，params {{"series_id": "序列号", "periods": 期数}}
- imf_series：IMF DataMapper，params {{"indicator": "指标代码", "countries": "CHN,USA", "periods": 期数}}
- dbnomics_series：DBnomics，params {{"series": "provider/dataset/series", "periods": 期数}}
- worldbank_series：World Bank，params {{"indicator": "指标代码", "countries": "CHN", "periods": 期数}}
- caam_article：中汽协行业要闻文字稿，params {{"keyword": "标题关键词", "category": "行业要闻|协会动态", "limit": 篇数}}；产销数据文章正文是图片，不要用它取销量，改用 cpca_sales
- web_search：网络检索兜底，params {{"query": "检索式", "limit": 份数}}
先配专用源；专用源明确取不到的再用 web_search，不要直接放弃。

只列大纲需要、但知识库页面里明显没有的关键数据，最多 8 条；每条都必须配上面能执行的源。
只输出 JSON：{{"gaps": [{{"item": "缺什么数据", "why": "哪节要用", "source": "stats_series|akshare_series|eastmoney_reports|cninfo_announce|hkex_announce|annual_report|periodic_report|sample_chain|cpca_sales|csindex_index|pbc_stat|fred_series|imf_series|dbnomics_series|worldbank_series|caam_article|web_search", "params": {{}}}}]}}"""


OUTLINE_PROMPT_COMPANY = """你是中文卖方公司研报的主笔，现在只定大纲，不写正文。

公司研究对象：{TITLE}
用户原始要求：{Q}

--- 公司研报权威框架（技能 company-report） ---
{FRAMEWORK}

--- 页面组织与图表版式参考 ---
{STRUCTURE}

--- 可比公司分析框架 ---
{COMPS}

--- 库内研报对该公司或其行业的覆盖（开头部分） ---
{SAMPLES}

知识库可用页面：
{PAGES}

要求：
1. 章节按上面的权威框架走，最多 {MAXS} 节。八节依次是投资摘要与评级、公司概况与历史沿革、行业空间与竞争格局、核心竞争力与护城河、经营与财务分析、盈利预测与关键假设、估值与投资建议、风险提示；缺料的节可以合并，但财务分析与估值两节必须保留
2. 财务分析节必须以公司年报为锚，并用最近一年内的半年报、季报更新最新数据，写明营收拆分、毛利率、费用率、ROE、现金流与资产负债的关键科目；定期报告还没入库时，大纲里照常列出这一节并标注「待补定期报告」
3. 每节写清：本节论点、需要的数据与表、可画图表的建议（只写数据、图型与口径，不编号、不写图题）、取料页面（写 slug）
4. 只用知识库页面里有的材料定结构；库里没有的数据不要在这里编，缺口由下一道程序另列
5. 输出 markdown：第一行 "# 大纲：{TITLE}"，其后每节 "## 一、标题" 形式，节内用短横线列论点/数据/图表建议/取料页
6. 不写正文，不写套话"""

SOURCE_ALIASES = {
    "stats": "stats_series", "nbs": "stats_series", "统计局": "stats_series",
    "akshare": "akshare_series", "ak": "akshare_series",
    "eastmoney": "eastmoney_reports", "东财": "eastmoney_reports",
    "cninfo": "cninfo_announce", "巨潮": "cninfo_announce",
    "hkex": "hkex_announce", "港交所": "hkex_announce", "披露易": "hkex_announce",
    "annual": "annual_report", "年报": "annual_report", "annual_report": "annual_report",
    "periodic": "periodic_report", "定期报告": "periodic_report", "periodic_report": "periodic_report",
    "web": "web_search", "web_search": "web_search", "网络检索": "web_search",
    "sample": "sample_chain", "sample_chain": "sample_chain",
    "hibor": "hibor_ranking", "hibor_ranking": "hibor_ranking",
    "cpca": "cpca_sales", "乘联会": "cpca_sales",
    "csindex": "csindex_index", "中证": "csindex_index", "中证指数": "csindex_index",
    "pbc": "pbc_stat", "人行": "pbc_stat", "央行": "pbc_stat", "人民银行": "pbc_stat",
    "fred": "fred_series", "imf": "imf_series",
    "dbnomics": "dbnomics_series", "worldbank": "worldbank_series",
    "世界银行": "worldbank_series", "caam": "caam_article", "中汽协": "caam_article",
}


def _read_skill(path, cap=14000):
    try:
        return path.read_text(encoding="utf-8")[:cap]
    except OSError:
        return ""


def _phase_outline(job_id, tid):
    job = load_job(job_id)
    company = job.get("kind") == "company"
    _tick(tid, 5, "选样板与取料页")
    job["samples"] = _ensure_samples(job, tid)
    sel, pages_text, _allowed = _material(job)
    if not sel and not job["samples"]:
        # 先把样板链与选页的日志落盘再报错，避免 _start 异常分支用旧磁盘版覆盖丢失
        save_job(job)
        raise RuntimeError("知识库里没有与该主题相关的页面，样板也没取到；先上传或取数入库后再试")
    job["pages"] = [{"slug": p["slug"], "title": str(p["meta"].get("title") or p["slug"])} for p in sel]
    _log(job, f"取料页 {len(sel)} 个：{('、'.join(p['slug'] for p in sel)) or '无'}")

    samples = "\n\n".join(_sample_text(s) for s in job["samples"]) or (
        "（库里没有提到该公司的研报，按公司页面与通用公司研报框架合成）" if company
        else "（库内没有可用样板，按双源框架合成）")
    _tick(tid, 40, "合成大纲")
    base = dict(TITLE=job["title"], Q=job["question"], SAMPLES=samples[:8000],
                PAGES=pages_text or "（没有选中页面）",
                MAXS=str(config.CFG.get("report_pipeline", {}).get("max_sections", 8)))
    if company:
        prompt = (OUTLINE_PROMPT_COMPANY
                  .replace("{TITLE}", base["TITLE"]).replace("{Q}", base["Q"])
                  .replace("{FRAMEWORK}", _read_skill(COMPANY_SKILL_PATH, cap=12000))
                  .replace("{STRUCTURE}", _read_skill(COMPANY_STRUCTURE_PATH, cap=3500))
                  .replace("{COMPS}", _read_skill(COMPANY_COMPS_PATH, cap=5000))
                  .replace("{SAMPLES}", base["SAMPLES"]).replace("{PAGES}", base["PAGES"])
                  .replace("{MAXS}", base["MAXS"]))
    else:
        prompt = (OUTLINE_PROMPT
                  .replace("{TITLE}", base["TITLE"]).replace("{Q}", base["Q"])
                  .replace("{STRUCTURE}", _read_skill(STRUCTURE_PATH, cap=9000))
                  .replace("{SECTOR}", _read_skill(SECTOR_PATH, cap=6000))
                  .replace("{COMPETITIVE}", _read_skill(COMPETITIVE_PATH, cap=6000))
                  .replace("{COMPS}", _read_skill(COMPS_PATH, cap=6000))
                  .replace("{SAMPLES}", base["SAMPLES"]).replace("{PAGES}", base["PAGES"])
                  .replace("{MAXS}", base["MAXS"]))
    outline = ask_mod.strip_glitch(llm.chat([{"role": "user", "content": prompt}], task="outline"))
    if not re.search(r"^##\s+", outline, re.M):
        raise RuntimeError("大纲没有解析出章节（模型未按 ## 标题输出），点继续重试")

    topic = _topic(job["title"])
    path = _unique_path(config.OUTPUTS_DIR,
                       f"大纲-个人-{topic}-{config.today_str().replace('-', '')}", ".md")
    path.write_text(outline.rstrip() + "\n", encoding="utf-8")
    job["outline"] = outline
    job["outline_path"] = path.relative_to(config.ROOT).as_posix()
    _log(job, f"大纲已存 {path.name}")

    _tick(tid, 70, "列数据缺口")
    gaps = _base_gaps(job)
    try:
        gaps += _drop_periodic_dupes(
            _extract_gaps(job, outline, [str(p["meta"].get("title") or p["slug"]) for p in sel]), job)
    except llm.LLMError as e:
        _log(job, f"缺口提取失败（本次不列外部缺口）：{e}")
    job["gaps"] = gaps
    if job["gaps"]:
        _log(job, f"缺口 {len(job['gaps'])} 条（含未摄入材料提醒）")
    job["status"] = "outline_pending"
    _log(job, f"大纲待确认，缺口 {len(job['gaps'])} 条")
    save_job(job)
    _tick(tid, 100, "大纲已生成，等你在报告页确认")


def _extract_gaps(job, outline, page_titles):
    titles = "、".join(page_titles) or "（无）"
    prompt = (GAPS_PROMPT
              .replace("{TITLE}", job["title"])
              .replace("{OUTLINE}", outline[:9000])
              .replace("{PAGES}", titles)
              .replace("{SERIES}", "、".join(fetcher.AKSHARE_SERIES.keys())))
    out = llm.chat([{"role": "user", "content": prompt}], task="select", response_json=True)
    valid = {s["id"] for s in fetcher.source_specs()}
    gaps = []
    for g in (out.get("gaps") or [])[:8]:
        item = str(g.get("item") or "").strip()
        if not item:
            continue
        source = str(g.get("source") or "none").strip().lower()
        source = SOURCE_ALIASES.get(source, source)
        params = g.get("params") if isinstance(g.get("params"), dict) else {}
        # 没有专用源不等于放弃：一律转网络检索，取不到再由取数环节停回缺口关卡
        if source not in valid or source == "none":
            source = "web_search"
            params = {"query": item, "limit": 3}
        elif source == "web_search" and not str(params.get("query") or "").strip():
            params["query"] = item
        gaps.append({
            "id": f"g{len(gaps) + 1}", "item": item, "why": str(g.get("why") or "").strip(),
            "fetch": {"source": source, "params": params},
            "status": "pending",
        })
    return gaps


def approve_outline(job_id, outline_md=None):
    job = load_job(job_id)
    if not job:
        raise KeyError("报告任务不存在")
    if outline_md and outline_md.strip():
        job["outline"] = outline_md.strip() + "\n"
        path = config.ROOT / job["outline_path"] if job.get("outline_path") else None
        if path:
            path.write_text(job["outline"], encoding="utf-8")
        if not re.search(r"^##\s+", job["outline"], re.M):
            raise ValueError("大纲里没有 ## 章节标题，先补上再确认")
        _log(job, "大纲已按网页编辑版覆盖")
        old_gaps = job.get("gaps") or []
        gaps = _base_gaps(job)
        try:
            with llm.use_model(job.get("model")):
                gaps += _drop_periodic_dupes(_extract_gaps(
                    job, job["outline"],
                    [p.get("title") or p.get("slug") for p in job.get("pages") or []]), job)
        except llm.LLMError as e:
            # 列缺口失败不能当成「没有缺口」直接成文，否则勾选关卡被绕过
            _log(job, f"重新列缺口失败，沿用原有 {len(old_gaps)} 条：{e}")
            have = {g["id"] for g in gaps}
            gaps += [g for g in old_gaps if g["id"] not in have and not g.get("required")]
        job["gaps"] = gaps
    save_job(job)
    if job.get("gaps"):
        job["status"] = "gaps_pending"
        save_job(job)
        return job
    _begin_write(job)
    return load_job(job_id)


def approve_gaps(job_id, gap_ids):
    job = load_job(job_id)
    if not job:
        raise KeyError("报告任务不存在")
    chosen = set(gap_ids or [])
    missing = [g["item"] for g in (job.get("gaps") or []) if g.get("required") and g["id"] not in chosen]
    if missing:
        raise ValueError("公司研报必须先补齐最近一年内的定期报告，不能跳过：" + "、".join(missing))
    no_source = [g["item"] for g in (job.get("gaps") or [])
                 if g["id"] in chosen and not ((g.get("fetch") or {}).get("source"))]
    if no_source:
        raise ValueError("这些缺口不能在网页端自动补，先摄入材料或取消勾选："
                         + "、".join(no_source))
    fetchable = []
    for g in job.get("gaps") or []:
        if g["id"] in chosen:
            g["status"] = "selected"
            if g.get("fetch"):
                fetchable.append(g)
        else:
            g["status"] = "skipped"
    save_job(job)
    if not fetchable:
        _begin_write(job)
        return {"started": "write", "fetchable": 0}

    job["status"] = "fetching"
    save_job(job)

    def run(tid):
        # 取数线程不继承主线程的模型上下文，这里补上，否则摄入会退回默认模型
        with llm.use_model(job.get("model")):
            return _fetch_gaps(tid)

    def _fetch_gaps(tid):
        notes = []

        def shortfall(r):
            """取数成功不等于数据可用：材料没入库、或入库 0 条，都算缺口没补上。"""
            failed = (r or {}).get("ingest_failed") or []
            if failed:
                return f"材料没入库（{'、'.join(failed)}），没有锚点"
            if int((r or {}).get("entries_count") or 0) <= 0:
                return "取到材料但没有形成可用的 wiki 知识条目"
            return ""

        # 本次勾选的数据缺口随取数带进摄入：财报摄入据此补抄 skill 字典外的目标数据
        targets = _gap_targets(job)
        for i, g in enumerate(fetchable, 1):
            tasks.update(tid, current=i - 1, detail=f"补缺口 {i}/{len(fetchable)}：{g['item']}")
            source = (g.get("fetch") or {}).get("source")
            params = (g.get("fetch") or {}).get("params") or {}
            r, err = None, ""
            try:
                r = fetcher.run_fetch(source, params, tid, targets=targets)
                err = shortfall(r)
            except Exception as e:
                err = str(e)
            if err and not g.get("required") and source != "web_search":
                tasks.update(tid, detail=f"专用源没补上，改网络检索：{g['item']}")
                try:
                    fb = fetcher.run_fetch("web_search", {"query": g["item"], "limit": 3}, tid,
                                           targets=targets)
                    ferr = shortfall(fb)
                    if ferr:
                        err += f"；网络检索也没补上：{ferr}"
                    else:
                        r, err = fb, ""
                        notes.append(f"{g['item']}：专用源没补上，网络检索补到")
                except Exception as e2:
                    err += f"；网络检索也失败：{e2}"
            if err:
                g["status"] = "failed"
                g["error"] = err
                notes.append(f"{g['item']}：失败（{err}）")
            else:
                g["status"] = "done"
                g["materials"] = list(dict.fromkeys(r.get("files") or []))
                g.pop("error", None)
                notes.append(f"{g['item']}：{r.get('summary', '完成')}")
            j = load_job(job_id)
            if j:
                for old in j.get("gaps") or []:
                    if old["id"] == g["id"]:
                        old["status"] = g["status"]
                        old["materials"] = list(g.get("materials") or [])
                        if g.get("error"):
                            old["error"] = g["error"]
                        else:
                            old.pop("error", None)
                _log(j, notes[-1])
                save_job(j)
        j = load_job(job_id)
        lost = [g for g in (j.get("gaps") or []) if g.get("status") not in ("done", "skipped")]
        if lost:
            tasks.update(tid, current=len(fetchable), detail="缺口没补齐，停回缺口关卡")
            j["status"] = "gaps_pending"
            j["error"] = ("这些缺口没补上，不能带着数据缺失成文：" + "、".join(
                f"{g['item']}（{g.get('error') or g.get('status')}）" for g in lost)
                + "。换检索词重试、上传材料，或取消勾选后重试")
            _log(j, j["error"])
            save_job(j)
            return {"summary": "；".join(notes + [j["error"]])}
        tasks.update(tid, current=len(fetchable), detail="缺口处理完，开始成文")
        _begin_write(j)
        return {"summary": "；".join(notes)}

    tasks.start("fetch", run, total=len(fetchable), label=f"研报补数《{job['title']}》")
    return {"started": "fetch", "fetchable": len(fetchable)}


def _begin_write(job):
    job = load_job(job["id"]) or job
    pending = [g for g in job.get("gaps") or []
               if g.get("status") in ("pending", "selected", "failed")]
    if pending:
        job["status"] = "gaps_pending"
        job["error"] = ("还有缺口没处理完，不能成文：" + "、".join(
            g.get("item") or g.get("id") for g in pending))
        _log(job, job["error"])
        save_job(job)
        return False
    job["status"] = "writing"
    job["error"] = ""
    save_job(job)
    _start(job["id"], _phase_write, f"{kind_label(job)}成文《{job['title']}》")
    return True


# ---------- 成文 ----------

WRITE_PROMPT = """你是中文卖方研报的主笔，正在写《{TITLE}》的第 {N}/{TOTAL} 节。

全篇大纲：
{OUTLINE}

本节大纲（只写这一节）：
{SECTION}

可用知识库页面：
{PAGES}

写作规则：
1. 只用上面的页面内容，禁止编造。页面没有的信息直接不写；禁止写「数据缺失」「待补」「页码待补」「——」「暂无数据」等占位符，表格单元格也不得用 — 表示缺数，缺数的行或列直接不写；缺口在成文前已经补完
2. 每个陈述句句尾带锚点 [[文件名.pdf#p页码]] 或 [[文件名.docx#para段落号]]，锚点只能原样取自页面内容里出现过的
3. 含数据的句子必须有锚点；无锚点的句子只能是判断、推断或行业常识，不得承载数据
4. 派生值（ROE、毛利率、增速、占比、差额、人均等由原文数字算出的值）写成「数值（测算：公式）」，如「17.76%（测算：归母净利/期末权益）」；公式用到的原始数字要在同句或相邻句写出，锚点挂在原始数字所在页
5. 锚点必须挂在数字实际出现的页。一段里多个数字分属不同页时分别挂，禁止把整段数字统一挂到同一页码
6. 跨期、跨口径的数字不直接并列或相减，除非写明期间与口径
7. 估值对比写清 PE/PB/PS 口径与日期；竞争格局写集中度、市占率、壁垒；不要空泛排比
8. 中文写作：少引号少冒号，删套话，一句一事，长句拆短
9. 输出本节 markdown：第一行 "## 本节标题"，正文分段，可用 markdown 表格
10. 不写其他节的内容，不写封面、目录、注释表
11. 每个表格上方必须有题注「**表N 标题**」，正文只写表格；不写图题、图N占位、「图表位说明」，实际图表由系统在文末以附图N生成
12. 表格里的每个数字也要能追溯到本节文字或表格题注里的锚点；找不到出处的数字不要放进表格
13. 公司财务数据的来源优先级：财报（年报、半年报、季报）> 公告/招股书 > 券商研报 > 其他材料。同一公司财务指标冲突时，以最新报告期的财报为准；研报数字只能作对照，并写明差异与各自报告期。行业总量数据不套此序，统计局、行业协会、海关等一手来源优先于研报预测
{EXTRA}"""

CHARTS_PROMPT = """下面是刚写完的研报全文。挑出能画图的数据，给图表 spec。

规则：
- 每个图至少 3 个时间点或 3 个类别；数值必须能在报告正文中找到，禁止新造或外推
- kind 用 line（趋势）、bar（分类对比）、hbar（横向排名）、pie（结构占比）
- 最多 {MAXC} 张；没有合适数据就给空数组
- note 只写数据来源文件名与统计口径，不重复具体数值，可取正文锚点里的文件名
只输出 JSON：{{"charts": [{{"title": "...", "kind": "line", "x": ["2024","2025"], "series": [{{"name": "...", "values": [1.2, 3.4]}}], "xlabel": "", "ylabel": "", "unit": "万辆", "note": "..."}}]}}

报告全文：
{REPORT}"""

REVIEW_PROMPT = """你是研报审稿人，只找硬伤，不改写全文。

{SCOPE}

报告全文：
{REPORT}

全文章节（判断结构是否齐全以这里为准）：
{HEADINGS}

程序已发现的锚点问题：
{MECH}

本段引用的锚点原文（判断数字与口径是否有据，先看这里）：
{EVIDENCE}

检查：数字与锚点是否对得上、口径是否写明、结论有没有超出材料、有无空话套话。判断口径与数字时以锚点原文为准，原文本身没写的年份拆分或统计范围，不能要求报告补写，也不能据此判硬伤。只有章节在「全文章节」里确实不存在才算缺章节。
只输出 JSON：{{"issues": [{{"level": "硬伤|建议", "where": "节名或原句", "problem": "一句话"}}]}}，最多 10 条。"""

REPAIR_PROMPT = """按下面列出的硬伤，给最小改动的替换对，不改写全文。

规则：
- old 必须从报告里逐字复制，能唯一定位到一处；new 是改好的同一片段
- 不改结构、不删节、不新造数字；涉及锚点的，只能换成报告里已出现过的真实锚点
- 数据缺失或口径对不上的表述，改法是删掉残缺部分或改成泛称，不是编造数值
- 每条问题都附了报告原句与锚点原文。口径缺失、期间不明、两处表述冲突，先核对原文再改：
  - 原文写明期间、范围、统计对象的，把该口径补进句子
  - 原文本身只给区间或合并口径的，按原文口径改写，不替原文拆分年份或补它没写的范围
  - 原文直接支持原句、原判罚不成立的，不为改而改
- 没有问题就给空数组

只输出 JSON：{{"fixes": [{{"old": "...", "new": "..."}}]}}

注意：new 必须是改好的正文片段本身。不要输出修订说明、核对结论（如「已核对，修正如下」「该锚点无误，保留」「原挂p1，改为p2」），那类文字是给审校记录看的，混进 new 会原样写进报告正文。

硬伤、报告原句与锚点原文：
{ISSUES}

报告全文：
{REPORT}"""

VERIFY_PROMPT = """你是审稿复核员。上一轮审校发现的问题列在下面，报告随后已做修改。
逐条判定每个问题在修改后报告里的现状，只看现状：
- 已解决：错误表述已被修正、替换或整句删除
- 误报：原判罚不成立，修改前的报告本无此错（审稿看错口径、锚点其实支持该表述等）
  锚点原文直接支持原句，或两条表述在各自口径下可以同时成立，也判误报
- 未解决：同样的错误仍在

只输出 JSON：{{"verdicts": [{{"idx": 1, "verdict": "已解决", "note": "一句话依据"}}]}}

问题清单（idx 对应，附报告原句与锚点原文）：
{FINDINGS}

修改后的报告全文：
{REPORT}"""

ORDER_PROMPT = """你是审校记录排版员。审校处置条目列在下面，报告全文随附。
把条目按其在报告中的位置先后重新排序：所在章节靠前的在前，同章节内按正文先后，
图表按所属章节算。锚点核验、全文这类定位不到具体章节的排最后。
同位置时硬伤在前、建议在后。只输出顺序，不改写任何条目内容。

只输出 JSON：{{"order": [2, 0, 1]}}

条目（idx [类别·级别] 位置：问题）：
{FINDINGS}

报告全文：
{REPORT}"""


def _parse_outline(md):
    sections, cur = [], None
    for ln in md.splitlines():
        if ln.startswith("## "):
            cur = {"heading": ln[3:].strip(), "body": []}
            sections.append(cur)
        elif cur is not None:
            cur["body"].append(ln)
    for s in sections:
        s["body"] = "\n".join(s["body"]).strip()
    return sections


def _parse_report_sections(md):
    out = {}
    cur = None
    for ln in md.splitlines():
        if ln.startswith("## "):
            cur = ln[3:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(ln)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _outline_for_write(text):
    """大纲里的「图表位：图N」会把图题带进正文，成文前删掉这些行。"""
    keep = [ln for ln in str(text or "").splitlines()
            if not re.match(r"^\s*(?:[-*+]\s*)?图表位\s*[:：]", ln)]
    return "\n".join(keep)


def _phase_write(job_id, tid):
    job = load_job(job_id)
    sections = _parse_outline(job.get("outline") or "")
    if not sections:
        raise RuntimeError("大纲没有章节，无法成文")
    maxs = int(config.CFG.get("report_pipeline", {}).get("max_sections", 8))
    sections = sections[:maxs]
    _tick(tid, 5, "取料")
    sel, pages_text, allowed = _material(job)
    if not sel:
        raise RuntimeError("没有可用于成文的知识库页面")

    report_path = config.ROOT / job["report_path"] if job.get("report_path") else None
    if report_path and report_path.exists():
        done = _parse_report_sections(report_path.read_text(encoding="utf-8"))
        _log(job, f"续写成文：已有 {len(done)} 节")
    else:
        done = {}

    for i, sec in enumerate(sections, 1):
        heading = sec["heading"]
        if heading in done and done[heading]:
            continue
        _tick(tid, 5 + int(85 * (i - 1) / len(sections)), f"写第 {i}/{len(sections)} 节：{heading}")
        extra = ""
        if job.get("kind") == "company":
            extra = ("11. 公司研报纪律：财务数字按第 13 条来源优先级取自已摄入的年报、半年报、季报；"
                     "券商研报里的公司数字只能做对照，不能替代公司自己的披露。币种与单位写清，"
                     "预测数字标「预计」并交代假设")
        prompt = (WRITE_PROMPT
                  .replace("{TITLE}", job["title"])
                  .replace("{N}", str(i)).replace("{TOTAL}", str(len(sections)))
                  .replace("{OUTLINE}", _outline_for_write(job.get("outline") or "")[:6000])
                  .replace("{SECTION}", f"## {heading}\n{_outline_for_write(sec['body'])}")
                  .replace("{PAGES}", pages_text)
                  .replace("{EXTRA}", extra))
        messages = [{"role": "user", "content": prompt}]
        text = ask_mod.strip_glitch(llm.chat(messages, task="report_write"))
        bad = ask_mod._violations(text, allowed)
        if bad:
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": (
                "以下锚点违规，请修正后重新输出本节完整内容（只改锚点相关内容）：\n- " + "\n- ".join(bad))})
            text = ask_mod.strip_glitch(llm.chat(messages, task="report_write"))
            bad = ask_mod._violations(text, allowed)
        text = _fix_tables_figs(text)
        text, fixes = _auto_fix_anchors(text)
        if fixes:
            _log(job, f"第 {i} 节成文自动回挂锚点 {len(fixes)} 处")
        mech = _anchor_checks(text)
        wrong_page = [p for p in mech if "锚点疑似挂错" in p]
        unanchored = [p for p in mech if p.startswith("无锚点数字")]
        other = [p for p in mech if p not in wrong_page and p not in unanchored]
        if mech:
            tips = []
            if wrong_page:
                tips.append("以下数字挂错了页码，请只改这些锚点，把每个数字挂到它实际出现的页；"
                            "派生值按「数值（测算：公式）」写法保留：\n- " + "\n- ".join(wrong_page))
            if unanchored:
                tips.append("以下句子带数字却没有锚点，从页面里找到这些数字的真实锚点补上；"
                            "页面里找不到出处的数字直接删掉，不写占位符：\n- " + "\n- ".join(unanchored))
            if other:
                tips.append("以下锚点问题也一并修正：\n- " + "\n- ".join(other))
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "\n\n".join(tips)})
            text = ask_mod.strip_glitch(llm.chat(messages, task="report_write"))
            text = _fix_tables_figs(text)
            text, fixes = _auto_fix_anchors(text)
            if fixes:
                _log(job, f"第 {i} 节模型改写后自动回挂锚点 {len(fixes)} 处")
            mech = _anchor_checks(text)
        placeholders = [m for m in ("数据缺失", "待补", "页码待补", "暂无数据", "——") if m in text]
        if placeholders:
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": (
                "本节出现占位符（" + "、".join(placeholders) + "）。成文前缺口已经补完，禁止留占位符；"
                "有数据的句子补锚点，没数据的句子直接删掉，重新输出本节完整内容。")})
            text = ask_mod.strip_glitch(llm.chat(messages, task="report_write"))
            text = _fix_tables_figs(text)
            text, fixes = _auto_fix_anchors(text)
            if fixes:
                _log(job, f"第 {i} 节去占位符后自动回挂锚点 {len(fixes)} 处")
            mech = _anchor_checks(text)
            placeholders = [m for m in ("数据缺失", "待补", "页码待补", "暂无数据", "——") if m in text]
        text = _fix_tables_figs(text)
        fig_refs = re.findall(r"(?<!附)图\s*\d+", wiki.ANCHOR_RE.sub("", text))
        if fig_refs:
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": (
                "正文出现图N占位或图题（" + "、".join(dict.fromkeys(fig_refs)) + "）。"
                "正文只写表格与文字，不写图题、图位说明；实际图表由系统在文末生成附图N。"
                "删掉这些图题与图位说明，重新输出本节完整内容。")})
            text = ask_mod.strip_glitch(llm.chat(messages, task="report_write"))
            text = _fix_tables_figs(text)
            text, fixes = _auto_fix_anchors(text)
            if fixes:
                _log(job, f"第 {i} 节去图位后自动回挂锚点 {len(fixes)} 处")
            fig_refs = re.findall(r"(?<!附)图\s*\d+", wiki.ANCHOR_RE.sub("", text))
            mech = _anchor_checks(text)
            placeholders = [m for m in ("数据缺失", "待补", "页码待补", "暂无数据", "——") if m in text]
        if mech or placeholders or fig_refs:
            problems = mech + ([f"仍有占位符：{'、'.join(placeholders)}"] if placeholders else [])
            if fig_refs:
                problems.append(f"仍有图N占位：{'、'.join(dict.fromkeys(fig_refs))}")
            for p in problems:
                _log(job, f"第 {i} 节成文拦截：{p}")
            raise RuntimeError(f"第 {i} 节《{heading}》锚点/占位符/图位未清干净，已停止成文："
                               + "；".join(problems[:3]))
        if not text.lstrip().startswith("## "):
            text = f"## {heading}\n\n{text.lstrip()}"
        done[heading] = text.strip()
        if bad:
            _log(job, f"第 {i} 节成文校验仍有 {len(bad)} 处锚点越界（已由回挂/改写处理）")
        _save_report(job, sections, done)
        save_job(job)

    _save_report(job, sections, done)
    # 各节独立起号导致拼接后表/图号重复，成文后全局重排一次（审校与出图都基于重排后的全文）
    path = config.ROOT / job["report_path"]
    path.write_text(_renumber_tables_figs(_fix_tables_figs(
        path.read_text(encoding="utf-8"))), encoding="utf-8")
    job["status"] = "charting"
    job["error"] = ""
    _log(job, f"成文完成，{len(sections)} 节")
    save_job(job)
    _start(job_id, _phase_charts, f"{kind_label(job)}图表《{job['title']}》")


_TABLE_FIG_RE = re.compile(r"(表|图)(\d+)")
_TABLE_FIG_HEAD_RE = re.compile(r"^\s*\*\*(表|图)(\d+)")
_TABLE_START_RE = re.compile(r"^\s*\|")
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|?\s*$")
_CAPTION_LINE_RE = re.compile(r"^\s*\*\*(表|图)\s*(\d*)[.、．:：]?\s*(.*?)\*\*\s*(.*)$")
_CAPTION_PLAIN_RE = re.compile(r"^\s*(表|图)\s*(\d+)\s*[.、．:：]?\s*(.+)$")
_IMG_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]+\)\s*$")
_MISSING_CELLS = ("—", "——", "-", "－", "--")


def _table_title(header_line, number):
    cells = [re.sub(r"\[\[[^\]]+\]\]", "", c).strip() for c in header_line.strip().strip("|").split("|")]
    cells = [c for c in cells if c and c not in ("—", "-")]
    title = "" if not cells else ("、".join(cells[:-1]) + "与" + cells[-1] if len(cells) > 1 else cells[0])
    title = title[:36] or "数据表"
    return f"**表{number} {title}**"


def _fix_tables_figs(md):
    """机械补齐表格题注，消掉「图标成图」的错位。

    markdown 表格上方必须是「**表N 标题**」；图题下方若是表格就改题注为表，
    图题下方既无表格也无图片的（正文图占位）直接删掉。表格没有题注时按表头生成。
    """
    lines = md.splitlines()
    out = []
    max_no = 0

    def is_table_start(idx):
        return (idx < len(lines) and _TABLE_START_RE.match(lines[idx])
                and idx + 1 < len(lines) and _TABLE_SEP_RE.match(lines[idx + 1]))

    def next_nonblank(idx):
        return next((x for x in lines[idx + 1:] if x.strip()), "")

    drop = set()
    i = 0
    while i < len(lines):
        if is_table_start(i):
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            for k in range(i + 2, j):
                cells = _table_cells(lines[k])
                if not any(c.strip() in _MISSING_CELLS for c in cells):
                    continue
                rest = [re.sub(r"(?:19|20)\d{2}\s*M\s*\d{1,2}", "", c)
                        for c in cells if c.strip() not in _MISSING_CELLS]
                if not rest or not any(re.search(r"\d", c) for c in rest):
                    drop.add(k)  # 整行只有月份标签/文字，没有实际数据，且含缺数格
            i = j
        else:
            i += 1

    # 数据行全被删掉的表连题注一起去掉，不留只有表头的空表
    empty = []
    i = 0
    while i < len(lines):
        if is_table_start(i):
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            if all(k in drop for k in range(i + 2, j)):
                empty.append((i, j))
            i = j
        else:
            i += 1
    for i, j in empty:
        cap = next((k for k in range(i - 1, -1, -1) if lines[k].strip()), None)
        if cap is not None:
            m = _CAPTION_LINE_RE.match(lines[cap]) or _CAPTION_PLAIN_RE.match(lines[cap])
            if m and m.group(1) == "表":
                drop.add(cap)
        drop.update(range(i, j))

    for i, ln in enumerate(lines):
        if i in drop:
            continue
        if is_table_start(i):
            cap_j, text = next(
                ((k, x) for k, x in reversed(list(enumerate(out))) if x.strip()), (None, ""))
            m = _CAPTION_LINE_RE.match(text)
            if not m and len(text) <= 100:
                m = _CAPTION_PLAIN_RE.match(text)
            if m:
                no = int(m.group(2) or 0) or max_no + 1
                title = m.group(3).strip()
                out[cap_j] = f"**表{no} {title}**" if title else _table_title(ln, no)
                max_no = max(max_no, no)
            else:
                max_no += 1
                if out and out[-1].strip():
                    out.append("")
                out.append(_table_title(ln, max_no))
                out.append("")
            if cap_j is not None and any(k in drop for k in range(i + 2, j)):
                labels = [c for k in range(i + 2, j) if k not in drop
                          for c in _table_cells(lines[k])
                          if re.fullmatch(r"(?:19|20)\d{2}M\d{1,2}", c)]
                if labels:
                    out[cap_j] = re.sub(
                        r"（(?:19|20)\d{2}M\d{1,2}[^）]*?(?:19|20)\d{2}M\d{1,2}）",
                        f"（{labels[0]}–{labels[-1]}）", out[cap_j], count=1)
            out.append(ln)
            continue
        m = _CAPTION_LINE_RE.match(ln)
        if m and m.group(1) == "图":
            nxt = next_nonblank(i)
            if not _IMG_LINE_RE.match(nxt) and not _TABLE_START_RE.match(nxt):
                continue  # 图题下既不是表也不是图，是正文图占位，删掉
        m = _CAPTION_PLAIN_RE.match(ln)
        if m and m.group(1) == "图" and len(ln) <= 100:
            nxt = next_nonblank(i)
            if not _IMG_LINE_RE.match(nxt) and not _TABLE_START_RE.match(nxt):
                continue  # 非加粗的短图题同样是图占位
        out.append(ln)
    return "\n".join(out)


def _renumber_tables_figs(md):
    """全文把 表N/图N 重排成全局连续编号，并同步节内引用。

    分节成文时各节独立起号，拼接后每节都有表1/图1。按出现顺序给标题行
    分配全局新号，再按节把引用换成同节标题的新号；文末附图是渲染产物，跳过。
    """
    lines = md.splitlines()
    counters = {"表": 0, "图": 0}
    mapping = {}
    captions = {}
    sec = 0
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            sec += 1
            continue
        m = _TABLE_FIG_HEAD_RE.match(ln)
        if not m:
            continue
        kind, old = m.group(1), m.group(2)
        counters[kind] += 1
        mapping.setdefault((sec, kind, old), counters[kind])
        captions[i] = (kind, counters[kind], old)

    sec = 0
    out = []
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            sec += 1
            out.append(ln)
            continue
        if ln.startswith("![") or ln.startswith("### 附图"):
            out.append(ln)
            continue
        if i in captions:
            kind, new, old = captions[i]
            out.append(ln.replace(f"{kind}{old}", f"{kind}{new}", 1))
            continue
        out.append(_TABLE_FIG_RE.sub(
            lambda m: f"{m.group(1)}{mapping[(sec, m.group(1), m.group(2))]}"
            if (sec, m.group(1), m.group(2)) in mapping else m.group(0), ln))
    return "\n".join(out)

def _renumber_tables_figs(md):
    """全文把 表N/图N 重排成全局连续编号。

    分节成文时各节独立起号，拼接后每节都有表1/图1。两遍处理：
    先逐节给加粗标题行（**表N …**）按出现顺序分配全局新号，再替换节内引用——
    引用与标题同节才能对上号，跨节或无标题的占位描述保留原号。
    文末图片引用行（![](charts/…)）的编号是渲染产物，跳过不动。
    """
    counters = {"表": 0, "图": 0}
    mapping = {}
    sec = 0
    for ln in md.splitlines():
        if ln.startswith("## "):
            sec += 1
        elif not ln.startswith("!["):
            for kind, num in _TABLE_FIG_HEAD_RE.findall(ln):
                counters[kind] += 1
                mapping[(sec, kind, num)] = counters[kind]

    sec = 0
    out = []

    def repl(m):
        new = mapping.get((sec, m.group(1), m.group(2)))
        return f"{m.group(1)}{new}" if new else m.group(0)

    for ln in md.splitlines():
        if ln.startswith("## "):
            sec += 1
        elif ln.startswith("!["):
            out.append(ln)
            continue
        else:
            ln = _TABLE_FIG_RE.sub(repl, ln)
        out.append(ln)
    return "\n".join(out)


def _save_report(job, sections, done):
    parts = [f"# {_display_title(job)}\n"]
    for sec in sections:
        text = done.get(sec["heading"])
        if text:
            parts.append(text.strip())
    if job.get("charts"):
        parts.append(charts.insert_into_report("", job["charts"]).strip())
    md = "\n\n".join(parts).rstrip() + "\n"
    if not job.get("report_path"):
        topic = _topic(job["title"])
        path = _unique_path(config.OUTPUTS_DIR,
                           f"report-个人-{topic}-{config.today_str().replace('-', '')}", ".md")
        job["report_path"] = path.relative_to(config.ROOT).as_posix()
    path = config.ROOT / job["report_path"]
    path.write_text(md, encoding="utf-8")
    return path


def _phase_charts(job_id, tid):
    job = load_job(job_id)
    path = config.ROOT / job["report_path"]
    report_md = path.read_text(encoding="utf-8")
    _tick(tid, 20, "让模型挑可画图的数据")
    maxc = int(config.CFG.get("report_pipeline", {}).get("max_charts", 8))
    got = []
    try:
        out = llm.chat([{"role": "user", "content": CHARTS_PROMPT
                         .replace("{MAXC}", str(maxc)).replace("{REPORT}", report_md[:16000])}],
                       task="audit", response_json=True)
        got = out.get("charts") or []
    except llm.LLMError as e:
        _log(job, f"图表 spec 生成失败（跳过图表）：{e}")
    specs = []
    text_nums = {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", report_md)}
    for spec in got:
        vals = [str(v).replace(",", "") for s in (spec.get("series") or []) for v in (s.get("values") or [])]
        if not vals or not any(v in text_nums for v in vals):
            continue
        specs.append(spec)
    _tick(tid, 50, "渲染图表")
    rendered = charts.render_all(specs, limit=maxc)
    job["charts"] = rendered
    if rendered:
        report_md = charts.insert_into_report(
            re.sub(r"\n## (?:图表|附图)\n.*$", "", report_md, flags=re.S).rstrip(), rendered)
        path.write_text(report_md, encoding="utf-8")
        _log(job, f"出图 {len(rendered)} 张")
    else:
        _log(job, "没有可画的合规数据，报告不含图表")
    job["status"] = "reviewing"
    job["error"] = ""
    save_job(job)
    _start(job_id, _phase_review, f"{kind_label(job)}审校《{job['title']}》")


# ---------- 审校 ----------

def _normalize_cn_numerals(text):
    return am.normalize_cn_numerals(text)


def _num_variants(n):
    return am.num_variants(n)


def _has_num(text, text_norm, n):
    return am.has_number(text, text_norm, n)


def _page_nums(text):
    return am.page_numbers(text)


def _num_on_page(value, page_nums):
    return am.num_on_page(value, page_nums)


def _unit_has_num(text, n):
    return am.unit_has_number(text, n)


def _other_unit_hint(cached, nums):
    """数字在当前页找不到时，回查同文件其他页；只对足够具体、出现页数很少的数字给提示，
    避免 6.1、10 这类到处都有的数字把真错锚淹掉。"""
    ordered = [n for n in set(nums)
               if not re.fullmatch(r"(?:19|20)\d{2}", n)
               and not re.fullmatch(r"0\d+", n)
               and len(n.replace(".", "")) >= 3]
    ordered.sort(key=lambda x: (len(x.replace(".", "")), x), reverse=True)
    for n in ordered:
        hits = [u for u in cached["units"] if _unit_has_num(u.get("text") or "", n)]
        if 1 <= len(hits) <= 3:
            kind = "段" if cached.get("unit") == "para" else "页"
            pages = "、".join(str(u["n"]) for u in hits)
            return f"；数字 {n} 实际出现在同文件第 {pages} {kind}，锚点疑似挂错"
    return ""


_SKIP_ANCHOR_PREFIX = ("#", "![", "|", ">")
_DATE_RE = re.compile(r"(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*日)?")


def _unanchored_nums(line):
    """段首无锚点、带实质数字的正文句子返回数字列表；表格、标题、图题、来源行不算。"""
    s = line.strip()
    if not s or s.startswith(_SKIP_ANCHOR_PREFIX):
        return []
    if re.match(r"^\*\*(?:表|图)\d+", s):
        return []
    if re.match(r"^(?:数据来源|资料来源|注[:：])", s) or re.fullmatch(r"\*\*[^*]+\*\*", s):
        return []
    if re.search(r"[（(]\s*测算\s*[:：]", s):
        return []
    clean = re.sub(r"\*\*|`", "", s)
    clean = _DATE_RE.sub("", clean)
    nums = [n for n, _u in am.number_terms(clean)]
    return nums if len(clean) >= 12 else []


def _table_cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table_checks(report_md):
    """表格数字核锚点：逐行找本行/题注/前段/表后来源行的锚点，覆盖不了的报出来。

    表头或单元格标了「测算」的列按派生值处理，不要求数字出现在原文。
    """
    paths = wiki_check.file_paths()
    caches = {}
    problems = []
    lines = report_md.splitlines()

    def unit_text(fname, loc):
        path = paths.get(fname)
        if not path:
            return f"__missing__{fname}"
        if fname not in caches:
            caches[fname] = load_cached(path)
        cached = caches[fname]
        if not cached:
            return ""
        unit = next((u for u in cached["units"] if u["n"] == wiki.loc_num(loc)), None)
        return (unit or {}).get("text") or ""

    def covered(texts, terms):
        got = set()
        for text in texts:
            for i, term in enumerate(terms):
                if i not in got and am.unit_has_term(text, term):
                    got.add(i)
        return got

    i = 0
    while i < len(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not (_TABLE_START_RE.match(lines[i]) and _TABLE_SEP_RE.match(nxt)):
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].strip().startswith("|"):
            j += 1
        header = _table_cells(lines[i])
        skip_cols = {k for k, h in enumerate(header) if "测算" in h}
        cap_idx = next((k for k in range(i - 1, -1, -1) if lines[k].strip()), None)
        caption = lines[cap_idx] if cap_idx is not None else ""
        prev_idx = next((k for k in range((cap_idx if cap_idx is not None else 0) - 1, -1, -1)
                         if lines[k].strip()), None)
        prev = lines[prev_idx] if prev_idx is not None else ""
        after = next((lines[k] for k in range(j, len(lines)) if lines[k].strip()), "")
        base = wiki.ANCHOR_RE.findall(caption) + wiki.ANCHOR_RE.findall(prev)
        if after.startswith(("数据来源", "资料来源")):
            base += wiki.ANCHOR_RE.findall(after)
        base_texts = [unit_text(f, loc) for f, loc in base]
        for row in lines[i + 2:j]:
            cells = _table_cells(row)
            aligned = len(cells) == len(header)
            row_anchors = wiki.ANCHOR_RE.findall(row)
            for k, cell in enumerate(cells):
                if (aligned and k in skip_cols) or "测算" in cell:
                    continue
                if cell.strip() in _MISSING_CELLS:
                    problems.append(f"表格单元格缺失数据（{cell.strip()}）：{row.strip()[:50]}")
                    continue
                terms = am.line_terms(cell)
                if not terms:
                    continue
                anchors = wiki.ANCHOR_RE.findall(cell) or row_anchors or base
                texts = [unit_text(f, loc) for f, loc in anchors]
                missing_file = [t[len("__missing__"):] for t in texts if str(t).startswith("__missing__")]
                if missing_file:
                    problems.append(f"表格锚点引用了 raw 中不存在的文件 {missing_file[0]}")
                    continue
                missing = [n for idx, (n, _u) in enumerate(terms) if idx not in covered(texts, terms)]
                if missing:
                    problems.append(
                        f"表格数字无锚点（{'、'.join(missing[:3])}）：{row.strip()[:50]}")
        i = j
    return problems


def _anchor_checks(report_md):
    """逐个数字核锚点：本句每个数字至少要有一个所挂锚点的原文能覆盖。

    只看有没有锚点不够，一句挂三页、每页各对一个数字会漏掉第三个数字无人支撑。
    """
    paths = wiki_check.file_paths()
    caches = {}
    problems = []
    for ln in report_md.splitlines():
        s = ln.strip()
        if s.startswith("|") or re.fullmatch(r"\*\*[^*]+\*\*", s):
            continue  # 表格行由题注/表源检查单独管，整行加粗的小标题不是数据句
        anchors = wiki.ANCHOR_RE.findall(ln)
        if not anchors:
            nums = _unanchored_nums(ln)
            if nums:
                problems.append(
                    f"无锚点数字（{'、'.join(nums[:3])}）：{ln.strip()[:40]}")
            continue
        # 派生值标了「测算」的，只核锚点真实性，不要求比值本身出现在原文
        derived = "测算" in ln
        terms = am.line_terms(wiki.ANCHOR_RE.sub("", ln))
        if derived or not terms:
            continue
        covered = set()
        first_cached = None
        for fname, loc in anchors:
            path = paths.get(fname)
            if not path:
                problems.append(f"锚点引用了 raw 中不存在的文件 {fname}#{loc}")
                continue
            if fname not in caches:
                caches[fname] = load_cached(path)
            cached = caches[fname]
            if not cached:
                problems.append(f"{fname} 没有提取缓存，锚点无法核验")
                continue
            first_cached = first_cached or cached
            unit = next((u for u in cached["units"] if u["n"] == wiki.loc_num(loc)), None)
            if unit is None:
                problems.append(f"锚点 {fname}#{loc} 超出单元范围")
                continue
            text = unit.get("text") or ""
            for i, t in enumerate(terms):
                if i not in covered and am.unit_has_term(text, t):
                    covered.add(i)
        missing = [n for i, (n, _u) in enumerate(terms) if i not in covered]
        if missing:
            hint = _other_unit_hint(first_cached, missing) if first_cached else ""
            problems.append(
                f"锚点疑似挂错：本句数字（{'、'.join(missing[:3])}）不在所挂锚点的原文里{hint}"
                f"：{ln.strip()[:60]}")
    problems.extend(_table_checks(report_md))
    return problems


def _delivery_problems(report_md):
    """交付前硬闸门：锚点、占位符、正文图位三类问题清零才允许转 Word。"""
    problems = list(_anchor_checks(report_md))
    for word in ("数据缺失", "待补", "页码待补", "暂无数据", "——"):
        if word in report_md:
            problems.append(f"仍有占位符「{word}」")
    refs = re.findall(r"(?<!附)图\s*\d+", wiki.ANCHOR_RE.sub("", report_md))
    if refs:
        problems.append("正文仍有图位：" + "、".join(dict.fromkeys(refs)))
    return problems


def _auto_fix_anchors(report_md):
    """成文后的机械回挂：同文件里能按数字找到正确页的，直接换锚点，不等模型改写。

    只在原句所有锚点都指向同一个文件、且回找能覆盖本句全部数字时动手，避免跨文件误改。
    """
    paths = wiki_check.file_paths()
    caches = {}
    out, fixes = [], []
    for ln in report_md.splitlines():
        s = ln.strip()
        if s.startswith("|") or re.fullmatch(r"\*\*[^*]+\*\*", s) or "测算" in ln:
            out.append(ln)
            continue
        anchors = wiki.ANCHOR_RE.findall(ln)
        terms = am.line_terms(wiki.ANCHOR_RE.sub("", ln))
        if not anchors or not terms:
            out.append(ln)
            continue
        fnames = {f for f, _loc in anchors}
        if len(fnames) != 1:
            out.append(ln)
            continue
        fname = next(iter(fnames))
        if fname not in caches:
            path = paths.get(fname)
            caches[fname] = load_cached(path) if path else None
        cached = caches[fname]
        if not cached:
            out.append(ln)
            continue
        covered = set()
        for _f, loc in anchors:
            unit = next((u for u in cached["units"] if u["n"] == wiki.loc_num(loc)), None)
            if unit is None:
                continue
            for i, t in enumerate(terms):
                if i not in covered and am.unit_has_term(unit.get("text") or "", t):
                    covered.add(i)
        missing = [t for i, t in enumerate(terms) if i not in covered]
        if not missing:
            out.append(ln)
            continue
        # 只补「恰好缺一个数字、且这个数字在同文件里只出现一页」的情况；多缺或常见数字交给模型
        if len(missing) != 1:
            out.append(ln)
            continue
        hits = [u["n"] for u in cached["units"]
                if am.unit_has_term(u.get("text") or "", missing[0])]
        if len(hits) != 1:
            out.append(ln)
            continue
        prefix = "para" if cached.get("unit") == "para" else "p"
        anchor = f"[[{fname}#{prefix}{hits[0]}]]"
        if anchor in ln:
            out.append(ln)
            continue
        out.append(ln.rstrip() + anchor)
        fixes.append((s[:50], hits))
    return "\n".join(out), fixes


_EVIDENCE_QUOTE_RE = re.compile(r"[「“\"]([^」”\"]{8,})[」”\"]")


def _anchor_source_lines(anchors, caches=None, per=500, total=7000, focus=None):
    """把锚点还原成原文片段，供审稿、修复、复核对照，避免只凭报告猜口径。

    focus 是问题里出现的数字：页首通常只是标题，命中数字时截取数字附近的窗口。
    """
    paths = wiki_check.file_paths()
    caches = caches if caches is not None else {}
    out, seen, used = [], set(), 0
    for fname, loc in anchors:
        key = (fname, loc)
        if key in seen:
            continue
        seen.add(key)
        path = paths.get(fname)
        if not path:
            line = f"[[{fname}#{loc}]]：raw 中找不到该文件"
        else:
            if fname not in caches:
                caches[fname] = load_cached(path)
            cached = caches[fname] or {}
            unit = next((u for u in cached.get("units") or [] if u["n"] == wiki.loc_num(loc)), None)
            text = re.sub(r"\s+", " ", (unit or {}).get("text") or "").strip()
            if not text:
                line = f"[[{fname}#{loc}]]：该页无文本"
            else:
                found = [t for t in (focus or []) if t and t in text]
                if found:
                    # 取页内出现次数最少的数字，长数字优先：3.4 比到处都有的 94 更能定位到问题句
                    tok = min(found, key=lambda t: (text.count(t), -len(t)))
                    start = max(0, text.find(tok) - per // 3)
                    text = ("…" if start else "") + text[start:start + per]
                    text += "…" if start + per < len(text) else ""
                else:
                    text = text[:per] + ("…" if len(text) > per else "")
                line = f"[[{fname}#{loc}]]：{text}"
        if used + len(line) > total:
            break
        out.append(line)
        used += len(line)
    return out


def _paragraph_at(lines, idx):
    a = b = idx
    while a > 0 and lines[a - 1].strip():
        a -= 1
    while b + 1 < len(lines) and lines[b + 1].strip():
        b += 1
    return "\n".join(lines[a:b + 1])


def _finding_evidence(finding, report_md, caches=None):
    """定位问题所指段落，附上该段锚点原文；定位不到返回空串。"""
    problem = str(finding.get("problem") or "")
    tokens = list(dict.fromkeys(
        t for t in re.findall(r"\d[\d,]*(?:\.\d+)?", problem)
        if len(t.replace(",", "").replace(".", "")) >= 2))
    non_year = [t for t in tokens if not re.fullmatch(r"(?:19|20)\d{2}", t)]
    tokens = non_year or tokens
    lines = report_md.splitlines()
    quotes = _EVIDENCE_QUOTE_RE.findall(problem)
    candidates = []
    for q in quotes:
        candidates.append(q)
        for part in re.split(r"…+|\.\.\.+", q):
            candidates.append(part)
            candidates.extend(p for p in re.split(r"[，。；：]", part) if len(p) >= 8)
    hit = None
    for q in candidates:
        hit = next((i for i, ln in enumerate(lines) if q and q in ln), None)
        if hit is not None:
            break
    if hit is None:
        # 审稿常改写原句再引用，逐字找不到时按问题里出现的数字定位段落
        scored = []
        for i, ln in enumerate(lines):
            if ln.strip().startswith(("数据来源", "资料来源", "注：", "### 附图")):
                continue
            score = sum(1 for t in tokens if t in ln)
            if score:
                scored.append((score, i))
        if scored:
            hit = max(scored)[1]
    if hit is None:
        where = str(finding.get("where") or "").strip()
        if len(where) >= 8:
            hit = next((i for i, ln in enumerate(lines) if where in ln), None)
    if hit is None:
        return ""
    block = _paragraph_at(lines, hit)
    anchors = wiki.ANCHOR_RE.findall(block)
    head = "报告原句：" + re.sub(r"\s+", " ", block.strip())[:420]
    if not anchors:
        return head + "\n（该段没有锚点）"
    return head + "\n锚点原文：\n" + "\n".join(
        _anchor_source_lines(anchors, caches, focus=tokens))


def _chunk_evidence(chunk, caches=None):
    anchors = wiki.ANCHOR_RE.findall(chunk)
    return "\n".join(_anchor_source_lines(anchors, caches))


def _report_chunks(md, size):
    chunks, cur, n = [], [], 0
    for ln in md.splitlines(keepends=True):
        if cur and n + len(ln) > size:
            chunks.append("".join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += len(ln)
    if cur:
        chunks.append("".join(cur))
    return chunks or [md]


def _verify_findings(job, report_md, findings, tid):
    """修复后复核：程序条目重跑锚点核验对照，模型条目让模型逐条判定，回填处置结果。"""
    mech_now = _anchor_checks(report_md)
    open_model = [f for f in findings
                  if f["kind"] == "模型" and f.get("outcome") not in ("已修复", "误报")]
    if open_model:
        caches = {}
        parts = []
        for i, f in enumerate(open_model, 1):
            text = f"{i}. [{f['level']}] {f['where']}：{f['problem']}"
            evidence = _finding_evidence(f, report_md, caches)
            if evidence:
                text += "\n   " + evidence.replace("\n", "\n   ")
            parts.append(text)
        listing = "\n".join(parts)
        try:
            _tick(tid, 80, f"复核 {len(open_model)} 条模型意见")
            out = llm.chat([{"role": "user", "content": VERIFY_PROMPT
                             .replace("{FINDINGS}", listing)
                             .replace("{REPORT}", report_md)}], task="review", response_json=True)
            verdicts = {int(v.get("idx") or 0): v for v in (out.get("verdicts") or []) if isinstance(v, dict)}
        except (llm.LLMError, ValueError) as e:
            _log(job, f"模型复核调用失败（相关条目按未解决留人工）：{e}")
            verdicts = {}
        for i, f in enumerate(open_model, 1):
            v = verdicts.get(i) or {}
            verdict = str(v.get("verdict") or "")
            if verdict == "已解决":
                f["outcome"], f["note"] = "已修复", str(v.get("note") or "")
            elif verdict == "误报":
                f["outcome"], f["note"] = "误报", str(v.get("note") or "")
            else:
                f["outcome"] = "未解决"
                f["note"] = str(v.get("note") or "") or "复核未给出结论，按未解决留人工"
    for f in findings:
        if f["kind"] != "程序" or f.get("outcome") in ("已修复", "误报"):
            continue
        if f["problem"] in mech_now:
            f["outcome"], f["note"] = "未解决", "修复后程序核验仍报此条"
        else:
            f["outcome"], f["note"] = "已修复", "修复后程序核验不再报此条"
    # 修复引入的新程序问题追加进清单，下一轮继续处理
    known = {f["problem"] for f in findings if f["kind"] == "程序"}
    for p in mech_now:
        if p not in known:
            findings.append({"kind": "程序", "level": "硬伤", "where": "锚点核验",
                             "problem": p, "outcome": "未解决", "note": "修复轮新引入"})
    return mech_now


def _order_findings(job, report_md, findings, tid):
    """处置完成后按报告位置整理条目顺序，记录按章节读起来顺；调用失败保持原序。"""
    if len(findings) < 2:
        return findings
    listing = "\n".join(f"{i}. [{f['kind']}·{f['level']}] {f['where']}：{f['problem']}"
                        for i, f in enumerate(findings))
    try:
        _tick(tid, 90, f"按报告位置整理 {len(findings)} 条条目顺序")
        out = llm.chat([{"role": "user", "content": ORDER_PROMPT
                         .replace("{FINDINGS}", listing)
                         .replace("{REPORT}", report_md)}], task="review", response_json=True)
        order = [int(x) for x in (out.get("order") or []) if str(x).strip().lstrip("-").isdigit()]
    except (llm.LLMError, ValueError, TypeError) as e:
        _log(job, f"审校条目排序调用失败（保持原序）：{e}")
        return findings
    if sorted(order) != list(range(len(findings))):
        _log(job, "审校条目排序返回不完整（保持原序）")
        return findings
    return [findings[i] for i in order]


# 审校答复腔特征词：正常研报正文不会这么写，new 里出现即说明模型把修订说明当成了替换文本
_REPAIR_META_WORDS = ("修正如下", "已核对", "不作改动", "锚点原文即为", "（原挂", "误报")


def _repair_rejected(old, new):
    """替换对闸门：拦下会把审校说明写进正文的坏替换。返回拒因，空串=放行。"""
    if any(w in new for w in _REPAIR_META_WORDS):
        return "new 是审校答复说明"
    if len(new) > 2 * len(old) + 80:
        return f"new 比 old 膨胀过多（{len(old)}→{len(new)} 字）"
    return ""


def _phase_review(job_id, tid):
    job = load_job(job_id)
    path = config.ROOT / job["report_path"]
    report_md = _fix_tables_figs(path.read_text(encoding="utf-8"))
    path.write_text(report_md.rstrip() + "\n", encoding="utf-8")
    _tick(tid, 10, "程序核对锚点")
    mech = _anchor_checks(report_md)
    _tick(tid, 30, "模型审稿")
    headings = "\n".join(ln for ln in report_md.splitlines() if ln.startswith("## ")) or "（无章节标题）"
    issues, seen = [], set()
    chunks = _report_chunks(report_md, 16000)
    for i, chunk in enumerate(chunks, 1):
        tasks.update(tid, current=20 + i, detail=f"模型审稿 第 {i} 段")
        if len(chunks) == 1:
            scope = "报告全文，逐条审。"
        else:
            scope = (f"下面是报告的第 {i}/{len(chunks)} 段，只是片段。只评这一段里的文字；"
                     "这一段没有的章节不算缺失，缺章节以「全文章节」为准。")
        try:
            out = llm.chat([{"role": "user", "content": REVIEW_PROMPT
                             .replace("{SCOPE}", scope)
                             .replace("{REPORT}", chunk)
                             .replace("{HEADINGS}", headings)
                             .replace("{MECH}", "\n".join(mech) or "无")
                             .replace("{EVIDENCE}", _chunk_evidence(chunk) or "无")}],
                           task="review", response_json=True)
        except llm.LLMError as e:
            _log(job, f"模型审稿第 {i} 段失败（保留程序核查结果）：{e}")
            continue
        for it in out.get("issues") or []:
            if not isinstance(it, dict):
                continue
            key = (str(it.get("where")), str(it.get("problem")))
            if key not in seen:
                seen.add(key)
                issues.append(it)
    issues = issues[:12]

    # 发现即处置：每条问题走 修复 → 复核 → 已修复/误报/未解决，审校记录只呈现处置结果
    findings = [{"kind": "程序", "level": "硬伤" if ("找不到本句数字" not in p or "锚点疑似挂错" in p) else "建议",
                 "where": "锚点核验", "problem": p} for p in mech]
    findings += [{"kind": "模型", "level": str(i.get("level") or "建议"),
                  "where": str(i.get("where") or ""), "problem": str(i.get("problem") or "")} for i in issues]
    repaired_rounds = 0
    for round_no in (1, 2):
        pending = [f for f in findings if f.get("outcome") not in ("已修复", "误报")]
        if round_no == 2:  # 第二轮只追硬伤与程序问题，建议级修不动就留人工
            pending = [f for f in pending if f["level"] == "硬伤" or f["kind"] == "程序"]
        if not pending:
            break
        _tick(tid, 40 + round_no * 10, f"按 {len(pending)} 条问题修第 {round_no} 轮")
        caches = {}
        parts = []
        for f in pending:
            text = f"- [{f['level']}] {f['where']}：{f['problem']}"
            evidence = _finding_evidence(f, report_md, caches)
            if evidence:
                text += "\n  " + evidence.replace("\n", "\n  ")
            parts.append(text)
        try:
            out = llm.chat([{"role": "user", "content": REPAIR_PROMPT
                             .replace("{ISSUES}", "\n".join(parts))
                             .replace("{REPORT}", report_md)}], task="review", response_json=True)
        except llm.LLMError as e:
            _log(job, f"第 {round_no} 轮修复调用失败：{e}")
            break
        fixed, changes, rejected = report_md, 0, 0
        for fx in out.get("fixes") or []:
            old, new = str(fx.get("old") or "").strip(), str(fx.get("new") or "").strip()
            why = _repair_rejected(old, new) if old and new and new != old else ""
            if why:
                rejected += 1
                _log(job, f"第 {round_no} 轮拒绝一处替换：{why}")
                continue
            if old and old in fixed and new and new != old:
                fixed = fixed.replace(old, new, 1)
                changes += 1
        if changes and len(fixed) >= len(report_md) * 0.9:
            fixed = _fix_tables_figs(ask_mod.strip_glitch(fixed))
            fixed, auto = _auto_fix_anchors(fixed)
            if auto:
                _log(job, f"第 {round_no} 轮修完自动回挂锚点 {len(auto)} 处")
            path.write_text(fixed.rstrip() + "\n", encoding="utf-8")
            report_md = fixed
            repaired_rounds += 1
            _log(job, f"第 {round_no} 轮按 {len(pending)} 条问题替换 {changes} 处"
                 + (f"（拒 {rejected} 处坏替换）" if rejected else ""))
        elif changes:
            _log(job, f"第 {round_no} 轮替换幅度过大被拒（{changes} 处），保留原文复核")
        else:
            _log(job, f"第 {round_no} 轮模型未给出可用替换，直接复核")
        mech = _verify_findings(job, report_md, findings, tid)

    report_md = _fix_tables_figs(report_md)
    report_md, auto = _auto_fix_anchors(report_md)
    if auto:
        _log(job, f"审校收尾自动回挂锚点 {len(auto)} 处")
    path.write_text(report_md.rstrip() + "\n", encoding="utf-8")
    mech = _anchor_checks(report_md)
    blocking = _delivery_problems(report_md)
    for f in findings:
        if f.get("level") == "硬伤" and f.get("outcome") not in ("已修复", "误报"):
            blocking.append(f"硬伤未解决：{f['where']}：{f['problem']}")
    known = {f["problem"] for f in findings}
    for p in blocking:
        if p not in known:
            findings.append({"kind": "程序", "level": "硬伤", "where": "交付闸门",
                             "problem": p, "outcome": "未解决", "note": "转 Word 前未清零"})
    findings = _order_findings(job, report_md, findings, tid)
    fixed_n = [f for f in findings if f.get("outcome") == "已修复"]
    fp_n = [f for f in findings if f.get("outcome") == "误报"]
    left_n = [f for f in findings if f.get("outcome") not in ("已修复", "误报")]
    n_prog = sum(1 for f in findings if f["kind"] == "程序")
    summary = (f"审校闭环：发现 {len(findings)} 条（程序 {n_prog}、模型 {len(findings) - n_prog}），"
               f"修复 {len(fixed_n)} 条，误报排除 {len(fp_n)} 条，遗留 {len(left_n)} 条"
               + (f"（遗留已逐条注明原因，待人工定夺）" if left_n else ""))
    note = _sample_top3_note(job)
    job["review"] = {"summary": summary, "findings": findings,
                     "mechanical": mech, "sample_note": note}
    if note:
        _log(job, "审校提示：" + note)
    _log(job, summary)
    if blocking:
        msg = "交付闸门拦截，未转 Word：" + "；".join(blocking[:3])
        job["status"] = "error"
        job["error"] = msg
        _log(job, msg)
        save_job(job)
        raise RuntimeError(msg)
    job["status"] = "docxing"
    job["error"] = ""
    save_job(job)
    _start(job_id, _phase_docx, f"{kind_label(job)}转 Word《{job['title']}》")


# ---------- Word 与候选 ----------

def _phase_docx(job_id, tid):
    job = load_job(job_id)
    job["error"] = ""
    if not shutil.which("pandoc"):
        _log(job, "没找到 pandoc，跳过 Word，md 照常交付")
        job["status"] = "candidates_pending"
        save_job(job)
        _start(job_id, _phase_af, f"{kind_label(job)}沉淀候选《{job['title']}》")
        return
    _tick(tid, 30, "pandoc 转 Word 并美化")
    from . import convert_report
    src = config.ROOT / job["report_path"]
    out = src.with_suffix(".docx")
    if out.exists():
        out = _unique_path(out.parent, out.stem, ".docx")
    try:
        convert_report.convert(src, out)
        job["docx_path"] = out.relative_to(config.ROOT).as_posix()
        _log(job, f"Word 已出：{out.name}")
    except Exception as e:
        _log(job, f"转 Word 失败（md 照常交付）：{e}")
    job["status"] = "candidates_pending"
    save_job(job)
    _start(job_id, _phase_af, f"{kind_label(job)}沉淀候选《{job['title']}》")


def _phase_af(job_id, tid):
    from . import report_distill
    job = load_job(job_id)
    _tick(tid, 30, "逐句分类 A-F")
    try:
        candidates = report_distill.build_candidates(job)
    except Exception as e:
        _log(job, f"候选生成失败（报告照常交付）：{e}")
        candidates = []
    if candidates:
        path = report_distill.save_candidates(job_id, candidates)
        job["candidates_path"] = path.relative_to(config.ROOT).as_posix()
        job["candidates_count"] = len(candidates)
        _log(job, f"沉淀候选 {len(candidates)} 条，待勾选")
        job["status"] = "candidates_pending"
    else:
        job["candidates_count"] = 0
        job["status"] = "done"
        _log(job, "没有可沉淀的 A/D/E 候选，流程完成")
    save_job(job)


# ---------- 续跑 ----------

def continue_job(job_id):
    job = load_job(job_id)
    if not job:
        raise KeyError("报告任务不存在")
    status = job.get("status")
    if status in ("outline_pending", "gaps_pending", "candidates_pending", "done"):
        return job
    if status == "error":
        phase = job.get("phase")
        mapping = {
            "_phase_outline": (_phase_outline, "outlining", "大纲"),
            "_phase_write": (_phase_write, "writing", "成文"),
            "_phase_charts": (_phase_charts, "charting", "图表"),
            "_phase_review": (_phase_review, "reviewing", "审校"),
            "_phase_docx": (_phase_docx, "docxing", "转 Word"),
            "_phase_af": (_phase_af, "candidates_pending", "沉淀候选"),
        }
        fn, new_status, label = mapping.get(phase, (_phase_outline, "outlining", "大纲"))
        job["status"], job["error"] = new_status, ""
        _log(job, f"按 /continue 重试 {phase}")
        save_job(job)
        _start(job_id, fn, f"{kind_label(job)}{label}《{job['title']}》")
        return load_job(job_id)
    if status in ("writing", "outlining", "fetching", "charting", "reviewing", "docxing"):
        # 任务可能随服务重启丢了，按当前状态从下一阶段接起
        if status == "writing":
            _start(job_id, _phase_write, f"{kind_label(job)}成文《{job['title']}》")
        elif status == "charting":
            _start(job_id, _phase_charts, f"{kind_label(job)}图表《{job['title']}》")
        elif status == "reviewing":
            _start(job_id, _phase_review, f"{kind_label(job)}审校《{job['title']}》")
        elif status == "docxing":
            _start(job_id, _phase_docx, f"{kind_label(job)}转 Word《{job['title']}》")
        elif status == "fetching":
            _begin_write(job)
        else:
            _start(job_id, _phase_outline, f"{kind_label(job)}大纲《{job['title']}》")
    return load_job(job_id)
