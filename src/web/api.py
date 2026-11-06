import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

from flask import Blueprint, jsonify, make_response, request

from .. import ask as ask_mod
from .. import audit as audit_mod
from .. import ai_review as ai_review_mod
from .. import config, llm, wiki
from .. import fix as fix_mod
from .. import page_proposals
from .. import ingest as ingest_mod
from .. import purge as purge_mod
from .. import report as report_mod
from .. import industry_report, report_distill
from .. import sessions, tasks
from .. import agent_rescue, fetcher, model_probe
from ..classify import classify_one, sanitize, unique_target
from ..pdf_extract import SUPPORTED_SUFFIXES, raw_file_index
from .render import anchor_pretty, render_md

api = Blueprint("api", __name__)


def _require_same_origin():
    """拦截跨站请求（CSRF）。问题 1 的第二道防线。

    删除文件、上传、审校、修复、沉淀都是"别的网页可以偷偷调"的接口：
    浏览器发跨站请求时，同源策略禁止 JS 读响应，但**请求照样会发出去**。
    两道校验：
      1. 必须带自定义头 X-Requested-With: wiki —— 跨站表单发不出自定义头
      2. Origin / Referer 必须是本站
    返回 None 表示放行；返回 (响应, 状态码) 表示拒绝。
    """
    if request.headers.get("X-Requested-With") != "wiki":
        return jsonify({"error": "缺少 X-Requested-With 头，拒绝跨站请求"}), 403
    host = request.host
    for h in (request.headers.get("Origin") or "", request.headers.get("Referer") or ""):
        if not h:
            continue
        try:
            from urllib.parse import urlparse
            netloc = urlparse(h).netloc
        except Exception:
            continue
        if netloc and netloc != host:
            return jsonify({"error": "跨站请求被拒绝"}), 403
    return None


# 错误脱敏（问题 21）：网页报错不该露出 API 服务商的原始响应、或电脑上文件的完整路径。
# 完整信息只进服务端日志，客户端只拿到一句人话。
def _looks_like_path(tok: str) -> bool:
    """像不像一个绝对路径（盘符开头、或是 / 开头）。"""
    return bool(tok) and (tok[0] in "/\\" or (len(tok) > 1 and tok[1] == ":"))


def _looks_like_http_body(tok: str) -> bool:
    """像不像 API 服务商返回的原始错误体。"""
    t = tok.strip()
    return t.startswith("HTTP ") and (":" in t) and ("{" in t or "[" in t)




def _safe_error(e, prefix: str = "处理失败") -> str:
    """把异常原文洗成一句不含敏感信息的说明。

    脱敏两样（问题 21）：
      1. API 服务商返回的原始错误体（可能含 key 片段、内部结构）
      2. 电脑上文件的完整路径
    完整信息只留在服务端日志里。
    """
    raw = str(e)
    detail = raw
    if _looks_like_http_body(detail):
        detail = "HTTP 错误响应（详见服务端日志）"
    else:
        # 按空白切词，逐词判断是不是路径
        cleaned = []
        for w in detail.split():
            cleaned.append("<路径已隐藏>" if _looks_like_path(w) else w)
        detail = " ".join(cleaned)
    detail = detail.strip()
    if len(detail) > 120:
        detail = detail[:120] + "…"
    return f"{prefix}：{detail}" if detail else prefix


def _conditional_json(payload, req):
    """带 ETag 的 JSON 响应：内容没变就回 304 空响应（问题 21：全量轮询浪费带宽）。

    Flask 的 make_conditional() 对 jsonify 不生效，这里显式算摘要做 ETag。
    """
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    etag = '"' + hashlib.md5(body.encode("utf-8")).hexdigest() + '"'
    if req.headers.get("If-None-Match") == etag:
        resp = make_response("", 304)
        resp.headers["ETag"] = etag
        return resp
    resp = make_response(body)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["ETag"] = etag
    return resp


def _msg_out(m):
    """消息出站加工：助手答案附渲染 HTML，提案行附可读文本。"""
    m = dict(m)
    if m.get("role") == "assistant" and m.get("content"):
        m["html"] = render_md(m["content"])
    prop = m.get("proposal")
    if prop:
        m["proposal"] = {**prop, "lines_pretty": [anchor_pretty(ln) for ln in prop.get("lines", [])]}
    return m


# ---------- 问答 ----------

def _resolve_model(raw):
    """请求里带的模型 id → (模型 id 或 None, 错误文案 或 None)。

    空 / 等于默认模型 = 用 config.json 的默认模型。
    """
    name = str(raw or "").strip()
    if not name or name == config.DEFAULT_MODEL:
        return None, None
    if name not in config.MODEL_PROFILES:
        return None, f"未知模型：{name}"
    return name, None


def _model_arg(data):
    """请求里带的模型 id，返回 (模型 id 或 None, 错误响应)。"""
    name, err = _resolve_model((data or {}).get("model"))
    if err:
        return None, (jsonify({"error": err}), 400)
    return name, None


def _run_ask(cid, mid, question, history, model):
    with llm.use_model(model):
        return _ask_body(cid, mid, question, history, model)


