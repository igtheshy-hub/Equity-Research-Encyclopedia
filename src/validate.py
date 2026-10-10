from . import config, wiki
from .ingest import load_manifest


def validate_all():
    problems = []
    manifest = load_manifest()
    files_info = {f["file"]: f for f in manifest["files"]}
    pages = wiki.load_pages()
    titles = {str(p["meta"].get("title") or p["slug"]) for p in pages}
    for p in pages:
        name = p["path"].name
        meta = p["meta"]
        for field in ("title", "type", "sources", "updated"):
            if not meta.get(field):
                problems.append(f"{name}: 缺少必填 front matter 字段 {field}")
        if meta.get("type") and meta["type"] != p["type"]:
            problems.append(f"{name}: front matter type={meta['type']} 与所在目录 {p['type']} 不一致")
        for f, pg in sorted(p["anchors"]):
            if f not in files_info:
                problems.append(f"{name}: 锚点引用未摄入的文件 {f}")
            elif not 1 <= pg <= files_info[f]["page_count"]:
                problems.append(f"{name}: 锚点 {f}#p{pg} 页码越界（该文件共 {files_info[f]['page_count']} 页）")
        for link in sorted(wiki.wikilinks_of(p["body"])):
            if link not in titles:
                problems.append(f"{name}: wikilink 指向不存在的页面 [[{link}]]")
    idx = config.INDEX_MD.read_text(encoding="utf-8") if config.INDEX_MD.exists() else ""
    for p in pages:
        if f"{p['slug']}.md" not in idx:
            problems.append(f"{p['path'].name}: 未收录进 index.md")
    return problems
