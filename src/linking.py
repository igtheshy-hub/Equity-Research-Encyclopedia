"""自动补双链：让知识页之间真正连动起来。

背景：编译只要求写出处锚点，正文从不写 [[双链]]，页面间的引用只剩来源摘要页
的「贡献」区，反链面板与图谱退化成「哪份材料提到过这页」。本模块在摄入收尾
（及手动 manage.py link）逐字扫描每页正文，凡提到**其他页面**的标题/别名
（精确匹配，非 source 页），就在该行首次出现处插入 [[双链]]。

原则：
- 零猜测：只链「名字逐字出现」的地方，程序可验证；不判断语义（图谱免责声明不变）
- 文字不变：只在原词外面包 [[ ]]，正文可见内容与出处锚点一律不动
- 零成本：纯程序，不调 LLM；幂等（已有双链/已链过的名字不重复插）
- 安全：跳过锚点与已有双链的区间；长名优先防「AI芯片电感」咬进「AI芯片电感行业」；
  跨页重名的名字不链（等用户清理）；ASCII 数字词不吃进更长数字串；
  锚点集合写盘前后必须相等，违反即整页跳过；全程 WIKI_LOCK + 原子写
"""
import re

from . import config, wiki

# 双链名里不允许出现的字符：写进 [[ ]] 后 WIKILINK_RE 不认（它排除 # | . 等），插了也白插
_BAD_NAME_CHARS = ("#", "|", ".", "[", "]", "\n")


def _bad_link_name(disp):
    return any(c in disp for c in _BAD_NAME_CHARS)


def build_candidates(pages):
    """候选名索引：非 source 页的标题与别名 → (显示名, 目标键)。

    - 跨页重名的名字整体不链（resolve 只会命中先建页，链了误导）
    - 同一页内重复的别名不构成冲突
    返回按显示名长度降序的 [(显示名, (type, slug))]，长名优先匹配。
    """
    index = {}
    conflicts = set()
    for p in pages:
        if p["type"] == "source":
            continue
        key = (p["type"], p["slug"])
        names = [str(p["meta"].get("title") or p["slug"])] + wiki.page_aliases(p["meta"])
        for disp in names:
            n = wiki._norm_name(disp)
            if not n or len(n) < 2 or _bad_link_name(disp):
                continue
            if n in index and index[n][1] != key:
                conflicts.add(n)
                continue
            index.setdefault(n, (disp, key))
    cands = [(disp, key) for n, (disp, key) in index.items() if n not in conflicts]
    cands.sort(key=lambda dk: -len(dk[0]))
    return cands


def _flanked_by_alnum(line, s, e):
    """匹配段自身的首/尾是 ASCII 字母数字、且紧贴着外侧的字母数字时不链。

    只防「数字被吃进更长的数字串」（300811 in 13008112、AI芯片 in xAI芯片y）：
    看的是匹配段**自己的**首尾字符；以中文收尾的名字（如「AI芯片电感」）后面
    紧跟数字完全合法，不该挡。
    """
    if line[s].isascii() and line[s].isalnum() and s > 0 \
            and line[s - 1].isascii() and line[s - 1].isalnum():
        return True
    if line[e - 1].isascii() and line[e - 1].isalnum() and e < len(line) \
            and line[e].isascii() and line[e].isalnum():
        return True
    return False


def link_line(line, candidates, norm2target, protected_extra=()):
    """在一行里给其他页面的名字插双链。返回 (新行, 插入数)。

    - 锚点、已有双链、protected_extra（本页自己的标题/别名出现处）都是保护区，不在里面匹配
    - 长名优先、占用区间不重叠：先匹配的候选把字面「占住」，短名咬不进去
    - 行里已有指向目标 T 的双链时，T 视作「本行已链」，不再插第二个（严格一遍幂等）
    - 同一目标在一行只链首次出现，避免满行蓝字
    """
    claimed = [(m.start(), m.end()) for m in wiki.ANCHOR_RE.finditer(line)]
    claimed += [(m.start(), m.end()) for m in wiki.WIKILINK_RE.finditer(line)]
    claimed += list(protected_extra)
    already_linked = set()
    for m in wiki.WIKILINK_RE.finditer(line):
        t = norm2target.get(wiki._norm_name(m.group(1)))
        if t is not None:
            already_linked.add(t)
    hits = []
    for disp, key in candidates:
        for m in re.finditer(re.escape(disp), line, re.IGNORECASE):
            s, e = m.span()
            if any(not (e <= cs or s >= ce) for cs, ce in claimed):
                continue
            if _flanked_by_alnum(line, s, e):
                continue
            claimed.append((s, e))
            hits.append((s, e, key))
    first = {}
    for s, e, key in hits:  # 已链过的目标与每个目标只保留行内首次出现
        if key in already_linked:
            continue
        if key not in first or s < first[key][0]:
            first[key] = (s, e)
    if not first:
        return line, 0
    out = line
    n = 0
    for s, e in sorted(first.values(), reverse=True):  # 从行尾往前插，偏移不失效
        out = out[:s] + "[[" + out[s:e] + "]]" + out[e:]
        n += 1
    return out, n


