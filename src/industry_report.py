"""研报管线（行业研报与公司研报共用）：样板 → 大纲（网页确认）→ 缺口取数（网页勾选）→ 分节成文 → 图表 → 审校 → Word → A-F 候选。

状态存在 data/report_jobs/<id>.json，每个阶段起一个 report 或 fetch 后台任务，状态机可跨服务重启续跑。
大纲确认（outline_pending）、缺口勾选（gaps_pending）、候选勾选（candidates_pending）是三个网页关卡。
"""
import json
import re
import shutil
import threading
import uuid

from . import ask as ask_mod
from . import charts, config, fetcher, ingest as ingest_mod, llm, tasks, wiki
from .classify import sanitize
from .pdf_extract import load_cached

JOB_LOCK = threading.RLock()

SKILL_DIR = config.ROOT / ".claude" / "skills"
STRUCTURE_PATH = SKILL_DIR / "earnings-analysis" / "references" / "report-structure.md"
SECTOR_PATH = SKILL_DIR / "sector-overview" / "SKILL.md"
COMPANY_SKILL_PATH = SKILL_DIR / "company-report" / "SKILL.md"
ANNUAL_RE = re.compile(r"(年度报告|年報|annual\s*report)", re.I)
DOC_GAP_RE = re.compile(r"未摄入材料|年度报告|年报|年報|公告|研报|招股书|招股說明書|募集说明|审计报告|全文|PDF", re.I)

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


def _annual_gap(job):
    """公司研报必选缺口：最近一期年报。库里有就不列。"""
    if job.get("kind") != "company" or _annual_in_library(job):
        return None
    subject = _subject(job)
    code = _stock_code(job)
    market = "hk" if code.endswith(".HK") else ("a" if code else "")
    return {
        "id": "annual",
        "item": f"{subject}最近一期年度报告（财报）",
        "why": "公司研报的财务分析与盈利预测必须以年报为锚，缺年报不能成文",
        "fetch": {"source": "annual_report",
                  "params": {"company": subject, "code": code, "market": market}},
        "status": "pending",
        "required": True,
    }


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
    """程序生成的缺口：未摄入材料提醒 + 公司年报必选。"""
    gaps = _pending_gap_items()
    annual = _annual_gap(job)
    if annual:
        gaps.insert(0, annual)
    return gaps


def _drop_annual_dupes(gaps, job):
    """模型自己也列了年报时，去掉重复项，保留程序生成的那条必选缺口。

    年报已在库的，其余指向年报的缺口改成不能自动补：重下同一份文件会被 SHA256 去重，
    库里的条目不会增加，标成可取数只会给假的「已补」。
    """
    subject = _subject(job)
    have = _annual_in_library(job)
    out = []
    for g in gaps:
        if g.get("required"):
            out.append(g)
            continue
        text = str(g.get("item") or "")
        if ("年报" in text or "年度报告" in text) and subject in text:
            continue
        fetch = g.get("fetch") or {}
        if have and fetch.get("source") == "annual_report":
            g["fetch"] = None
            note = f"库内已有年报 {have.get('file') or ''}，重下同一份不会新增条目"
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


def _material_industry(job):
    """行业研报取料人：行业页与概念页优先，公司页只留少量做竞争格局对照。"""
    sel, _have = _material_base(job)
    order = {"industry": 0, "concept": 1, "company": 2}
    sel.sort(key=lambda p: order.get(p["type"], 3))
    sel = sel[:10]
    text = "\n\n".join(
        f"### {p['meta'].get('title', p['slug'])}（slug: {p['slug']}，类型 {p['type']}）\n{p['body']}"
        for p in sel
    )
    return sel, text, ask_mod._allowed_from_pages(sel)


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
    sel = sel[:10]
    text = "\n\n".join(
        f"### {p['meta'].get('title', p['slug'])}（slug: {p['slug']}，类型 {p['type']}）\n{p['body']}"
        for p in sel
    )
    return sel, text, ask_mod._allowed_from_pages(sel)


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
--- 样板研报骨架（开头部分） ---
{SAMPLES}

知识库可用页面：
{PAGES}

