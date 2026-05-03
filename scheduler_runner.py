"""Runify · BlogWriter — standalone scheduler runner

用于把 Web 和 Scheduler 拆成两个独立进程：
- Web: START_SCHEDULER=false gunicorn app:app --workers 4 --threads 4
- Scheduler: python scheduler_runner.py

注意：本文件在导入 app 前强制设置 START_SCHEDULER=false，
避免 app.py 模块导入时自动启动 scheduler，然后由本文件手动调用 start_runtime(app)。
"""

import os
import sys
import time
import fcntl

LOCK_FILE = "/tmp/runify_scheduler.lock"

def acquire_singleton_lock():
    """防止多个 scheduler 同时运行（进程级锁）"""
    lock_file = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except BlockingIOError:
        print("[SchedulerRunner] 已有 scheduler 在运行，当前进程退出")
        sys.exit(1)

# ⭐ 在 import app 之前获取锁
_lock = acquire_singleton_lock()

# 必须在 import app 之前设置
os.environ["START_SCHEDULER"] = "false"

from app import app, start_runtime  # noqa: E402


if __name__ == "__main__":
    start_runtime(app)
    print("[SchedulerRunner] scheduler started")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("[SchedulerRunner] stopped")