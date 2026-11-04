import json
import shutil
import threading
import uuid
from datetime import datetime

from . import config

# 问答会话存储：内存为主、逐次落盘 data/conversations.json，重启不丢。
# 消息里的 proposal 是沉淀提案（只算未写），applied_lines 记已提取进 wiki 的行，不再重复呈现。

_lock = threading.Lock()
_convs = None
# 启动时发现损坏文件留下的提示，供网页 /api/health 展示
_WARNINGS = []


def _path():
    """会话文件路径：**运行时**从 config 取。

    原来这里在 import 时就把 config.DATA_DIR 定死成模块常量 _PATH，
    之后 config.DATA_DIR 被改（测试隔离、换数据目录）就改不动它——
    结果测试里任何碰会话的用例都会写进真实的 data/conversations.json。
    """
    return config.DATA_DIR / "conversations.json"


def _load():
    global _convs
    if _convs is None:
        # 路径运行时求（测试隔离 / 换数据目录都能生效），
        # 损坏文件的备份重建沿用上游逻辑（不静默清空，页面能看到提示）
        p = _path()
        _convs = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("顶层不是对象")
                _convs = data
            except (ValueError, OSError) as e:
                # 损坏的会话文件先备份再重建，绝不静默清空；用户在页面上能看到提示
                backup = p.with_name(
                    f"conversations.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
                try:
                    shutil.copy2(p, backup)
                    where = f"已备份到 {backup.relative_to(config.ROOT).as_posix()}"
                except OSError:
                    where = "备份失败"
                _WARNINGS.append(
                    f"{p.name} 无法读取（{e}），{where}。本次以空会话启动，原文件未被删除。")
    return _convs


def startup_warnings():
    with _lock:
        _load()
        return list(_WARNINGS)


def _save():
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_convs, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def create(title="新会话"):
    with _lock:
        convs = _load()
        cid = uuid.uuid4().hex[:10]
        convs[cid] = {
            "id": cid,
            "title": title,
            "created_at": config.now_iso(),
            "messages": [],
        }
        _save()
        return convs[cid]


def get(cid):
    with _lock:
        return _load().get(cid)


def list_conversations():
    with _lock:
        convs = list(_load().values())
    out = []
    for c in convs:
        last = c["messages"][-1]["ts"] if c["messages"] else c["created_at"]
        out.append({
            "id": c["id"],
            "title": c["title"],
            "created_at": c["created_at"],
            "messages": len(c["messages"]),
            "last_ts": last,
        })
    out.sort(key=lambda x: x["last_ts"], reverse=True)
    return out


def delete(cid):
    """删掉一个会话。返回 True（删掉了）/ False（本来就没有）。"""
    with _lock:
        convs = _load()
        if cid not in convs:
            return False
        del convs[cid]
        _save()
        return True


def rename(cid, title):
    """给会话改名。返回新名字；会话不存在返回 None。"""
    title = str(title or "").strip()
    if not title:
        return None
    title = title[:60]
    with _lock:
        convs = _load()
        conv = convs.get(cid)
        if conv is None:
            return None
        conv["title"] = title
        _save()
        return title


def append_message(cid, msg):
    with _lock:
        conv = _load().get(cid)
        if conv is None:
            return None
        msg = dict(msg)
        msg["id"] = uuid.uuid4().hex[:10]
        msg["ts"] = config.now_iso()
        conv["messages"].append(msg)
        # 会话标题取首个用户提问的前 20 字
        if conv["title"] == "新会话" and msg.get("role") == "user":
            conv["title"] = msg["content"][:20]
        _save()
        return msg


def update_message(cid, mid, **fields):
    with _lock:
        conv = _load().get(cid)
        if conv is None:
            return None
        msg = next((m for m in conv["messages"] if m["id"] == mid), None)
        if msg is None:
            return None
        msg.update(fields)
        _save()
        return msg


def history_for_ask(cid, turns=4):
    """取最近 N 轮问答给 prompt 作多轮上下文，答案截断控制长度。"""
    conv = get(cid)
    if not conv:
        return []
    pairs = []
    msgs = conv["messages"]
    i = len(msgs) - 1
    while i >= 0 and len(pairs) < turns:
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("status") == "done" and m.get("kind") == "answer":
            q = next((x["content"] for x in reversed(msgs[:i]) if x.get("role") == "user"), "")
            if q:
                pairs.append({"q": q[:200], "a": str(m.get("content") or "")[:400]})
                i -= 2
                continue
        i -= 1
    pairs.reverse()
    return pairs