要求：
1. 章节顺序固定按行业分析的展开逻辑，最多 {MAXS} 节：产业链（上游、中游、下游与价值分布）→ 生产端（产能、产量、成本与供给格局）→ 需求端（需求结构、下游景气、渗透率）→ 市场规模（量价与规模测算）→ 竞争格局（份额、集中度、玩家分层）→ 增长驱动（政策、技术、渗透率、出口等）→ 盈利与估值 → 投资要点 → 风险提示。产业链必须放在第一节，投资要点必须放在行业分析之后，不得用投资要点开篇；缺料的章节可合并，但产业链、生产端、需求端、市场规模、竞争格局、增长驱动六节必须保留
2. 每节写清：本节论点、需要的数据与表、图表位、取料页面（写 slug）
3. 只用知识库页面里有的材料定结构；库里没有的数据不要在这里编，缺口由下一道程序另列
4. 输出 markdown：第一行 "# 大纲：{TITLE}"，其后每节 "## 一、标题" 形式，节内用短横线列论点/数据/图表/取料页
5. 不写正文，不写套话"""

GAPS_PROMPT = """你在给研报大纲列数据缺口。报告主题：{TITLE}

大纲：
{OUTLINE}

知识库已有页面：{PAGES}

可用的免登录取数源（source / params），source 必须原样使用下面的英文 id：
- stats_series：国家统计局，params {{"keyword": "中文指标名", "freq": "auto|月|季|年", "periods": 期数}}
- akshare_series：AKShare 宏观序列，params {{"series": "以下之一：{SERIES}", "periods": 期数}}
- eastmoney_reports：东财研报下载，params {{"industry": "行业", "broker": "券商可空", "limit": 份数}}
- cninfo_announce：巨潮公告，params {{"query": "检索词", "limit": 份数}}
- annual_report：公司年报，params {{"company": "公司名", "market": "a|hk"}}
- sample_chain：行业样板研报下载链，params {{"industry": "行业", "limit": 份数}}
- web_search：网络检索兜底，params {{"query": "检索式", "limit": 份数}}
先配专用源；专用源明确取不到的再用 web_search，补不了源的就用 "none"。

只列大纲需要、但知识库页面里明显没有的关键数据，最多 8 条；每条必须能用上面的源取到，或写 none。
只输出 JSON：{{"gaps": [{{"item": "缺什么数据", "why": "哪节要用", "source": "stats_series|akshare_series|eastmoney_reports|cninfo_announce|annual_report|sample_chain|web_search|none", "params": {{}}}}]}}"""


OUTLINE_PROMPT_COMPANY = """你是中文卖方公司研报的主笔，现在只定大纲，不写正文。

公司研究对象：{TITLE}
用户原始要求：{Q}

--- 公司研报权威框架（技能 company-report） ---
{FRAMEWORK}

--- 库内研报对该公司或其行业的覆盖（开头部分） ---
{SAMPLES}

知识库可用页面：
{PAGES}

