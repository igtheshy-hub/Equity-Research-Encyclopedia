"""AI 初审：在脚本筛查（audit）之上做第二轮「初审」。

审校脚本只会「找问题」；哪条更要紧、是不是真问题，原来全靠人工逐条看。
本模块加一层 AI 初判：**不删减任何筛查出的问题**，只给每条补上
1) 严重性（高/中/低）2) 性质判断（实质矛盾/口径不同/表述严谨问题/…）
3) 一句话说明，然后按严重性排序，产出「AI 初审版」报告（outputs/audit-ai-<ts>.md），
人工从「高」往「低」看即可。

AI 的判断只作参考，最终裁决仍是人工——所以这里只写 outputs/ 与 data/，绝不碰 wiki/。
"""
import json
import re
from datetime import datetime

from . import config, llm, tasks, wiki

# 一次给 AI 判 20 条矛盾：太大容易超上下文，太小请求次数翻倍
BATCH = 20

SEV_ORDER = {"高": 0, "中": 1, "低": 2}

# 同一严重性里，类别按「需要人动脑的程度」排：矛盾最先，孤立页垫底
CATEGORY_ORDER = ["矛盾", "锚点存疑", "格式损坏", "需复核", "合并建议",
                  "未摄入文件", "过期", "内容缺口", "孤立", "时点差异"]

JUDGE_PROMPT = """你是知识库审校的初审员。下面每条是页面里被筛查出的「疑似矛盾」陈述对（A vs B）。请逐条判断性质与严重性。

判定标准：
- 实质矛盾：同一指标、相同口径、相同时点下，双方说法相反或数值不同（如市场份额 45% vs 30%）→ sev=高
- 口径不同：双方统计范围不同导致数值不同，不是真矛盾（如「主业毛利率 20%」vs「毛利率 25%」、「归母净利润」vs「扣非净利润」）→ sev=中
- 表述严谨问题：实质一致，只是措辞程度不同（如「已满仓」vs「已基本满仓」、「大幅提升」vs「明显提升」）→ sev=低
- 无法判断：依据不足，需人工看原文 → sev=中

要求：why 用一句话，写给要复核的人看，直接说清「为什么算/不算矛盾」。

只输出 JSON：
{"items": [{"i": 序号, "verdict": "实质矛盾 或 口径不同 或 表述严谨问题 或 无法判断", "sev": "高 或 中 或 低", "why": "一句话说明"}]}

--- 待判清单开始 ---
{ITEMS}
--- 待判清单结束 ---"""


