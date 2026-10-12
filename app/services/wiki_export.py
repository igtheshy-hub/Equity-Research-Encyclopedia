"""把 SQLite 里的 KPage 同步导出为 Markdown 文件（与 Igshy 主仓对齐）。

每篇知识页在 `wiki/<type>/<slug>.md` 落一份 YAML frontmatter + Markdown 正文，
方便团队 git diff / pull request review / blame。

类型 → 目录：
  company  →  wiki/companies/
  industry →  wiki/industries/
  concept  →  wiki/concepts/
  source   →  wiki/sources/    （每份材料一份摘要）
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Iterable, Optional

from app.config import settings


# ---------------------------------------------------------------------------
# 类型 → 目录映射
# ---------------------------------------------------------------------------
_TYPE_DIRS = {
    "company": "companies",
    "industry": "industries",
    "concept": "concepts",
    "source": "sources",
}

# 节枚举（与 schema/CLAUDE.md §2 对齐）
SECTION_ENUMS = {
    "company": ["公司概况", "行业格局", "经营与财务", "盈利预测", "估值", "风险提示", "其他"],
    "industry": ["行业概况", "行业格局", "经营与财务", "盈利预测", "估值", "风险提示", "其他"],
    "concept": ["定义与口径", "方法要点", "政策背景", "应用示例", "其他"],
    "source": [],   # 来源页不强制分节
}


# ---------------------------------------------------------------------------
# path
# ---------------------------------------------------------------------------
def wiki_root() -> Path:
    """`wiki/` 目录（项目根下，git 可管理）。"""
    return settings.wiki_data_dir.parent / "wiki"


def page_path(type_: str, slug: str) -> Path:
    sub = _TYPE_DIRS.get(type_, "concepts")
    return wiki_root() / sub / f"{slug}.md"


def ensure_dirs() -> None:
    """建 `wiki/<type>/` 各子目录。"""
    root = wiki_root()
    for d in _TYPE_DIRS.values():
        (root / d).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 渲染 frontmatter + body
# ---------------------------------------------------------------------------
def _yaml_escape(v) -> str:
    """YAML frontmatter 字段的安全值。"""
    if v is None:
        return ""
    s = str(v)
    if any(ch in s for ch in [':', '#', '\n', '"', "'", '\t']) or s.startswith(("-", "?", "!", "&", "*")) or "[" in s or "]" in s or "{" in s or "}" in s:
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{s}"'
    return s


def _render_yaml(meta: dict) -> str:
    """把 dict 渲染为简单 YAML frontmatter。

    不引 PyYAML 以减少一个第三方依赖；只覆盖本项目用的标量 / 列表 / 嵌套 dict。
    """
    def emit(prefix: str, key: str, value) -> list[str]:
        if value is None or value == "" or value == []:
            return []
        if isinstance(value, bool):
            return [f"{prefix}{key}: {'true' if value else 'false'}"]
        if isinstance(value, (int, float)):
            return [f"{prefix}{key}: {value}"]
        if isinstance(value, str):
            return [f"{prefix}{key}: {_yaml_escape(value)}"]
        if isinstance(value, list):
            if not value:
                return []
            lines = [f"{prefix}{key}:"]
            for item in value:
                if isinstance(item, (str, int, float, bool)):
                    lines.append(f"{prefix}  - {_yaml_escape(item)}" if isinstance(item, str) else f"{prefix}  - {item}")
                elif isinstance(item, dict):
                    sub = emit(prefix + "  - ", "", item)
                    # 列表项 dict 用特殊前缀
                    flat = []
                    for k, v in item.items():
                        flat.extend([f"{prefix}    {k}: {_yaml_escape(v) if not isinstance(v, (int, float, bool)) else v}"])
                    lines.append(f"{prefix}  -")
                    lines.extend(flat)
                else:
                    lines.append(f"{prefix}  - {_yaml_escape(item)}")
            return lines
        if isinstance(value, dict):
            lines = [f"{prefix}{key}:"]
            for k, v in value.items():
                lines.extend(emit(prefix + "  ", k, v))
            return lines
        return [f"{prefix}{key}: {_yaml_escape(value)}"]

    lines: list[str] = []
    for k, v in meta.items():
        lines.extend(emit("", k, v))
    return lines


def render_markdown(kpage, sections: Iterable) -> str:
    """把 KPage + sections 渲染为 YAML frontmatter + body Markdown。"""
    type_ = kpage.type or "concept"
    title = kpage.title or kpage.slug

    meta = {
        "title": title,
        "type": type_,
    }
    sources_meta = []
    # KPage 自己 frontmatter 用 sources 表示数据来源（路径列表）
    if kpage.brief:
        meta["summary"] = kpage.brief
    if kpage.topic_tags:
        meta["aliases"] = kpage.topic_tags
    if type_ == "source" and kpage.source_filename:
        meta["sources"] = [f"raw/{kpage.source_filename}"]

    today = dt.date.today().isoformat()
    meta["updated"] = today

    body_lines: list[str] = [f"# {title}", ""]
    section_order = SECTION_ENUMS.get(type_, [])
    sections_by_name = {s.heading or "其他": s for s in sections}

    if type_ == "source":
        # 来源摘要页不强制分节：按 sections 顺序直接展开
        for s in sections:
            body_lines.extend(_section_to_lines(s))
    else:
        for name in section_order:
            if name in sections_by_name:
                body_lines.extend(_section_to_lines(sections_by_name[name]))
        # 多出节也保留
        for name in sorted(set(sections_by_name) - set(section_order)):
            body_lines.extend(_section_to_lines(sections_by_name[name]))

    fm = "\n".join(_render_yaml(meta)) + "\n"
    body = "\n".join(body_lines).rstrip() + "\n"
    return f"---\n{fm}---\n{body}"


def _section_to_lines(section) -> list[str]:
    out: list[str] = []
    if section.heading:
        out.append(f"## {section.heading}")
        out.append("")
    body = (section.body_md or "").rstrip()
    if body:
        out.append(body)
        out.append("")
    return out


# ---------------------------------------------------------------------------
# 写盘
# ---------------------------------------------------------------------------
def write_kpage(kpage, sections: Iterable) -> Optional[Path]:
    """把 KPage 导出为 Markdown 文件，返回路径；类型缺失则跳过。"""
    type_ = kpage.type or "concept"
    if type_ not in _TYPE_DIRS:
        return None
    ensure_dirs()
    path = page_path(type_, kpage.slug)
    text = render_markdown(kpage, sections)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def write_source_page(*, source_filename: str, title: str, source_topics: Iterable[str],
                       source_summary: str, source_date: str = "", file_relative_path: str = "") -> Optional[Path]:
    """为某份材料自动生成 `wiki/sources/<标题>.md` 摘要页（Igshy 同款）。"""
    ensure_dirs()
    slug = re.sub(r"[^\w一-鿿_-]+", "-", title).strip("-")[:80] or "未命名源"
    path = wiki_root() / "sources" / f"{slug}.md"
    if path.exists():
        # 同名摘要已存在，跳过；若想合并，把现有 history + 这次新增即可
        return path
    today = dt.date.today().isoformat()
    meta = {
        "title": title,
        "type": "source",
        "sources": [f"raw/{source_filename}"],
        "updated": today,
        "summary": source_summary,
        "aliases": [],
    }
    info_lines = [
        f"- 文件：`raw/{source_filename}`",
        f"- 报告日期：{source_date or '未识别'}",
    ]
    link_lines = ["- " + t for t in source_topics] or ["- （暂无）"]
    body_lines = [
        f"# {title}",
        "",
        "## 材料信息",
        "",
        *info_lines,
        "",
        "## 摘要",
        "",
        source_summary or "(暂无)",
        "",
        "## 贡献主题",
        "",
        *link_lines,
    ]
    fm = "\n".join(_render_yaml(meta)) + "\n"
    body = "\n".join(body_lines).rstrip() + "\n"
    text = f"---\n{fm}---\n{body}"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path
