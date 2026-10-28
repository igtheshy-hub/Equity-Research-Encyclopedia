import threading
import time
import uuid

from . import config, llm

# 后台任务注册表：所有耗时操作（问答/归类/摄入/审校/修复/报告）都在线程里跑，
# 状态留在服务端，页面切换只靠轮询取快照，互不打断。

_lock = threading.RLock()
_TASKS = {}
_ORDER = []

# 这些类型同一时间只允许一个在跑，后来的排队等待
MUTEX_KINDS = {"classify", "ingest", "audit", "fix", "report"}
# 内存里最多保留这么多任务；超出的优先清掉已结束且最老的，不再只等页面确认
MAX_TASKS = 200

KIND_LABELS = {
    "ask": "问答",
    "classify": "取名入库",
    "ingest": "摄入",
    "audit": "审校",
    "fix": "修复",
    "report": "报告",
    "delete": "删除原文件",
}


class TaskCancelled(RuntimeError):
    """用户点了取消。后台任务在循环里调用 check_cancel 抛出。"""


def _new_id():
    return uuid.uuid4().hex[:12]


def _set(tid, **fields):
    with _lock:
        t = _TASKS.get(tid)
        if t is not None:
            t.update(fields)


def _cancelled(tid):
    with _lock:
        t = _TASKS.get(tid)
        return bool(t and t.get("cancel_requested"))


def check_cancel(tid):
    """后台循环在每个可中断点调用；None（命令行/无任务）直接放过。

    取消和超时都在这里统一变成 TaskCancelled，循环里只需调一次。
    """
    if not tid:
        return
    with _lock:
        t = _TASKS.get(tid)
        if t is None:
            return
        if t.get("cancel_requested"):
            raise TaskCancelled("任务已取消")
        deadline = t.get("deadline")
        if deadline and time.time() > deadline:
            t["cancel_requested"] = True
            raise TaskCancelled("任务超时，已自动停止")


def is_cancelled(tid):
    return _cancelled(tid)


def _prune_locked():
    # 先把已 ack 或已从注册表移除的 id 从顺序表里清掉
    _ORDER[:] = [tid for tid in _ORDER if tid in _TASKS]
    while len(_TASKS) > MAX_TASKS:
        candidates = [tid for tid in _ORDER
                      if tid in _TASKS and _TASKS[tid]["status"] not in ("running", "waiting")]
        if not candidates:
            break
        old = candidates[0]
        _ORDER.remove(old)
        _TASKS.pop(old, None)


def start(kind, fn, total=1, label="", model=None, timeout_s=None):
    """起后台线程跑 fn(task_id)。互斥类型若同类在跑，任务先置 waiting 排队，前一个结束自动接续。

    model 是网页下拉选中的模型；线程内部用 llm.use_model 包住，取名/摄入/审校/修复
    这些原先只读 config.json 默认模型的环节也跟问答一样按本次选择走。
    """
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
            "model": model or config.DEFAULT_MODEL,
            "created_at": config.now_iso(),
            "acknowledged": False,
            "cancel_requested": False,
            "deadline": (time.time() + timeout_s) if timeout_s else None,
        }
        _TASKS[tid] = t
        _ORDER.append(tid)
        _prune_locked()
        kl = _KIND_LOCKS.get(kind)

    def _runner():
        acquired = False
        try:
            if kl is not None:
                if not kl.acquire(blocking=False):
                    _set(tid, status="waiting", detail="排队等待中（同类任务进行中）")
                    # 排队也要能被取消，并且不占着锁
                    while True:
                        check_cancel(tid)
                        if kl.acquire(timeout=0.5):
                            acquired = True
                            break
                else:
                    acquired = True
                check_cancel(tid)
                _set(tid, status="running", detail="")
            with llm.use_model(model):
                result = fn(tid)
            _set(tid, result=result, status="done", percent=100, detail="")
        except TaskCancelled as e:
            _set(tid, status="cancelled", detail=str(e))
        except Exception as e:  # 后台任务异常只记入任务，不带崩服务
            _set(tid, status="error", error=str(e))
        finally:
            if acquired and kl is not None:
                kl.release()

    threading.Thread(target=_runner, daemon=True, name=f"task-{kind}-{tid}").start()
    return tid


def cancel(tid):
    """请求取消。只对排队中/运行中的任务有效；真正停下由各任务循环里的 check_cancel 完成。"""
    with _lock:
        t = _TASKS.get(tid)
        if t is None or t["status"] not in ("running", "waiting"):
            return False
        t["cancel_requested"] = True
        t["detail"] = "正在取消…"
        return True


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
        try:
            _ORDER.remove(tid)
        except ValueError:
            pass


def active_counts():
    with _lock:
        c = {}
        for t in _TASKS.values():
            if t["status"] in ("running", "waiting"):
                c[t["kind"]] = c.get(t["kind"], 0) + 1
        return c


with _lock:
    _KIND_LOCKS = {k: threading.Lock() for k in MUTEX_KINDS}
