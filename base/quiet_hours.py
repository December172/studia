"""Beijing-time task curfew, independent of the server's timezone.

Web jobs and their helpers already share a thread-name prefix (reporting.child_thread_name).
Request boundaries use it to wait without restarting a paper or repeating a post.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
QUIET_MESSAGE = "北京时间 23:00–次日 07:00 为夜间暂停时段，期间不能启动或继续任务，07:00 后自动恢复"
CHECKPOINTS = {}


def quiet_hours(now=None):
    now = datetime.fromtimestamp(time.time() if now is None else now, BEIJING)
    active = now.hour >= 23 or now.hour < 7
    change = now.replace(hour=7 if active else 23, minute=0, second=0, microsecond=0)
    if change <= now:
        change += timedelta(days=1)
    return {"active": active, "message": QUIET_MESSAGE, "changes_in": (change - now).total_seconds()}


def checkpoint():
    """Only task threads wait; logins and ordinary page reads keep working."""
    thread = threading.current_thread()
    check = CHECKPOINTS.get(thread.name.split("/", 1)[0])
    if check is not None:
        # Heartbeats belong to one paper, which can end before the enclosing job does.
        check(getattr(thread, "studia_stop", None))