def _ask_body(cid, mid, question, history, model):
    try:
        result = ask_mod.ask(question, history=history, propose=True)
    except Exception as e:
        sessions.update_message(cid, mid, status="error", kind="error", content=_safe_error(e, "问答失败"))
        raise
    if result["kind"] == "report":
        title = result["title"]
        domain = result.get("domain") or "general"
        if result.get("report_type") in ("industry", "company"):
            kind = result["report_type"]
            kind_name = industry_report.KIND_LABELS[kind]
            try:
                job = industry_report.start_job(question, title, model=model, domain=domain, kind=kind)
                sessions.update_message(cid, mid, status="done", kind="report_ack",
                                        content=f"已开始生成{kind_name}《{title}》，先到报告页确认大纲。",
                                        report_job=job["id"], report_kind=kind)
                return {"kind": "report", "report_job": job["id"], "report_kind": kind}
            except Exception as e:  # 管线起不来就退回内置底稿，不让问答卡死
                sessions.update_message(cid, mid, status="running", kind="pending",
                                        content=f"{kind_name}管线启动失败（{e}），改用内置底稿。")

        def _report_run(t):
            with llm.use_model(model):
                p = report_mod.generate(title, question, task_id=t, domain=domain)
            return {"file": p.name}
        rid = tasks.start("report", _report_run, label=f"报告《{title}》")
        sessions.update_message(cid, mid, status="done", kind="report_ack",
                                content=f"已开始生成报告《{title}》，完成后可在产出页查看。", report_task=rid)
        return {"kind": "report", "report_task": rid}
    fields = dict(status="done", kind=result["kind"], content=result["answer_md"],
                  selected=result["step1"]["selected"], reason=result["step1"]["reason"],
                  validation=result["validation"], proposal=result.get("proposal"),
                  model=llm.active_model())
    if result["kind"] == "referral":
        fields["candidates"] = result.get("candidates") or []
    if result.get("deep"):
        fields["deep"] = True
    sessions.update_message(cid, mid, **fields)
    return {"kind": result["kind"]}


def _run_deep(cid, mid, question, files, history, model):
    with llm.use_model(model):
        try:
            result = ask_mod.deep_ask(question, history=history, files=files)
        except Exception as e:
            sessions.update_message(cid, mid, status="error", kind="error", content=f"深查失败：{e}")
            raise
        sessions.update_message(cid, mid, status="done", kind="answer", deep=True,
                                content=result["answer_md"], selected=[], reason=result["step1"]["reason"],
                                validation=result["validation"], proposal=result.get("proposal"),
                                model=llm.active_model())
    return {"kind": "answer", "deep": True}


@api.post("/api/ask")
def api_ask():
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    question = str(data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "问题不能为空"}), 400
    model, err = _model_arg(data)
    if err:
        return err
    conv = sessions.get(data.get("conv_id") or "") or sessions.create()
    sessions.append_message(conv["id"], {"role": "user", "content": question})
    history = sessions.history_for_ask(conv["id"], config.CFG["ask"].get("history_turns", 4))
    ph = sessions.append_message(conv["id"], {"role": "assistant", "content": "", "status": "running", "kind": "pending"})
    tid = tasks.start("ask", lambda t: _run_ask(conv["id"], ph["id"], question, history, model),
                      model=model)
    return jsonify({"conv_id": conv["id"], "msg_id": ph["id"], "task_id": tid})


@api.post("/api/deep")
def api_deep():
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    model, err = _model_arg(data)
    if err:
        return err
    conv = sessions.get(data.get("conv_id") or "")
    msg = next((m for m in (conv or {}).get("messages", []) if m["id"] == data.get("msg_id")), None)
    if not conv or not msg:
        return jsonify({"error": "会话或消息不存在"}), 404
    idx = next(i for i, m in enumerate(conv["messages"]) if m["id"] == msg["id"])
    question = next((m["content"] for m in reversed(conv["messages"][:idx]) if m.get("role") == "user"), "")
    files = data.get("files") or None
    history = sessions.history_for_ask(conv["id"], config.CFG["ask"].get("history_turns", 4))
    ph = sessions.append_message(conv["id"], {"role": "assistant", "content": "", "status": "running", "kind": "pending"})
    tid = tasks.start("ask", lambda t: _run_deep(conv["id"], ph["id"], question, files, history, model),
                      model=model)
    return jsonify({"conv_id": conv["id"], "msg_id": ph["id"], "task_id": tid})


# ---------- 会话 ----------

@api.get("/api/conversations")
def api_conversations():
    return _conditional_json({"items": sessions.list_conversations()}, request)


@api.post("/api/conversations")
def api_conversations_new():
    if err := _require_same_origin():
        return err
    conv = sessions.create()
    return jsonify({"conv": conv})


@api.delete("/api/conversations/<cid>")
def api_conversation_delete(cid):
    """删掉一个会话（问题 21：以前只能新建不能删，历史永久堆积）。"""
    if err := _require_same_origin():
        return err
    if not sessions.delete(cid):
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"ok": True})


