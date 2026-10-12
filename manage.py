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
    for mod in ("httpx", "pdfplumber", "flask", "dotenv", "yaml", "markdown", "docx"):
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
    r = ask_mod.ask("马斯克的火星殖民计划最新进展如何？", propose=False)
    if r["kind"] == "referral":
        print("  [好] 知识库外问题正确走了 raw 文件名回退路径")
    else:
        print(f"  [败] 库外问题未走回退：{str(r['answer_md'])[:60]}")
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


def cmd_ingest(args):
    from src import pdf_extract
    from src.ingest import iter_raw_files
    if args.file:
        targets = [config.ROOT / args.file]
    else:
        targets = list(iter_raw_files())
    if not targets:
        print("raw/ 正式目录下没有可摄入文件。先跑收件台（python manage.py sort）或用网页上传。")
        return 0
    for p in targets:
        print(f"== {p.name} ==")
        try:
            units, count, scanned, unit_kind = pdf_extract.extract_to_store(p)
        except Exception as e:
            print(f"  提取失败: {e}")
            continue
        word = "段落/块" if unit_kind == "para" else "页"
        print(f"  {word}数: {count}  提取缓存: data/extracts/{p.stem}.units.json")
        if scanned:
            print("  [拒] 疑似扫描版（有效页平均字符过少），不会进入编译")
        else:
            preview = next((u["text"] for u in units if u["text"]), "")
            print(f"  首个有效单元预览: {preview[:120]}")
    if not args.dry_run:
        from src import ingest
        results = ingest.run_all(targets)
        if results:
            print(f"\n完成 {len(results)} 份材料的摄入，详情见 wiki/log.md")
    return 0


def cmd_ask(args):
    from src import ask as ask_mod
    result = ask_mod.ask(args.question)
    if result["kind"] == "report":
        from src import report as report_mod
        print(f"识别为报告请求《{result['title']}》，开始生成…")
        path = report_mod.generate(result["title"], args.question)
        print(f"报告已存：{path.relative_to(config.ROOT).as_posix()}")
        return 0
    sel = result["step1"]
    print(f"问题：{result['question']}")
    print(f"选页：{'、'.join(sel['selected']) or '（无）'}（{sel['reason']}）")
    print()
    print(result["answer_md"])
    if result["kind"] == "referral":
        print("\n（可改用网页端点击「进一步查询」对 raw 原文深查）")
    if not result["validation"]["ok"]:
        print(f"\n[警告] 答案锚点校验未通过：{'；'.join(result['validation']['violations'])}")
    prop = result.get("proposal")
    if prop:
        print(f"\n[提案] 检出 {len(prop['lines'])} 条可沉淀的新知识（"
              f"{'并入 ' + prop['target_title'] if prop['action'] == 'merge' else '新建' + prop['title']}）：")
        for ln in prop["lines"]:
            print(f"  - {ln}")
        try:
            ans = input("写入 wiki？[y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans in ("y", "yes"):
            d = ask_mod.apply_distill(prop)
            if d:
                print(f"[沉淀] {d['lines']} 条写入 {d['page']}，详见 wiki/log.md")
    return 0


def cmd_delete_report(args):
    """删除一份已摄入报告：manifest 条目 + 来源页 + 主题页 sources 联动。"""
    from src.admin import delete_ingested, list_ingested
    config.ensure_dirs()

    if args.all:
        targets = list_ingested()
        if not targets:
            print("没有已摄入的报告。")
            return 0
        print(f"将删除 {len(targets)} 份报告（manifest + 来源页 + 主题页 sources 联动）。")
        for f in targets:
            print(f"  {f['file']}  sha={f.get('sha256','')[:12]}...")
        try:
            ans = input("确认? 输入 yes 继续: ").strip()
        except EOFError:
            ans = ""
        if ans != "yes":
            print("取消。")
            return 0
        ok, fail = 0, 0
        for f in targets:
            r = delete_ingested(filename=f["file"])
            if r.get("found"):
                ok += 1
                print(f"  [删] {r['filename']}  sources清理={r.get('topic_pages_touched')}  锚点={r.get('anchors_removed')}")
            else:
                fail += 1
                print(f"  [跳] {f['file']}: {r.get('message')}")
        print(f"\n完成：成功 {ok} / 跳过 {fail}")
        return 0

    if not args.file and not args.sha:
        print("用法: python manage.py delete-report --file <文件名>")
        print("      python manage.py delete-report --sha <sha256>")
        print("      python manage.py delete-report --all  (默认会交互确认)")
        return 1

    if args.dry_run:
        # dry-run 模式：仅显示要删什么，不真删
        from src.admin import _find_manifest_entry
        e = _find_manifest_entry(filename=args.file, sha256=args.sha)
        if e is None:
            print(f"[dry-run] manifest 中未找到 {args.file or args.sha}")
            return 1
        print(f"[dry-run] 将删除：")
        print(f"  文件: {e['file']}")
        print(f"  path: {e.get('path')}")
        print(f"  sha256: {e.get('sha256')}")
        print(f"  触及主题: {', '.join(e.get('topics_touched') or [])}")
        print(f"  提取缓存: data/extracts/{Path(e['file']).stem}.units.json")
        return 0

    r = delete_ingested(filename=args.file, sha256=args.sha)
    if not r.get("found"):
        print(f"[删] {r.get('message')}")
        return 1
    print(f"已删除：{r['filename']}")
    print(f"  manifest 条目: {'删' if r['manifest_removed'] else '未变'}")
    print(f"  来源页 wiki/sources/: {'删' if r['source_page_deleted'] else '无'}")
    print(f"  主题页联动清理: {r['topic_pages_touched']} 个")
    print(f"  正文锚点清理: {r['anchors_removed']} 处")
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
    p.add_argument("--file", help="指定单个文件路径")
    p.add_argument("--all", action="store_true", help="扫描 raw/ 全部材料（默认行为）")
    p.add_argument("--dry-run", action="store_true", help="只做提取预览，不调用 LLM 编译")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("ask", help="两步问答")
    p.add_argument("question")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("audit", help="四类审校")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("delete-report", help="删除一份已摄入报告（联动清理 sources + 锚点）")
    p.add_argument("--file", help="按文件名删除")
    p.add_argument("--sha", help="按 sha256 删除")
    p.add_argument("--all", action="store_true", help="批量；默认会交互确认")
    p.add_argument("--dry-run", action="store_true", help="只看会删什么，不真删")
    p.set_defaults(func=cmd_delete_report)

    p = sub.add_parser("validate", help="全库格式校验")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("serve", help="启动本地网页")
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
