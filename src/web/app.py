import re

from flask import (Flask, abort, redirect, render_template, request,
                   send_file, url_for)

from .. import config, wiki
from ..docx_html import docx_to_html
from ..pdf_extract import raw_file_index
from .api import api
from .render import TYPE_LABELS, render_md

app = Flask(__name__)
app.register_blueprint(api)


@app.template_filter("md")
def _md_filter(text):
    from markupsafe import Markup
    from .render import md_filter
    return md_filter(text)


@app.context_processor
def _inject():
    return {"TYPE_LABELS": TYPE_LABELS,
            "MODEL_CHOICES": config.model_choices(),
            "DEFAULT_MODEL": config.DEFAULT_MODEL}


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/kb")
def kb():
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
    return render_template("kb.html", groups=groups)


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


@app.route("/file/<name>")
def raw_file(name):
    # PDF 原样发送（PDF.js 用）；docx 返回带块号的 HTML 片段（对照定位用）
    p = raw_file_index().get(name)
    if p is None:
        abort(404)
    if p.suffix.lower() == ".docx":
        return docx_to_html(p)
    return send_file(p, mimetype="application/pdf")


@app.route("/viewer")
def viewer():
    name = request.args.get("file", "").strip()
    loc = request.args.get("loc", "").strip()
    hl = request.args.get("hl", "").strip()
    if not name or name not in raw_file_index():
        abort(404)
    is_docx = name.lower().endswith(".docx")
    return render_template("viewer.html", file=name, loc=loc, hl=hl, is_docx=is_docx)


@app.route("/find/<path:target>")
def find(target):
    for p in wiki.load_pages():
        title = str(p["meta"].get("title") or p["slug"])
        if target in (title, p["slug"]):
            return redirect(url_for("show_page", ptype=p["type"], slug=p["slug"]))
    abort(404)


@app.route("/ask")
def ask_page():
    return render_template("ask.html")


@app.route("/audit")
def audit_list():
    reports = sorted((p.name for p in config.OUTPUTS_DIR.glob("audit-*.md")), reverse=True)
    return render_template("audit.html", reports=reports)


@app.route("/outputs")
def outputs_page():
    files = sorted(config.OUTPUTS_DIR.glob("*.md"), reverse=True)
    reports = [p.name for p in files if not p.name.startswith(("memo-", "audit-"))]
    return render_template("outputs.html", reports=reports)


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
    for p in wiki.load_pages():
        if p["type"] == "source":
            continue
        meta = p["meta"]
        hist = [str(h) for h in (meta.get("history") or [])]
        created = hist[0][:10] if hist else ""
        sources = sorted(set(re.findall(r"[\w一-鿿·()（）\-]+?\.(?:pdf|docx)", " ".join(hist))))
        if any("会话中提取" in h for h in hist):
            sources.append("会话中提取")
        title = str(meta.get("title") or p["slug"])
        tags = "、".join(str(a) for a in (meta.get("aliases") or [])) or "—"
        rows.append({
            "title": title,
            "type": TYPE_LABELS[p["type"]],
            "tags": tags,
            "summary": str(meta.get("summary") or "") or "—",
            "created": created or "—",
            "updated": str(meta.get("updated") or "") or "—",
            "sources": "、".join(sources) or "—",
            "url": url_for("show_page", ptype=p["type"], slug=p["slug"]),
            "search": " ".join([title, tags, str(meta.get("summary") or ""), " ".join(sources)]),
        })
    return render_template("log.html", rows=rows)
