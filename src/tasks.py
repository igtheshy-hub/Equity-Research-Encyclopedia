import threading
import uuid

from . import config

# 后台任务注册表：所有耗时操作（问答/归类/摄入/审校/修复/报告）都在线程里跑，
# 状态留在服务端，页面切换只靠轮询取快照，互不打断。

_lock = threading.RLock()
_TASKS = {}
_ORDER = []

# 这些类型同一时间只允许一个在跑，后来的排队等待
MUTEX_KINDS = {"classify", "ingest", "audit", "fix", "report"}
MAX_TASKS = 200

KIND_LABELS = {
    "ask": "问答",
    "classify": "取名入库",
    "ingest": "摄入",
    "audit": "审校",
    "fix": "修复",
    "report": "报告",
}


def _new_id():
    return uuid.uuid4().hex[:12]


def start(kind, fn, total=1, label=""):
    """起后台线程跑 fn(task_id)。互斥类型若同类在跑，任务先置 waiting 排队，前一个结束自动接续。"""
    with _lock:
        tid = _new_id()
        t = {
            "id": tid,
            "kind": kind,
            "kind_label": KIND_LABELS.get(kind, kind),
            "status": "running",
            "total": max(1, int(total)),
            "current": 0,
            "percent": 0,
            "label": label,
            "detail": "",
            "result": None,
            "error": None,
            "created_at": config.now_iso(),
            "acknowledged": False,
        }
        _TASKS[tid] = t
        _ORDER.append(tid)
        while len(_ORDER) > MAX_TASKS:
            old = _ORDER.pop(0)
            old_t = _TASKS.get(old)
            if old_t and old_t["acknowledged"]:
                _TASKS.pop(old, None)
        kl = _KIND_LOCKS.get(kind)

    def _runner():
        try:
            if kl is not None:
                if not kl.acquire(blocking=False):
                    update(tid, detail="排队等待中（同类任务进行中）")
                    t["status"] = "waiting"
                    kl.acquire()
                t["status"] = "running"
                t["detail"] = ""
            t["result"] = fn(tid)
            t["status"] = "done"
            t["percent"] = 100
            t["detail"] = ""
        except Exception as e:  # 后台任务异常只记入任务，不带崩服务
            t["status"] = "error"
            t["error"] = str(e)
        finally:
            if kl is not None:
                kl.release()

    threading.Thread(target=_runner, daemon=True, name=f"task-{kind}-{tid}").start()
    return tid


def update(tid, current=None, detail="", label=None):
    with _lock:
        t = _TASKS.get(tid)
        if t is None:
            return
        if current is not None:
            t["current"] = current
            t["percent"] = min(100, int(current * 100 / t["total"]))
        if detail:
            t["detail"] = detail
        if label:
            t["label"] = label


def snapshot(kinds=None):
    with _lock:
        out = [dict(t) for t in _TASKS.values()
               if not t["acknowledged"] and (kinds is None or t["kind"] in kinds)]
    out.sort(key=lambda t: t["created_at"])
    return out


def acknowledge(tid):
    with _lock:
        _TASKS.pop(tid, None)


def active_counts():
    with _lock:
        c = {}
        for t in _TASKS.values():
            if t["status"] in ("running", "waiting"):
                c[t["kind"]] = c.get(t["kind"], 0) + 1
        return c


with _lock:
    _KIND_LOCKS = {k: threading.Lock() for k in MUTEX_KINDS}