@api.patch("/api/conversations/<cid>")
def api_conversation_rename(cid):
    """给会话改名（问题 21）。"""
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    title = sessions.rename(cid, data.get("title") or "")
    if title is None:
        return jsonify({"error": "会话不存在或标题为空"}), 400
    return jsonify({"ok": True, "title": title})


@api.get("/api/conversations/<cid>")
def api_conversation(cid):
    conv = sessions.get(cid)
    if conv is None:
        return jsonify({"error": "会话不存在"}), 404
    out = dict(conv)
    out["messages"] = []
    for m in conv["messages"]:
        m = dict(m)
        prop = m.get("proposal")
        if prop:
            applied = set(m.get("applied_lines") or [])
            m["proposal"] = {**prop, "lines": [ln for ln in prop.get("lines", []) if ln not in applied]}
            if not m["proposal"]["lines"]:
                m["proposal"] = None
        out["messages"].append(_msg_out(m))
    return jsonify(out)


# ---------- 沉淀提案 ----------

@api.post("/api/distill/propose")
def api_distill_propose():
    # 这个接口会调 LLM（要花钱），也要挡跨站请求
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    conv = sessions.get(data.get("conv_id") or "")
    if conv is None:
        return jsonify({"error": "会话不存在"}), 404
    items = []
    for m in conv["messages"]:
        prop = m.get("proposal")
        if not prop:
            continue
        applied = set(m.get("applied_lines") or [])
        remaining = [ln for ln in prop.get("lines", []) if ln not in applied]
        if remaining:
            items.append({"conv_id": conv["id"], "msg_id": m["id"],
                          "proposal": {**prop, "lines": remaining,
                                       "lines_pretty": [anchor_pretty(ln) for ln in remaining]}})
    return jsonify({"items": items})


@api.get("/api/proposals/pending")
def api_proposals_pending():
    """只读查询，不把提案标记为已提醒。

    以前页面一加载就写 presented=True，用户还没看见弹窗（刷新、切页、直接关掉）
    提案就永久消失。现在只有用户点「不再提醒」才 dismiss；点「稍后处理」只推迟到
    snoozed_until 之后，提案始终还在会话里。
    """
    now = config.now_iso()
    items = []
    for c in sessions.list_conversations():
        conv = sessions.get(c["id"])
        for m in conv["messages"]:
            prop = m.get("proposal")
            if not prop or m.get("proposal_dismissed"):
                continue
            snooze = m.get("proposal_snoozed_until")
            if snooze and str(snooze) > now:
                continue
            applied = set(m.get("applied_lines") or [])
            remaining = [ln for ln in prop.get("lines", []) if ln not in applied]
            if not remaining:
                continue
            items.append({"conv_id": conv["id"], "msg_id": m["id"],
                          "conv_title": conv["title"],
                          "proposal": {**prop, "lines": remaining,
                                       "lines_pretty": [anchor_pretty(ln) for ln in remaining]}})
    return jsonify({"items": items})


@api.post("/api/proposals/defer")
def api_proposals_defer():
    """稍后处理：只把提醒推迟一段时间，不丢提案。"""
    data = request.get_json(force=True, silent=True) or {}
    conv = sessions.get(data.get("conv_id") or "")
    mid = data.get("msg_id")
    if conv is None or not any(m["id"] == mid for m in conv["messages"]):
        return jsonify({"error": "提案不存在"}), 404
    try:
        hours = max(1, min(24 * 30, int(data.get("hours") or 24)))
    except (TypeError, ValueError):
        hours = 24
    until = (datetime.fromisoformat(config.now_iso()).timestamp() + hours * 3600)
    until_iso = datetime.fromtimestamp(until).isoformat(timespec="seconds")
    sessions.update_message(conv["id"], mid, proposal_snoozed_until=until_iso)
    return jsonify({"ok": True, "until": until_iso})


@api.post("/api/proposals/dismiss")
def api_proposals_dismiss():
    """不再提醒：只有用户明确点这个按钮才把提案标为已处理。"""
    data = request.get_json(force=True, silent=True) or {}
    conv = sessions.get(data.get("conv_id") or "")
    mid = data.get("msg_id")
    if conv is None or not any(m["id"] == mid for m in conv["messages"]):
        return jsonify({"error": "提案不存在"}), 404
    sessions.update_message(conv["id"], mid, proposal_dismissed=True)
    return jsonify({"ok": True})


@api.post("/api/distill/apply")
def api_distill_apply():
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    results = []
    for a in data.get("applies") or []:
        conv = sessions.get(a.get("conv_id") or "")
        msg = next((m for m in (conv or {}).get("messages", []) if m["id"] == a.get("msg_id")), None)
        if not conv or not msg or not msg.get("proposal"):
            results.append({"ok": False, "msg_id": a.get("msg_id"), "error": "提案不存在"})
            continue
        try:
            ret = ask_mod.apply_distill(msg["proposal"], chosen_lines=a.get("lines"))
        except Exception as e:
            results.append({"ok": False, "msg_id": msg["id"], "error": _safe_error(e)})
            continue
        if not ret:
            results.append({"ok": False, "msg_id": msg["id"], "error": "没有可写入的行"})
            continue
        chosen = a.get("lines") or msg["proposal"].get("lines") or []
        applied = set(msg.get("applied_lines") or []) | set(chosen)
        sessions.update_message(conv["id"], msg["id"], applied_lines=sorted(applied))
        results.append({"ok": True, "msg_id": msg["id"], **ret})
    return jsonify({"results": results})


