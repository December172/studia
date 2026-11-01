"""UI-independent, best-effort observations. Never drive business decisions."""
from dataclasses import dataclass
from typing import Optional
import logging


@dataclass(frozen=True)
class ProgressEvent:
    kind: str
    name: str = ""
    total: Optional[int] = None
    status: str = ""
    unit: str = "项"
    completed: Optional[int] = None
    key: str = ""


class Reporter:
    def __init__(self, on_progress=None):
        self.callback = on_progress

    def emit(self, kind, name="", total=None, status="", unit="项", completed=None, key=""):
        if self.callback is not None:
            try:
                self.callback(ProgressEvent(kind, str(name), total, status, unit, completed, str(key)))
            except Exception:
                # Rendering must not stop a course, retry a request or a submission.
                logging.getLogger(__name__).debug("Progress observer failed", exc_info=True)

    def begin(self, name, total=None, unit="项"):
        self.emit("begin", name, total=total, unit=unit)

    def add_total(self, count):
        self.emit("add_total", total=count)

    def detail(self, name):
        self.emit("detail", name)

    def result(self, name, status):
        self.emit("result", name, status=status)

    def resources(self, name, completed, total=None):
        self.emit("resources", name, total=total, completed=completed, unit="项资源")

    def questions(self, name, completed, total=None):
        self.emit("questions", name, total=total, completed=completed, unit="题")

    def unit(self, key, name, state):
        """One learning unit changed state: running, done, skipped or failed."""
        self.emit("unit", name, status=state, key=key)


def log_callback(callback, message):
    """Compatibility for injected output callbacks, with no console dependency."""
    if callback is None:
        from MoocMain.log import Logger
        Logger("ai.runner").get_log().info(message)
    else:
        try:
            callback(message)
        except Exception:
            logging.getLogger(__name__).debug("Log observer failed", exc_info=True)
