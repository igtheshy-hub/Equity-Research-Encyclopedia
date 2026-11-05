from . import config, wiki
from .pdf_extract import duplicate_files
from .wiki_check import (  # noqa: F401  重新导出，兼容既有调用（tests/test_filing_rules.py 等）
    LINE_PK_RE,
    PERSONAL_VIEW_RE,
    _line_format_problems,
    _management_problems,
    content_problems,
    files_info,
)


def validate_all():
    problems = []
    # 坏页面单独报出来，而不是解析失败就整站中断；其他页面继续校验
    for path, err in wiki.check_pages():
        problems.append(f"{path.relative_to(config.ROOT).as_posix()}: {err}")
    info = files_info()
    unit_cache = {}
    pages = wiki.load_pages()
    titles = {str(p["meta"].get("title") or p["slug"]) for p in pages}
    for name, paths in sorted(duplicate_files().items()):
        where = "、".join(p.relative_to(config.ROOT).as_posix() for p in paths)
        problems.append(f"原文件同名多份：{name}（{where}），锚点无法区分，请改名后重新摄入")
    for p in pages:
        problems.extend(content_problems(p["path"].name, p["meta"], p["body"],
                                         expected_type=p["type"], info=info, titles=titles,
                                         unit_cache=unit_cache))
    idx = config.INDEX_MD.read_text(encoding="utf-8") if config.INDEX_MD.exists() else ""
    for p in pages:
        if f"{p['slug']}.md" not in idx:
            problems.append(f"{p['path'].name}: 未收录进 index.md")
    return problems
