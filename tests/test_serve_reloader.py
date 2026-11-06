"""热重载闲时门与启动清扫：换 worker 等任务跑完，重启遗留任务标成可续跑。"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _common import check  # noqa: E402
from src import config, industry_report, serve_reloader  # noqa: E402


class _FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def _loop(monkeypatch, busy_seq, clock):
    monkeypatch.setattr(serve_reloader, "time", clock)
    loop = serve_reloader.IdleGatedReloader()
    loop.log_reload = lambda name: None
    seq = iter(busy_seq)
    calls = {"exit": 0}

    def fake_busy():
        try:
            return next(seq)
        except StopIteration:
            return False

    loop._busy = fake_busy
    loop._do_exit = lambda: calls.__setitem__("exit", calls["exit"] + 1)
    return loop, calls


def test_reload_waits_until_idle(monkeypatch):
    print("热重载：有任务在跑时等任务结束再换 worker")
    clock = _FakeClock()
    loop, calls = _loop(monkeypatch, [True, True, False], clock)
    loop.trigger_reload("src/x.py")
    check("空闲后退出一次", calls["exit"], 1)
    check("等了两轮（每轮 2 秒）", clock.slept,
          [serve_reloader.POLL_S, serve_reloader.POLL_S])


def test_reload_exits_after_cap(monkeypatch):
    print("热重载：任务一直不空闲，等满上限仍退出换新")
    clock = _FakeClock()
    loop, calls = _loop(monkeypatch, [True] * 100000, clock)
    loop.trigger_reload("src/x.py")
    check("超时仍退出", calls["exit"], 1)
    check("等满上限才退", clock.now - 1000.0 >= serve_reloader.IDLE_WAIT_S, True)


def test_reloader_registered():
    print("热重载：注册进 werkzeug 的重载器表")
    from werkzeug._reloader import reloader_loops
    serve_reloader.IdleGatedReloader.register()
    check("注册键", reloader_loops.get(serve_reloader.RELOADER_KEY),
          serve_reloader.IdleGatedReloader)


def test_recover_interrupted_jobs(monkeypatch, tmp_path):
    print("启动清扫：上个进程留下的进行中任务标成可续跑")
    jobs_dir = Path(tmp_path) / "data" / "report_jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "REPORT_JOBS_DIR", jobs_dir)
    (jobs_dir / "j1.json").write_text(json.dumps(
        {"id": "j1", "status": "writing", "title": "中断的"}, ensure_ascii=False),
        encoding="utf-8")
    (jobs_dir / "j2.json").write_text(json.dumps(
        {"id": "j2", "status": "candidates_pending", "title": "正常的"}, ensure_ascii=False),
        encoding="utf-8")
    (jobs_dir / "j3.json").write_text(json.dumps(
        {"id": "j3", "status": "docxing"}, ensure_ascii=False),
        encoding="utf-8")

    got = industry_report.recover_interrupted_jobs()
    check("标出两个进行中任务", got, ["j1", "j3"])
    j1 = json.loads((jobs_dir / "j1.json").read_text(encoding="utf-8"))
    check("状态改 error", j1["status"], "error")
    check("错误信息提示可续跑", "继续" in j1["error"], True)
    check("日志记了中断", any("中断" in e["msg"] for e in j1.get("log") or []), True)
    j2 = json.loads((jobs_dir / "j2.json").read_text(encoding="utf-8"))
    check("待勾选任务不动", j2["status"], "candidates_pending")
    j3 = json.loads((jobs_dir / "j3.json").read_text(encoding="utf-8"))
    check("缺 id 的用文件名补", j3["id"], "j3")