要求：
1. 章节按上面的权威框架走，最多 {MAXS} 节。八节依次是投资摘要与评级、公司概况与历史沿革、行业空间与竞争格局、核心竞争力与护城河、经营与财务分析、盈利预测与关键假设、估值与投资建议、风险提示；缺料的节可以合并，但财务分析与估值两节必须保留
2. 财务分析节必须以公司年报为锚，写明营收拆分、毛利率、费用率、ROE、现金流与资产负债的关键科目；年报还没入库时，大纲里照常列出这一节并标注「待补年报」
3. 每节写清：本节论点、需要的数据与表、图表位、取料页面（写 slug）
4. 只用知识库页面里有的材料定结构；库里没有的数据不要在这里编，缺口由下一道程序另列
5. 输出 markdown：第一行 "# 大纲：{TITLE}"，其后每节 "## 一、标题" 形式，节内用短横线列论点/数据/图表/取料页
6. 不写正文，不写套话"""

SOURCE_ALIASES = {
    "stats": "stats_series", "nbs": "stats_series", "统计局": "stats_series",
    "akshare": "akshare_series", "ak": "akshare_series",
    "eastmoney": "eastmoney_reports", "东财": "eastmoney_reports",
    "cninfo": "cninfo_announce", "巨潮": "cninfo_announce",
    "annual": "annual_report", "年报": "annual_report", "annual_report": "annual_report",
    "web": "web_search", "web_search": "web_search", "网络检索": "web_search",
    "sample": "sample_chain", "sample_chain": "sample_chain",
    "hibor": "hibor_ranking", "hibor_ranking": "hibor_ranking",
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
                  .replace("{SAMPLES}", base["SAMPLES"]).replace("{PAGES}", base["PAGES"])
                  .replace("{MAXS}", base["MAXS"]))
    else:
        prompt = (OUTLINE_PROMPT
                  .replace("{TITLE}", base["TITLE"]).replace("{Q}", base["Q"])
                  .replace("{STRUCTURE}", _read_skill(STRUCTURE_PATH))
                  .replace("{SECTOR}", _read_skill(SECTOR_PATH))
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
        gaps += _drop_annual_dupes(
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
        gaps.append({
            "id": f"g{len(gaps) + 1}", "item": item, "why": str(g.get("why") or "").strip(),
            "fetch": {"source": source, "params": params} if source in valid else None,
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
                gaps += _drop_annual_dupes(_extract_gaps(
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
        raise ValueError("公司研报必须先补公司年报，不能跳过：" + "、".join(missing))
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
        # 本次勾选的数据缺口随取数带进摄入：财报摄入据此补抄 skill 字典外的目标数据
        targets = _gap_targets(job)
        for i, g in enumerate(fetchable, 1):
            tasks.update(tid, current=i - 1, detail=f"补缺口 {i}/{len(fetchable)}：{g['item']}")
            try:
                r = fetcher.run_fetch(g["fetch"]["source"], g["fetch"].get("params") or {}, tid,
                                      targets=targets)
                failed_files = r.get("ingest_failed") or []
                if failed_files:
                    g["status"] = "failed"
                    g["ingest_failed"] = True
                    notes.append(f"{g['item']}：下载完成但入库失败（{'、'.join(failed_files)}），"
                                 "材料没进库就没有锚点")
                else:
                    g["status"] = "done"
                    g.pop("ingest_failed", None)
                    notes.append(f"{g['item']}：{r.get('summary', '完成')}")
            except Exception as e:
                fb = None
                source = (g.get("fetch") or {}).get("source")
                if not g.get("required") and source and source != "web_search":
                    tasks.update(tid, detail=f"专用源失败，改网络检索：{g['item']}")
                    try:
                        fb = fetcher.run_fetch("web_search",
                                               {"query": g["item"], "limit": 3}, tid,
                                               targets=targets)
                    except Exception as e2:
                        e = f"{e}；网络检索也失败：{e2}"
                if fb and not fb.get("ingest_failed"):
                    g["status"] = "done"
                    notes.append(f"{g['item']}：专用源失败（{e}），改网络检索，{fb.get('summary', '')}")
                else:
                    g["status"] = "failed"
                    extra = ("，网络检索入库失败：" + "、".join(fb.get("ingest_failed") or [])) if fb else ""
                    notes.append(f"{g['item']}：失败（{e}）{extra}")
                    if fb:
                        g["ingest_failed"] = True
            j = load_job(job_id)
            if j:
                for old in j.get("gaps") or []:
                    if old["id"] == g["id"]:
                        old["status"] = g["status"]
                        if g.get("ingest_failed"):
                            old["ingest_failed"] = True
                        else:
                            old.pop("ingest_failed", None)
                _log(j, notes[-1])
                save_job(j)
        j = load_job(job_id)
        bad = [g["item"] for g in (j.get("gaps") or []) if g.get("required") and g["status"] != "done"]
        if bad:
            tasks.update(tid, current=len(fetchable), detail="年报未取到，停回缺口关卡")
            j["status"] = "gaps_pending"
            j["error"] = "公司年报未取到，不能成文：" + "、".join(bad)
            _log(j, j["error"])
            save_job(j)
            return {"summary": "；".join(notes + [j["error"]])}
        no_anchor = [g["item"] for g in (j.get("gaps") or []) if g.get("ingest_failed")]
        if no_anchor:
            tasks.update(tid, current=len(fetchable), detail="材料未入库，停回缺口关卡")
            j["status"] = "gaps_pending"
            j["error"] = ("补到的材料没入库，不能成文（材料没进库就取不到锚点）："
                          + "、".join(no_anchor) + "。可重试取数，或到首页上传材料后重试")
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
    job["status"] = "writing"
    job["error"] = ""
    save_job(job)
    _start(job["id"], _phase_write, f"{kind_label(job)}成文《{job['title']}》")


# ---------- 成文 ----------

WRITE_PROMPT = """你是中文卖方研报的主笔，正在写《{TITLE}》的第 {N}/{TOTAL} 节。