def _transform_body(body, cands, norm2target, own_names):
    """对一个正文做整页补链。返回 (新正文, 插入数)；0 表示无改动。

    own_names 是本页自己的标题与别名：它们的出现处是保护区——
    自己名字的子串（如「AI芯片电感」里的「AI芯片」）不该链给别的页面。
    """
    inserted = 0
    out_lines = []
    for ln in body.splitlines():
        if ln.lstrip().startswith("#"):  # 标题行不插链接标记
            out_lines.append(ln)
            continue
        extra = []
        for disp in own_names:
            for m in re.finditer(re.escape(disp), ln, re.IGNORECASE):
                extra.append(m.span())
        new_ln, n = link_line(ln, cands, norm2target, extra)
        inserted += n
        out_lines.append(new_ln)
    new_body = "\n".join(out_lines)
    if body.endswith("\n") and not new_body.endswith("\n"):
        new_body += "\n"
    return new_body, inserted


def cross_link_all(task_id=None, dry_run=False):
    """全库补双链。返回 {inserts, pages, samples, dry_run}。

    对每页（非 source）：逐行插双链 → 锚点集合前后必须相等 → 持锁写盘 +
    history 留痕。幂等：已链过的不重复插，跑第二次必然 0 改动。
    """
    from . import tasks

    pages = wiki.load_pages()
    candidates = build_candidates(pages)
    norm2target = {wiki._norm_name(d): k for d, k in candidates}
    total = 0
    changed = 0
    samples = []
    for p in pages:
        tasks.check_cancel(task_id)
        if p["type"] == "source":  # 来源页的「贡献」区本来就是双链，不加工
            continue
        own = (p["type"], p["slug"])
        own_names = [str(p["meta"].get("title") or p["slug"])] + wiki.page_aliases(p["meta"])
        cands = [(d, k) for d, k in candidates if k != own]  # 不自链
        new_body, inserted_here = _transform_body(p["body"], cands, norm2target, own_names)
        if not inserted_here:
            continue
        # 双链是注释性标记，锚点集合一个都不能动；变了说明匹配越界，整页放弃
        if wiki.anchors_of(new_body) != wiki.anchors_of(p["body"]):
            continue
        total += inserted_here
        changed += 1
        if len(samples) < 5:
            for old_ln, new_ln in zip(p["body"].splitlines(), new_body.splitlines()):
                if old_ln != new_ln and len(samples) < 5:
                    samples.append({"page": str(p["meta"].get("title") or p["slug"]),
                                    "before": old_ln[:80], "after": new_ln[:90]})
        if dry_run:
            continue
        with wiki.WIKI_LOCK:
            # 锁内重读最新内容再改（摄入线可能刚更新过这页），不要盖掉并发写入
            try:
                meta, body = wiki.parse_page(p["path"])
            except (ValueError, OSError):
                continue
            fresh_body, inserted_fresh = _transform_body(body, cands, norm2target, own_names)
            if not inserted_fresh:
                continue
            if wiki.anchors_of(fresh_body) != wiki.anchors_of(body):
                continue
            meta["history"] = list(meta.get("history") or []) + [
                f"{config.today_str()} 自动补双链（+{inserted_fresh} 处）"]
            wiki.write_page(p["path"], meta, fresh_body)
    stats = {"inserts": total, "pages": changed, "samples": samples, "dry_run": dry_run}
    if total and not dry_run:
        wiki.append_log("link-cross", f"自动补双链 {total} 处，覆盖 {changed} 页")
    return stats
