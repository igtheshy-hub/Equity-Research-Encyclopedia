#!/usr/bin/env python
"""研报团队百科 — 统一入口。

用法（在项目根目录）：
    python manage.py serve               # 启动网站（默认 127.0.0.1:5000）
    python manage.py serve --port 8765  # 临时改端口
    python manage.py serve --reload     # 开发模式：自动重载
    python manage.py audit              # 立即跑一次审校
    python manage.py seed --pdf <path>  # 端到端跑示例 PDF
    python manage.py rebuild-fts        # 重建 FTS5 索引
    python manage.py init-db            # 显带表
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

# 设计原则：
# - 进程入口唯一，协作者只需记 `python manage.py <cmd>`
# - 默认 5000 端口以与 Igshy 信中描述一致
# - 每个子命令独立、易测

import argparse
import datetime as dt
import sys
from pathlib import Path

# 让 `python manage.py <cmd>` 在不安装包的前提下也能 import app.*
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def cmd_serve(args: argparse.Namespace) -> int:
    """启动 FastAPI 服务。"""
    import uvicorn

    from app.config import settings

    host = args.host or settings.wiki_host
    port = args.port or settings.wiki_port
    reload_flag = bool(args.reload)

    print(f"\n  Research Wiki running on http://{host}:{port}\n")
    if reload_flag:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            reload=True,
            reload_dirs=[str(ROOT / "app"), str(ROOT / "templates"), str(ROOT)],
        )
    else:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            reload=False,
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """列出所有 Report 的当前状态，便于排查 stuck / extracting / pending_llm_retry。"""
    from app.models.db import init_db, session_scope
    from app.models.orm import Report
    from collections import Counter

    init_db()
    with session_scope() as s:
        reports = s.query(Report).order_by(Report.id).all()
        if not reports:
            print("没有任何 Report。")
            return 0
        now = dt.datetime.now(dt.UTC)
        print(f"{'ID':>4}  {'状态':<22}  {'页':>3}  {'最后动作':<10}  文件")
        print("-" * 90)
        for r in reports:
            last = r.ingested_at or r.created_at
            age = ""
            if last:
                # SQLite 返回 naive，标准化为 aware 后再算差
                if last.tzinfo is None:
                    last_aware = last.replace(tzinfo=dt.UTC)
                else:
                    last_aware = last
                delta = now - last_aware
                if delta.days > 0:
                    age = f"{delta.days}d前"
                elif delta.seconds > 3600:
                    age = f"{delta.seconds // 3600}h前"
                elif delta.seconds > 60:
                    age = f"{delta.seconds // 60}m前"
                else:
                    age = "刚才"
            err = (r.error or "")[:60]
            print(f"{r.id:>4}  {r.status:<22}  {r.page_count:>3}  {age:<10}  {r.original_filename}")
            if r.status == "pending_llm_retry" and err:
                print(f"      ↳ 错误: {err}")
            if r.status == "extracting":
                print(f"      ↳ 后台编译中… 若 5 分钟没动，可 `python manage.py reingest --report-id {r.id}`")
        cnt = Counter(r.status for r in reports)
        print()
        print("状态汇总:", dict(cnt))
        if cnt.get("extracting", 0) > 0:
            print(f"  → {cnt['extracting']} 份后台编译中（正常）。")
        if cnt.get("pending_llm_retry", 0) > 0:
            print(f"  → {cnt['pending_llm_retry']} 份 LLM 失败，重试: `python manage.py reingest --report-id N`")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """立即跑一次审校。"""
    from app.config import settings
    from app.models.db import init_db, session_scope
    from app.services.audit import run_audit

    logging_setup()
    init_db()
    with session_scope() as s:
        run = run_audit(s)
        print(f"\nrun_id = {run.id}  status = {run.status}")
        print(f"summary: {run.summary}")
        if run.error:
            print(f"error: {run.error}")
        return 0 if run.status == "succeeded" else 1


def cmd_selftest(args: argparse.Namespace) -> int:
    """体检：依赖、配置、当前 LLM provider 连通、库外问答回退、Markdown 目录就绪。"""
    from app.config import settings
    from app.models.db import init_db, Base, engine

    def _ok(msg: str) -> None:
        print(f"  [好] {msg}")
    def _missing(msg: str) -> None:
        print(f"  [缺] {msg}")

    print("== 依赖检查 ==")
    required = ["fastapi", "uvicorn", "sqlalchemy", "pydantic", "anthropic",
                "openai", "pymupdf", "jieba", "apscheduler", "httpx"]
    for m in required:
        try:
            __import__(m)
            _ok(m)
        except ImportError as e:
            _missing(f"{m}: {e}")

    print("== 配置检查 ==")
    init_db()
    _ok(f"data dir = {settings.wiki_data_dir}")
    _ok(f"db path = {settings.wiki_db_path}")
    _ok(f"LLM provider = {settings.llm_provider}, default model = {settings.llm_default_model}")
    api_key_map = {
        "claude": settings.anthropic_api_key,
        "openai_compat": settings.openai_api_key,
        "zhipu": settings.zhipu_api_key,
    }
    cur = (settings.llm_provider or "claude").lower()
    if api_key_map.get(cur):
        _ok(f"{settings.llm_provider} API key 已配置")
    else:
        _missing(f"{settings.llm_provider} API key 未配置（先去 .env 填）")

    print("== 目录检查（wiki/ 与 data/）==")
    from app.services import wiki_export
    wiki_export.ensure_dirs()
    _ok(f"wiki/ 目录就绪（{wiki_export.wiki_root()}）")

    print("== LLM provider 连通性（短 ping，可选）==")
    ans = input("  试调一次当前 provider？(y/N) ").strip().lower()
    if ans in ("y", "yes"):
        from app.llm.router import chat_with_fallback_sync
        from app.llm.base import LLMMessage, LLMOptions
        try:
            r = chat_with_fallback_sync(
                [LLMMessage(role="user", content="请只回一字：好")],
                options=LLMOptions(max_tokens=8),
            )
            _ok(f"回复：{r.content.strip()[:20]}")
        except Exception as e:  # noqa: BLE001
            _missing(f"调用失败：{e}")

    print("== 库外问答早返回路径 ==")
    from app.models.db import session_scope
    from app.services.qa import ask_question
    with session_scope() as s:
        r = ask_question("随便一句与知识库无关的提问", session=s)
        if r.out_of_scope and "知识库中暂无" in r.answer_md:
            _ok("库外问题正确返回早退回答")
        else:
            _missing(f"库外回答未走早退：{r.answer_md[:80]}")
    print("\nselftest 完成。")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """全库格式校验：KPage 必有 frontmatter、对照 Igshy schema 节枚举。"""
    init_db_safe()
    from app.models.db import session_scope
    from app.models.orm import KnowledgePage
    from app.services.citation import every_nontrivial_sentence_has_citation, ANCHOR_RE

    problems: list[str] = []
    with session_scope() as s:
        for kp in s.query(KnowledgePage).all():
            for sec in (kp.sections or []):
                ok, issues = every_nontrivial_sentence_has_citation(sec.body_md or "")
                if not ok:
                    problems.append(
                        f"{kp.slug} / {sec.heading or '未命名'}：{len(issues)} 句缺引用"
                    )
                # 锚点格式合法性
                for m in ANCHOR_RE.finditer(sec.body_md or ""):
                    fn = m.group(1).strip()
                    if "[" in fn or "]" in fn or "#" in fn or "|" in fn:
                        problems.append(f"{kp.slug}：文件名不安全 {fn!r}")
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("全库格式校验通过。")
    return 0


def init_db_safe() -> None:
    """小工具：init_db + 抑制日志噪音。"""
    from app.config import settings
    from app.models.db import init_db
    init_db()


def cmd_seed(args: argparse.Namespace) -> int:
    """用样例 PDF 端到端跑 ingest。"""
    from app.config import settings
    from app.models.db import init_db, session_scope
    from app.services.ingest import ingest_pdf_path

    logging_setup()
    init_db()
    if not args.pdf:
        print("[seed] 已建/校验数据库。给 --pdf 走完整流程。")
        return 0
    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"[seed] 找不到 PDF：{pdf}")
        return 2
    print(f"[seed] 正在摄取：{pdf}")
    with session_scope() as s:
        report = ingest_pdf_path(pdf, session=s)
        print(f"  · status={report.status} page_count={report.page_count}")
    return 0 if report.status == "compiled" else 0  # pending_llm_retry 视为可接受


def cmd_delete_report(args: argparse.Namespace) -> int:
    """删除一份已摄入的 Report（连带 Pages / Citations）。"""
    from app.config import settings
    from app.models.db import init_db, session_scope
    from app.models.orm import Report
    from app.services.admin import delete_report

    logging_setup()
    init_db()
    with session_scope() as s:
        targets: list = []
        if args.report_id:
            r = s.get(Report, args.report_id)
            if r is None:
                print(f"[删] 找不到 r.id={args.report_id}")
                return 1
            targets.append(r)
        elif args.all:
            targets = s.query(Report).order_by(Report.id).all()
            if not args.force:
                print(f"将删除 {len(targets)} 份报告（含所有 Pages + Citations）。")
                for r in targets:
                    print(f"  r.id={r.id:2}  {r.original_filename}")
                try:
                    ans = input("确认? 输入 yes 继续: ").strip()
                except EOFError:
                    ans = ""
                if ans != "yes":
                    print("取消。")
                    return 0
        else:
            print("用法：python manage.py delete-report --id N")
            print("      python manage.py delete-report --all  （需 --force 或交互确认）")
            return 1

        for r in targets:
            print(f"== r.id={r.id}  {r.original_filename}  status={r.status} ==")
            res = delete_report(s, r.id)
            print(f"  删 citations: {res.get('deleted_citations')}, pages: {res.get('deleted_pages')}")
            for ka in res.get("kpage_actions") or []:
                print(f"  kp.id={ka['kpage_id']} {ka['action']}: {ka['title']}")
    return 0


def cmd_rebuild_fts(args: argparse.Namespace) -> int:
    """重建 FTS5 索引。"""
    from app.config import settings
    from app.models.db import init_db, session_scope
    from app.services.retrieval import rebuild_index

    logging_setup()
    init_db()
    with session_scope() as s:
        n = rebuild_index(s)
    print(f"FTS5 已重建，条目数 = {n}")
    return 0


def cmd_init_db(args: argparse.Namespace) -> int:
    """显式建表。"""
    from app.config import settings
    from app.models.db import init_db

    logging_setup()
    init_db()
    print(f"数据库就绪：{settings.wiki_db_path}")
    return 0


def cmd_reingest(args: argparse.Namespace) -> int:
    """重新跑 pending_llm_retry 的报告（上次 LLM 编译失败，但 PDF 已抽取存 DB）。

    不重新做 PDF 抽取，直接拿页面文本再喂一次 LLM。
    现在支持批量 `--all`（处理所有 pending_llm_retry）和 `--report-id N`。
    """
    from app.config import settings
    from app.models.db import init_db, session_scope
    from app.models.orm import Report, Page
    from app.services.ingest import _compile_with_llm

    logging_setup()
    init_db()
    with session_scope() as s:
        q = s.query(Report)
        if args.report_id:
            q = q.filter(Report.id == args.report_id)
        elif args.all:
            q = q.filter(Report.status == "pending_llm_retry")
        else:
            print("用法：python manage.py reingest --all  或  --report-id N")
            return 1
        targets = q.all()
        if not targets:
            print("没有可重跑的报告。")
            return 0
        # 一次 session 内逐个跑（_compile_with_llm 各自独立 commit）
        ok = 0
        for r in targets:
            extract = None  # 我们构造轻量版 ExtractResult 复用 _compile_with_llm
            from app.services.pdf_extract import ExtractResult
            pages = s.query(Page).filter(Page.report_id == r.id).order_by(Page.page_no).all()
            from app.services.pdf_extract import ExtractedPage
            extract = ExtractResult(
                report_path=None,  # type: ignore
                sha256=r.sha256,
                page_count=len(pages),
                pages=[
                    ExtractedPage(
                        page_no=p.page_no, text=p.text,
                        ocr_used=p.ocr_used, ocr_engine=p.ocr_engine,
                        char_count=p.char_count,
                    )
                    for p in pages
                ],
                metadata={"broker": r.broker, "title": r.title,
                          "report_date": r.report_date.isoformat() if r.report_date else "",
                          "page_count": r.page_count},
            )
            try:
                _compile_with_llm(s, r, extract)
                # ingest_pdf_path 才会把 status 改 compiled；
                # reingest 是裸调用，自己写。
                r.status = "compiled"
                r.error = None
                r.ingested_at = dt.datetime.now(dt.UTC)
                s.add(r)
                ok += 1
                print(f"  [好] r.id={r.id} ({r.original_filename}) 重新编译成功")
            except Exception as e:  # noqa: BLE001
                r.status = "pending_llm_retry"
                r.error = repr(e)
                s.add(r)
                print(f"  [败] r.id={r.id}: {type(e).__name__}: {str(e)[:160]}")
        # session_scope 退出时统一 commit
    # 再单独 session 看最终状态
    with session_scope() as s:
        for r in s.query(Report).filter(Report.status == "pending_llm_retry").all():
            print(f"  pending r.id={r.id}: {r.error[:80] if r.error else ''}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """生成内置示例 PDF 并跑 ingest（不依赖外网）。"""
    from app.config import settings
    from app.models.db import init_db, session_scope
    from app.routes.demo_api import _build_sample_pdf, SAMPLE_PDF_FILENAME
    from app.services.ingest import ingest_pdf_path
    from sqlalchemy import select
    from app.models.orm import Report
    from app.config import settings
    from app.models.db import init_db, session_scope
    from app.routes.demo_api import _build_sample_pdf, SAMPLE_PDF_FILENAME
    from app.services.ingest import ingest_pdf_path
    from sqlalchemy import select
    from app.models.orm import Report

    logging_setup()
    init_db()
    pdf_dir = settings.wiki_data_dir / "reports"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdf_dir / SAMPLE_PDF_FILENAME

    # 已是 compiled → 复用
    with session_scope() as s:
        existing = s.execute(
            select(Report).where(Report.original_filename == SAMPLE_PDF_FILENAME)
        ).scalar_one_or_none()
        if existing and existing.status == "compiled":
            print(f"[demo] 已存在：report_id={existing.id}")
            return 0

    if not pdf.exists():
        _build_sample_pdf(pdf)
    with session_scope() as s:
        report = ingest_pdf_path(pdf, session=s)
        print(f"[demo] report_id={report.id} status={report.status} page_count={report.page_count}")
    return 0


def logging_setup() -> None:
    import logging

    from app.config import settings
    logging.basicConfig(
        level=settings.wiki_log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="研报团队百科 — 管理入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="启动网站")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--host", type=str, default=None)
    p_serve.add_argument("--reload", action="store_true",
                         help="开发模式：监听文件变更自动重载")
    p_serve.set_defaults(func=cmd_serve)

    sub.add_parser("audit", help="立即跑一次审校").set_defaults(func=cmd_audit)
    sub.add_parser("init-db", help="建表（含轻量 ALTER 迁移）").set_defaults(func=cmd_init_db)
    sub.add_parser("rebuild-fts", help="重建 FTS5 索引").set_defaults(func=cmd_rebuild_fts)
    sub.add_parser("status", help="列出所有 Report 当前状态（排查 stuck/extracting）").set_defaults(func=cmd_status)

    p_del = sub.add_parser("delete-report", help="删除一份已摄入的 Report（连带 Pages / Citations）")
    p_del.add_argument("--id", dest="report_id", type=int, default=None)
    p_del.add_argument("--all", action="store_true", help="批量；默认会交互确认")
    p_del.add_argument("--force", action="store_true", help="跳过确认（脚本里调）")
    p_del.set_defaults(func=cmd_delete_report)
    sub.add_parser("demo", help="导入内置示例 PDF").set_defaults(func=cmd_demo)
    sub.add_parser("selftest", help="体检：依赖、配置、连通、库外问答回退").set_defaults(func=cmd_selftest)

    p_re = sub.add_parser("reingest", help="重跑 pending_llm_retry 的报告（不重抽 PDF）")
    p_re.add_argument("--all", action="store_true", help="处理所有 pending_llm_retry")
    p_re.add_argument("--report-id", type=int, default=None)
    p_re.set_defaults(func=cmd_reingest)
    sub.add_parser("validate", help="全库格式校验").set_defaults(func=cmd_validate)

    p_seed = sub.add_parser("seed", help="摄取 PDF")
    p_seed.add_argument("--pdf", type=str, default=None)
    p_seed.add_argument("--question", type=str, default=None)
    p_seed.add_argument("--audit", action="store_true")
    p_seed.set_defaults(func=cmd_seed)

    args = parser.parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