def _ts_pretty(ts):
    try:
        return datetime.strptime(str(ts), "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(ts or "")


def _load_base(ts):
    """定位本次初审依据的审校结果（data/audit-<ts>.json）。ts 为空取最新一份。"""
    if ts:
        p = config.DATA_DIR / f"audit-{ts}.json"
        if not p.is_file():
            raise FileNotFoundError(f"找不到审校结果 data/audit-{ts}.json，请先执行审校")
        return json.loads(p.read_text(encoding="utf-8"))
    files = sorted(p for p in config.DATA_DIR.glob("audit-*.json") if "-ai-" not in p.name)
    if not files:
        raise FileNotFoundError("还没有审校结果（data/ 里没有 audit-*.json），请先执行审校")
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _fixed_items(data):
    """非矛盾类问题按固定规则给严重性与说明——这些类别 AI 再判也不会有不同答案。"""
    items = []
    for d in data.get("anchor_doubts") or []:
        if isinstance(d, dict):
            items.append({**d, "category": "锚点存疑", "sev": "高",
                          "why": "问答引用的数字在它标注的原文页里找不到，出处可能不实，需人工核对原句"})
    for b in data.get("bad_pages") or []:
        if isinstance(b, dict):
            items.append({**b, "category": "格式损坏", "sev": "高",
                          "why": "页面文件解析失败，内容读不出来，需先修复格式"})
    for p in data.get("needs_review_pages") or []:
        if isinstance(p, dict):
            items.append({**p, "category": "需复核", "sev": "中",
                          "why": p.get("reason") or "原文件删除后条目失去出处锚点，需人工确认去留"})
    for m in data.get("merge_suggestions") or []:
        if not isinstance(m, dict):
            continue
        if m.get("kind") == "pair":
            items.append({**m, "category": "合并建议", "sev": "中",
                          "why": "两页标题互相包含，疑似同一主题拆成了两页，需人工确认合并方向"})
        else:
            items.append({**m, "category": "合并建议", "sev": "低",
                          "why": m.get("note") or "条目过少且来源单一，疑似碎片页"})
    for f in data.get("missing_files") or []:
        items.append({"path": f, "category": "未摄入文件", "sev": "中",
                      "why": "raw/ 里存在这份文件，但知识库里没有它的内容，账实不符"})
    stale_days = config.CFG["audit"]["stale_days"]
    for s in data.get("stale") or []:
        if not isinstance(s, dict):
            continue
        items.append({**s, "category": "过期",
                      "sev": "中" if s.get("note") else "低",
                      "why": s.get("note") or f"页面超过 {stale_days} 天未更新，数据时效需人工确认"})
    for g in data.get("gaps_by_page") or []:
        for x in (g.get("gaps") or []):
            if isinstance(x, dict) and x.get("point"):
                items.append({"page": g.get("page"), "category": "内容缺口", "sev": "低",
                              "point": x.get("point"),
                              "why": x.get("why") or "投研常用信息点，页面还没覆盖"})
    for o in data.get("orphans") or []:
        items.append({"title": o, "category": "孤立", "sev": "低",
                      "why": "没有任何页面引用它，可能缺双链或该并入别的页"})
    # 时点差异：审校已认定「属正常更新，不报警」，初审一律 低
    for t in data.get("time_diffs") or []:
        if isinstance(t, dict):
            items.append({**t, "category": "时点差异", "sev": "低",
                          "why": t.get("note") or "不同日期报告对同一指标的不同预测，属正常更新"})
    return items


def _judge_conflicts(items, task_id):
    """矛盾类交给 AI 逐条判断；失败/输出缺条目的，落回「无法判断/中」，绝不丢条目。"""
    idx = [i for i, x in enumerate(items) if x["category"] == "矛盾"]
    unjudged = 0
    for start in range(0, len(idx), BATCH):
        batch = idx[start:start + BATCH]
        tasks.check_cancel(task_id)
        if task_id:
            # 进度口径与 audit 一致：percent 交给 detail 文本说清（第几批/共几批）
            tasks.update(task_id, current=start // BATCH + 1,
                         detail=f"AI 初判矛盾第 {start // BATCH + 1}/"
                                f"{(len(idx) + BATCH - 1) // BATCH} 批（{len(batch)} 条）")
        listing = "\n".join(
            f"{n + 1}. 页面「{items[i].get('page', '')}」"
            f"A：「{items[i].get('quote_a', '')}」{items[i].get('anchor_a', '')} vs "
            f"B：「{items[i].get('quote_b', '')}」{items[i].get('anchor_b', '')}"
            for n, i in enumerate(batch))
        try:
            out = llm.chat([{"role": "user", "content": JUDGE_PROMPT.replace("{ITEMS}", listing)}],
                           task="ai_review", response_json=True)
        except llm.LLMError:
            unjudged += len(batch)
            continue
        got = {}
        for r in (out.get("items") or []):
            if isinstance(r, dict) and isinstance(r.get("i"), int) and 1 <= r["i"] <= len(batch):
                got[r["i"] - 1] = r
        for n, i in enumerate(batch):
            r = got.get(n)
            if r and r.get("sev") in SEV_ORDER:
                items[i]["sev"] = str(r["sev"])
                items[i]["verdict"] = str(r.get("verdict") or "无法判断")
                items[i]["why"] = str(r.get("why") or "AI 未给出说明，请人工判断")
            else:
                unjudged += 1
    return unjudged


def _sev_defaults(items):
    """兜底：任何没拿到严重性的条目按「中」处理，保证排序完整。

    矛盾条目若 AI 没给出判断（请求失败/漏答），显式标「无法判断」，
    报告里的人工一眼能看出哪些要自己看原文。
    """
    for x in items:
        if x.get("sev") not in SEV_ORDER:
            x["sev"] = "中"
        if x.get("category") == "矛盾" and not x.get("verdict"):
            x["verdict"] = "无法判断"
        x.setdefault("why", "请人工判断")


def _conflict_block(n, x):
    out = [f"### {n}. [{x.get('page', '')}] {x.get('verdict', '')}"]
    out += [f"- A：「{x.get('quote_a', '')}」{x.get('anchor_a', '')}",
            f"- B：「{x.get('quote_b', '')}」{x.get('anchor_b', '')}",
            f"- AI 说明：{x.get('why', '')}"]
    return out


def _plain_line(n, x):
    cat = x.get("category", "")
    if cat == "锚点存疑":
        # 说人话：哪个数字、来自哪次问答、标注了哪一页、为什么存疑。
        # 原句里自带的 [[锚点]] 会和出处标注混在一起，展示前先剥掉。
        num = x.get("number")
        q = str(x.get("question") or "").strip()
        loc = str(x.get("loc") or "")
        where = f"{x.get('file', '')} 第{loc.removeprefix('p')}页" if loc.startswith("p") \
            else f"{x.get('file', '')}#{loc}"
        sent = re.sub(r"\s+", " ", wiki.ANCHOR_RE.sub("", str(x.get("sentence") or ""))).strip()[:90]
        if num is not None and q:
            main = f"问答「{q[:40]}」里的数字 {num}，标注的出处是 {where}，但这一页原文里找不到这个数字，需人工核对"
        elif num is not None:
            main = f"数字 {num} 标注的出处是 {where}，但这一页原文里找不到它，需人工核对"
        else:
            main = f"{x.get('reason') or '出处存疑'}（标注 {where}），需人工核对"
        tail = f"。原句：「{sent}」" if sent else ""
    elif cat == "格式损坏":
        head, tail = x.get("path", ""), f"：{x.get('error', '')}"
    elif cat == "需复核":
        head, tail = x.get("title", ""), f"：{x.get('why', '')}"
    elif cat == "合并建议" and x.get("kind") == "pair":
        head, tail = f"{x.get('a', {}).get('title', '')} ↔ {x.get('b', {}).get('title', '')}", f"：{x.get('why', '')}"
    elif cat == "未摄入文件":
        head, tail = x.get("path", ""), f"：{x.get('why', '')}"
    elif cat == "过期":
        head, tail = x.get("title", ""), f"：{x.get('why', '')}"
    elif cat == "内容缺口":
        head, tail = f"[{x.get('page', '')}] {x.get('point', '')}", f"（{x.get('why', '')}）"
    elif cat == "孤立":
        head, tail = x.get("title", ""), f"：{x.get('why', '')}"
    elif cat == "时点差异":
        head = f"[{x.get('page', '')}] A「{x.get('quote_a', '')}」{x.get('anchor_a', '')} vs B「{x.get('quote_b', '')}」{x.get('anchor_b', '')}"
        tail = f"：{x.get('why', '')}"
    else:  # 合并建议（碎片页）等
        head, tail = x.get("title", ""), f"：{x.get('why', '')}"
    if cat == "锚点存疑":  # 该分支组装的是整句 main，不走下面的 head 拼接
        return f"- {n}. [{cat}] {main}{tail}"
    return f"- {n}. [{cat}] {head}{tail}"


def _render_md(items, base_ts, model_name, unjudged):
    lines = [f"# AI 初审报告（基于审校 {_ts_pretty(base_ts)}）", ""]
    lines += ["> AI 只做初判排序与一句话说明，**没有删减任何筛查出的问题**；每条仍需人工确认后再处理。", ""]
    sev_counts = {s: sum(1 for x in items if x["sev"] == s) for s in ("高", "中", "低")}
    lines += [f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　模型：{model_name}"]
    lines += [f"- 共 {len(items)} 条：高 {sev_counts['高']} · 中 {sev_counts['中']} · 低 {sev_counts['低']}"
              f"。按严重性排序，建议从「高」往下看。", ""]
    if unjudged:
        lines += [f"⚠ 其中 {unjudged} 条矛盾 AI 未能判断（已标「无法判断」），请直接人工复核。", ""]
    n = 0
    for sev in ("高", "中", "低"):
        subset = [x for x in items if x["sev"] == sev]
        lines += [f"## {sev}（{len(subset)} 条）", ""]
        for cat in CATEGORY_ORDER:
            group = [x for x in subset if x["category"] == cat]
            if not group:
                continue
            lines += [f"#### {cat}（{len(group)} 条）", ""]
            for x in group:
                n += 1
                if cat == "矛盾":
                    lines += _conflict_block(n, x)
                else:
                    lines += [_plain_line(n, x)]
            lines.append("")
    if not items:
        lines += ["（本次审校没有筛查出问题，无需初审。）", ""]
    return "\n".join(lines), sev_counts


def run(ts=None, task_id=None):
    base = _load_base(ts)
    base_ts = str(base.get("ts") or ts or "")
    items = []
    for c in base.get("conflicts") or []:
        if isinstance(c, dict):
            items.append({**c, "category": "矛盾"})
    items += _fixed_items(base)
    unjudged = _judge_conflicts(items, task_id) if items else 0
    tasks.check_cancel(task_id)
    _sev_defaults(items)
    items.sort(key=lambda x: (SEV_ORDER.get(x.get("sev"), 1),
                              CATEGORY_ORDER.index(x["category"]) if x.get("category") in CATEGORY_ORDER else 99))
    md, sev_counts = _render_md(items, base_ts, llm.active_model(), unjudged)

    config.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.OUTPUTS_DIR / f"audit-ai-{base_ts}.md"
    out_path.write_text(md, encoding="utf-8")
    (config.DATA_DIR / f"audit-ai-{base_ts}.json").write_text(json.dumps({
        "ts": base_ts,
        "base_report": f"audit-{base_ts}.md",
        "model": llm.active_model(),
        "generated": config.now_iso(),
        "counts": sev_counts,
        "unjudged": unjudged,
        "items": items,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    wiki.append_log("ai_review",
                    f"AI 初审 {out_path.name}（基于 audit-{base_ts}.md）："
                    f"高 {sev_counts['高']} · 中 {sev_counts['中']} · 低 {sev_counts['低']}，"
                    f"未判 {unjudged} 条；仅供排序参考，未删减问题")
    return {"path": out_path, "sev_counts": sev_counts, "total": len(items), "unjudged": unjudged}
