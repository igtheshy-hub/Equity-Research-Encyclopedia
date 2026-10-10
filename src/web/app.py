import io
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import quote

import markdown as md_lib
from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, url_for)
from markupsafe import Markup

from .. import ask as ask_mod
from .. import config, wiki
from ..classify import classify_one, sanitize, unique_target
from ..ingest import iter_pdfs, load_manifest, run_all

app = Flask(__name__)

TYPE_LABELS = {"company": "公司", "industry": "行业", "concept": "概念", "source": "来源摘要"}
_LAST_ASK = {}


def _anchor_repl(m):
    fname, page = m.group(1), m.group(2)
    label = fname[:-4] if fname.lower().endswith(".pdf") else fname
    href = f"/pdf/{quote(fname)}#page={page}"
    return f'<sup class="cite"><a href="{href}" target="_blank" rel="noopener">{label}·p{page}</a></sup>'


def _wikilink_repl(m):
    t = m.group(1).strip()
    return f'<a class="wikilink" href="/find/{quote(t)}">{t}</a>'


def render_md(text):
    text = wiki.ANCHOR_RE.sub(_anchor_repl, str(text or ""))
    text = wiki.WIKILINK_RE.sub(_wikilink_repl, text)
    return md_lib.markdown(text, extensions=["tables"])


@app.template_filter("md")
def _md_filter(text):
    return Markup(render_md(text))


@app.context_processor
def _inject():
    return {"TYPE_LABELS": TYPE_LABELS}


@app.route("/")
def home():
    groups = []
    for t, label in TYPE_LABELS.items():
        items = []
        for p in wiki.load_pages():
            if p["type"] != t:
                continue
            e = wiki.build_index_entry(p)
            e["url"] = url_for("show_page", ptype=t, slug=p["slug"])
            items.append(e)
        groups.append({"label": label, "pages": items})
    return render_template("index.html", groups=groups)


@app.route("/page/<ptype>/<slug>")
def show_page(ptype, slug):
    try:
        path = wiki.slug_to_path(ptype, slug)
    except ValueError:
        abort(404)
    if not path.exists():
        abort(404)
    meta, body = wiki.parse_page(path)
    ctx = {
        "ptype": ptype,
        "slug": slug,
        "meta": meta,
        "body_html": render_md(body),
        "statements": sum(1 for ln in body.splitlines() if ln.strip().startswith("- ")),
    }
    return render_template("page.html", **ctx)


@app.route("/pdf/<name>")
def pdf(name):
    info = next((f for f in load_manifest()["files"] if f["file"] == name), None)
    if info is None:
        abort(404)
    p = (config.ROOT / info["path"]).resolve()
    if config.RAW_DIR.resolve() not in p.parents or not p.exists():
        abort(404)
    return send_file(p, mimetype="application/pdf")


@app.route("/find/<path:target>")
def find(target):
    for p in wiki.load_pages():
        title = str(p["meta"].get("title") or p["slug"])
        if target in (title, p["slug"]):
            return redirect(url_for("show_page", ptype=p["type"], slug=p["slug"]))
    abort(404)


@app.route("/ask", methods=["GET", "POST"])
def ask_view():
    result = token = error = None
    if request.method == "POST":
        q = request.form.get("question", "").strip()
        if not q:
            error = "问题不能为空"
        else:
            try:
                result = ask_mod.ask(q)
                token = uuid.uuid4().hex
                _LAST_ASK.clear()
                _LAST_ASK[token] = result
            except Exception as e:
                error = f"问答失败：{e}"
    return render_template("ask.html", result=result, token=token, error=error)


@app.route("/memo/save", methods=["POST"])
def memo_save():
    result = _LAST_ASK.get(request.form.get("token", ""))
    if result is None:
        return render_template("error.html", message="这次问答的结果已过期，请重新提问后再存备忘"), 400
    title = sanitize(request.form.get("title", "").strip()) or sanitize(result["question"][:30]) or "未命名"
    path = ask_mod.save_memo(result, title)
    return redirect(url_for("show_output", name=path.name))


@app.route("/upload")
def upload_page():
    return render_template("upload.html")


@app.route("/upload/do", methods=["POST"])
def upload_do():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"results": [{"ok": False, "file": "", "error": "没有收到文件"}]})
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for f in files:
        orig = f.filename or "未命名"
        if not orig.lower().endswith(".pdf"):
            results.append({"ok": False, "file": orig, "error": "只接受 PDF 文件"})
            continue
        stem = sanitize(Path(orig).stem) or "未命名"
        dest = config.INBOX_DIR / unique_target(config.INBOX_DIR, stem + ".pdf")
        f.save(dest)
        try:
            r = classify_one(dest)
        except Exception as e:
            r = {"ok": False, "file": dest.name, "error": str(e)}
        results.append(r)
    return jsonify({"results": results})


@app.route("/ingest")
def ingest_page():
    by_file = {f["file"]: f for f in load_manifest()["files"]}
    rows = []
    for p in iter_pdfs():
        info = by_file.get(p.name)
        rows.append({
            "rel": p.relative_to(config.ROOT).as_posix(),
            "ingested": bool(info),
            "page_count": info and info.get("page_count"),
            "report_date": info and info.get("report_date") or "",
            "entries": info and info.get("entries_count"),
        })
    inbox = sorted(p.name for p in config.INBOX_DIR.glob("*.pdf"))
    return render_template("ingest.html", rows=rows, inbox=inbox)


@app.route("/ingest/do", methods=["POST"])
def ingest_do():
    from .. import validate
    data = request.get_json(force=True, silent=True) or {}
    valid = {p.relative_to(config.ROOT).as_posix(): p for p in iter_pdfs()}
    if data.get("all_new"):
        known = {f["file"] for f in load_manifest()["files"]}
        targets = [p for rel, p in valid.items() if p.name not in known]
    else:
        targets = [valid[rel] for rel in (data.get("paths") or []) if rel in valid]
    if not targets:
        return jsonify({"results": [], "log": "没有选中可摄入的文件", "problems": []})
    buf = io.StringIO()
    with redirect_stdout(buf):
        results = run_all(targets)
        problems = validate.validate_all()
    return jsonify({"results": results, "log": buf.getvalue(), "problems": problems})


@app.route("/audit")
def audit_list():
    reports = sorted((p.name for p in config.OUTPUTS_DIR.glob("audit-*.md")), reverse=True)
    return render_template("audit.html", reports=reports)


@app.route("/audit/run", methods=["POST"])
def audit_run():
    from .. import audit
    r = audit.run()
    return jsonify({"file": r["path"].name, "counts": r["counts"]})


@app.route("/outputs")
def outputs_page():
    files = sorted(config.OUTPUTS_DIR.glob("*.md"), reverse=True)
    memos = [p.name for p in files if p.name.startswith("memo-")]
    reports = [p.name for p in files if not p.name.startswith("memo-")]
    return render_template("outputs.html", memos=memos, reports=reports)


@app.route("/outputs/<name>")
def show_output(name):
    if "/" in name or "\\" in name or ".." in name:
        abort(404)
    p = config.OUTPUTS_DIR / name
    if not p.is_file():
        abort(404)
    return render_template("report.html", name=name, html=render_md(p.read_text(encoding="utf-8")))


@app.route("/log")
def log_page():
    rows = []
    if config.LOG_MD.exists():
        for line in config.LOG_MD.read_text(encoding="utf-8").splitlines():
            if not line.startswith("|"):
                continue
            parts = [x.strip() for x in line.strip().strip("|").split("|")]
            if len(parts) >= 3:
                rows.append(parts[:3])
    rows.reverse()
    return render_template("log.html", rows=rows)
