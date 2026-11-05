"""自动派救火员：阶段范围、任务上限、detached 启动命令与 --settings 注入。"""
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import agent_rescue  # noqa: E402


def _job(**kw):
    job = {"id": "abc12345", "kind": "company", "status": "error"}
    job.update(kw)
    return job


class _FakeStdin:
    def __init__(self, proc):
        self.proc = proc

    def write(self, data):
        self.proc.written.append(data)

    def close(self):
        self.proc.stdin_closed = True


class _FakeProc:
    """带 stdin 管道的假子进程：spawn 会往 stdin 写提示词后关掉。"""

    def __init__(self, pid=7):
        self.pid = pid
        self.written = []
        self.stdin_closed = False
        self.stdin = _FakeStdin(self)

    def wait(self):
        return 0


def test_agent_by_kind():
    assert agent_rescue.AGENT_BY_KIND["company"] == "公司研报-救火员"
    assert agent_rescue.AGENT_BY_KIND["industry"] == "行业研报-救火员"
    assert "_phase_write" in agent_rescue.AUTO_RESCUE_PHASES
    assert "_phase_review" in agent_rescue.AUTO_RESCUE_PHASES
    assert "_phase_docx" in agent_rescue.AUTO_RESCUE_PHASES


def test_spawn_rejects_out_of_scope_phase():
    meta, why = agent_rescue.spawn(_job(), "_phase_outline")
    assert meta is None
    assert "不在自动救火范围" in why


def test_spawn_requires_error_state():
    meta, why = agent_rescue.spawn(_job(status="writing"), "_phase_write")
    assert meta is None
    assert "不在 error 状态" in why


def test_spawn_respects_job_limit():
    meta, why = agent_rescue.spawn(
        _job(auto_rescue=[{"phase": "_phase_write"}]), "_phase_write")
    assert meta is None
    assert "已自动派过 1 次" in why


def test_spawn_unknown_kind():
    meta, why = agent_rescue.spawn(_job(kind="other"), "_phase_write")
    assert meta is None
    assert "没有对应救火员" in why


def test_spawn_missing_claude(monkeypatch):
    monkeypatch.setattr(agent_rescue.shutil, "which", lambda _: None)
    meta, why = agent_rescue.spawn(_job(), "_phase_write")
    assert meta is None
    assert "找不到 claude" in why


def _fake_rescue_settings(_model=None):
    return {"base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-pro", "key_env": "LLM_API_KEY", "key": "sk-test"}


def test_spawn_builds_detached_command(tmp_path, monkeypatch):
    exe = r"C:\fake\claude.CMD" if os.name == "nt" else "/fake/claude"
    captured = {}
    proc = _FakeProc(pid=4321)
    proc.wait = lambda: threading.Event().wait()   # 停住清理线程，设置文件留给断言

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(agent_rescue.shutil, "which", lambda _: exe)
    monkeypatch.setattr(agent_rescue.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(agent_rescue.config, "ROOT", tmp_path)
    monkeypatch.setattr(agent_rescue.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(agent_rescue.config, "now_iso", lambda: "2026-11-05T16:30:00")
    monkeypatch.setattr(agent_rescue.config, "rescue_settings", _fake_rescue_settings)

    meta, why = agent_rescue.spawn(_job(model="deepseek-v4-flash"), "_phase_write")
    assert meta is not None
    assert "已启动" in why
    assert meta["pid"] == 4321
    assert meta["agent"] == "公司研报-救火员"
    assert meta["log"].startswith("data/auto_rescue/")

    cmd = captured["cmd"]
    if os.name == "nt":
        assert cmd[:2] == [os.environ.get("COMSPEC") or "cmd.exe", "/c"]
        assert cmd[2] == exe
    else:
        assert cmd[0] == exe
    assert "-p" in cmd
    # 提示词走 stdin 不进命令行：cmd /c 会截断带换行的参数（待修清单第 45 条）
    assert cmd[cmd.index("-p") + 1] == "--agent"
    assert not any("\n" in a or "\r" in a for a in cmd)
    written = b"".join(proc.written).decode("utf-8")
    assert "abc12345" in written
    assert "_phase_write" in written
    assert proc.stdin_closed is True
    for flag, value in (("--agent", "公司研报-救火员"),
                        ("--allowedTools", "Read,Write,Edit,Glob,Grep,Bash"),
                        ("--permission-mode", "acceptEdits"),
                        ("--permission-prompts", "none"),
                        ("--output-format", "text")):
        assert flag in cmd
        assert cmd[cmd.index(flag) + 1] == value
    assert "--disallowedTools" in cmd
    disallowed = cmd[cmd.index("--disallowedTools") + 1]
    assert "Bash(git *)" in disallowed
    assert "Bash(rm *)" in disallowed

    assert "--settings" in cmd
    settings_path = Path(cmd[cmd.index("--settings") + 1])
    assert settings_path.parent == tmp_path / "data" / "auto_rescue"
    assert settings_path.name == "abc12345-phase_write-20261105-163000.settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings == {"env": {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_MODEL": "deepseek-v4-pro",
        "ANTHROPIC_AUTH_TOKEN": "sk-test",
    }}

    kwargs = captured["kwargs"]
    assert "env" not in kwargs
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdin"] == subprocess.PIPE
    assert kwargs["close_fds"] is True
    if os.name == "nt":
        assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True

    log_path = tmp_path / meta["log"]
    assert log_path.exists()


