import json
from datetime import datetime
from pathlib import Path
from queue import Queue

from flask import Blueprint, jsonify, request

from .. import ask as ask_mod
from .. import audit as audit_mod
from .. import config, llm, wiki
from .. import fix as fix_mod
from .. import purge as purge_mod
from .. import report as report_mod
from .. import sessions, tasks
from .. import ingest as ingest_mod
from ..classify import classify_one, sanitize, unique_target
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

def _model_arg(data):
    """请求里带的模型 id，返回 (模型 id 或 None, 错误响应)。空 = 用 config.json 的默认模型。"""
    name = str(data.get("model") or "").strip()
    if not name or name == config.DEFAULT_MODEL:
        return None, None
    if name not in config.MODEL_PROFILES:
        return None, (jsonify({"error": f"未知模型：{name}"}), 400)
    return name, None


def _run_ask(cid, mid, question, history, model):
    with llm.use_model(model):
        return _ask_body(cid, mid, question, history, model)


def _ask_body(cid, mid, question, history, model):
    try:
        result = ask_mod.ask(question, history=history, propose=True)
    except Exception as e:
        sessions.update_message(cid, mid, status="error", kind="error", content=f"问答失败：{e}")
        raise
    if result["kind"] == "report":
        title = result["title"]
        domain = result.get("domain") or "general"

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
    tid = tasks.start("ask", lambda t: _run_ask(conv["id"], ph["id"], question, history, model))
    return jsonify({"conv_id": conv["id"], "msg_id": ph["id"], "task_id": tid})


@api.post("/api/deep")
def api_deep():
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
    tid = tasks.start("ask", lambda t: _run_deep(conv["id"], ph["id"], question, files, history, model))
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

def _pipeline(names):
    """取名线与摄入线并行：取名线每入库一份就把路径塞进队列，摄入线同时取走摄入，两条线互不等待。"""
    queue = Queue()

    def classify_line(tid):
        results = []
        try:
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
                    queue.put(config.ROOT / r["target"])
                tasks.update(tid, current=i)
        finally:
            queue.put(None)  # 收工信号：摄入线做完队列里剩下的就退出
        tasks.update(tid, current=len(names), detail="")
        return results

    def ingest_line(tid):
        results = []
        done = 0
        while True:
            target = queue.get()
            if target is None:
                break
            done += 1
            tasks.update(tid, current=done - 1, detail=f"第 {done} 份：{target.name} 正在摄入知识点")
            manifest = ingest_mod.load_manifest()
            before = {f["sha256"] for f in manifest["files"]}
            try:
                r = ingest_mod.ingest_one(target, manifest)
            except Exception as e:
                print(f"  摄入失败: {e}")
                r = {"file": target.name, "error": str(e)}
            if r:
                results.append(r)
            # 逐份落盘，且只追加本轮新增的条目：删除线并发删掉的条目不会被这份旧清单冲回来
            ingest_mod.append_manifest([f for f in manifest["files"] if f["sha256"] not in before])
            tasks.update(tid, current=done)
        tasks.update(tid, current=len(names), detail="")
        ingest_mod.finish(ingest_mod.load_manifest(), [r for r in results if "error" not in r])
        return results

    return classify_line, ingest_line


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
    classify_line, ingest_line = _pipeline(saved)
    tid = tasks.start("classify", classify_line, total=len(saved), label=f"取名入库 {len(saved)} 份")
    tasks.start("ingest", ingest_line, total=len(saved), label=f"摄入 {len(saved)} 份")
    return jsonify({"task_id": tid, "saved": saved, "rejected": rejected})


# ---------- 原文件 ----------

@api.get("/api/files")
def api_files():
    return jsonify({"items": purge_mod.file_entries()})


@api.post("/api/files/delete")
def api_files_delete():
    data = request.get_json(force=True, silent=True) or {}
    names = [str(n).strip() for n in (data.get("files") or []) if str(n).strip()]
    if not names:
        return jsonify({"error": "没有选中要删除的文件"}), 400
    tid = tasks.start("delete", lambda t: purge_mod.purge_files(names, task_id=t),
                      total=len(names), label=f"删除 {len(names)} 份原文件")
    return jsonify({"task_id": tid})


# ---------- 审校与修复 ----------

@api.post("/api/audit/run")
def api_audit_run():
    def run(tid):
        r = audit_mod.run(task_id=tid)
        return {"file": r["path"].name, "counts": r["counts"]}
    tid = tasks.start("audit", run, label="四类健康检查")
    return jsonify({"task_id": tid})


def _ts_pretty(ts):
    """审校文件名里的时间戳 20261013-222015 → 2026-10-13 22:20。"""
    try:
        return datetime.strptime(str(ts), "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


@api.get("/api/audit/latest")
def api_audit_latest():
    files = sorted(config.DATA_DIR.glob("audit-*.json"))
    if not files:
        return jsonify({"error": "还没有审校结果"}), 404
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    data["ts_pretty"] = _ts_pretty(data.get("ts"))
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
