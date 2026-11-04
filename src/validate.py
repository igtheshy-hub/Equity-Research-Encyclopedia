from . import config, wiki
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
    info = files_info()
    unit_cache = {}
    pages = wiki.load_pages()
    titles = {str(p["meta"].get("title") or p["slug"]) for p in pages}
    for p in pages:
        problems.extend(content_problems(p["path"].name, p["meta"], p["body"],
                                         expected_type=p["type"], info=info, titles=titles,
                                         unit_cache=unit_cache))
    idx = config.INDEX_MD.read_text(encoding="utf-8") if config.INDEX_MD.exists() else ""
    for p in pages:
        if f"{p['slug']}.md" not in idx:
            problems.append(f"{p['path'].name}: 未收录进 index.md")
    return problems
