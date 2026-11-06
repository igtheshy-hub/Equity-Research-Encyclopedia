"""网页服务热重载：只在没有任务在跑时才换 worker，免得打断正在生成的研究报告。

Werkzeug 自带的 Stat reloader 一发现 py 文件变化就退出子进程换新的，进程内所有
后台任务线程会跟着一起死。这里把「退出换新」推迟到任务表空闲，最多等
IDLE_WAIT_S；超时仍退出，遗留任务由 industry_report.recover_interrupted_jobs()
在下次启动时标成可续跑。父进程（监督进程）始终不死，救火员不再需要杀进程、
nohup 重起那一套动作。
"""
from __future__ import annotations

import time

from werkzeug._reloader import StatReloaderLoop, reloader_loops

from . import tasks

RELOADER_KEY = "kbidle"
IDLE_WAIT_S = 10 * 60
POLL_S = 2


class IdleGatedReloader(StatReloaderLoop):
    def trigger_reload(self, filename):
        self.log_reload(filename)
        deadline = time.time() + IDLE_WAIT_S
        while self._busy() and time.time() < deadline:
            time.sleep(POLL_S)
        self._do_exit()

    def _busy(self):
        return bool(tasks.active_counts())

    def _do_exit(self):
        # 3 是 werkzeug 约定的「需要重启」退出码，父进程看到会拉起新 worker
        raise SystemExit(3)

    @classmethod
    def register(cls):
        reloader_loops[RELOADER_KEY] = cls