全篇大纲：
{OUTLINE}

本节大纲（只写这一节）：
{SECTION}

可用知识库页面：
{PAGES}

写作规则：
1. 只用上面的页面内容，禁止编造。页面没有的信息明说「数据缺失」或直接不写
2. 每个陈述句句尾带锚点 [[文件名.pdf#p页码]] 或 [[文件名.docx#para段落号]]，锚点只能原样取自页面内容里出现过的
3. 含数据的句子必须有锚点；无锚点的句子只能是判断、推断或行业常识，不得承载数据
4. 派生值（ROE、毛利率、增速、占比、差额、人均等由原文数字算出的值）写成「数值（测算：公式）」，如「17.76%（测算：归母净利/期末权益）」；公式用到的原始数字要在同句或相邻句写出，锚点挂在原始数字所在页
5. 锚点必须挂在数字实际出现的页。一段里多个数字分属不同页时分别挂，禁止把整段数字统一挂到同一页码
6. 跨期、跨口径的数字不直接并列或相减，除非写明期间与口径
7. 估值对比写清 PE/PB/PS 口径与日期；竞争格局写集中度、市占率、壁垒；不要空泛排比
8. 中文写作：少引号少冒号，删套话，一句一事，长句拆短
9. 输出本节 markdown：第一行 "## 本节标题"，正文分段，可用 markdown 表格
10. 不写其他节的内容，不写封面、目录、注释表
{EXTRA}"""

CHARTS_PROMPT = """下面是刚写完的研报全文。挑出能画图的数据，给图表 spec。

规则：
- 每个图至少 3 个时间点或 3 个类别；数值必须能在报告正文中找到，禁止新造或外推
- kind 用 line（趋势）、bar（分类对比）、hbar（横向排名）、pie（结构占比）
- 最多 {MAXC} 张；没有合适数据就给空数组
- note 写数据来源文件名与口径，可取正文锚点里的文件名
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

检查：数字与锚点是否对得上、口径是否写明、结论有没有超出材料、有无空话套话。只有章节在「全文章节」里确实不存在才算缺章节。
只输出 JSON：{{"issues": [{{"level": "硬伤|建议", "where": "节名或原句", "problem": "一句话"}}]}}，最多 10 条。"""

REPAIR_PROMPT = """按下面列出的硬伤，给最小改动的替换对，不改写全文。

规则：
- old 必须从报告里逐字复制，能唯一定位到一处；new 是改好的同一片段
- 不改结构、不删节、不新造数字；涉及锚点的，只能换成报告里已出现过的真实锚点
- 数据缺失或口径对不上的表述，改法是删掉残缺部分或改成泛称，不是编造数值
- 没有问题就给空数组

只输出 JSON：{{"fixes": [{{"old": "...", "new": "..."}}]}}

硬伤：
{ISSUES}

报告全文：
{REPORT}"""

VERIFY_PROMPT = """你是审稿复核员。上一轮审校发现的问题列在下面，报告随后已做修改。
逐条判定每个问题在修改后报告里的现状，只看现状：
- 已解决：错误表述已被修正、替换或整句删除
- 误报：原判罚不成立，修改前的报告本无此错（审稿看错口径、锚点其实支持该表述等）
- 未解决：同样的错误仍在

只输出 JSON：{{"verdicts": [{{"idx": 1, "verdict": "已解决", "note": "一句话依据"}}]}}

问题清单（idx 对应）：
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
            extra = ("11. 公司研报纪律：财务数字只取自已摄入的年报、半年报、季报；券商研报里的公司数字只能做对照，"
                     "不能替代公司自己的披露。币种与单位写清，预测数字标「预计」并交代假设")
        prompt = (WRITE_PROMPT
                  .replace("{TITLE}", job["title"])
                  .replace("{N}", str(i)).replace("{TOTAL}", str(len(sections)))
                  .replace("{OUTLINE}", (job.get("outline") or "")[:6000])
                  .replace("{SECTION}", f"## {heading}\n{sec['body']}")
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
        wrong_page = [p for p in _anchor_checks(text) if "锚点疑似挂错" in p]
        if wrong_page:
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": (
                "以下数字挂错了页码，请只改这些锚点，把每个数字挂到它实际出现的页；"
                "派生值按「数值（测算：公式）」写法保留：\n- " + "\n- ".join(wrong_page))})
            text = ask_mod.strip_glitch(llm.chat(messages, task="report_write"))
        if not text.lstrip().startswith("## "):
            text = f"## {heading}\n\n{text.lstrip()}"
        done[heading] = text.strip()
        if bad:
            _log(job, f"第 {i} 节锚点仍有问题 {len(bad)} 处")
        _save_report(job, sections, done)
        save_job(job)

    _save_report(job, sections, done)
    job["status"] = "charting"
    _log(job, f"成文完成，{len(sections)} 节")
    save_job(job)
    _start(job_id, _phase_charts, f"{kind_label(job)}图表《{job['title']}》")


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
            re.sub(r"\n## 图表\n.*$", "", report_md, flags=re.S).rstrip(), rendered)
        path.write_text(report_md, encoding="utf-8")
        _log(job, f"出图 {len(rendered)} 张")
    else:
        _log(job, "没有可画的合规数据，报告不含图表")
    job["status"] = "reviewing"
    save_job(job)
    _start(job_id, _phase_review, f"{kind_label(job)}审校《{job['title']}》")