# ---------- 待建页提案 ----------

@api.get("/api/page-proposals/pending")
def api_page_proposals_pending():
    """只读查询，不改变任何状态（语义同蒸馏提案的 pending）。"""
    return jsonify({
        "items": page_proposals.pending_items(),
        "threshold": page_proposals.threshold(),
    })


@api.post("/api/page-proposals/defer")
def api_page_proposals_defer():
    """稍后处理：只把提醒推迟一段时间（默认 24h），不丢提案。"""
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name or page_proposals.defer(name) is None:
        return jsonify({"error": "提案不存在"}), 404
    return jsonify({"ok": True})


@api.post("/api/page-proposals/dismiss")
def api_page_proposals_dismiss():
    """不再提醒：只有用户明确点这个按钮才永久消失。"""
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name or page_proposals.dismiss(name) is None:
        return jsonify({"error": "提案不存在"}), 404
    return jsonify({"ok": True})


@api.post("/api/page-proposals/confirm")
def api_page_proposals_confirm():
    """确认建页：逐项起 mini 摄入后台任务（类型与摄入互斥排队，可取消、有进度）。

    成功标 done（不再重复出现）；失败回 pending 并附原因，处理后可重试。
    """
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    items = [x for x in (data.get("items") or [])
             if isinstance(x, dict) and str(x.get("name") or "").strip()]
    if not items:
        return jsonify({"error": "没有勾选任何建页建议"}), 400
    results = []
    for x in items:
        name = str(x.get("name")).strip()
        etype = str(x.get("entity_type") or "concept")
        if page_proposals.mark_building(name) is None:
            results.append({"ok": False, "name": name, "error": "提案不存在或已处理"})
            continue

        def run(t, name=name, etype=etype):
            try:
                ret = page_proposals.build_page(name, etype, task_id=t)
                page_proposals.mark_done(name)
                return ret
            except Exception as e:
                page_proposals.mark_pending(name, _safe_error(e, "建页失败"))
                raise

        tid = tasks.start("ingest", run, label=f"建页：{name}")
        results.append({"ok": True, "name": name, "task_id": tid})
    return jsonify({"results": results})


# ---------- 上传与摄入链 ----------

def _pipeline(names, focus=None):
    """取名线与摄入线并行：取名线每入库一份就把路径塞进队列，摄入线同时取走摄入，两条线互不等待。

    两条线每个循环都检查取消/超时；摄入线取队列用 1 秒超时轮询，不会因为取名线异常
    退出而永远等下去。
    """
    queue = Queue()
    focus = focus or []

    def classify_line(tid):
        results = []
        try:
            for i, name in enumerate(names, 1):
                tasks.check_cancel(tid)
                tasks.update(tid, current=i - 1, detail=f"第 {i}/{len(names)} 份：{name} 正在取名并进入资料库")
                p = config.INBOX_DIR / name
                if not p.exists():
                    results.append({"ok": False, "file": name, "error": "文件不在收件台"})
                    continue
                try:
                    r = classify_one(p)
                except Exception as e:
                    r = {"ok": False, "file": name, "error": _safe_error(e)}
                results.append(r)
                if r.get("ok"):
                    queue.put(config.ROOT / r["target"])
                tasks.update(tid, current=i)
        finally:
            queue.put(None)  # 收工信号：摄入线做完队列里剩下的就退出
        tasks.update(tid, current=len(names), detail="")
        return results

    def ingest_line(tid):
        results = []
        done = 0
        try:
            while True:
                tasks.check_cancel(tid)
                try:
                    target = queue.get(timeout=1.0)
                except Empty:
                    continue
                if target is None:
                    break
                done += 1
                tasks.update(tid, current=done - 1, detail=f"第 {done} 份：{target.name} 正在摄入知识点")
                manifest = ingest_mod.load_manifest()
                before = {f["sha256"] for f in manifest["files"]}
                try:
                    r = ingest_mod.ingest_one(target, manifest, focus=focus, task_id=tid)
                except Exception as e:
                    print(f"  摄入失败: {e}")
                    r = {"file": target.name, "error": _safe_error(e)}
                if r:
                    results.append(r)
                # 逐份落盘，且只追加本轮新增的条目：删除线并发删掉的条目不会被这份旧清单冲回来
                ingest_mod.append_manifest([f for f in manifest["files"] if f["sha256"] not in before])
                tasks.update(tid, current=done)
        finally:
            # 无论正常结束、取消还是超时，都把已摄入的部分写进索引，避免半截状态
            tasks.update(tid, current=len(names), detail="")
            ingest_mod.finish(ingest_mod.load_manifest(), [r for r in results if "error" not in r])
        return results

    return classify_line, ingest_line


