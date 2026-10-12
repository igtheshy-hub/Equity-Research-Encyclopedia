import json
from pathlib import Path

from flask import Blueprint, jsonify, request

from .. import admin as admin_mod
from .. import ask as ask_mod
from .. import audit as audit_mod
from .. import config, llm, wiki
from .. import fix as fix_mod
from .. import report as report_mod
from .. import sessions, tasks
from ..classify import classify_one, sanitize, unique_target
from ..ingest import run_all as ingest_run_all
from ..pdf_extract import SUPPORTED_SUFFIXES
from .render import anchor_pretty, render_md

api = Blueprint("api", __name__)


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

def _run_ask(cid, mid, question, history):
    try:
        result = ask_mod.ask(question, history=history, propose=True)
    except Exception as e:
        sessions.update_message(cid, mid, status="error", kind="error", content=f"问答失败：{e}")
        raise
    if result["kind"] == "report":
        title = result["title"]

        def _report_run(t):
            p = report_mod.generate(title, question, task_id=t)
            return {"file": p.name}
        rid = tasks.start("report", _report_run, label=f"报告《{title}》")
        sessions.update_message(cid, mid, status="done", kind="report_ack",
                                content=f"已开始生成报告《{title}》，完成后可在产出页查看。", report_task=rid)
        return {"kind": "report", "report_task": rid}
    fields = dict(status="done", kind=result["kind"], content=result["answer_md"],
                  selected=result["step1"]["selected"], reason=result["step1"]["reason"],
                  validation=result["validation"], proposal=result.get("proposal"))
    if result["kind"] == "referral":
        fields["candidates"] = result.get("candidates") or []
    if result.get("deep"):
        fields["deep"] = True
    sessions.update_message(cid, mid, **fields)
    return {"kind": result["kind"]}


def _run_deep(cid, mid, question, files, history):
    try:
        result = ask_mod.deep_ask(question, history=history, files=files)
    except Exception as e:
        sessions.update_message(cid, mid, status="error", kind="error", content=f"深查失败：{e}")
        raise
    sessions.update_message(cid, mid, status="done", kind="answer", deep=True,
                            content=result["answer_md"], selected=[], reason=result["step1"]["reason"],
                            validation=result["validation"], proposal=result.get("proposal"))
    return {"kind": "answer", "deep": True}


@api.post("/api/ask")
def api_ask():
    data = request.get_json(force=True, silent=True) or {}
    question = str(data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "问题不能为空"}), 400
    conv = sessions.get(data.get("conv_id") or "") or sessions.create()
    sessions.append_message(conv["id"], {"role": "user", "content": question})
    history = sessions.history_for_ask(conv["id"], config.CFG["ask"].get("history_turns", 4))
    ph = sessions.append_message(conv["id"], {"role": "assistant", "content": "", "status": "running", "kind": "pending"})
    tid = tasks.start("ask", lambda t: _run_ask(conv["id"], ph["id"], question, history))
    return jsonify({"conv_id": conv["id"], "msg_id": ph["id"], "task_id": tid})


@api.post("/api/deep")
def api_deep():
    data = request.get_json(force=True, silent=True) or {}
    conv = sessions.get(data.get("conv_id") or "")
    msg = next((m for m in (conv or {}).get("messages", []) if m["id"] == data.get("msg_id")), None)
    if not conv or not msg:
        return jsonify({"error": "会话或消息不存在"}), 404
    idx = next(i for i, m in enumerate(conv["messages"]) if m["id"] == msg["id"])
    question = next((m["content"] for m in reversed(conv["messages"][:idx]) if m.get("role") == "user"), "")
    files = data.get("files") or None
    history = sessions.history_for_ask(conv["id"], config.CFG["ask"].get("history_turns", 4))
    ph = sessions.append_message(conv["id"], {"role": "assistant", "content": "", "status": "running", "kind": "pending"})
    tid = tasks.start("ask", lambda t: _run_deep(conv["id"], ph["id"], question, files, history))
    return jsonify({"conv_id": conv["id"], "msg_id": ph["id"], "task_id": tid})


# ---------- 会话 ----------

@api.get("/api/conversations")
def api_conversations():
    return jsonify({"items": sessions.list_conversations()})


@api.post("/api/conversations")
def api_conversations_new():
    conv = sessions.create()
    return jsonify({"conv": conv})


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
    items = []
    for c in sessions.list_conversations():
        conv = sessions.get(c["id"])
        for m in conv["messages"]:
            prop = m.get("proposal")
            if not prop or m.get("presented"):
                continue
            applied = set(m.get("applied_lines") or [])
            remaining = [ln for ln in prop.get("lines", []) if ln not in applied]
            if not remaining:
                continue
            sessions.update_message(conv["id"], m["id"], presented=True)
            items.append({"conv_id": conv["id"], "msg_id": m["id"],
                          "conv_title": conv["title"],
                          "proposal": {**prop, "lines": remaining,
                                       "lines_pretty": [anchor_pretty(ln) for ln in remaining]}})
    return jsonify({"items": items})