def test_auto_rescue_records_dispatch(monkeypatch):
    from src import industry_report as ir

    job = _job()
    monkeypatch.setattr(ir.agent_rescue, "spawn", lambda job, phase: (
        {"phase": phase, "agent": "公司研报-救火员",
         "log": "data/auto_rescue/x.log"}, "已启动"))
    monkeypatch.setattr(ir, "save_job", lambda j: None)

    ir._auto_rescue(job, "_phase_write")
    assert job["auto_rescue"][0]["agent"] == "公司研报-救火员"
    assert any("已自动派公司研报-救火员" in x["msg"] for x in job["log"])


def test_auto_rescue_skips_out_of_scope(monkeypatch):
    from src import industry_report as ir

    job = _job()
    called = {"n": 0}

    def fake_spawn(job, phase):
        called["n"] += 1
        return None, "x"

    monkeypatch.setattr(ir.agent_rescue, "spawn", fake_spawn)
    monkeypatch.setattr(ir, "save_job", lambda j: None)

    ir._auto_rescue(job, "_phase_outline")
    assert called["n"] == 0
    assert "auto_rescue" not in job


def test_auto_rescue_swallows_spawn_error(monkeypatch):
    from src import industry_report as ir

    job = _job()

    def boom(job, phase):
        raise OSError("no claude")

    monkeypatch.setattr(ir.agent_rescue, "spawn", boom)
    monkeypatch.setattr(ir, "save_job", lambda j: None)

    ir._auto_rescue(job, "_phase_write")
    assert any("自动派救火员失败" in x["msg"] for x in job["log"])


def test_start_hook_dispatches_on_error(monkeypatch):
    from src import industry_report as ir

    job = _job()
    seen = []

    def boom(job_id, tid):
        raise RuntimeError("boom")

    boom.__name__ = "_phase_write"
    monkeypatch.setattr(ir, "load_job", lambda job_id: job)
    monkeypatch.setattr(ir, "save_job", lambda j: None)
    monkeypatch.setattr(ir.tasks, "start", lambda *a, **k: a[1](None))
    monkeypatch.setattr(ir, "_auto_rescue", lambda job, phase: seen.append(phase))

    try:
        ir._start("abc12345", boom, "测试")
    except RuntimeError:
        pass
    assert seen == ["_phase_write"]
    assert job["status"] == "error"
    assert job["phase"] == "_phase_write"