@api.post("/api/upload")
def api_upload():
    if err := _require_same_origin():
        return err
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "没有收到文件"}), 400
    # 网页上选的模型对取名、摄入同样生效，不用再改 config.json
    model, err = _model_arg(request.form)
    if err:
        return err

    # 关注点（可选）：前端勾选「按关注点优先提炼」才带
    focus = []
    if request.form.get("use_focus"):
        try:
            parsed = json.loads(request.form.get("focus") or "[]")
            if isinstance(parsed, list):
                focus = [str(x).strip() for x in parsed if str(x).strip()]
        except (ValueError, TypeError):
            focus = []

    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    saved, rejected = [], []
    for f in files:
        orig = f.filename or "未命名"
        suffix = Path(orig).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            rejected.append(orig)
            continue
        stem = sanitize(Path(orig).stem) or "未命名"
        dest = config.INBOX_DIR / unique_target(config.INBOX_DIR, stem + suffix)
        f.save(dest)
        saved.append(dest.name)
    if not saved:
        return jsonify({"error": "只接受 PDF 与 Word(.docx) 文件", "rejected": rejected}), 400
    classify_line, ingest_line = _pipeline(saved, focus)
    timeout_s = config.CFG.get("ingest", {}).get("task_timeout_s") or 7200
    tid = tasks.start("classify", classify_line, total=len(saved),
                      label=f"取名入库 {len(saved)} 份", model=model, timeout_s=timeout_s)
    tasks.start("ingest", ingest_line, total=len(saved),
                label=f"摄入 {len(saved)} 份", model=model, timeout_s=timeout_s)
    return jsonify({"task_id": tid, "saved": saved, "rejected": rejected, "focus": focus, "model": model})


# ---------- 原文件 ----------

@api.get("/api/files")
def api_files():
    return jsonify({"items": purge_mod.file_entries()})


@api.post("/api/files/delete")
def api_files_delete():
    # 问题 1（CSRF）：删除是不可撤销的破坏性操作，必须挡住"别的网页偷偷调我"。
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    names = [str(n).strip() for n in (data.get("files") or []) if str(n).strip()]
    if not names:
        return jsonify({"error": "没有选中要删除的文件"}), 400
    tid = tasks.start("delete", lambda t: purge_mod.purge_files(names, task_id=t),
                      total=len(names), label=f"删除 {len(names)} 份原文件")
    return jsonify({"task_id": tid})


def _ingest_pick(names):
    """网页勾选的未摄入文件 → 可摄入路径。收件台、_ 隔离目录与同内容重复件一律拒绝。"""
    idx = raw_file_index()
    allowed = (config.REPORTS_DIR, config.FILINGS_DIR, config.POLICIES_DIR)
    known = {f.get("sha256"): f.get("file") for f in ingest_mod.load_manifest()["files"]
             if f.get("sha256")}
    paths, rejected = [], []
    for name in names:
        path = idx.get(name)
        reason = ""
        if not path:
            reason = "找不到文件"
        elif not any(path.is_relative_to(d) for d in allowed):
            reason = "只接受 raw/reports、raw/filings、raw/policies 下的文件"
        elif any(part.startswith("_") for part in path.relative_to(config.RAW_DIR).parts[:-1]):
            reason = "隔离目录不摄入"
        else:
            try:
                dup = known.get(ingest_mod.sha256_of(path))
            except OSError as e:
                reason = f"读取失败：{e}"
            else:
                if dup:
                    reason = f"与已入库文件 {dup} 内容相同，不重复摄入"
        if reason:
            rejected.append({"file": name, "reason": reason})
        else:
            paths.append(path)
    return paths, rejected


@api.post("/api/files/ingest")
def api_files_ingest():
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    names = [str(n).strip() for n in (data.get("files") or []) if str(n).strip()]
    if not names:
        return jsonify({"error": "没有选中要摄入的文件"}), 400
    paths, rejected = _ingest_pick(names)
    if not paths:
        return jsonify({"error": "没有可摄入的文件", "rejected": rejected}), 400

    def run(tid):
        def progress(i, name):
            tasks.update(tid, current=i - 1, detail=f"摄入 {name}")
        results = ingest_mod.run_all(paths, progress=progress)
        tasks.update(tid, current=len(paths))
        return {"ingested": len(results), "files": [r.get("file") for r in results]}

    tid = tasks.start("ingest", run, total=len(paths), label=f"摄入 {len(paths)} 份原文件")
    return jsonify({"task_id": tid, "accepted": [p.name for p in paths], "rejected": rejected})


# ---------- 审校与修复 ----------

@api.post("/api/audit/run")
def api_audit_run():
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    model, err = _model_arg(data)
    if err:
        return err

    def run(tid):
        r = audit_mod.run(task_id=tid)
        return {"file": r["path"].name, "counts": r["counts"], "model": model}
    tid = tasks.start("audit", run, label="四类健康检查", model=model)
    return jsonify({"task_id": tid, "model": model})


