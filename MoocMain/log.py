# -*- coding: utf-8 -*-
# @Time : 2022/5/23 10:44
# @Author : Melon
# @Site :
# @Note :
# @File : log.py
# @Software: PyCharm
import datetime
import logging
import os
import sys
import re


def redact(message):
    """Redact credentials in legacy request/response diagnostics before rendering or writing."""
    message = str(message)
    message = re.sub(r"(?i)(Bearer\s+)[^\s,'\"}]+", r"\1[hidden]", message)
    message = re.sub(
        r"(?i)((?:access_token|refresh_token|token|api_key|password|authorization|cookie)[\"']?\s*[:=]\s*[\"']?)([^\s,\"'&}\]]+)",
        r"\1[hidden]", message,
    )
    return message


class RedactFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def is_dev_mode() -> bool:
    """命令行是否带 -dev / --dev"""
    return any(arg in ("-dev", "--dev") for arg in sys.argv[1:])


def line_context():
    """Whose work a log line is, e.g. "alice/ab12cd34"; the web server replaces this, elsewhere it is empty."""
    return ""


class DailyFileHandler(logging.FileHandler):
    """Writes each record to that day's file, so a server running past midnight starts a new file
    (the admin's log export reads files by date)."""

    def __init__(self):
        self.day = datetime.date.today()
        super().__init__(self.path(self.day), encoding='utf-8', delay=True)

    @staticmethod
    def path(day):
        return './icve-ai-assistant-log_{t}.log'.format(t=day.isoformat())

    def emit(self, record):
        day = datetime.date.fromtimestamp(record.created)
        if day != self.day:
            # Called under the handler's lock (Handler.handle), so switching files is safe.
            if self.stream:
                self.stream.close()
                self.stream = None
            self.baseFilename, self.day = os.path.abspath(self.path(day)), day
        try:
            who = line_context()
        except Exception:
            who = ""
        record.who = "[%s]" % who if who else ""
        super().emit(record)


class Logger:
    def __init__(self, name):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level=logging.DEBUG)
        if not any(isinstance(f, RedactFilter) for f in self.logger.filters):
            self.logger.addFilter(RedactFilter())

        # Console handlers are owned exclusively by ui.py.
        self.logger.propagate = True

        # 文件日志始终带完整上下文，便于排查
        if not any(isinstance(h, logging.FileHandler) for h in self.logger.handlers):
            formatter = logging.Formatter(
                '[%(asctime)s][%(thread)d][%(filename)s][line: %(lineno)d][%(levelname)s]%(who)s :: %(message)s')
            fh = DailyFileHandler()
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(formatter)
            self.logger.addHandler(fh)

    def get_log(self):
        return self.logger
