"""wiki 页面写入校验钩子：Claude 写/改 wiki/**.md 后由 PostToolUse 调起，把格式与锚点关把关。

stdin 收 Claude Code 的钩子 JSON（tool_input.file_path）；校验单页：front matter、行格式、
锚点真实性与越界、wikilink 指向。有问题 exit 2 并把问题清单打到 stderr 反馈给模型修正。
index 收录与全库一致性由 python manage.py validate 管，这里不查。

规则实现与网页写盘前校验共用 src/wiki_check.py，两边不会漂移。

用法（CLI 调试）：python .claude/hooks/page_check.py <页面路径>
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src import wiki_check  # noqa: E402


def main():
    # 钩子 JSON 以 UTF-8 进管道，Windows 下 stdin 默认 GBK，中文路径会被解坏
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 1:
        target = Path(sys.argv[1]).resolve()
    else:
        hook = json.loads(sys.stdin.read() or "{}")
        target = Path(str((hook.get("tool_input") or {}).get("file_path") or "")).resolve()
    try:
        target.relative_to(ROOT / "wiki")
    except ValueError:
        sys.exit(0)  # 不在 wiki/ 下，不归这个钩子管
    if target.name in ("index.md", "log.md") or not target.exists():
        sys.exit(0)  # 索引/日志页不校验；删除类操作不校验
    problems = wiki_check.check_page(target)
    if problems:
        print("wiki 页面校验未通过：", file=sys.stderr)
        for p in problems:
            print(f"- {p}", file=sys.stderr)
        print("请修正后重写。锚点只能引用已摄入文件、页码不得越界；无锚点内容不能写入 wiki。", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