@api.post("/api/distill/apply")
def api_distill_apply():
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
            results.append({"ok": False, "msg_id": msg["id"], "error": str(e)})
            continue
        if not ret:
            results.append({"ok": False, "msg_id": msg["id"], "error": "没有可写入的行"})
            continue
        chosen = a.get("lines") or msg["proposal"].get("lines") or []
        applied = set(msg.get("applied_lines") or []) | set(chosen)
        sessions.update_message(conv["id"], msg["id"], applied_lines=sorted(applied))
        results.append({"ok": True, "msg_id": msg["id"], **ret})
    return jsonify({"results": results})


# ---------- 上传与摄入链 ----------

def _classify_task(names):
    def run(tid):
        results, targets = [], []
        for i, name in enumerate(names, 1):
            tasks.update(tid, current=i - 1, detail=f"第 {i}/{len(names)} 份：{name} 正在取名并进入资料库")
            p = config.INBOX_DIR / name
            if not p.exists():
                results.append({"ok": False, "file": name, "error": "文件不在收件台"})
                continue
            try:
                r = classify_one(p)
            except Exception as e:
                r = {"ok": False, "file": name, "error": str(e)}
            results.append(r)
            if r.get("ok"):
                targets.append(config.ROOT / r["target"])
        tasks.update(tid, current=len(names), detail="")
        if targets:
            tasks.start("ingest", _ingest_task(targets), total=len(targets), label=f"摄入 {len(targets)} 份")
        return results
    return run


def _ingest_task(paths):
    def run(tid):
        def progress(i, name):
            tasks.update(tid, current=i - 1, detail=f"第 {i}/{len(paths)} 份：{name} 正在摄入")
        results = ingest_run_all(paths, progress=progress)
        tasks.update(tid, current=len(paths), detail="")
        return results
    return run


@api.post("/api/upload")
def api_upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "没有收到文件"}), 400
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
    tid = tasks.start("classify", _classify_task(saved), total=len(saved), label=f"取名入库 {len(saved)} 份")
    return jsonify({"task_id": tid, "saved": saved, "rejected": rejected})


# ---------- 审校与修复 ----------

@api.post("/api/audit/run")
def api_audit_run():
    def run(tid):
        r = audit_mod.run(task_id=tid)
        return {"file": r["path"].name, "counts": r["counts"]}
    tid = tasks.start("audit", run, label="四类健康检查")
    return jsonify({"task_id": tid})


@api.get("/api/audit/latest")
def api_audit_latest():
    files = sorted(config.DATA_DIR.glob("audit-*.json"))
    if not files:
        return jsonify({"error": "还没有审校结果"}), 404
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    data["pages"] = [{
        "slug": p["slug"], "type": p["type"],
        "title": str(p["meta"].get("title") or p["slug"]),
    } for p in wiki.load_pages() if p["type"] != "source"]
    return jsonify(data)


@api.post("/api/audit/fix")
def api_audit_fix():
    data = request.get_json(force=True, silent=True) or {}
    actions = [a for a in (data.get("actions") or []) if isinstance(a, dict)]
    if not actions:
        return jsonify({"error": "没有勾选任何修复动作"}), 400
    tid = tasks.start("fix", lambda t: {"notes": fix_mod.apply_fixes(actions, task_id=t)},
                      total=len(actions), label=f"按审校报告修复 {len(actions)} 项")
    return jsonify({"task_id": tid})


# ---------- 已摄入管理（删除 / 列表）----------

@api.get("/api/ingest/list")
def api_ingest_list():
    """列出所有已摄入报告（用于删除 UI）。"""
    items = []
    for f in admin_mod.list_ingested():
        items.append({
            "file": f.get("file"),
            "sha256": f.get("sha256"),
            "path": f.get("path"),
            "ingested_at": f.get("ingested_at"),
            "topics_touched": f.get("topics_touched") or [],
            "entries_count": f.get("entries_count"),
            "source_type": f.get("source_type"),
        })
    return jsonify({"items": items})


@api.post("/api/ingest/delete")
def api_ingest_delete():
    """删除一份已摄入报告（manifest + 来源页 + 主题页 sources 联动）。"""
    data = request.get_json(force=True, silent=True) or {}
    filename = (data.get("file") or "").strip()
    sha = (data.get("sha256") or "").strip()
    if not filename and not sha:
        return jsonify({"error": "必须传 file 或 sha256"}), 400
    try:
        result = admin_mod.delete_ingested(filename=filename or None, sha256=sha or None)
    except Exception as e:
        log_exception = True
        return jsonify({"error": f"删除失败: {e}"}), 500
    if not result.get("found"):
        return jsonify({"error": result.get("message") or "未找到"}), 404
    return jsonify({"ok": True, **result})


# ---------- 任务 ----------

@api.get("/api/tasks")
def api_tasks():
    kinds = request.args.get("kinds")
    kinds = set(kinds.split(",")) if kinds else None
    return jsonify({"tasks": tasks.snapshot(kinds), "active": tasks.active_counts()})


@api.post("/api/tasks/<tid>/ack")
def api_task_ack(tid):
    tasks.acknowledge(tid)
    return jsonify({"ok": True})