# ---------- 审校 ----------

_NUM_SCALES = (1, 1e3, 1e4, 1e5, 1e8, 1e-3, 1e-4, 1e-5, 1e-8)
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG = {"万": 10000, "亿": 100000000}
_CN_RUN_RE = re.compile(r"[零〇一二三四五六七八九十百千万亿两]+")


def _cn_run_to_arabic(run):
    """把一段中文数字转成阿拉伯数字串：二零二五→2025，二十三→23，一万二千→12000。"""
    if not run:
        return run
    if not any(ch in _CN_UNITS or ch in _CN_BIG for ch in run):
        return "".join(str(_CN_DIGITS[ch]) for ch in run)
    total = section = num = 0
    for ch in run:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            section += (num or 1) * _CN_UNITS[ch]
            num = 0
        elif ch in _CN_BIG:
            section = (section + num) * _CN_BIG[ch]
            total += section
            section = num = 0
    return str(total + section + num)


def _normalize_cn_numerals(text):
    """年报常用「二零二五年四月十日」，比对前归一成阿拉伯数字；单字只在年月日前后才转。"""
    def repl(m):
        run = m.group(0)
        if len(run) == 1 and m.string[m.end():m.end() + 1] not in "年月日季度":
            return run
        return _cn_run_to_arabic(run)
    return _CN_RUN_RE.sub(repl, text)


def _num_variants(n):
    """0175 与 00175 这类前导零差异，按去掉前导零的写法再试一次。"""
    out = [n]
    stripped = n.lstrip("0")
    if stripped and stripped != n:
        out.append(stripped)
    return out


def _has_num(text, text_norm, n):
    return any(v in text or v in text_norm for v in _num_variants(n))


def _page_nums(text):
    out = []
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def _num_on_page(value, page_nums):
    """报告按亿元写、原文按千元列示时，换算量级后再比，相对差 2‰ 内算命中。"""
    for scale in _NUM_SCALES:
        target = value * scale
        for p in page_nums:
            if p and abs(p - target) <= abs(p) * 0.002:
                return True
    return False


def _unit_has_num(text, n):
    text_norm = _normalize_cn_numerals(text)
    if _has_num(text, text_norm, n):
        return True
    try:
        return _num_on_page(float(n), _page_nums(text_norm))
    except ValueError:
        return False


def _other_unit_hint(cached, nums):
    """数字在当前页找不到时，回查同文件其他页；只对足够具体、出现页数很少的数字给提示，
    避免 6.1、10 这类到处都有的数字把真错锚淹掉。"""
    ordered = [n for n in set(nums)
               if not re.fullmatch(r"(?:19|20)\d{2}", n)
               and not re.fullmatch(r"0\d+", n)
               and len(n.replace(".", "")) >= 4]
    ordered.sort(key=lambda x: (len(x.replace(".", "")), x), reverse=True)
    for n in ordered:
        hits = [u for u in cached["units"] if _unit_has_num(u.get("text") or "", n)]
        if 1 <= len(hits) <= 2:
            kind = "段" if cached.get("unit") == "para" else "页"
            return f"；数字 {n} 实际出现在同文件第 {hits[0]['n']} {kind}，锚点疑似挂错"
    return ""


