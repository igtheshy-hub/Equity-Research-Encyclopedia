"""报告图表渲染：matplotlib 出 PNG，存 outputs/charts/，中文用系统字体。

图表 spec 由成文阶段的大模型给，字段：
  {title, kind: line|bar|hbar|pie, x: [...], series: [{name, values: [...]}],
   xlabel, ylabel, unit, note}
数值允许带逗号、% 的字符串；解析不了的整点跳过。
"""
import re

from . import config

PALETTE = ["#2b5bd7", "#8aa4e8", "#1e9e62", "#e0a13a", "#cf3c3c", "#7a5bd7"]


def _num(v):
    s = str(v if v is not None else "").replace(",", "").replace("％", "%").strip()
    s = s.rstrip("%").replace("−", "-")
    try:
        return float(s)
    except ValueError:
        return None


def _setup_font():
    import matplotlib
    from matplotlib import font_manager

    names = {f.name for f in font_manager.fontManager.ttflist}
    for name in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC"):
        if name in names:
            matplotlib.rcParams["font.sans-serif"] = [name]
            break
    matplotlib.rcParams["axes.unicode_minus"] = False


def _clean_name(title):
    from .classify import sanitize

    return (sanitize(title)[:30] or "图").strip()


def render(spec, number=1):
    """渲染单张图，返回 {title, file, path, note}；数据不足返回 None。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _setup_font()

    x = [str(v) for v in (spec.get("x") or [])]
    series = []
    for s in (spec.get("series") or []):
        vals = [_num(v) for v in (s.get("values") or [])]
        if any(v is not None for v in vals):
            series.append({"name": str(s.get("name") or ""), "values": vals})
    if not series:
        return None
    n = max(len(x), *(len(s["values"]) for s in series))
    if n == 0:
        return None
    x = x + [str(i + 1) for i in range(len(x), n)]
    series = [{"name": s["name"], "values": (s["values"] + [None] * n)[:n]} for s in series]

    kind = spec.get("kind") if spec.get("kind") in ("line", "bar", "hbar", "pie") else "bar"
    fig, ax = plt.subplots(figsize=(6.6, 3.6), dpi=200)
    title = re.sub(r"^(?:附?图)\s*\d+\s*[·.、:：-]?\s*", "",
                   str(spec.get("title") or "").strip()) or f"附图{number}"
    unit = str(spec.get("unit") or "")

    if kind == "pie":
        vals = [v if v is not None else 0 for v in series[0]["values"]]
        labels = [f"{lab} {v:g}{unit}" for lab, v in zip(x, vals)]
        ax.pie(vals, labels=labels, autopct="%1.0f%%", startangle=90,
               colors=PALETTE[:len(vals)], textprops={"fontsize": 9})
        ax.axis("equal")
    elif kind == "line":
        for i, s in enumerate(series):
            ax.plot(range(n), s["values"], marker="o", markersize=4, linewidth=1.8,
                    color=PALETTE[i % len(PALETTE)], label=s["name"] or None)
        ax.set_xticks(range(n))
        ax.set_xticklabels(x, rotation=45 if n > 8 else 0, ha="right" if n > 8 else "center")
        ax.grid(axis="y", color="#e8ecf3", linewidth=0.8)
        if len(series) > 1:
            ax.legend(frameon=False, fontsize=9)
    else:
        import numpy as np

        width = 0.8 / len(series)
        idx = np.arange(n)
        for i, s in enumerate(series):
            vals = [v if v is not None else 0 for v in s["values"]]
            if kind == "hbar":
                ax.barh(idx + (i - (len(series) - 1) / 2) * width, vals, height=width * 0.92,
                        color=PALETTE[i % len(PALETTE)], label=s["name"] or None)
            else:
                ax.bar(idx + (i - (len(series) - 1) / 2) * width, vals, width=width * 0.92,
                       color=PALETTE[i % len(PALETTE)], label=s["name"] or None)
        if kind == "hbar":
            ax.set_yticks(idx)
            ax.set_yticklabels(x)
            ax.grid(axis="x", color="#e8ecf3", linewidth=0.8)
        else:
            ax.set_xticks(idx)
            ax.set_xticklabels(x, rotation=45 if n > 8 else 0, ha="right" if n > 8 else "center")
            ax.grid(axis="y", color="#e8ecf3", linewidth=0.8)
        if len(series) > 1:
            ax.legend(frameon=False, fontsize=9)

    ax.set_title(title, fontsize=12, pad=12)
    if kind != "pie":
        if spec.get("xlabel"):
            ax.set_xlabel(str(spec["xlabel"]), fontsize=9.5)
        if spec.get("ylabel") or unit:
            ax.set_ylabel(str(spec.get("ylabel") or unit), fontsize=9.5)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)

    out_dir = config.OUTPUTS_DIR / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"附图{number}-{_clean_name(title)}.png"
    path = out_dir / fname
    i = 2
    while path.exists():
        path = out_dir / f"附图{number}-{_clean_name(title)}-{i}.png"
        i += 1
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return {"title": title, "file": path.name,
            "path": path.relative_to(config.ROOT).as_posix(),
            "note": str(spec.get("note") or "")}


def render_all(specs, limit=None):
    results = []
    for spec in specs or []:
        if limit is not None and len(results) >= limit:
            break
        try:
            got = render(spec, number=len(results) + 1)
        except Exception:
            continue
        if got:
            results.append(got)
    return results


def insert_into_report(report_md, charts):
    """把图表以「附图」节追加到报告末尾，每图带标题、图片与来源注。

    编号用「附图N」与正文里的图占位编号（图N）区分：正文占位是写作建议，
    实际渲染的图按渲染顺序独立编号，两套不互相对应。
    """
    if not charts:
        return report_md
    parts = ["\n## 附图\n"]
    for n, c in enumerate(charts, 1):
        note = f"\n数据来源：{c['note']}\n" if c.get("note") else "\n"
        parts.append(f"\n### 附图{n}·{c['title']}\n\n![]({ 'charts/' + c['file'] })\n{note}")
    return report_md.rstrip() + "\n" + "".join(parts)
