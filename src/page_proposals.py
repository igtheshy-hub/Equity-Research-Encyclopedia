"""待建页提案：摄入时发现「该建而没建的页面」，建不建由用户勾选拍板。

机制（机器干活、人决策）：
- 编译模型在通读全文时顺手抄出 entities（只抓名词，不判断），程序汇总计数、
  过滤已有页面后，高频未建页实体进入提案队列 data/page_proposals.json；
- 语义照抄蒸馏提案：pending 只读查询；「稍后处理」只推迟 24 小时；
  「不再提醒」才永久消失；确认后走 mini 摄入定向建页，成功标 done、失败回 pending；
- 存储读写沿用清单类文件的既有纪律：临时文件原子替换 + 旧版 .bak 备份。
  同一实体名处理过（done/dismissed）后不再重复出现。
"""
import json
import os
import threading
import uuid
from datetime import datetime

from . import config, wiki

_lock = threading.RLock()


def _path():
    """提案文件路径：运行时从 config 取（测试隔离 / 换数据目录都生效）。"""
    return config.DATA_DIR / "page_proposals.json"


def threshold():
    """建页提案的最低提及次数，config.json 的 ingest.page_proposal_threshold，默认 3。"""
    try:
        return max(1, int(config.CFG.get("ingest", {}).get("page_proposal_threshold", 3)))
    except (TypeError, ValueError):
        return 3


def load():
    p = _path()
    if not p.exists():
        return {"proposals": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("proposals"), list):
            return data
    except (ValueError, OSError):
        pass
    return {"proposals": []}


