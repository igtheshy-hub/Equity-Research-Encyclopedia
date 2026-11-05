"""阶段异常时把研报救火员当外部程序启动。

Flask 里没有 Claude Code 的 agent 运行时，救火员只能以子进程方式启动：
`claude -p --agent <救火员> ...`。子进程用 detached 方式与网页服务分离，
救火员重启 Flask 时不会把自己一起杀掉。

同一任务只自动派一次，救火员自己的定义里已有 5 轮上限；再失败就留给
人工派活，避免网页一报错就反复烧完整会话。

模型和 key 不在本机 ~/.claude/settings.json 里，跟随任务所选模型从 config.json
的 anthropic 子段取（key 读 .env），经 --settings 临时文件传入：settings 的
env 段会覆盖继承的环境变量，直接 Popen(env=) 注入不生效。临时文件在子进程
结束后删除，派活前与服务启动时各清扫一次超龄残留。

提示词必须走标准输入：Windows 的 cmd /c 在第一个换行处截断整条命令，多行
提示词作为命令行参数会把后面的 --agent、权限参数、--settings 全部切掉，
救火员会以普通主会话身份启动（2026-11-05 实测，详见待修清单第 45 条）。
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config

AGENT_BY_KIND = {
    "company": "公司研报-救火员",
    "industry": "行业研报-救火员",
}
AUTO_RESCUE_PHASES = ("_phase_write", "_phase_review", "_phase_docx")
MAX_PER_JOB = 1
# claude 只在启动瞬间读 settings 文件，此后文件即成弃子，删了也不影响在跑的救火进程；
# 10 分钟足够覆盖最慢启动，超龄一律当残留清掉。每次派活前和服务启动时各扫一遍，
# 救火员重启 Flask 杀掉旧进程的清理线程时，下一次服务启动也能把含 key 的文件清干净
SETTINGS_STALE_S = 10 * 60
# 派活超过这个时长仍 error 就不再算救火中：救火员定义上限 5 轮，跑不到这么久；
# 也防 pid 被新进程复用导致网页永远显示救火中
RESCUE_STALE_S = 2 * 3600


def _claude_command(exe: str, *args: str) -> list[str]:
    """Windows 上 claude 是 .CMD 批处理，Popen 不能直接执行，要经 cmd /c。"""
    if os.name == "nt" and Path(exe).suffix.lower() in (".cmd", ".bat"):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/c", exe, *args]
    return [exe, *args]


def _settings_json(rsc):
    """救火员的 --settings 文件内容：只放端点、模型和 key，不继承用户级配置。"""
    return json.dumps({"env": {
        "ANTHROPIC_BASE_URL": rsc["base_url"],
        "ANTHROPIC_MODEL": rsc["model"],
        "ANTHROPIC_AUTH_TOKEN": rsc["key"],
    }}, ensure_ascii=False)


def _delete_settings_after(proc, path):
    """子进程结束后删掉含 key 的临时设置文件，Windows 文件短占用时重试。"""
    proc.wait()
    for _ in range(5):
        try:
            path.unlink(missing_ok=True)
            return
        except OSError:
            time.sleep(1)


def _sweep_stale_settings(log_dir):
    """清掉崩溃或被杀进程留下的设置文件，避免含 key 的文件长期滞留。"""
    now = time.time()
    for p in log_dir.glob("*.settings.json"):
        try:
            if now - p.stat().st_mtime > SETTINGS_STALE_S:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def startup_sweep():
    """服务启动时清扫：上次进程死亡留下的含 key 设置文件在此清掉。"""
    _sweep_stale_settings(config.DATA_DIR / "auto_rescue")


def _prompt(job_id: str, phase: str) -> str:
    return (
        f"救火任务：报告任务 {job_id} 在 {phase} 阶段失败，已自动派你处理。\n"
        f"先读 data/report_jobs/{job_id}.json 的 status、phase、error、stuck 和最后 30 条 log，"
        "再读 data/report_jobs/ 下该任务的 sectionN.md 失败草稿（如存在）与 raw 原文，定位根因。\n"
        "按你的代理定义修：能改代码就改，改完重启网页服务、跑全量测试，"
        f"再调 POST http://127.0.0.1:5000/api/report/{job_id}/continue 续跑，"
        "盯到 status 不在 running/queued。\n"
        "不写 wiki、不删文件、不装包、不碰 git；不放宽锚点核验、勾稽 R1–R4、交付闸门。\n"
        "这是自动派活，同一任务最多一次。修不动就把根因、试过的修法、下一步写进 stuck 后停手。"
    )


def spawn(job, phase):
    """按任务状态启动对应救火员，返回 (meta, reason)。

    meta 非空表示子进程已启动；reason 说明没派活的原因，供调用方写日志。
    """
    if phase not in AUTO_RESCUE_PHASES:
        return None, f"{phase} 不在自动救火范围"
    if job.get("status") != "error":
        return None, "任务不在 error 状态"
    agent = AGENT_BY_KIND.get(str(job.get("kind") or ""))
    if not agent:
        return None, f"任务类型 {job.get('kind')} 没有对应救火员"
    prior = job.get("auto_rescue") or []
    if len(prior) >= MAX_PER_JOB:
        return None, f"本任务已自动派过 {len(prior)} 次"

    job_id = str(job.get("id") or "")
    if not job_id:
        return None, "任务没有 id"
    exe = shutil.which("claude")
    if not exe:
        return None, "找不到 claude 命令"
    rsc = config.rescue_settings(str(job.get("model") or "") or None)
    if not rsc:
        return None, ("任务所选模型没有 Anthropic 兼容端点配置"
                      "（llm.models 的 anthropic 子段或 .env 的 key 缺失）")

    log_dir = config.DATA_DIR / "auto_rescue"
    log_dir.mkdir(parents=True, exist_ok=True)
    _sweep_stale_settings(log_dir)
    stamp = config.now_iso().replace(":", "").replace("-", "").replace("T", "-")
    log_path = log_dir / f"{job_id}-{phase.lstrip('_')}-{stamp}.log"
    settings_path = log_dir / f"{job_id}-{phase.lstrip('_')}-{stamp}.settings.json"
    try:
        settings_path.write_text(_settings_json(rsc), encoding="utf-8")
    except OSError as e:
        return None, f"写救火员设置文件失败：{e}"
    cmd = _claude_command(
        exe,
        "-p",
        "--agent", agent,
        "--allowedTools", "Read,Write,Edit,Glob,Grep,Bash",
        "--disallowedTools", "Bash(git *),Bash(pip *),Bash(pip3 *),Bash(conda *),"
                             "Bash(npm *),Bash(rm *),Bash(del *),Bash(rmdir *)",
        "--permission-mode", "acceptEdits",
        "--permission-prompts", "none",
        "--output-format", "text",
        "--settings", str(settings_path),
    )
    kwargs = {}
    if os.name == "nt":
        # DETACHED_PROCESS 会把 cmd 的标准输出句柄丢掉，救火日志变成空文件；
        # CREATE_NO_WINDOW 不占控制台、能保留重定向，taskkill 父进程也不会带走它
        kwargs["creationflags"] = (subprocess.CREATE_NO_WINDOW
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    try:
        with open(log_path, "ab") as out:
            # 提示词走 stdin：cmd /c 会截断带换行的命令行参数（待修清单第 45 条），
            # claude -p 无位置参数时从标准输入读提示词
            proc = subprocess.Popen(
                cmd, cwd=str(config.ROOT), stdin=subprocess.PIPE,
                stdout=out, stderr=subprocess.STDOUT, close_fds=True, **kwargs)
    except OSError as e:
        settings_path.unlink(missing_ok=True)
        return None, f"启动救火员子进程失败：{e}"
    try:
        proc.stdin.write(_prompt(job_id, phase).encode("utf-8"))
        proc.stdin.close()
    except OSError:
        # 子进程秒退时管道已断，退出原因会留在救火日志里
        pass
    threading.Thread(target=_delete_settings_after, args=(proc, settings_path),
                     daemon=True).start()
    return {
        "at": config.now_iso(),
        "phase": phase,
        "agent": agent,
        "pid": proc.pid,
        "log": log_path.relative_to(config.ROOT).as_posix(),
    }, f"{agent} 已启动"


def pid_alive(pid) -> bool:
    """判断进程是否仍在运行。Windows 上 os.kill(pid, 0) 会真把进程杀掉，只能走 OpenProcess。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def rescue_active(job) -> bool:
    """error 任务的自动救火子进程是否仍在跑，供网页把红色失败换成救火进行中。"""
    if job.get("status") != "error":
        return False
    prior = job.get("auto_rescue") or []
    if not prior:
        return False
    last = prior[-1]
    try:
        started = datetime.fromisoformat(str(last.get("at") or ""))
        if (datetime.now() - started).total_seconds() > RESCUE_STALE_S:
            return False
    except ValueError:
        pass
    return pid_alive(last.get("pid"))