def test_spawn_industry_uses_industry_agent(tmp_path, monkeypatch):
    captured = {}
    proc = _FakeProc(pid=99)

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return proc

    monkeypatch.setattr(agent_rescue.shutil, "which", lambda _: "/fake/claude")
    monkeypatch.setattr(agent_rescue.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(agent_rescue.config, "ROOT", tmp_path)
    monkeypatch.setattr(agent_rescue.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(agent_rescue.config, "now_iso", lambda: "2026-11-05T16:30:00")
    monkeypatch.setattr(agent_rescue.config, "rescue_settings", _fake_rescue_settings)

    meta, _why = agent_rescue.spawn(_job(kind="industry"), "_phase_review")
    assert meta["agent"] == "行业研报-救火员"
    assert "行业研报-救火员" in captured["cmd"]


def _stub_spawn_env(monkeypatch, tmp_path, now="2026-11-05T17:00:00"):
    """Popen 换成不真起进程的桩，数据目录与时间固定在 tmp_path。"""
    monkeypatch.setattr(agent_rescue.shutil, "which", lambda _: "/fake/claude")
    monkeypatch.setattr(agent_rescue.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc())
    monkeypatch.setattr(agent_rescue.config, "ROOT", tmp_path)
    monkeypatch.setattr(agent_rescue.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(agent_rescue.config, "now_iso", lambda: now)
    monkeypatch.setattr(agent_rescue.config, "rescue_settings", _fake_rescue_settings)


def test_spawn_missing_anthropic_config(tmp_path, monkeypatch):
    called = {"n": 0}

    def fake_popen(cmd, **kwargs):
        called["n"] += 1

    monkeypatch.setattr(agent_rescue.shutil, "which", lambda _: "/fake/claude")
    monkeypatch.setattr(agent_rescue.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(agent_rescue.config, "ROOT", tmp_path)
    monkeypatch.setattr(agent_rescue.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(agent_rescue.config, "rescue_settings", lambda _m=None: None)

    meta, why = agent_rescue.spawn(_job(model="gonemodel"), "_phase_write")
    assert meta is None
    assert "anthropic" in why
    assert called["n"] == 0


def test_spawn_sweeps_stale_settings(tmp_path, monkeypatch):
    log_dir = tmp_path / "data" / "auto_rescue"
    log_dir.mkdir(parents=True)
    stale = log_dir / "old.settings.json"
    fresh = log_dir / "fresh.settings.json"
    stale.write_text("{}", encoding="utf-8")
    fresh.write_text("{}", encoding="utf-8")
    past = time.time() - agent_rescue.SETTINGS_STALE_S - 60
    os.utime(stale, (past, past))

    _stub_spawn_env(monkeypatch, tmp_path)
    meta, _why = agent_rescue.spawn(_job(), "_phase_write")
    assert meta is not None
    assert not stale.exists()
    assert fresh.exists()


def test_startup_sweep_clears_orphan_settings(tmp_path, monkeypatch):
    # 救火员重启 Flask 杀掉旧进程的清理线程时，含 key 的文件靠下次启动清扫兜底
    log_dir = tmp_path / "data" / "auto_rescue"
    log_dir.mkdir(parents=True)
    orphan = log_dir / "orphan.settings.json"
    orphan.write_text("{}", encoding="utf-8")
    fresh = log_dir / "fresh.settings.json"
    fresh.write_text("{}", encoding="utf-8")
    past = time.time() - agent_rescue.SETTINGS_STALE_S - 60
    os.utime(orphan, (past, past))
    monkeypatch.setattr(agent_rescue.config, "DATA_DIR", tmp_path / "data")

    agent_rescue.startup_sweep()
    assert not orphan.exists()
    assert fresh.exists()


def test_settings_deleted_after_exit(tmp_path, monkeypatch):
    gate = threading.Event()
    proc = _FakeProc(pid=8)
    proc.wait = lambda: gate.wait(5)

    monkeypatch.setattr(agent_rescue.shutil, "which", lambda _: "/fake/claude")
    monkeypatch.setattr(agent_rescue.subprocess, "Popen",
                        lambda cmd, **kw: proc)
    monkeypatch.setattr(agent_rescue.config, "ROOT", tmp_path)
    monkeypatch.setattr(agent_rescue.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(agent_rescue.config, "now_iso",
                        lambda: "2026-11-05T18:00:00")
    monkeypatch.setattr(agent_rescue.config, "rescue_settings", _fake_rescue_settings)

    meta, _why = agent_rescue.spawn(_job(), "_phase_write")
    assert meta is not None
    settings_path = (tmp_path / "data" / "auto_rescue"
                     / "abc12345-phase_write-20261105-180000.settings.json")
    assert settings_path.exists()
    gate.set()
    deadline = time.time() + 5
    while settings_path.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert not settings_path.exists()


def test_spawn_cleans_settings_when_popen_fails(tmp_path, monkeypatch):
    def boom(cmd, **kwargs):
        raise OSError("no cmd")

    monkeypatch.setattr(agent_rescue.shutil, "which", lambda _: "/fake/claude")
    monkeypatch.setattr(agent_rescue.subprocess, "Popen", boom)
    monkeypatch.setattr(agent_rescue.config, "ROOT", tmp_path)
    monkeypatch.setattr(agent_rescue.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(agent_rescue.config, "now_iso",
                        lambda: "2026-11-05T18:30:00")
    monkeypatch.setattr(agent_rescue.config, "rescue_settings",
                        _fake_rescue_settings)

    meta, why = agent_rescue.spawn(_job(), "_phase_write")
    assert meta is None
    assert "启动救火员子进程失败" in why
    assert not list((tmp_path / "data" / "auto_rescue").glob("*.settings.json"))


def test_spawn_tolerates_broken_stdin(tmp_path, monkeypatch):
    # 子进程秒退时 stdin 管道可能已断，spawn 不能因此判派活失败
    proc = _FakeProc(pid=9)

    def broken_write(data):
        raise BrokenPipeError()

    proc.stdin.write = broken_write
    monkeypatch.setattr(agent_rescue.shutil, "which", lambda _: "/fake/claude")
    monkeypatch.setattr(agent_rescue.subprocess, "Popen",
                        lambda cmd, **kw: proc)
    monkeypatch.setattr(agent_rescue.config, "ROOT", tmp_path)
    monkeypatch.setattr(agent_rescue.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(agent_rescue.config, "now_iso",
                        lambda: "2026-11-05T18:40:00")
    monkeypatch.setattr(agent_rescue.config, "rescue_settings",
                        _fake_rescue_settings)

    meta, why = agent_rescue.spawn(_job(), "_phase_write")
    assert meta is not None
    assert "已启动" in why


def test_pid_alive_current_process():
    assert agent_rescue.pid_alive(os.getpid()) is True


def test_pid_alive_dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert agent_rescue.pid_alive(p.pid) is False


def test_pid_alive_rejects_garbage():
    assert agent_rescue.pid_alive(None) is False
    assert agent_rescue.pid_alive("x") is False
    assert agent_rescue.pid_alive(-1) is False


def test_rescue_active_follows_status_and_pid():
    live = os.getpid()
    fresh = datetime.now().replace(microsecond=0).isoformat()
    job = _job(status="error",
               auto_rescue=[{"phase": "_phase_write", "pid": live, "at": fresh}])
    assert agent_rescue.rescue_active(job) is True
    job = _job(status="error",
               auto_rescue=[{"phase": "_phase_write", "pid": -1, "at": fresh}])
    assert agent_rescue.rescue_active(job) is False
    job = _job(status="writing",
               auto_rescue=[{"phase": "_phase_write", "pid": live, "at": fresh}])
    assert agent_rescue.rescue_active(job) is False
    assert agent_rescue.rescue_active(_job(status="error")) is False


def test_rescue_active_treats_long_dispatch_as_stale():
    # pid 复用/进程挂死兜底：派活超过 RESCUE_STALE_S 就不再算救火中
    live = os.getpid()
    stale = (datetime.now() - timedelta(seconds=agent_rescue.RESCUE_STALE_S + 60)
             ).replace(microsecond=0).isoformat()
    job = _job(status="error",
               auto_rescue=[{"phase": "_phase_write", "pid": live, "at": stale}])
    assert agent_rescue.rescue_active(job) is False