def save(data):
    """写提案文件：临时文件原子替换 + 旧版备份成 .bak（与 manifest 同一纪律）。"""
    with _lock:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False, indent=1)
        if p.exists():
            try:
                bak = p.with_suffix(p.suffix + ".bak")
                bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        tmp = p.with_suffix(p.suffix + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            from .wiki import _atomic_replace
            _atomic_replace(tmp, p)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


def merge_detected(entities, entity_types, source_file, decided_titles=()):
    """摄入收尾调用：把本报告检出的实体并入提案队列。返回本次新增的提案数。

    过滤链：计数 ≥ 阈值 → 已有页面/别名不算（resolve_target 命中即跳过）→
    本报告已决定建页的标题不算 → 提案文件里已有的名字（任意状态）不再重复添加。
    """
    pages = wiki.load_pages()
    data = load()
    have = {str(pr.get("name") or "").strip() for pr in data["proposals"]}
    decided = {str(t).strip() for t in decided_titles}
    thr = threshold()
    added = 0
    for name, count in sorted(entities.items(), key=lambda kv: (-kv[1], kv[0])):
        name = str(name).strip()
        if not name or count < thr:
            continue
        if wiki.resolve_target(name, pages) is not None:
            continue  # 已是某页的标题/别名，不产生提案
        if name in decided:
            continue
        if name in have:
            continue
        data["proposals"].append({
            "name": name,
            "count": int(count),
            "source_file": str(source_file),
            "entity_type": entity_types.get(name) or "concept",
            "status": "pending",
            "first_seen": config.now_iso(),
            "snoozed_until": None,
            "dismissed": False,
        })
        have.add(name)
        added += 1
    if added:
        save(data)
    return added


def pending_items():
    """当前该提醒的提案：只读查询，不改变任何状态。

    dismissed / 非 pending 状态 / snoozed_until 未到的都不出现；刷新、关弹窗不丢提案。
    """
    now = config.now_iso()
    items = []
    for pr in load()["proposals"]:
        if pr.get("status") != "pending" or pr.get("dismissed"):
            continue
        snooze = pr.get("snoozed_until")
        if snooze and str(snooze) > now:
            continue
        items.append(dict(pr))
    items.sort(key=lambda p: (-int(p.get("count") or 0), p.get("name") or ""))
    return items


def _update(name, **fields):
    with _lock:
        data = load()
        pr = next((x for x in data["proposals"] if x.get("name") == name), None)
        if pr is None:
            return None
        pr.update(fields)
        save(data)
        return pr


def defer(name, hours=24):
    """稍后处理：只把提醒推迟一段时间，不丢提案。"""
    until = (datetime.fromisoformat(config.now_iso()).timestamp() + hours * 3600)
    until_iso = datetime.fromtimestamp(until).isoformat(timespec="seconds")
    return _update(name, snoozed_until=until_iso)


def dismiss(name):
    """不再提醒：只有用户明确点这个按钮才永久消失。"""
    return _update(name, dismissed=True)


def mark_building(name):
    """确认建页：置 building，mini 摄入期间不再弹。返回提案；不存在时 None。"""
    return _update(name, status="building")


def mark_done(name):
    return _update(name, status="done")


def mark_pending(name, note=""):
    """mini 摄入失败（如缓存被清）：回到 pending 让用户处理后能重试。"""
    return _update(name, status="pending", last_error=note or None)


def build_page(name, entity_type, task_id=None):
    """确认提案后的动作 = mini 摄入，不是空壳页。

    以该实体为主题，从源文件的逐单元文本缓存定向编译知识行（复用 ingest 现有
    编译函数与提示词），走标准归并 + 锚点校验写入（merge 到已有页或新建页），
    全程写 wiki/log.md。产出条目句句带锚点，与正常摄入页完全同构。
    缓存缺失时任务失败提示重新摄入，不静默降级。
    """
    from . import tasks
    from .ingest import _apply_create, _apply_merge, compile_file, load_manifest
    from .pdf_extract import DuplicateFileError, find_raw_file, load_cached

    pr = next((x for x in load()["proposals"] if x.get("name") == name), None)
    source_file = str((pr or {}).get("source_file") or "").strip()
    type_ = entity_type if entity_type in ("company", "industry", "concept") else "concept"

    tasks.check_cancel(task_id)
    if not source_file:
        raise RuntimeError(f"提案「{name}」没有记录来源文件，无法定向摄入")
    try:
        path = find_raw_file(source_file)
    except DuplicateFileError as e:
        raise RuntimeError(str(e)) from e
    if path is None:
        raise RuntimeError(f"找不到原文件 {source_file}，请确认它还在资料库里")
    cached = load_cached(path)
    if not cached or not (cached.get("units") or []):
        raise RuntimeError(f"没有 {source_file} 的提取缓存（可能中途清过缓存），"
                           f"请对该文件重新摄入后再确认提案")

    tasks.update(task_id, detail=f"正在从 {source_file} 定向编译「{name}」的知识条目…")
    compiled = compile_file(path, cached["units"], cached.get("unit") or "page",
                            task_id=task_id, only_entity=name)
    topics = [t for t in compiled["topics"] if any((t.get("sections") or {}).values())]
    topic = topics[0] if topics else None
    if not topic:
        raise RuntimeError(f"从 {source_file} 没有编译出「{name}」的知识条目"
                           f"（该文件可能没有提到它，或内容不足），请人工确认后再试")
    # 锚点校验已在 compile_file 内完成（违规行直接丢弃，不许无锚点行入库）
    total = sum(len(v) for v in topic["sections"].values())
    rel = path.relative_to(config.ROOT).as_posix()
    note_prefix = f"建页提案确认，定向摄入 {source_file}"

    pages = wiki.load_pages()
    hit = wiki.resolve_target(name, pages)
    if hit:
        page = next(p for p in pages if (p["type"], p["slug"]) == hit)
        count = _apply_merge(page["path"], page["meta"], page["body"], topic, [rel], note_prefix)
        dest = f"并入 {page['slug']}"
    else:
        new_path, count = _apply_create({"action": "create", "type": type_}, topic, [rel], note_prefix)
        dest = f"新建 {new_path.relative_to(config.WIKI_DIR).as_posix()}"
    wiki.rebuild_index_md(wiki.load_pages())
    wiki.append_log("propose-page",
                    f"实体「{name}」定向摄入 {source_file}（{count} 条），{dest}")
    return {"name": name, "page": dest, "entries": count}
