import argparse
import importlib
import sys
import time

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
    print("\nselftest 通过。")
    return 0


def cmd_ingest(args):
    print("摄入管道在后续步骤实现（当前为骨架）。")
    return 0


def cmd_ask(args):
    print("问答管道在后续步骤实现（当前为骨架）。")
    return 0


def cmd_audit(args):
    print("审校引擎在后续步骤实现（当前为骨架）。")
    return 0


def cmd_validate(args):
    print("全库校验在后续步骤实现（当前为骨架）。")
    return 0


def cmd_serve(args):
    print("网页界面在后续步骤实现（当前为骨架）。")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="manage.py", description="研报百科管理入口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("selftest", help="体检：依赖、配置、GLM 连通")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("ingest", help="摄入 raw/ 中的材料")
    p.add_argument("--file", help="指定单个 PDF 路径")
    p.add_argument("--all", action="store_true", help="扫描 raw/ 全部新材料")
    p.add_argument("--dry-run", action="store_true", help="只做提取预览，不调用 LLM")
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
