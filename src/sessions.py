import json
import threading
import uuid

from . import config

# 问答会话存储：内存为主、逐次落盘 data/conversations.json，重启不丢。
# 消息里的 proposal 是沉淀提案（只算未写），applied_lines 记已提取进 wiki 的行，不再重复呈现。

_PATH = config.DATA_DIR / "conversations.json"
_lock = threading.Lock()
_convs = None


def _load():
    global _convs
    if _convs is None:
        if _PATH.exists():
            try:
                _convs = json.loads(_PATH.read_text(encoding="utf-8"))
            except ValueError:
                _convs = {}
        else:
            _convs = {}
    return _convs


def _save():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_convs, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_PATH)


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
