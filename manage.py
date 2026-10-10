import argparse
import importlib
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src import config


def cmd_selftest(args):
    ok = True
    print("== 依赖检查 ==")
    for mod in ("httpx", "pdfplumber", "flask", "dotenv", "yaml", "markdown"):
        try:
            importlib.import_module(mod)
            print(f"  [好] {mod}")
        except ImportError as e:
            ok = False
            print(f"  [缺] {mod}: {e}")
    print("== 配置检查 ==")
    if config.ZHIPU_API_KEY:
        print("  [好] 已从 .env 读到 ZHIPU_API_KEY")
    else:
        ok = False
        print("  [缺] ZHIPU_API_KEY 为空：复制 .env.example 为 .env 并填入智谱 API Key（https://open.bigmodel.cn）")
    config.ensure_dirs()
    print("  [好] 目录结构就绪")
    if not ok:
        print("\nselftest 未通过（见上），修好后重跑。")
        return 1
    print("== GLM 连通性 ==")
    from src import llm
    t0 = time.monotonic()
    try:
        reply = llm.chat([{"role": "user", "content": "请只回复两个字：正常"}], task="select")
    except llm.LLMError as e:
        print(f"  [败] {e}")
        print("  若为模型名问题，改 config.json 的 llm.model 后重试。")
        return 1
    dt = time.monotonic() - t0
    print(f"  [好] {config.CFG['llm']['model']} 响应 {dt:.1f}s：{reply.strip()[:30]}")
    print("== 库外问答检查 ==")
    from src import ask as ask_mod
    r = ask_mod.ask("马斯克的火星殖民计划最新进展如何？", distill=False)
    if r["answer_md"].strip() == ask_mod.REFUSAL:
        print("  [好] 知识库外问题正确走了拒绝路径")
    else:
        print(f"  [败] 库外问题未拒绝：{r['answer_md'][:60]}")
        return 1
    print("== 全库校验 ==")
    from src import validate
    problems = validate.validate_all()
    if problems:
        for x in problems:
            print(f"  [问题] {x}")
        return 1
    print("  [好] 全库校验通过")
    print("\nselftest 通过。")
    return 0


def cmd_sort(args):
    from src.classify import classify_inbox
    classify_inbox()
    return 0


def _iter_pdfs():
    for d in (config.REPORTS_DIR, config.FILINGS_DIR, config.POLICIES_DIR):
        yield from sorted(d.rglob("*.pdf"))


def cmd_ingest(args):
    from src import pdf_extract
    if args.file:
        targets = [Path(args.file).resolve()]
    else:
        targets = list(_iter_pdfs())
    if not targets:
        print("raw/ 正式目录下没有 PDF。先跑收件台（python manage.py sort）或直接把文件放进 raw/ 对应目录。")
        return 0
    for p in targets:
        print(f"== {p.name} ==")
        try:
            pages, n, scanned = pdf_extract.extract_to_store(p)
        except Exception as e:
            print(f"  提取失败: {e}")
            continue
        print(f"  页数: {n}  逐页文本: data/extracts/{p.stem}.pages.json")
        if scanned:
            print("  [拒] 疑似扫描版（有效页平均字符过少），不会进入编译")
        else:
            preview = next((pg["text"] for pg in pages if pg["text"]), "")
            print(f"  首个有效页预览: {preview[:120]}")
    if not args.dry_run:
        from src import ingest
        results = ingest.run_all(targets)
        if results:
            print(f"\n完成 {len(results)} 份材料的摄入，详情见 wiki/log.md")
    return 0


def cmd_ask(args):
    from src import ask as ask_mod
    result = ask_mod.ask(args.question)
    sel = result["step1"]
    print(f"问题：{result['question']}")
    print(f"选页：{'、'.join(sel['selected']) or '（无）'}")
    if sel["reason"]:
        print(f"理由：{sel['reason']}")
    print()
    print(result["answer_md"])
    if not result["validation"]["ok"]:
        print(f"\n[警告] 答案锚点校验未通过：{'；'.join(result['validation']['violations'])}")
    if result.get("distill"):
        d = result["distill"]
        print(f"\n[沉淀] 新知识 {d['lines']} 条写入 {d['page']}，详见 wiki/log.md")
    if args.save:
        path = ask_mod.save_memo(result, args.save)
        print(f"\n备忘已存：{path.relative_to(config.ROOT).as_posix()}")
    return 0


def cmd_audit(args):
    from src import audit
    r = audit.run()
    print("审校完成：")
    for k, v in r["counts"].items():
        print(f"  {k}: {v}")
    print(f"报告：{r['path'].relative_to(config.ROOT).as_posix()}")
    return 0


def cmd_validate(args):
    from src import validate
    problems = validate.validate_all()
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for x in problems:
            print(f"  - {x}")
        return 1
    print("全库校验通过。")
    return 0


def cmd_serve(args):
    from src.web.app import app
    print("启动本地网页：http://127.0.0.1:5000 （Ctrl+C 停止）")
    app.run(host="127.0.0.1", port=5000, debug=False)
    return 0


def main():
    ap = argparse.ArgumentParser(prog="manage.py", description="研报百科管理入口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("selftest", help="体检：依赖、配置、GLM 连通")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("sort", help="收件台：归类 raw/inbox/ 中的新文件")
    p.set_defaults(func=cmd_sort)

    p = sub.add_parser("ingest", help="摄入 raw/ 中的材料")
    p.add_argument("--file", help="指定单个 PDF 路径")
    p.add_argument("--all", action="store_true", help="扫描 raw/ 全部材料（默认行为）")
    p.add_argument("--dry-run", action="store_true", help="只做提取预览，不调用 LLM 编译")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("ask", help="两步问答")
    p.add_argument("question")
    p.add_argument("--save", metavar="标题", help="把问答结果存为 outputs/ 研究备忘")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("audit", help="四类审校")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("validate", help="全库格式校验")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("serve", help="启动本地网页")
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