def _anchor_checks(report_md):
    manifest = {f["file"]: f for f in ingest_mod.load_manifest()["files"]}
    problems = []
    for ln in report_md.splitlines():
        anchors = wiki.ANCHOR_RE.findall(ln)
        if not anchors:
            continue
        # 派生值已按「（测算：公式）」标注的，只核锚点真实性，不要求比值本身出现在原文
        derived = bool(re.search(r"[（(]\s*测算\s*[:：]", ln))
        clean = wiki.ANCHOR_RE.sub("", ln)
        nums = [n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", clean)]
        nums = [n for n in nums if len(n.replace(".", "")) >= 2]
        values = []
        for n in nums:
            try:
                values.append(float(n))
            except ValueError:
                continue
        for fname, loc in anchors:
            f = manifest.get(fname)
            if not f:
                problems.append(f"锚点引用了清单里没有的文件 {fname}#{loc}")
                continue
            cached = load_cached(config.ROOT / f["path"])
            if not cached:
                problems.append(f"{fname} 没有提取缓存，锚点无法核验")
                continue
            unit = next((u for u in cached["units"] if u["n"] == wiki.loc_num(loc)), None)
            if unit is None:
                problems.append(f"锚点 {fname}#{loc} 超出单元范围")
                continue
            if derived or not nums:
                continue
            text = unit.get("text") or ""
            text_norm = _normalize_cn_numerals(text)
            if not any(_has_num(text, text_norm, n) for n in nums):
                page_nums = _page_nums(text_norm)
                if not any(_num_on_page(v, page_nums) for v in values):
                    hint = _other_unit_hint(cached, nums)
                    problems.append(
                        f"{fname}#{loc} 的原文里找不到本句数字（{'、'.join(nums[:3])}）{hint}")
    return problems


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
        listing = "\n".join(f"{i}. [{f['level']}] {f['where']}：{f['problem']}"
                            for i, f in enumerate(open_model, 1))
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


def _phase_review(job_id, tid):
    job = load_job(job_id)
    path = config.ROOT / job["report_path"]
    report_md = path.read_text(encoding="utf-8")
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
                             .replace("{MECH}", "\n".join(mech) or "无")}], task="review", response_json=True)
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
        try:
            out = llm.chat([{"role": "user", "content": REPAIR_PROMPT
                             .replace("{ISSUES}", "\n".join(
                                 f"- [{f['level']}] {f['where']}：{f['problem']}" for f in pending))
                             .replace("{REPORT}", report_md)}], task="review", response_json=True)
        except llm.LLMError as e:
            _log(job, f"第 {round_no} 轮修复调用失败：{e}")
            break
        fixed, changes = report_md, 0
        for fx in out.get("fixes") or []:
            old, new = str(fx.get("old") or "").strip(), str(fx.get("new") or "").strip()
            if old and old in fixed and new and new != old:
                fixed = fixed.replace(old, new, 1)
                changes += 1
        if changes and len(fixed) >= len(report_md) * 0.9:
            fixed = ask_mod.strip_glitch(fixed)
            path.write_text(fixed.rstrip() + "\n", encoding="utf-8")
            report_md = fixed
            repaired_rounds += 1
            _log(job, f"第 {round_no} 轮按 {len(pending)} 条问题替换 {changes} 处")
        elif changes:
            _log(job, f"第 {round_no} 轮替换幅度过大被拒（{changes} 处），保留原文复核")
        else:
            _log(job, f"第 {round_no} 轮模型未给出可用替换，直接复核")
        mech = _verify_findings(job, report_md, findings, tid)

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
    job["status"] = "docxing"
    save_job(job)
    _start(job_id, _phase_docx, f"{kind_label(job)}转 Word《{job['title']}》")


# ---------- Word 与候选 ----------

def _phase_docx(job_id, tid):
    job = load_job(job_id)
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
