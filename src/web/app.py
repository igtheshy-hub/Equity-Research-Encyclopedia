import re

from flask import (Flask, abort, redirect, render_template, request,
                   send_file, url_for)

from .. import config, purge, wiki
from ..docx_html import docx_to_html
from ..pdf_extract import DuplicateFileError, find_raw_file
from .api import api
from .render import TYPE_LABELS, render_md

app = Flask(__name__)
app.register_blueprint(api)


def _bootstrap():
    """新克隆的仓库运行数据是空的；启动时补齐目录与基础文件，首页直接可用。"""
    config.ensure_dirs()
    if not config.INDEX_MD.exists():
        wiki.rebuild_index_md(wiki.load_pages())
    if not config.LOG_MD.exists():
        config.LOG_MD.write_text("# 操作日志\n\n| 日期 | 操作 | 说明 |\n|---|---|---|\n", encoding="utf-8")


_bootstrap()


@app.errorhandler(404)
def _not_found(_e):
    return render_template("error.html", message="页面或文件不存在。"), 404


@app.errorhandler(DuplicateFileError)
def _duplicate_file(e):
    return render_template("error.html", message=str(e)), 409


@app.errorhandler(500)
def _server_error(_e):
    return render_template("error.html",
                           message="处理这个请求时出错了，其他页面不受影响；详情见命令行窗口。"), 500


@app.template_filter("md")
def _md_filter(text):
    from markupsafe import Markup
    from .render import md_filter
    return md_filter(text)


@app.context_processor
def _inject():
    return {"TYPE_LABELS": TYPE_LABELS,
            "MODEL_CHOICES": config.model_choices(),
            "MODEL_PROVIDER_CHOICES": config.provider_choices(),
            "MODEL_IDS": list(config.MODEL_PROFILES.keys()),
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
    entries = [e for g in groups for e in g["pages"]]
    stats = {
        "pages": len(entries),
        "sources": len({s for e in entries for s in e["source_files"]}),
        "updated": max((e["updated"] for e in entries if e["updated"]), default=""),
    }
    return render_template("kb.html", groups=groups, stats=stats)


@app.route("/page/<ptype>/<slug>")
def show_page(ptype, slug):
    try:
        path = wiki.slug_to_path(ptype, slug)
    except ValueError:
        abort(404)
    if not path.exists():
        abort(404)
    try:
        meta, body = wiki.parse_page(path)
    except (ValueError, OSError) as e:
        # 单页格式坏了只影响这一页，整站和导航照常；用 200 让浏览器直接显示中文说明，
        # 而不是再落进全站 500 兜底页
        return render_template("error.html",
                               message=f"知识库页面 {path.name} 格式损坏，无法显示：{e}")
    ctx = {
        "ptype": ptype,
        "slug": slug,
        "meta": meta,
        "body_html": render_md(body),
        "statements": sum(1 for ln in body.splitlines() if ln.strip().startswith("- ")),
        # 反链面板：渲染时现场计算（不缓存），tag 显示规范标题而非双链原文
        "backlinks": wiki.inbound_links(ptype, slug),
    }
    return render_template("page.html", **ctx)


@app.route("/file/<name>")
def raw_file(name):
    # PDF 原样发送（PDF.js 用）；docx 返回带块号的 HTML 片段（对照定位用）
    p = find_raw_file(name)  # 同名多份会抛 DuplicateFileError，由全局处理成中文提示
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
    if not name or find_raw_file(name) is None:
        abort(404)
    is_docx = name.lower().endswith(".docx")
    return render_template("viewer.html", file=name, loc=loc, hl=hl, is_docx=is_docx)


@app.route("/files")
def files_page():
    return render_template("files.html", rows=purge.file_entries())


@app.route("/find/<path:target>")
def find(target):
    # 解析顺序 title → slug → aliases（精确匹配），见 wiki.resolve_target
    tgt = wiki.resolve_target(target)
    if tgt is None:
        abort(404)
    return redirect(url_for("show_page", ptype=tgt[0], slug=tgt[1]))


@app.route("/ask")
def ask_page():
    return render_template("ask.html")


@app.route("/fetch")
def fetch_page():
    return render_template("fetch.html")


@app.route("/report")
def report_page():
    return render_template("industry_report.html")


@app.route("/audit")
def audit_list():
    # 报告清单由前端调 /api/audit/reports 动态渲染（新报告跑完即出现，不用刷新整页）
    return render_template("audit.html")


@app.route("/export", methods=["GET", "POST"])
def export_page():
    """Excel 导出：财报科目 / 派生指标 / 知识条目 / 来源汇总。

    GET  渲染勾选表单
    POST 按勾选生成 xlsx 并作为附件下载
    """
    import io as _io

    from .. import excel_export

    if not excel_export.available():
        return render_template("error.html",
                               message="导出 Excel 需要 openpyxl。请在项目目录运行："
                                       "python -m pip install -r requirements.txt，然后重启网页。"), 500
    from ..excel_export import available_companies, build_workbook, export_preview

    companies, periods = available_companies()
    preview = export_preview()

    if request.method == "POST":
        # 每张表勾选的列：表单里是 cols_<sheet>，收成 {"items": [...], ...}
        cols = {}
        for key in ("items", "indicators", "entries", "sources"):
            picked = request.form.getlist(f"cols_{key}")
            if picked:
                cols[key] = picked
        data = build_workbook(
            sheets=request.form.getlist("sheets") or ["items", "indicators", "entries", "sources"],
            types=request.form.getlist("types") or ["company", "industry", "concept"],
            keyword=request.form.get("keyword", "").strip(),
            date_from=request.form.get("date_from", "").strip(),
            date_to=request.form.get("date_to", "").strip(),
            companies=request.form.getlist("companies") or None,
            periods=request.form.getlist("periods") or None,
            cols=cols or None,
        )
        fname = f"wiki_export_{config.today_str().replace('-', '')}.xlsx"
        return send_file(
            _io.BytesIO(data),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=fname,
        )

    return render_template("export.html", companies=companies, periods=periods,
                           preview=preview)


@app.route("/outputs")
def outputs_page():
    """产出列表页。

    模板 outputs.html 一直在，但这条路由在重构时掉了 —— 顶部 nav 点「产出」直接 404。
    """
    files = sorted(config.OUTPUTS_DIR.glob("*.md"), reverse=True)
    reports = [p.name for p in files if not p.name.startswith(("memo-", "audit-"))]
    return render_template("outputs.html", reports=reports)


@app.route("/outputs/charts/<name>")
def output_chart(name):
    if "/" in name or "\\" in name or ".." in name:
        abort(404)
    p = config.OUTPUTS_DIR / "charts" / name
    if not p.is_file():
        abort(404)
    return send_file(p, mimetype="image/png")


@app.route("/outputs/download/<name>")
def output_download(name):
    if "/" in name or "\\" in name or ".." in name:
        abort(404)
    p = config.OUTPUTS_DIR / name
    if not p.is_file() or p.suffix.lower() not in (".docx", ".md"):
        abort(404)
    return send_file(p, as_attachment=True, download_name=name)


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
