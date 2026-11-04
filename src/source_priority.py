"""来源优先级：公司财报 > 公告/招股书 > 券商研报 > 政策及其他材料。

财报是公司自己披露的法定文件，同一指标与研报冲突时以最新报告期财报为准；
研报数字只能作对照，并注明差异与各自报告期。审校、问答、研报成文共用这里的口径。
"""
import json
import re
from pathlib import Path

from . import config

LABELS = {0: "财报", 1: "公告", 2: "研报", 3: "政策", 4: "其他"}
RULE = ("公司财务数据的来源优先级：财报（年报、半年报、季报）> 公告/招股书 > 券商研报 > 其他材料。"
        "同一公司财务指标冲突时，以最新报告期的财报为准；研报数字只能作对照，并注明差异与各自报告期。"
        "行业总量数据不套此序，统计局、行业协会、海关等一手来源优先于研报预测。")

_ANCHOR_RE = re.compile(r"\[\[([^\[\]]+?)#(p\d+|para\d+)\]\]")
_FILING_WORDS = ("财报", "年报", "季报", "半年报", "三季报", "年度报告", "季度报告")
_ANNOUNCE_WORDS = ("公告", "招股书", "招股说明书", "募集说明", "上市公告")


def _manifest_map(manifest=None):
    if manifest is None:
        try:
            manifest = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {"files": []}
    out = {}
    for f in manifest.get("files") or []:
        if f.get("file"):
            out[f["file"]] = f
        if f.get("path"):
            out[f["path"]] = f
    return out


def _name_of(ref):
    s = str(ref or "").strip()
    m = _ANCHOR_RE.search(s)
    if m:
        s = m.group(1)
    s = s.split("#", 1)[0]
    return Path(s).name


def classify(ref, manifest=None):
    """返回 filing / announcement / report / policy / other。"""
    name = _name_of(ref)
    entry = _manifest_map(manifest).get(name) or {}
    path = str(entry.get("path") or "")
    low = name.casefold()
    if path.startswith("raw/filings/") or path.startswith("raw\\filings\\"):
        return "filing"
    if "研报" in low:
        return "report"
    if path.startswith("raw/policies/") or path.startswith("raw\\policies\\"):
        return "policy"
    if "数据快照" in path or "数据快照" in name:
        return "other"
    if any(w in low for w in _ANNOUNCE_WORDS):
        return "announcement"
    if any(w in low for w in _FILING_WORDS):
        return "filing"
    kind = entry.get("source_type")
    if kind == "filing":
        return "filing"
    if kind == "report":
        return "report"
    if kind == "policy":
        return "policy"
    return "other"


_RANK = {"filing": 0, "announcement": 1, "report": 2, "policy": 3, "other": 4}


def rank(ref, manifest=None):
    return _RANK[classify(ref, manifest)]


def label(ref, manifest=None):
    return LABELS[rank(ref, manifest)]


def prefer(a, b, manifest=None):
    """返回 'a'、'b' 或 'tie'：优先级高者胜。"""
    ra, rb = rank(a, manifest), rank(b, manifest)
    if ra < rb:
        return "a"
    if rb < ra:
        return "b"
    return "tie"


def page_legend(page, manifest=None, limit=8):
    """页面来源构成，供问答与成文提示词判断哪条锚点更可信。"""
    files = []
    seen = set()
    for name, _loc in _ANCHOR_RE.findall(str(page.get("body") or "")):
        name = Path(name).name
        if name not in seen:
            seen.add(name)
            files.append(name)
    if not files:
        return ""
    groups = {}
    for name in files:
        groups.setdefault(label(name, manifest), []).append(name)
    order = ["财报", "公告", "研报", "政策", "其他"]
    parts = []
    for lab in order:
        names = groups.get(lab) or []
        if names:
            shown = "、".join(names[:limit])
            more = f" 等 {len(names)} 份" if len(names) > limit else ""
            parts.append(f"{lab}（{shown}{more}）")
    text = "来源类型：" + "；".join(parts)
    if groups.get("财报") and any(lab != "财报" for lab in groups):
        text += "。冲突时以财报为准"
    return text


def conflict_note(anchor_a, anchor_b, manifest=None):
    la, lb = label(anchor_a, manifest), label(anchor_b, manifest)
    p = prefer(anchor_a, anchor_b, manifest)
    if p == "a":
        note = f"来源优先级：A 为{la}、B 为{lb}，以 A 为准"
    elif p == "b":
        note = f"来源优先级：A 为{la}、B 为{lb}，以 B 为准"
    else:
        note = f"来源同为{la}，按报告期判断新旧"
    return {"source_a": la, "source_b": lb, "prefer": p, "source_note": note}