def _ts_pretty(ts):
    """审校文件名里的时间戳 20261013-222015 → 2026-10-13 22:20。"""
    try:
        return datetime.strptime(str(ts), "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


_TS_RE = re.compile(r"^\d{8}-\d{6}$")


@api.get("/api/audit/reports")
def api_audit_reports():
    """审校报告清单（含 AI 初审版），供审校页动态陈列。

    每次新报告跑完前端重取一次即可，不再依赖整页刷新；
    摘要计数从 data/ 里的结构化结果读，读不到就不显示摘要。
    """
    items = []
    for p in config.OUTPUTS_DIR.glob("audit-*.md"):
        kind = "ai_review" if "-ai-" in p.name else "audit"
        ts = p.name.removeprefix("audit-").removeprefix("ai-").removesuffix(".md")
        entry = {"file": p.name, "kind": kind, "ts": ts, "ts_pretty": _ts_pretty(ts)}
        dj = config.DATA_DIR / (("audit-ai-" if kind == "ai_review" else "audit-") + ts + ".json")
        if dj.is_file():
            try:
                d = json.loads(dj.read_text(encoding="utf-8"))
                entry["counts"] = d.get("counts") or d.get("sev_counts") or {}
            except (OSError, ValueError):
                pass
        items.append(entry)
    # 同一时间戳排在一起（审校在前、它的 AI 初审紧随其后），整体按时间倒序
    items.sort(key=lambda x: (x["ts"], 1 if x["kind"] == "audit" else 0), reverse=True)
    return jsonify({"items": items})


@api.post("/api/audit/ai-review")
def api_audit_ai_review():
    """AI 初审：对指定（默认最新）审校结果按严重性排序并逐条给一句话说明。

    只写 outputs/ 与 data/，不动 wiki；AI 的判断仅供参考，问题一条不删。
    """
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    model, err = _model_arg(data)
    if err:
        return err
    ts = str(data.get("ts") or "").strip()
    # ts 要拼进文件名，格式不对就拒掉，不给路径穿越留口子
    if ts and not _TS_RE.fullmatch(ts):
        return jsonify({"error": "时间戳格式不对，应为 20261013-222015 这样的形式"}), 400

    def run(tid):
        r = ai_review_mod.run(ts=ts or None, task_id=tid)
        return {"file": r["path"].name, "counts": r["sev_counts"],
                "total": r["total"], "unjudged": r["unjudged"], "model": model}
    tid = tasks.start("ai_review", run, label="AI 初审排序", model=model)
    return jsonify({"task_id": tid, "model": model})


@api.get("/api/audit/latest")
def api_audit_latest():
    # AI 初审产物叫 audit-ai-*.json，也命中 audit-* 通配，要排除，
    # 否则「最新审校」会指到初审文件上（初审文件名排序在数字时间戳之后）
    files = sorted(p for p in config.DATA_DIR.glob("audit-*.json") if "-ai-" not in p.name)
    if not files:
        return jsonify({"error": "还没有审校结果"}), 404
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    data["ts_pretty"] = _ts_pretty(data.get("ts"))
    # 已有 AI 初审版就一并返回，前端好显示入口；没有就不带这个字段
    ai_md = config.OUTPUTS_DIR / f"audit-ai-{data.get('ts')}.md"
    if ai_md.is_file():
        data["ai_report"] = ai_md.name
        try:
            ai_json = json.loads(
                (config.DATA_DIR / f"audit-ai-{data.get('ts')}.json").read_text(encoding="utf-8"))
            data["ai_counts"] = ai_json.get("counts") or {}
            # 修复向导用：AI 初判过的条目清单（已按严重性排序、每条带 sev/verdict/why），
            # 前端优先按它生成修复选项；items 缺失（旧版初审文件）就不带，向导退回脚本报告
            items = ai_json.get("items")
            if isinstance(items, list) and items:
                data["ai_items"] = items
        except (OSError, ValueError):
            pass
    data["pages"] = [{
        "slug": p["slug"], "type": p["type"],
        "title": str(p["meta"].get("title") or p["slug"]),
    } for p in wiki.load_pages() if p["type"] != "source"]
    return jsonify(data)


@api.post("/api/audit/fix")
def api_audit_fix():
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    actions = [a for a in (data.get("actions") or []) if isinstance(a, dict)]
    if not actions:
        return jsonify({"error": "没有勾选任何修复动作"}), 400
    model, err = _model_arg(data)
    if err:
        return err
    tid = tasks.start("fix", lambda t: {"notes": fix_mod.apply_fixes(actions, task_id=t),
                                        "model": model},
                      total=len(actions), label=f"按审校报告修复 {len(actions)} 项",
                      model=model)
    return jsonify({"task_id": tid, "model": model})


# ---------- 图谱 ----------

@api.get("/api/graph")
def api_graph():
    """知识图谱数据：节点=wiki 页面，边=双链引用（只画程序可验证的引用，不画语义关系）。

    只读接口不要求 CSRF 头（与现有只读路由口径一致）。
    节点 inbound 按「引用它的页面数」计（去重）；解析失败的双链丢弃并计数，
    由前端注明「N 条无法解析的链接未显示」。
    """
    pages = wiki.load_pages()
    id_of = {}
    nodes = []
    for p in pages:
        key = f"{p['type']}/{p['slug']}"
        id_of[(p["type"], p["slug"])] = key
        nodes.append({
            "id": key, "slug": p["slug"], "type": p["type"],
            "title": str(p["meta"].get("title") or p["slug"]),
            "statements": p["statements"],
        })
    edge_set = set()
    unresolved = 0
    for p in pages:
        for link in wiki.wikilinks_of(p["body"]):
            tgt = wiki.resolve_target(link, pages)
            if tgt is None:
                unresolved += 1
                continue
            if tgt == (p["type"], p["slug"]):  # 自引用不画
                continue
            edge_set.add((id_of[(p["type"], p["slug"])], id_of[tgt]))
    links = [{"src": s, "dst": d} for s, d in sorted(edge_set)]
    inbound = {}
    for e in links:
        inbound[e["dst"]] = inbound.get(e["dst"], 0) + 1
    for n in nodes:
        n["inbound"] = inbound.get(n["id"], 0)
    return jsonify({"nodes": nodes, "links": links, "unresolved": unresolved})


# ---------- 主动取数 ----------

@api.get("/api/fetch/sources")
def api_fetch_sources():
    # items 只给取数页能手动选的源；labels 给缺口清单用的全集，
    # periodic_report、web_search 这类管线内部补缺口的源也要有中文名
    labels = {s["id"]: s["label"] for s in fetcher.source_specs()}
    return jsonify({"items": fetcher.source_specs(only=fetcher.MANUAL_SOURCE_IDS),
                    "labels": labels})


@api.post("/api/fetch")
def api_fetch_run():
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    source = str(data.get("source") or "").strip()
    specs = {s["id"]: s for s in fetcher.source_specs(only=fetcher.MANUAL_SOURCE_IDS)}
    if source not in specs:
        return jsonify({"error": "未知取数源"}), 400
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    tid = tasks.start("fetch", lambda t: fetcher.run_fetch(source, params, t),
                      label=f"取数·{specs[source]['label']}")
    return jsonify({"task_id": tid})


# ---------- 行业研报管线 ----------

def _job_brief(job):
    rescuing = agent_rescue.rescue_active(job)
    label = industry_report.STATUS_LABELS.get(job["status"], job["status"])
    return {
        "id": job["id"], "title": job["title"], "status": job["status"],
        "status_label": f"{label}（救火中）" if rescuing else label,
        "rescue_active": rescuing,
        "rescue": agent_rescue.rescue_state(job),
        "kind": job.get("kind") or "industry",
        "kind_label": industry_report.KIND_LABELS.get(job.get("kind"), "行业研报"),
        "error": job.get("error") or "",
        "outline_path": job.get("outline_path") or "",
        "report_path": job.get("report_path") or "",
        "docx_path": job.get("docx_path") or "",
        "gaps": job.get("gaps") or [],
        "candidates_count": job.get("candidates_count") or 0,
        "updated_at": job.get("updated_at") or "",
    }


@api.get("/api/report/jobs")
def api_report_jobs():
    items = industry_report.list_jobs()
    items.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return jsonify({"items": items})


@api.get("/api/report/<jid>")
def api_report_job(jid):
    job = industry_report.load_job(jid)
    if not job:
        return jsonify({"error": "报告任务不存在"}), 404
    out = dict(job)
    out["rescue_active"] = agent_rescue.rescue_active(job)
    out["rescue"] = agent_rescue.rescue_state(job)
    label = industry_report.STATUS_LABELS.get(job["status"], job["status"])
    out["status_label"] = f"{label}（救火中）" if out["rescue_active"] else label
    out["kind"] = job.get("kind") or "industry"
    out["kind_label"] = industry_report.KIND_LABELS.get(out["kind"], "行业研报")
    out["candidates"] = report_distill.load_candidates(jid)
    out["pages"] = job.get("pages") or []
    return jsonify(out)


@api.post("/api/report/<jid>/outline")
def api_report_outline(jid):
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    outline_md = data.get("outline_md")
    try:
        job = industry_report.approve_outline(jid, outline_md)
    except KeyError as e:
        return jsonify({"error": str(e) or "报告任务不存在"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"job": _job_brief(job)})


@api.post("/api/report/<jid>/gaps")
def api_report_gaps(jid):
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    ids = [str(x) for x in (data.get("gap_ids") or [])]
    try:
        ret = industry_report.approve_gaps(jid, ids)
    except KeyError as e:
        return jsonify({"error": str(e) or "报告任务不存在"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(ret)


@api.post("/api/report/<jid>/continue")
def api_report_continue(jid):
    if err := _require_same_origin():
        return err
    try:
        job = industry_report.continue_job(jid)
    except KeyError as e:
        return jsonify({"error": str(e) or "报告任务不存在"}), 404
    return jsonify({"job": _job_brief(job)})


@api.post("/api/report/<jid>/candidates/apply")
def api_report_candidates_apply(jid):
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    ids = [str(x) for x in (data.get("candidate_ids") or [])]
    try:
        ret = report_distill.apply(jid, ids, industry_report.load_job, industry_report.save_job)
    except KeyError as e:
        return jsonify({"error": str(e) or "报告任务不存在"}), 404
    return jsonify(ret)


@api.post("/api/report/<jid>/delete")
def api_report_delete(jid):
    if err := _require_same_origin():
        return err
    try:
        ret = industry_report.delete_job(jid)
    except KeyError as e:
        return jsonify({"error": str(e) or "报告任务不存在"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(ret)


@api.post("/api/report/<jid>/fallback")
def api_report_fallback(jid):
    if err := _require_same_origin():
        return err
    job = industry_report.load_job(jid)
    if not job:
        return jsonify({"error": "报告任务不存在"}), 404

    def run(t):
        with llm.use_model(job.get("model")):
            p = report_mod.generate(job["title"], job["question"], task_id=t,
                                    domain=job.get("domain") or "general")
        j = industry_report.load_job(jid) or job
        j["fallback_path"] = p.relative_to(config.ROOT).as_posix()
        industry_report.save_job(j)
        return {"file": p.name}

    rid = tasks.start("report", run, label=f"内置底稿《{job['title']}》")
    job["status"] = "fallback"
    job["error"] = ""
    job["fallback_task"] = rid
    job.setdefault("log", []).append({"ts": config.now_iso(), "msg": "改走内置四段式底稿"})
    industry_report.save_job(job)
    return jsonify({"task_id": rid})


# ---------- 任务 ----------

@api.get("/api/tasks")
def api_tasks():
    kinds = request.args.get("kinds")
    kinds = set(kinds.split(",")) if kinds else None
    return _conditional_json(
        {"tasks": tasks.snapshot(kinds), "active": tasks.active_counts()}, request)


@api.post("/api/tasks/<tid>/ack")
def api_task_ack(tid):
    tasks.acknowledge(tid)
    return jsonify({"ok": True})


@api.post("/api/tasks/<tid>/cancel")
def api_task_cancel(tid):
    ok = tasks.cancel(tid)
    if not ok:
        return jsonify({"error": "任务已结束或不存在，无法取消"}), 400
    return jsonify({"ok": True})


# ---------- 模型接入 ----------

@api.get("/api/models")
def api_models():
    return jsonify({"models": config.model_choices(),
                    "providers": config.provider_choices()})


@api.post("/api/models")
def api_model_add():
    """添加模型：先验两条协议，都通了才落盘，坏配置在添加时当场报出来。"""
    if err := _require_same_origin():
        return err
    data = request.get_json(force=True, silent=True) or {}
    provider = str(data.get("provider") or "").strip()
    model_id = str(data.get("model_id") or "").strip()
    label = str(data.get("label") or "").strip()
    anthropic_model = str(data.get("anthropic_model") or "").strip()
    api_key = str(data.get("api_key") or "").strip() or config.provider_key(provider)
    if provider not in config.MODEL_PROVIDERS:
        return jsonify({"error": "不支持的服务商，只能从下拉清单里选"}), 400
    if not model_id:
        return jsonify({"error": "模型 ID 不能为空"}), 400
    if model_id in config.CFG["llm"]["models"]:
        return jsonify({"error": "模型 ID 与内置模型重名，不能覆盖内置档案"}), 400
    if not api_key:
        return jsonify({"error": "请填写 API Key（.env 里也没有这家服务商的 key）"}), 400

    prov = config.MODEL_PROVIDERS[provider]
    rescue_model = anthropic_model or model_id
    with ThreadPoolExecutor(max_workers=2) as ex:
        fc = ex.submit(model_probe.probe_chat, prov["chat_base_url"], api_key, model_id)
        fr = ex.submit(model_probe.probe_anthropic, prov["anthropic_base_url"],
                       api_key, rescue_model)
        chat_ok, chat_msg = fc.result()
        rescue_ok, rescue_msg = fr.result()
    if not (chat_ok and rescue_ok):
        return jsonify({"error": "模型未通过连通测试，未保存",
                        "chat": chat_msg, "rescue": rescue_msg}), 400
    try:
        added = config.add_custom_model(provider, model_id, api_key,
                                        label=label, anthropic_model=rescue_model)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "model": added,
                    "chat": chat_msg, "rescue": rescue_msg})


@api.get("/api/health")
def api_health():
    """页面启动自检：模型、依赖、损坏文件等集中在这里给前端提示。"""
    from .. import excel_export
    warnings = list(sessions.startup_warnings())
    for p, e in wiki.check_pages():
        warnings.append(f"知识库页面 {p.name} 格式损坏，已跳过：{e}")
    if not config.LLM_API_KEY:
        warnings.append(f"未配置 {config.LLM_API_KEY_ENV}，问答/取名/摄入会失败；"
                        "请复制 .env.example 为 .env 并填入 API Key。")
    return jsonify({
        "ok": not warnings,
        "warnings": warnings,
        "model": config.DEFAULT_MODEL,
        "models": config.model_choices(),
        "features": {"openpyxl": excel_export.available()},
    })
