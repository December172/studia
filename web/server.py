"""Local web interface: a 127.0.0.1-only HTTP server for every supported platform."""
from __future__ import annotations

import base64
import hmac
import json
import logging
import mimetypes
import os
import queue
import random
import re
import secrets
import threading
import time
import webbrowser
import zlib
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from concurrent.futures import ThreadPoolExecutor

import MoocMain.log
from AIMoocMain.api import AIMoocApi
from AIMoocMain.exam_api import ExamAPI, ExamError, ExamServiceError, TaskUnavailableError
from AIMoocMain.export_questions import paper_display_name, paper_exam_id
from AIMoocMain.main import AIMoocHandler, course_units as mooc_units
from ai.client import AIClient, AIConfig, AIError, AIServiceError
from ai.runner import AnswerRunner
from base.concurrency import INTERACTIVE_WORKERS
from base.auth import AuthenticationExpired, PlatformUnavailable
from base.quiet_hours import CHECKPOINTS, QUIET_MESSAGE, quiet_hours
from ChaoxingMain.api import ChaoxingApi, ChaoxingError, LoginError, WorkUnavailable
from ChaoxingMain.exercise import ChaoxingExerciseRunner
from ChaoxingMain.main import catalog as cx_catalog, course_units as cx_units
from ChaoxingMain.study import ChaoxingStudyHandler
from NewMoocMain.oauth_login import oauth_login
from settings import DEFAULT_PROFILE_ID, ModelProfile, SettingsError, SettingsStore
from task_capabilities import task_availability, task_type_label
from task_metrics import completion_with_score, score_text
from ZHSMain.answer import ZHSAnswerRunner, catalog
from ZHSMain.api import CaptchaRequired, StudyTimeLimit, ZHSApi, ZHSError, course_offers
from ZHSMain.main import ZHSHandler, course_units as zhs_units
from ZHSMain.meeting import MeetingHandler
from ZHSMain.memory import AnswerMemory
from ZHSMain.qa import ANSWER_CAP, ASK_PACE, DAILY_ANSWERS, DAILY_QUESTIONS, QA_PACE, QUESTION_CAP, REFERENCE_ANSWERS, \
    MAX_QUESTION, QaBridge, _clean_question, answer_questions, publish_questions, qa_room, question_flaw, write_questions
from ai.memory import MoocAnswerMemory
from ZYKMoocMain.api import ZYKMoocApi
from ZYKMoocMain.main import ZYKMoocHandler

STATIC = Path(__file__).resolve().parent / "static"
REVIEW_MINUTES = 30
QR_REFRESHES = 5
QR_TABS = 3  # QR codes kept open per platform: one per tab or device showing the login page
HISTORY = 20
# Jobs one person runs at once (an administrator can change it in hosted mode); more wait in line.
MAX_JOBS = 4
# ponytail: running plus waiting jobs per person, so one browser cannot pile up threads.
MAX_PENDING = 20
SHARED_MODEL_ID = "admin-shared"
# The platform's unit and paper lists take seconds to tens of seconds; reuse them for a while.
CACHE_TTL = 600
LABEL = {"study": "自动学习", "review": "每日复习", "meeting": "见面课", "answer": "AI 答题", "qa": "问答区答题", "ask": "问答区提问",
         "mastery": "掌握度学习"}
# Jobs that play course content conflict with each other; answering is separate.
# Answering the board and asking on it are separate lines, so drafting questions never waits out an answering run.
GROUP = {"study": "learn", "review": "learn", "meeting": "learn", "answer": "answer", "mastery": "answer", "qa": "qa", "ask": "ask"}
QA_POSTS = 300  # posts kept per account for the web list
PEER_ANSWERS = 5  # a question with 5 valid answers earns its asker the point; more from us adds nothing
PEER_DELAY = 30 * 60  # seconds a classmate's question waits before people here answer it
PEERS_MARKER = "qa-peers.on"  # present while the person takes part in 站内互答
# How often the Zhihuishu sign-in centre is touched for everyone signed in (Studia.keep_passport). Its ticket
# lapses two hours after last use, taking the Q&A board with it; touching it inside that window keeps it.
KEEPALIVE = 50 * 60
# ...but never past eight hours from the scan: in the keep-alive logs every ticket still held at 7.7–7.9 hours and
# was gone by 8.5–8.8, touched or not (the sign-in centre's defaults: two hours idle, eight at most). Studying
# carries on; the Q&A board needs a fresh scan after this.
PASSPORT_TTL = 8 * 3600
# Set while the sign-in centre says this person's ticket is gone: only a fresh scan brings the Q&A board back, and
# nothing else tells them (studying carries on), so the page asks for one.
PASSPORT_LAPSED = "zhs_passport_lapsed"
DRAFT_TIMEOUT = 300  # reasoning models take one to two minutes to write a single question
# Running job id -> its Studia app. A job thread is named "studia-job-<id>" and passes that
# name on to the helper threads it starts, so a log line can be traced back to its job.
JOBS = {}
# Restarts (an update): jobs that were running are written next to the settings and resumed on the
# next start if it comes within the hour; each job is resumed at most a few times in a row.
RESUME_WINDOW = 3600
RESUME_LIMIT = 3
STEADY = 600  # seconds a resumed job must run before its resume count starts over
INTERRUPTED = "更新时中断"
JOB_THREAD = re.compile(r"studia-job-([0-9a-f]+)")


def _job_context():
    """Tag runtime-log lines written by a job (or a helper thread it started) with its owner and job id."""
    match = JOB_THREAD.match(threading.current_thread().name)
    if not match:
        return ""
    owner = getattr(JOBS.get(match[1]), "owner", None)
    return "%s/%s" % (owner, match[1]) if owner else "任务 %s" % match[1]


MoocMain.log.line_context = _job_context
# Into the daily log file like the platform modules, so the admin's log export shows sign-ins and expiries.
logger = MoocMain.log.Logger(__name__).get_log()

# Order is the sidebar order. "answer"/"review" mark what the web UI can run today.
PLATFORMS = {
    "mooc": {"name": "MOOC", "short": "MOOC", "login": "browser", "api": AIMoocApi,
             "handler": AIMoocHandler, "units": mooc_units, "answer": True, "review": False, "needs_ai": True},
    "zyk": {"name": "资源库", "short": "资源库", "login": "browser", "api": ZYKMoocApi,
            "handler": ZYKMoocHandler, "units": None, "answer": False, "review": False, "needs_ai": False, "hidden": True},
    "zhs": {"name": "智慧树", "short": "智慧树", "login": "qr", "api": ZHSApi,
            "handler": ZHSHandler, "units": zhs_units, "answer": True, "review": True, "qa": True, "needs_ai": False},
    "cx": {"name": "超星", "short": "超星", "login": "qr", "password": True, "api": ChaoxingApi,
           "handler": ChaoxingStudyHandler, "units": cx_units, "answer": True, "review": False,
           "needs_ai": False},
}


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def key_hint(key):
    key = key or ""
    return (key[:3] + "…" + key[-4:]) if len(key) > 10 else "已保存"


class Platform:
    """One platform account: its client, cached courses and an in-flight login."""

    def __init__(self, pid):
        self.id = pid
        self.spec = PLATFORMS[pid]
        self.client = None
        self.rows = []
        self.entries = {}
        # (kind, course id) -> (time, value) for the slow unit and paper lists.
        self.cache = {}
        self.login = None
        # QR login id -> {"client", "qr", "refreshes"}. Each page showing a code polls its own: with one
        # shared code, a second tab starting over swapped the code out under the first, whose scans then
        # never registered and people scanned again and again.
        self.qr = {}

    def public(self, logged_in, rescan=False, rescan_at=None):
        spec = self.spec
        return {"id": self.id, "name": spec["name"], "short": spec["short"], "login": spec["login"],
                "password": bool(spec.get("password")), "logged_in": logged_in, "rescan": rescan,
                "rescan_at": rescan_at,
                "features": {"study": spec.get("study", True), "answer": spec["answer"],
                             "review": spec["review"], "qa": bool(spec.get("qa"))}}


class Studia:
    """All state behind the web UI; one running job at a time across platforms."""

    def __init__(self, store=None, oauth=oauth_login, *, reports_dir="ai_results", oauth_url=None, memory=None, mooc_memory=None,
                 job_limit=None, zhs_parallel=None, peers=None, shared_store=None):
        self.store = store or SettingsStore()
        self.shared_store = shared_store
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.platforms = {pid: Platform(pid) for pid, spec in PLATFORMS.items() if not spec.get("hidden")}
        self.oauth = oauth
        # Hosted mode: answer reports stay in the user's own directory, jobs share
        # a site-wide slot pool, and browser login returns to the server instead
        # of a localhost listener.
        self.reports_dir = reports_dir
        # How many of this person's jobs may run at once; read each time, so an admin's change applies at once.
        self.job_limit = job_limit or (lambda: MAX_JOBS)
        # Whether different Zhihuishu courses may play videos at the same time (an admin switch, off by default).
        self.zhs_parallel = zhs_parallel or (lambda: False)
        # Hosted mode: questions other people on this site asked in the same class, answered first (Hub.peer_questions).
        self.peers = peers or (lambda course_id, recruit_id: [])
        self.oauth_url = oauth_url
        # Hosted mode passes the site-wide memory; locally it lives next to the settings file.
        self.memory = memory
        self.mooc_memory = mooc_memory
        # The account name in hosted mode, shown in runtime-log lines; None locally.
        self.owner = None
        self.jobs = {}
        self.history = self._saved().get("history", [])[:HISTORY]
        # Set while the service shuts down: what was running stays on disk for resume().
        self.closing = False
        self.subscribers = set()

    # ------------------------------------------------------------ events

    def publish(self, event):
        job = self.jobs.get(event.get("job"))
        if job is not None and event.get("type") in ("log", "progress"):
            line = _history_line(event)
            if line:
                job["lines"].insert(0, line)
                del job["lines"][60:]
        if job is not None and event.get("type") == "progress":
            _track(job.setdefault("progress", {}), event)
            if event.get("kind") == "unit" and event.get("status") != "running":
                self._mark_unit(job, event.get("key"), event.get("status"))
        for subscriber in list(self.subscribers):
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                pass

    def _mark_unit(self, job, unit_id, unit_state):
        """Carry a finished unit into the cached grid. The cache holds the platform's answer from when the job
        began, so a page reloaded mid-run showed every unit learned since then as not started again."""
        platform = self.platforms.get(job["platform"])
        hit = platform and platform.cache.get(("units", job["course"]))
        for chapter in (hit[1] if hit else []):
            for unit in chapter.get("units", []):
                if unit.get("id") == unit_id:
                    unit["state"] = unit_state

    def progress_of(self, job):
        """The on_progress callback for one job's business code."""
        return lambda event: self.publish(dict(asdict(event), type="progress", job=job["id"]))

    def progress_event(self, job, kind, name, *, total=None, status="", unit="项"):
        self.publish({"type": "progress", "kind": kind, "name": name, "total": total,
                      "status": status, "unit": unit, "completed": None, "job": job["id"]})

    # ------------------------------------------------------------ state

    def platform(self, pid):
        platform = self.platforms.get(pid)
        if platform is None:
            raise HttpError(404, "不支持这个平台")
        return platform

    def logged_in(self, platform):
        return platform.client is not None or bool(self.store.session(platform.id))

    def state(self):
        return {"platforms": [p.public(self.logged_in(p), *self.passport(p)) for p in self.platforms.values()],
                "jobs": self._running(), "history": [_job_public(j) for j in self.history],
                "job_limit": self.job_limit(), "zhs_parallel": self.zhs_parallel(), "quiet_hours": quiet_hours()}

    def client(self, platform):
        with self.lock:
            if platform.client is None:
                saved = self.store.session(platform.id)
                if not saved:
                    raise HttpError(401, "请先登录%s" % platform.spec["name"])
                if "cookies" in saved:
                    platform.client = platform.spec["api"](cookies=saved["cookies"])
                else:
                    platform.client = platform.spec["api"](access_token=saved["access_token"])
            return platform.client

    def drop(self, platform):
        with self.lock:
            if platform.client is not None and hasattr(platform.client, "close"):
                platform.client.close()
            platform.client, platform.rows, platform.entries, platform.cache = None, [], {}, {}

    def passport(self, platform):
        """(rescan, rescan_at) for the page: whether the Q&A board waits for a fresh scan, and when the ticket
        from the last scan runs out (None when the scan time is unknown or the ticket is already gone)."""
        if platform.id != "zhs" or not self.logged_in(platform):
            return False, None
        ends = self.passport_ends()
        if self.store.get(PASSPORT_LAPSED) or (ends is not None and time.time() >= ends):
            return True, None
        return False, ends

    def passport_ends(self):
        since = self.store.get("zhs_signed_in_at")
        return since + PASSPORT_TTL if isinstance(since, (int, float)) else None

    def keep_passport(self):
        """Keep this person's Zhihuishu logins from going idle. The business login answers one light call (a
        login it confirms gone is deleted, so the page asks for a scan). The sign-in centre is touched and the
        answer logged with the hours since the scan, until it says the ticket is gone past the eight-hour mark:
        then only a scan helps and asking again would fill the log. True/False for the ticket, None when not
        signed in or the platform could not be reached."""
        platform = self.platforms.get("zhs")
        if platform is None or not self.logged_in(platform):
            return None
        self.client(platform)
        if self.signed_in(platform) is False:  # one call now and then keeps it from going idle while nothing runs
            self.expire(platform)
            return None
        ends = self.passport_ends()
        if self.store.get(PASSPORT_LAPSED) and (ends is None or time.time() >= ends):
            return False
        try:
            alive = self.client(platform).passport_alive()
        except Exception as exc:  # a network blip says nothing about the ticket
            logger.info("%s智慧树登录中心保活没连上：%s", self.who(), type(exc).__name__)
            return None
        since = self.store.get("zhs_signed_in_at")
        age = "（扫码后 %.1f 小时）" % ((time.time() - since) / 3600) if isinstance(since, (int, float)) else ""
        if alive:
            logger.info("%s智慧树登录中心票据有效，已保活%s", self.who(), age)
        else:
            logger.warning("%s智慧树登录中心票据已失效，问答区要重新扫码才能进%s", self.who(), age)
        self.passport_seen(alive)
        self.save_sessions(platform)  # the sign-in centre may have renewed cookies
        return alive

    def passport_seen(self, alive):
        """Remember what the sign-in centre last said; the page asks for a fresh scan while the ticket is gone.
        Open pages hear about a change at once."""
        if bool(self.store.get(PASSPORT_LAPSED)) == (not alive):
            return
        self.store.set(PASSPORT_LAPSED, None if alive else time.time())
        self.publish({"type": "account"})

    def board_refused(self, platform):
        """The Q&A board sent us to sign in. Ask the sign-in centre why: a lapsed ticket needs a fresh scan,
        anything else may pass on another try."""
        try:
            alive = self.client(platform).passport_alive()
        except Exception:
            alive = None
        if alive is False:
            self.passport_seen(False)
            return "智慧树登录中心过期了，重新扫码后问答区才能进"
        return "问答区没进去，稍后重试；一直不行就重新扫码智慧树"

    def who(self):
        """Log prefix naming the Studia account on a hosted server; request threads carry no job tag."""
        return "%s · " % self.owner if self.owner else ""

    def signed_in(self, platform):
        """Ask the platform plainly whether the login still holds: True, False, or None when it cannot tell.
        A 401/403 from one business call is not proof; rate limiting and a course the account cannot open
        answer the same way, and wiping the login over them signed people out after nothing had expired."""
        client = platform.client
        if client is None:
            return False
        try:
            if hasattr(client, "signed_in"):
                return bool(client.signed_in())
            client.get_info(strict=True)
            return True
        except AuthenticationExpired:
            return False
        except Exception:
            return None

    def expire(self, platform):
        """A call came back unauthorized. Delete the saved login only once the platform confirms it is gone."""
        name = platform.spec["name"]
        if self.signed_in(platform) is not False:
            logger.warning("%s%s 拒绝了一次请求，但登录仍然有效，保留登录", self.who(), name)
            return HttpError(503, "%s 暂时拒绝了请求，请稍后重试" % name)
        logger.warning("%s%s 登录已失效，已删除保存的登录状态", self.who(), name)
        with self.lock:
            self.drop(platform)
            self.store.clear_session(platform.id)
        self.publish({"type": "account"})
        return HttpError(401, "%s 登录已过期，请重新登录" % name)

    # ------------------------------------------------------------ login

    def login_start(self, pid, body=None):
        platform = self.platform(pid)
        body = body or {}
        # 扫码登录的平台也可以在网页上改用账号密码（平台支持 password 能力时）。
        # 前端提交账号密码表单时一定带 username/password 字段；空表单也走账号密码，
        # 由 password_login 报「请输入账号和密码」，而不是悄悄改成扫码。
        if platform.spec["login"] == "password" or (
                platform.spec.get("password") and ("username" in body or "password" in body)):
            return self.password_login(platform, body)
        with self.lock:
            if platform.spec["login"] == "qr":
                client = platform.spec["api"]()
                qr_token, image = client.login_qr()
                qid = secrets.token_hex(6)
                platform.qr[qid] = {"client": client, "qr": qr_token, "refreshes": 0}
                for old in list(platform.qr)[:-QR_TABS]:
                    platform.qr.pop(old)["client"].close()
                logger.info("%s%s 出了登录二维码（%d 个页面在等扫码）", self.who(), platform.spec["name"], len(platform.qr))
                return {"qr": _data_url(image), "id": qid}
            if self.oauth_url is not None:
                platform.login = {"status": "waiting", "message": "请在新打开的页面登录职教云，完成后会自动回到这里"}
                return {"status": "waiting", "message": platform.login["message"], "open": self.oauth_url(pid)}
            if platform.login and platform.login.get("status") == "waiting":
                return {"status": "waiting", "message": "已在浏览器打开登录页"}
            platform.login = {"status": "waiting", "message": "已在浏览器打开职教云登录页，登录完成后会自动回到这里"}
            threading.Thread(target=self._browser_login, args=(platform,), daemon=True).start()
            return {"status": "waiting", "message": platform.login["message"]}

    def password_login(self, platform, body):
        """Sign in with the platform's own account form and keep the cookie jar.

        The password is never stored: only the cookie the platform hands back is saved, so a
        later request can restore the session without asking again.
        """
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if not username or not password:
            raise HttpError(400, "请输入%s账号和密码" % platform.spec["name"])
        try:
            client = platform.spec["api"](username, password)
        except LoginError as exc:
            raise HttpError(401, str(exc) or "账号或密码不正确")
        except (AuthenticationExpired, PlatformUnavailable, ChaoxingError) as exc:
            raise HttpError(502, str(exc) or "登录失败，请稍后重试")
        try:
            rows = client.course_list()
        except AuthenticationExpired:
            client.close()
            raise HttpError(401, "%s登录已过期，请重新登录" % platform.spec["name"])
        except (PlatformUnavailable, ChaoxingError) as exc:
            client.close()
            raise HttpError(502, str(exc) or "课程列表读取失败")
        with self.lock:
            self.drop(platform)
            platform.client, platform.rows = client, rows
            try:
                self.store.save_session(platform.id, client)
            except SettingsError:
                # A failed write must not throw away a login that already works this session.
                logger.warning("保存%s登录状态失败，本次登录仍可使用", platform.spec["name"], exc_info=True)
        logger.info("%s%s 登录成功", self.who(), platform.spec["name"])
        self.publish({"type": "account"})
        return {"status": "done", "message": "登录成功"}

    def _browser_login(self, platform):
        token = self.oauth(timeout=300)
        if not token:
            platform.login.update(status="failed", message="登录失败或超时，请重试")
            return
        self.finish_browser_login(platform.id, token)

    def finish_browser_login(self, pid, token):
        platform = self.platform(pid)
        if platform.login is None:
            platform.login = {"status": "waiting", "message": ""}
        state = platform.login
        try:
            client = platform.spec["api"](token=token)
            if not client.access_token:
                raise PlatformUnavailable("未获取有效的平台登录状态，请重试")
            client.my_course_list(page_size=9999, strict=True)
            self.drop(platform)
            platform.client = client
            self.store.save_session(platform.id, client)
            state.update(status="done", message="登录成功")
            logger.info("%s%s 登录成功", self.who(), platform.spec["name"])
            self.publish({"type": "account"})
        except Exception as exc:
            logger.exception("%s%s 浏览器登录失败", self.who(), platform.spec["name"])
            state.update(status="failed", message=str(exc) or "登录失败")

    def login_status(self, pid, qid=""):
        platform = self.platform(pid)
        name = platform.spec["name"]
        with self.lock:
            if platform.spec["login"] == "browser":
                login = platform.login
                if login is None:
                    raise HttpError(409, "请先开始登录")
                if login["status"] != "waiting":
                    platform.login = None
                return {"status": login["status"], "message": login["message"]}
            login = platform.qr.get(qid)
            if login is None:  # a newer page took its place, or it already finished
                return {"status": "failed", "message": "这个二维码已失效，请点击刷新"}
            try:
                result = login["client"].login_qr_status(login["qr"])
            except PlatformUnavailable as exc:
                return {"status": "waiting", "message": str(exc)}
            status = result.get("status")
            if status == -1:
                return {"status": "waiting", "message": "等待扫码"}
            if status == 0:
                return {"status": "scanned", "message": "已扫码，请在手机上确认登录"}
            if status == 1:
                client = platform.qr.pop(qid)["client"]
                try:
                    client.finish_qr_login(result.get("oncePassword", ""))
                except Exception as exc:  # 手机上已确认，但换登录状态失败：说出来，别让页面一直转
                    logger.exception("%s%s 扫码已确认，但登录没完成", self.who(), name)
                    client.close()
                    return {"status": "failed", "message": "扫码已确认，但登录没完成（%s），请刷新二维码重试" % (str(exc) or "平台没响应")}
                if hasattr(client, "shared_exam_sso"):  # 智慧树扫码后还要建考试会话
                    try:
                        client.shared_exam_sso()
                    except (AuthenticationExpired, PlatformUnavailable, ZHSError):
                        logger.warning("考试会话初始化失败，进入共享课试卷时将重新尝试")
                for other in platform.qr.values():  # the other pages' codes are moot now
                    other["client"].close()
                platform.qr.clear()
                self.drop(platform)
                platform.client = client
                self.store.save_session(platform.id, client)
                logger.info("%s%s 扫码登录成功", self.who(), name)
                if pid == "zhs":  # a scan hands out a new ticket; its eight hours count from here
                    self.store.set("zhs_signed_in_at", time.time())
                    self.store.set(PASSPORT_LAPSED, None)
                self.publish({"type": "account"})  # other tabs and devices pick up the login
                return {"status": "done", "message": "登录成功"}
            if status == 2:
                login["refreshes"] += 1
                if login["refreshes"] > QR_REFRESHES:
                    platform.qr.pop(qid)["client"].close()
                    return {"status": "failed", "message": "二维码多次过期，请点击刷新"}
                try:
                    refresh = getattr(login["client"], "refresh_login_qr", None)
                    login["qr"], image = refresh(login["qr"]) if refresh else login["client"].login_qr()
                except (PlatformUnavailable, ChaoxingError, ZHSError) as exc:
                    platform.qr.pop(qid)["client"].close()
                    return {"status": "failed", "message": "二维码刷新失败：%s" % (str(exc) or "平台没响应")}
                return {"status": "refreshed", "message": "二维码已过期，已自动刷新", "qr": _data_url(image)}
            if status == 3:
                platform.qr.pop(qid)["client"].close()
                logger.info("%s%s 扫码登录中止：%s", self.who(), name, result.get("message") or "已在手机上取消")
                return {"status": "failed", "message": result.get("message") or "已在手机上取消登录"}
            return {"status": "waiting", "message": ""}

    def logout(self, pid):
        platform = self.platform(pid)
        with self.lock:  # under the lock so save_sessions() cannot write the old cookies back in between
            self.drop(platform)
            self.store.clear_session(pid)
            if pid == "zhs":
                self.store.set(PASSPORT_LAPSED, None)
                self.store.set("zhs_signed_in_at", None)
        logger.info("%s%s 退出登录", self.who(), platform.spec["name"])
        self.publish({"type": "account"})
        return {}

    # ------------------------------------------------------------ courses

    def courses(self, pid):
        platform = self.platform(pid)
        client = self.client(platform)
        try:
            if pid == "cx":
                rows = client.course_list()
            else:
                result = client.my_course_list(page_size=9999, strict=True) if pid != "zhs" else client.my_course_list()
                rows = result.get("rows") if isinstance(result, dict) else None
        except AuthenticationExpired:
            raise self.expire(platform)
        except ChaoxingError as exc:
            raise HttpError(502, str(exc) or "课程列表读取失败")
        if not isinstance(rows, list):
            raise PlatformUnavailable("课程列表读取失败，请稍后重试")
        shares = self._score_shares(platform, client, rows)
        with self.lock:
            platform.rows = rows
        return {"courses": [_course(pid, row, shares.get(_course_id(pid, row))) for row in rows]}

    def _score_shares(self, platform, client, rows):
        """Each shared course's assessment rule (course id -> shares), read side by side and kept a while.
        A course whose rule cannot be read is left out, so it keeps the platform's features."""
        shared = [row for row in rows if (row.get("zhs") or {}).get("kind") == "shared"]

        def read(row):
            z = row["zhs"]
            try:
                return self._cached(platform, ("shares", z["courseId"]), False,
                                    lambda: client.score_shares(z["courseId"], z["recruitId"]))
            except AuthenticationExpired:
                raise
            except Exception as exc:
                logger.info("读不到「%s」的考核规则：%s", row.get("courseName"), exc)
                return None

        if not shared:
            return {}
        with ThreadPoolExecutor(min(len(shared), INTERACTIVE_WORKERS)) as pool:
            found = list(pool.map(read, shared))
        return {_course_id(platform.id, row): value for row, value in zip(shared, found) if value is not None}

    def score(self, pid, course_id, fresh=False):
        """One shared course's earned points per share (the 成绩分析 page), read when its page opens."""
        platform = self.platform(pid)
        row = self.find_row(platform, course_id)
        z = row.get("zhs") or {}
        if pid != "zhs" or z.get("kind") != "shared":
            raise HttpError(400, "这门课没有考核得分可读")
        client = self.client(platform)
        try:
            return {"earned": self._cached(platform, ("earned", z["courseId"]), fresh,
                                           lambda: client.score_earned(z["courseId"], z["recruitId"]))}
        except AuthenticationExpired:
            raise self.expire(platform)

    def find_row(self, platform, course_id):
        if not platform.rows:
            self.courses(platform.id)
        for row in platform.rows:
            if _course_id(platform.id, row) == course_id:
                return row
        raise HttpError(404, "没有找到这门课程，请刷新课程列表")

    def _cached(self, platform, key, fresh, load):
        hit = platform.cache.get(key)
        if hit and not fresh and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        value = load()
        platform.cache[key] = (time.time(), value)
        return value

    def tasks(self, pid, course_id, fresh=False):
        platform = self.platform(pid)
        if not platform.spec["answer"]:
            raise HttpError(400, "%s 的网页版答题还在接入中，请先用终端版" % platform.spec["name"])
        row = self.find_row(platform, course_id)
        listed, result = self._cached(platform, ("tasks", course_id), fresh, lambda: self._read_tasks(platform, row))
        with self.lock:
            platform.entries[course_id] = listed
        return {"tasks": result}

    def _read_tasks(self, platform, row):
        pid = platform.id
        client = self.client(platform)
        try:
            if pid == "cx":
                entries = cx_catalog(client, row)
            elif pid == "zhs":
                entries = catalog(client, row)
            else:
                entries = _mooc_catalog(client, row)
        except AuthenticationExpired:
            raise self.expire(platform)
        except CaptchaRequired as exc:
            raise HttpError(409, str(exc))
        except ChaoxingError as exc:
            raise HttpError(502, str(exc) or "作业列表读取失败")
        except ExamError as exc:
            raise HttpError(502, str(exc) or "试卷列表读取失败")
        listed = {}
        result = []
        for entry in entries:
            _, item, category = entry
            task_id = _task_id(pid, item, category)
            listed[task_id] = entry
            availability = task_availability(item, category)
            done = item.get("_completion_status") == "submitted"
            if pid == "zhs":
                score = item.get("score")
            else:
                score = score_text(item)
                score = None if score in ("?", "--") else score
            result.append({
                "id": task_id,
                "name": (item.get("name") if pid in ("zhs", "cx") else paper_display_name(item)) or "未命名试卷",
                "category": "作业" if pid == "zhs" and category == 1 else "考试" if pid == "zhs" else task_type_label(item, category),
                # A handed-in paper reads 已提交 even once closed or out of attempts; the reason says which.
                "status": "已提交" if done else availability.label or STATUS_LABEL.get(item.get("_completion_status"), "未作答"),
                "score": score,
                "available": availability.available,
                "done": done and not item.get("_redo"),
                "reason": availability.reason,
                "hint": item.get("_hint", ""),
                "redo": bool(item.get("_redo")),
                "redo_left": item.get("answerCount") if item.get("_redo") else None,
            })
        return listed, result

    def units(self, pid, course_id, fresh=False):
        """Every learning unit of one course by chapter, for the course grid."""
        platform = self.platform(pid)
        if platform.spec["units"] is None:
            raise HttpError(400, "%s 暂不支持查看学习单元" % platform.spec["name"])
        row = self.find_row(platform, course_id)
        try:
            return {"chapters": self._cached(platform, ("units", course_id), fresh,
                                             lambda: platform.spec["units"](self.client(platform), row))}
        except AuthenticationExpired:
            raise self.expire(platform)
        except CaptchaRequired as exc:
            raise HttpError(409, str(exc))
        except ChaoxingError as exc:
            raise HttpError(502, str(exc) or "学习单元读取失败") from None

    # ------------------------------------------------------------ jobs

    def start_job(self, body, *, minutes=None, resumes=0, paused=False, restoring=False):
        if not restoring:
            self._check_daytime()
        platform = self.platform(str(body.get("platform", "")))
        kind = body.get("type")
        course_id = str(body.get("course", ""))
        allowed = ({"study"} if platform.spec.get("study", True) else set()) | \
                  ({"review"} if platform.spec["review"] else set()) | \
                  ({"answer"} if platform.spec["answer"] else set()) | \
                  ({"mastery", "meeting"} if platform.id == "zhs" else set()) | \
                  ({"qa", "ask"} if platform.spec.get("qa") else set())
        if kind not in allowed:
            raise HttpError(400, "这个平台暂不支持该操作")
        row = self.find_row(platform, course_id)
        if kind == "mastery" and not course_features(platform.id, row)["mastery"]:
            raise HttpError(400, "掌握度学习目前仅支持智慧树共享课")
        room = self.qa_room(platform, row) if kind in ("qa", "ask") else None  # reads the score: before the lock
        if kind == "meeting" and (row.get("zhs") or {}).get("kind") != "shared":
            raise HttpError(400, "见面课仅支持智慧树共享课")
        if kind in ("review", "qa", "ask"):
            shares = (platform.cache.get(("shares", (row.get("zhs") or {}).get("courseId"))) or (0, None))[1]
            if not course_features(platform.id, row, shares)["qa" if kind == "ask" else kind]:
                raise HttpError(400, "这门课的考核里没有%s，不用%s" % (
                    ("学习习惯分", "每日复习") if kind == "review" else ("问答互动分", "刷问答区")))
        with self.lock:
            if not restoring:  # Course/score reads above may have crossed 23:00.
                self._check_daytime()
            self._check_conflicts(platform.id, course_id, kind)
            job = {"id": secrets.token_hex(4), "kind": kind, "title": LABEL[kind], "course": course_id,
                   "platform": platform.id, "course_name": row.get("courseName") or row.get("title") or "课程",
                   "cancel": threading.Event(),
                   "state": "queued", "summary": "", "started": time.time(), "ended": None, "lines": [], "progress": {},
                   "resumes": resumes}
            if resumes:
                job["lines"].append({"text": "服务更新后自动继续", "level": "", "time": time.strftime("%H:%M")})
            if kind == "answer":
                selected = self._selected(platform, course_id, body.get("tasks") or [])
                target = lambda: self._answer(platform, selected, bool(body.get("auto_submit")), job)
            elif kind == "mastery":
                target = lambda: self._mastery(platform, row, job)
            elif kind == "qa":
                options = _qa_options(body)
                if not room["answer"]:
                    raise HttpError(400, room["why"]["answer"])
                options["limit"] = min(options["limit"], room["answer"])
                target = lambda: self._qa(platform, row, options, job)
            elif kind == "ask":
                texts = _ask_texts(body)
                if len(texts) > room["ask"]:
                    raise HttpError(400, room["why"]["ask"] if not room["ask"] else "今天还能发 %d 条提问，删掉几条再发" % room["ask"])
                target = lambda: self._ask(platform, row, texts, job)
            else:
                job["minutes"] = (REVIEW_MINUTES if minutes is None else minutes) if kind == "review" else 0
                target = lambda: self._study(platform, row, job["minutes"], job)
            self.jobs[job["id"]] = job
            JOBS[job["id"]] = self
            CHECKPOINTS["studia-job-%s" % job["id"]] = lambda stop=None: self._checkpoint(job, stop)
            if paused:  # an administrator's pause, carried over a restart
                job["state"] = "paused"
            self._take_turn(job)
            self._save()
        public = _job_public(job)
        self.publish(dict(public, type="job"))
        threading.Thread(target=self._run_job, args=(job, target, platform), daemon=True,
                         name="studia-job-%s" % job["id"]).start()
        # The page shows the job from this reply too, not only from the event stream, which a proxy may hold back.
        return {"ok": True, "id": job["id"], "job": public}

    def _check_conflicts(self, pid, course_id, kind):
        if len(self.jobs) >= MAX_PENDING:
            raise HttpError(409, "进行中和排队的任务已有 %d 个，请等一些结束" % MAX_PENDING)
        for other in self.jobs.values():
            if GROUP[other["kind"]] != GROUP[kind] or other["platform"] != pid:
                continue
            if other["course"] == course_id:
                raise HttpError(409, "这门课正在%s，请等它结束或先停止" % other["title"])
            if pid == "zhs" and GROUP[kind] == "learn" and not self.zhs_parallel():
                # Zhihuishu treats two videos playing on one account as multiple windows.
                # Within one course videos always play one at a time (ZHSHandler), switch or not.
                raise HttpError(409, "智慧树同一账号同时只能播放一个视频，请等「%s」的%s结束"
                                % (other["course_name"], other["title"]))

    def stop_job(self, job_id=None):
        """Stop one job, or every running job when no id is given."""
        with self.lock:
            for job in list(self.jobs.values()):
                if job_id in (None, job["id"]):
                    job["hold"] = None  # a stop outranks a pause or a step aside on its way
                    job["cancel"].set()
        return {}

    # ------------------------------------------------------------ this person's line of jobs

    def act_job(self, action, job_id, body=None, who=""):
        """pause, continue or move one job, by the person or (who="管理员") an administrator. Stop has its own route."""
        if action == "move":
            return self.move_job(job_id, to=(body or {}).get("to"), swap=(body or {}).get("swap"), who=who)
        return (self.pause_job if action == "pause" else self.continue_job)(job_id, who)

    def pause_job(self, job_id, who=""):
        """Set a job aside: it stays in the list but takes no run slot. A learning job that is running
        stops at once and later carries on from the progress the platform recorded."""
        with self.lock:
            job = self._job(job_id)
            if job["state"] == "paused" or job.get("hold") == "pause":
                return {}
            if job["state"] == "queued":
                job["state"] = "paused"
                self._save()
            elif GROUP[job["kind"]] != "learn":
                raise HttpError(400, "答题和问答在运行中不能暂停，只能停止")
            else:
                job["hold"] = "pause"
                job["cancel"].set()
        self._announce(job, "%s暂停了这个任务" % who)
        return {}

    def continue_job(self, job_id, who=""):
        """A paused job joins the back of the queue (the arrows move it up from there)."""
        with self.lock:
            self._check_daytime()
            job = self._job(job_id)
            if job.get("hold") == "pause":   # still stopping: let it come back queued instead
                job["hold"] = "queue"
            elif job["state"] == "paused":
                job["state"] = "queued"
            else:
                return {}
            job["order"] = max(_order(j) for j in self.jobs.values()) + 1
            self._take_turn(job)
            self._save()
        self._announce(job, "%s让这个任务继续，排在最后" % who)
        return {}

    def move_job(self, job_id, to=None, swap=None, who=""):
        """Move a waiting job within this person's queue (to: its new place, 0 = next to run), or swap it into
        a running job's slot (swap: that job's id), which then waits at the front of the queue. The first
        job_limit() jobs in line run: a learning job pushed out steps aside and later carries on from the
        platform's progress. Answering is never interrupted."""
        with self.lock:
            self._check_daytime()
            job = self._job(job_id)
            running, queue = self._line()
            if job not in queue:
                raise HttpError(400, "只能移动排队中的任务")
            queue.remove(job)
            other = None
            if swap is not None:
                other = self.jobs.get(str(swap))
                if other not in running:
                    raise HttpError(409, "那个任务已经不在运行，请刷新")
                if GROUP[other["kind"]] != "learn":
                    raise HttpError(400, "答题和问答在运行中不能替换")
                running[running.index(other)] = job
                queue.insert(0, other)
            else:
                try:
                    queue.insert(max(0, int(to)), job)
                except (TypeError, ValueError):
                    raise HttpError(400, "位置无效") from None
            line = running + queue
            for place, j in enumerate(line):
                j["order"] = place
            limit = self.job_limit()
            # Slots the queued jobs now in front need, beyond the free ones (a job on its way out frees its slot);
            # the learning jobs furthest back give them up.
            short = sum(1 for j in line[:limit] if j["state"] == "queued") - (
                limit - sum(1 for j in self.jobs.values() if j["state"] == "running" and not j.get("hold")))
            aside = [j for j in reversed(line[limit:])
                     if j["state"] == "running" and not j.get("hold") and GROUP[j["kind"]] == "learn"][:max(0, short)]
            for j in aside:
                j["hold"] = "queue"
                j["cancel"].set()
            for j in queue:
                if j["state"] == "queued":
                    self._take_turn(j)
            self._save()
        if other is not None:
            self._announce(job, "%s把这个任务换上，「%s」先排队" % (who, other["course_name"]))
            self._announce(other, "%s让「%s」先跑，这个任务排在队首，学过的不受影响" % (who, job["course_name"]))
        for j in line:  # every place in line may have moved
            if other is None or j not in (job, other):
                self.publish(dict(_job_public(j), type="job"))
        return {}

    def _line(self):
        """This person's running jobs and queue, each in order. Paused jobs, and jobs on their way to a pause,
        are in neither; a job stepping aside is already in the queue. Call with self.lock held."""
        jobs = sorted((j for j in self.jobs.values() if j["state"] != "paused" and j.get("hold") != "pause"), key=_order)
        going = [j for j in jobs if j["state"] == "running" and j.get("hold") != "queue"]
        return going, [j for j in jobs if j not in going]

    def _job(self, job_id):
        job = self.jobs.get(job_id)
        if job is None:
            raise HttpError(404, "任务已经结束")
        return job

    def _announce(self, job, text):
        """Tell the job's page what changed: a line in its log, and its new state."""
        self.publish({"type": "log", "text": text, "level": "", "job": job["id"]})
        self.publish(dict(_job_public(job), type="job"))

    def _check_daytime(self):
        if quiet_hours()["active"]:
            raise HttpError(409, QUIET_MESSAGE)

    def enforce_quiet_hours(self):
        """Stop playback through its existing cancellation path; leave papers/posts in place.

        Browser players must close, since blocking Python alone leaves JavaScript playing.
        Answers wait at request boundaries instead: restarting them could repeat a submission.
        """
        active = quiet_hours()["active"]
        with self.lock:
            changed = False
            for job in list(self.jobs.values()):
                night = active and job["state"] != "paused" and job.get("hold") != "pause"
                if bool(job.get("night_paused")) == night:
                    continue
                job["night_paused"] = night
                changed = True
                if night and job["state"] == "running" and GROUP[job["kind"]] == "learn" and not job["cancel"].is_set():
                    job["hold"] = "night"
                    job["cancel"].set()
                self._announce(job, QUIET_MESSAGE if night else "夜间暂停时段结束，任务按原顺序继续")
            if changed:
                self._save()

    def _checkpoint(self, job, stop=None):
        if stop is not None and stop.is_set():
            raise KeyboardInterrupt
        if not quiet_hours()["active"]:
            return
        self.enforce_quiet_hours()
        while quiet_hours()["active"]:
            if job["cancel"].wait(1) or job["id"] not in self.jobs or (stop is not None and stop.is_set()):
                raise KeyboardInterrupt

    def _set_aside(self, job):
        """The job's work stopped because an administrator paused it or gave its slot away (job["hold"]).
        Keep it in the list, paused or queued, for its thread to take its turn again. Returns False
        when it was a plain stop and the job should end."""
        with self.lock:
            hold = job.pop("hold", None)
            if not hold or self.closing:
                return False
            if job.get("minutes") and job.get("running_since"):
                # Review time still owed: only the unplayed part comes back.
                job["minutes"] = max(1, round(job["minutes"] - (time.time() - job["running_since"]) / 60))
            job.update(state="paused" if hold == "pause" else "queued", running_since=None, cancel=threading.Event())
            self._save()
        self.publish(dict(_job_public(job), type="job"))
        return True

    def _running(self):
        """Running jobs first, then the queue in the order it will run, then the paused ones."""
        rank = {"running": 0, "queued": 1, "paused": 2}
        return [_job_public(job) for job in sorted(self.jobs.values(), key=lambda j: (rank.get(j["state"], 1), _order(j)))]

    def jobs_state(self):
        return {"jobs": self._running(), "history": [_job_public(j) for j in self.history]}

    def _selected(self, platform, course_id, task_ids):
        entries = platform.entries.get(course_id) or {}
        selected = [entries[str(task_id)] for task_id in task_ids if str(task_id) in entries]
        if not selected:
            raise HttpError(400, "请先选择试卷（试卷列表可能已过期，请刷新）")
        return selected

    def _take_turn(self, job):
        """Start a waiting job when this person has a free place; first come, first served, unless an
        the person or an administrator moved it (move_job). A paused job waits for continue_job.
        Call with self.lock held. Returns whether the job may run."""
        if quiet_hours()["active"]:
            return False
        if job["state"] == "running":
            return True
        if job["state"] == "paused":
            return False
        running = sum(1 for other in self.jobs.values() if other["state"] == "running")
        ahead = sum(1 for other in self.jobs.values()
                    if other["state"] == "queued" and other is not job and _order(other) < _order(job))
        if running + ahead >= self.job_limit():
            return False
        job["state"], job["running_since"] = "running", time.time()
        return True

    def _wait_turn(self, job):
        """Wait (cancellably) for a place among this person's running jobs before touching any platform."""
        while True:
            with self.lock:
                waited, ready = job["state"] != "running", self._take_turn(job)
            if ready:
                break
            if job["cancel"].wait(1):
                raise KeyboardInterrupt
        if waited:
            self.publish(dict(_job_public(job), type="job"))

    def _run_job(self, job, target, platform):
        state, summary = "done", ""
        try:
            while True:
                try:
                    self._wait_turn(job)
                    summary = target() or "已完成"
                except KeyboardInterrupt:
                    if not self._set_aside(job):
                        raise
                    continue
                # Paused or stepped aside by an administrator: wait for its turn again, then carry on.
                if not (job["cancel"].is_set() and self._set_aside(job)):
                    break
            if job["cancel"].is_set():
                state, summary = "stopped", "已停止"
        except KeyboardInterrupt:
            state, summary = "stopped", "已停止"
        except (CaptchaRequired, StudyTimeLimit, NeedsRetry) as exc:
            # Only the user can clear these: solve the captcha on the platform, or come back tomorrow.
            state, summary = "waiting", str(exc) or "平台要求验证"
        except AuthenticationExpired:
            state, summary = "waiting", str(self.expire(platform))
        except Exception as exc:
            logger.exception("网页任务失败")
            state, summary = "error", str(exc) or "任务失败"
        finally:
            if platform is not None:
                # The job changed this course's progress and papers; read them afresh next time.
                platform.cache.pop(("units", job["course"]), None)
                platform.cache.pop(("tasks", job["course"]), None)
                for stale in [k for k in platform.cache if k[0] == "earned"]:  # points may have moved
                    platform.cache.pop(stale, None)
                self.save_sessions(platform)
            with self.lock:
                if self.closing and state == "stopped":
                    summary = INTERRUPTED
                job.update(state=state, summary=summary, ended=time.time())
                self.history.insert(0, job)
                del self.history[HISTORY:]
                self.jobs.pop(job["id"], None)
                self._save()
            self.publish(dict(_job_public(job), type="job"))
            JOBS.pop(job["id"], None)
            CHECKPOINTS.pop("studia-job-%s" % job["id"], None)

    # ------------------------------------------------------------ restarts

    def _jobs_path(self):
        directory = getattr(self.store, "directory", None)
        return Path(directory) / "jobs.json" if directory is not None else None

    def _saved(self):
        try:
            data = json.loads(self._jobs_path().read_text(encoding="utf-8"))
        except (AttributeError, OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, stopped_at=None):
        """Write the running jobs and recent history to disk. Call with self.lock held."""
        if self.closing:
            return
        now = stopped_at or time.time()
        active = [{"platform": j["platform"], "type": j["kind"], "course": j["course"], "course_name": j["course_name"],
                   # Only back-to-back restarts count toward RESUME_LIMIT (a job that brings the service down);
                   # one that has studied for a while since its last start is not a crash loop.
                   "resumes": 0 if now - (j.get("running_since") or now) > STEADY else j.get("resumes", 0),
                   # Review time still owed: its length minus what it has already played.
                   "minutes": max(0, j.get("minutes", 0) - (now - j["running_since"]) / 60 if j.get("running_since") else j.get("minutes", 0)),
                   "paused": j["state"] == "paused" or j.get("hold") == "pause",
                   "night_paused": bool(j.get("night_paused")) or (quiet_hours()["active"] and
                                   j["state"] != "paused" and j.get("hold") != "pause")}
                  # In queue order, so a restart starts them again in the order an administrator left them.
                  for j in sorted(self.jobs.values(), key=lambda j: (j["state"] != "running", _order(j)))]
        data = {"stopped_at": stopped_at, "saved_at": now, "active": active,
                "history": [_job_public(j) for j in self.history]}
        path = self._jobs_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            logger.warning("任务记录保存失败", exc_info=True)

    def save_sessions(self, *platforms):
        """Write each live client's current cookies back to the store. The platform renews them while
        they are in use, and the copy saved at sign-in may no longer work after a restart."""
        for platform in platforms or self.platforms.values():
            with self.lock:
                client = platform.client
                if client is None:
                    continue
                try:
                    # Token platforms save the access token; 超星 has none and saves its cookie jar. Only 超星
                    # restores from cookies, so another platform's client without a token keeps its older copy.
                    if getattr(client, "access_token", None) or platform.id == "cx":
                        self.store.save_session(platform.id, client)
                except Exception:  # a cookie jar changing under us, a full disk: keep the older copy
                    logger.warning("保存%s登录状态失败", platform.spec["name"], exc_info=True)

    def shutdown(self):
        """The service is stopping (usually an update): keep the running jobs on disk for resume(), then stop them."""
        self.save_sessions()
        with self.lock:
            self._save(stopped_at=time.time())
            self.closing = True
        self.stop_job()

    def resume(self):
        """Start again the jobs a restart interrupted. Learning continues from the progress the platform
        recorded; answering and Q&A posting are left to the person, since a paper may already be in or an answer public."""
        data = self._saved()
        active = [entry for entry in data.get("active") or [] if isinstance(entry, dict) and entry.get("type") in LABEL]
        if not active:
            return
        fresh = time.time() - (data.get("stopped_at") or data.get("saved_at") or 0) < RESUME_WINDOW
        for entry in active:
            kind = entry["type"]
            if kind == "review" and entry.get("minutes", 0) < 1:
                continue
            # A paused job does not run by itself, so it comes back however long the service was down.
            paused = entry.get("paused") is True and GROUP[kind] == "learn"
            night = entry.get("night_paused") is True
            if kind in ("answer", "mastery", "qa", "ask") or (not paused and not night and (not fresh or entry.get("resumes", 0) >= RESUME_LIMIT)):
                self._record(entry, "stopped", INTERRUPTED)
                continue
            body = {"platform": entry.get("platform"), "type": kind, "course": entry.get("course")}
            for attempt in range(3):
                try:
                    self.start_job(body, minutes=max(1, round(entry.get("minutes", 0))),
                                   resumes=0 if paused or night else entry.get("resumes", 0) + 1, paused=paused, restoring=True)
                    break
                except HttpError as exc:
                    if exc.status == 401:
                        self._record(entry, "waiting", str(exc))
                        break
                    if exc.status != 409:  # 409: the same course is already running again
                        self._record(entry, "error", str(exc))
                    break
                except Exception as exc:
                    # The platform may not answer yet right after a restart; try again shortly.
                    if attempt == 2:
                        self._record(entry, "error", str(exc) or "恢复任务失败")
                    else:
                        time.sleep(20)
        with self.lock:
            self._save()

    def _record(self, entry, state, summary):
        """A history line for a job that resume() did not start again."""
        kind = entry["type"]
        now = time.time()
        job = {"id": secrets.token_hex(4), "kind": kind, "title": LABEL[kind], "course": entry.get("course"),
               "platform": entry.get("platform"), "course_name": entry.get("course_name") or "课程",
               "state": state, "summary": summary, "started": now, "ended": now, "lines": [], "progress": {}}
        with self.lock:
            self.history.insert(0, job)
            del self.history[HISTORY:]
        self.publish(dict(_job_public(job), type="job"))

    def _ai(self, cancel_message, timeout=0):
        """An AI client for the configured model. With a timeout, each call may take at least that long
        and a model whose request format is known is not tested first: on a slow reasoning model the
        test is another minute the person sits waiting for."""
        config = self.ai_config()
        if config is None:
            raise HttpError(400, cancel_message)
        ai = AIClient(replace(config, timeout=max(int(config.timeout or 0), timeout)))
        if timeout and config.api_format != "auto":
            return ai
        store = self._health_store(config)
        try:
            ai.check_connection()
        except AIError as exc:
            ai.session.close()
            store.save_ai_health(config, "error", str(exc))
            raise
        store.save_ai_health(ai.config, "ok", previous_config=config)
        return ai

    def _study(self, platform, row, minutes, job):
        spec = platform.spec
        kwargs = {"client": self.client(platform), "course_rows": [row], "on_progress": self.progress_of(job),
                  "cancel_event": job["cancel"], "max_workers": self.store.concurrency()["study"]}
        if job.get("kind") == "meeting":
            handler = MeetingHandler(**kwargs)
            if handler.blocked is not None:
                raise handler.blocked
            if handler.failed:
                raise ZHSError(handler.summary)
            if handler.pending:
                raise NeedsRetry(handler.summary + "；未开放或需现场参加的课请查看日志")
            return handler.summary
        ai = None
        captcha_ai = None
        if spec["needs_ai"]:
            ai = kwargs["activity_ai"] = self._ai("MOOC 学习会自动参与讨论和评价，请先在设置里添加 AI 模型")
        elif platform.id == "zhs":
            config = self.ai_config()
            if config is not None:
                captcha_ai = kwargs["captcha_ai"] = AIClient(config)
        if minutes:
            kwargs["review_minutes"] = minutes
        try:
            blocked = getattr(spec["handler"](**kwargs), "blocked", None)
            if blocked is not None:
                raise blocked
        finally:
            if ai is not None:
                ai.session.close()
            if captcha_ai is not None:
                captcha_ai.session.close()
        return "今日复习完成" if minutes else "学习完成"

    def _mastery(self, platform, row, job):
        ai = self._ai("掌握度练习需要 AI，请先在设置里添加模型")
        try:
            runner = ZHSAnswerRunner(
                self.client(platform), ai, auto_submit=True, on_progress=self.progress_of(job),
                cancel_event=job["cancel"], max_workers=self.store.concurrency()["ai"],
                memory=self.memory or AnswerMemory(Path(self.store.path).parent / "zhs-answers.json"))
            result = runner.run_mastery(row)
            if result["status"] == "ai_error":
                self._health_store(ai.config).save_ai_health(ai.config, "error", result["message"])
            if result["status"] not in ("submitted", "skipped"):
                raise HttpError(502, result["message"])
            return result["message"]
        finally:
            ai.session.close()

    def _answer(self, platform, selected, auto_submit, job):
        cancel, on_progress = job["cancel"], self.progress_of(job)
        ai = self._ai("请先在设置里添加 AI 模型")
        try:
            workers = self.store.concurrency()["ai"]
            if platform.id == "cx":
                runner = ChaoxingExerciseRunner(self.client(platform), ai, auto_submit=auto_submit,
                                                on_progress=on_progress, cancel_event=cancel)
                unit = "份作业"

                def run_paper(course, item, category):
                    return runner.run(course, item["_point"], item, item["_job_info"])
            elif platform.id == "zhs":
                runner = ZHSAnswerRunner(
                    self.client(platform), ai, auto_submit=auto_submit, on_progress=on_progress,
                    cancel_event=cancel, max_workers=workers,
                    memory=self.memory or AnswerMemory(Path(self.store.path).parent / "zhs-answers.json"))
                run_paper, unit = runner.run_paper, "份试卷"
            else:
                # No manual-answer or confirm callbacks: unanswerable papers stay unsubmitted.
                runner = AnswerRunner(ExamAPI(self.client(platform)), ai, output_dir=self.reports_dir,
                                      auto_submit=auto_submit, on_progress=on_progress, max_workers=workers,
                                      memory=self.mooc_memory or MoocAnswerMemory(Path(self.store.path).parent / "mooc-answers.json"))
                run_paper, unit = runner.run_paper, "份试卷"
            self.progress_event(job, "begin", "AI 答题", total=len(selected), unit=unit)
            done = 0
            for course, item, category in selected:
                if cancel.is_set():
                    break
                name = (item.get("name") if platform.id in ("zhs", "cx") else paper_display_name(item)) \
                    or ("作业" if platform.id == "cx" else "试卷")
                try:
                    result = run_paper(course, item, category)
                except (CaptchaRequired, StudyTimeLimit):
                    self.progress_event(job, "result", name, status="waiting")
                    raise
                except (TaskUnavailableError, WorkUnavailable) as exc:
                    result = {"status": "skipped", "message": str(exc)}
                except AIServiceError as exc:
                    result = {"status": "ai_error", "message": str(exc), "stop_queue": True}
                except ChaoxingError as exc:
                    result = {"status": "error", "message": str(exc)}
                except ExamError as exc:
                    result = {"status": "error", "message": str(exc), "stop_queue": isinstance(exc, ExamServiceError)}
                status = result["status"]
                if status == "ai_error":
                    self.store.save_ai_health(ai.config, "error", result.get("message", "AI 服务失败"))
                self.progress_event(job, "result", name, status=status)
                if result.get("message"):
                    self.publish({"type": "log", "text": f"{name}：{result['message']}", "job": job["id"],
                                  "level": "error" if status in ("error", "ai_error") else ""})
                done += status in ("submitted", "saved", "preview")
                if result.get("stop_queue") or status in ("ai_error", "submission_unknown", "submitting", "paused"):
                    break
            return "处理完成 %d/%d %s" % (done, len(selected), unit)
        finally:
            ai.session.close()

    def _board(self, platform, row):
        """Open the course's Q&A board, wrapped so what gets posted is remembered for the web list."""
        z = row.get("zhs") or {}
        # 共享课带 recruitId；智慧课程只有 classId，问答区 URL 需要两者之一。
        course_id, recruit_id = z.get("courseId"), z.get("recruitId") or z.get("classId")
        if not course_id or not recruit_id:
            raise HttpError(400, "这门课没有可作答的问答区（缺少课程或班级 ID）")
        record = lambda kind, question, text, post_id, qid=None: self._record_post(
            course_id, kind, question, text, post_id, qid=qid, recruit=recruit_id)
        return _Board(QaBridge(self.client(platform).access_token, course_id=course_id, recruit_id=recruit_id), record,
                      peers=lambda: self.peers(course_id, recruit_id) if self.sharing_qa() else [])

    def _pace(self, job, pace=None):
        return lambda _interval: job["cancel"].wait(random.uniform(*(pace or QA_PACE)))  # wakes at once on stop

    def _qa(self, platform, row, options, job):
        """问答区：取题目 → AI 写回答 → 发布。只在平台返回 200 后计数。"""
        ai = self._ai("请先在设置里添加 AI 模型")
        limit = options["limit"]

        def event(kind, name, status=""):
            if kind == "begin":
                self.progress_event(job, "begin", "问答区答题", total=limit, unit="条")
            elif kind == "result":
                self.progress_event(job, "result", name, status=status)

        try:
            with self._board(platform, row) as board:
                stats = answer_questions(board, ai, max_answers=0, limit=limit, interval=1,
                                         state_path=str(Path(self.store.path).parent / "zhs-qa-answers.json"),
                                         should_stop=job["cancel"].is_set, on_event=event, sleep=self._pace(job),
                                         voice=zlib.crc32((self.owner or "local").encode()))  # one person, one voice
            return _tally("回答了 %d 条提问", stats["answered"], stats["failed"])
        except AuthenticationExpired:
            # 问答区每次进站都靠登录中心的票据认人，它比业务登录短命；被跳到登录页不等于整个登录失效。
            # 只停下这个任务，不删登录：真过期了，学习任务会照常发现。
            raise NeedsRetry(self.board_refused(platform)) from None
        finally:
            ai.session.close()

    def _ask(self, platform, row, texts, job):
        """问答区提问：把用户确认过的提问逐条发布。"""
        def event(kind, name, status=""):
            if kind == "begin":
                self.progress_event(job, "begin", "问答区提问", total=len(texts), unit="条")
            elif kind == "result":
                self.progress_event(job, "result", name, status=status)

        try:
            with self._board(platform, row) as board:
                stats = publish_questions(board, texts, interval=1, should_stop=job["cancel"].is_set,
                                          on_event=event, sleep=self._pace(job, ASK_PACE))
            return _tally("发布了 %d 条提问", stats["published"], stats["failed"])
        except AuthenticationExpired:
            raise NeedsRetry(self.board_refused(platform)) from None

    def qa_room(self, platform, row):
        """How many questions and answers this course can still take: what is left under the caps that score
        and today's share of the daily limit. The platform counts yesterday's posts by this morning; today's
        come from our own record."""
        z = row.get("zhs") or {}
        today = _beijing_day(time.time())
        posted = {"ask": 0, "answer": 0}
        for post in self._posts():
            if str(post.get("course")) == str(z.get("courseId")) and _beijing_day(post.get("at") or 0) == today:
                posted[post.get("kind")] = posted.get(post.get("kind"), 0) + 1
        try:
            counts = self._cached(platform, ("earned", z.get("courseId")), False,
                                  lambda: self.client(platform).score_earned(z["courseId"], z["recruitId"]))["qa"]
        except Exception:  # without the score the daily limit still holds
            counts = {}
        counts = counts if isinstance(counts, dict) else {}
        number = lambda value: value if isinstance(value, (int, float)) else 0
        room, why = {}, {}
        for kind, valid, cap, daily, word in (("ask", counts.get("questions"), QUESTION_CAP, DAILY_QUESTIONS, "问"),
                                              ("answer", counts.get("answers"), ANSWER_CAP, DAILY_ANSWERS, "答")):
            room[kind] = qa_room(number(valid), posted[kind], cap, daily)
            why[kind] = ("已满 %d 条，再%s不加分" % (cap, word) if number(valid) + posted[kind] >= cap
                         else "今天%s满了，明天再来" % word)
        return dict(room, posted=posted, why=why)

    def qa_draft(self, pid, body):
        """AI drafts new questions for one course, picking up threads its board has barely answered.
        Nothing is posted: the person reads and edits them, then starts an "ask" job."""
        platform = self.platform(pid)
        course_id = str(body.get("course", ""))
        row = self.find_row(platform, course_id)
        shares = (platform.cache.get(("shares", (row.get("zhs") or {}).get("courseId"))) or (0, None))[1]
        if not course_features(platform.id, row, shares)["qa"]:
            raise HttpError(400, "这门课的考核里没有问答互动分，不用刷问答区")
        # The page asks for one question at a time and shows each as it comes; the ones it already has
        # lead the reference list, so the model steers away from them and a repeat is dropped.
        have = [str(text)[:MAX_QUESTION] for text in body.get("have") or [] if isinstance(text, str)][:5] \
            if isinstance(body.get("have"), list) else []
        ai = self._ai("请先在设置里添加 AI 模型", timeout=DRAFT_TIMEOUT)
        try:
            try:
                reference = self._cached(platform, ("qa_reference", course_id), False, lambda: self._reference(platform, row))
            except AuthenticationExpired:
                raise HttpError(409, self.board_refused(platform)) from None
            try:
                units = self._cached(platform, ("units", course_id), False, lambda: platform.spec["units"](self.client(platform), row))
            except Exception:  # without the lesson list the model asks about the course as a whole
                units = []
            topics = [unit["name"] for chapter in units for unit in chapter.get("units", [])]
            return {"questions": write_questions(ai, have + reference, 1, course_name=row.get("courseName") or "这门课",
                                                 topics=topics)}
        finally:
            ai.session.close()

    def _reference(self, platform, row):
        with self._board(platform, row) as board:
            return [item["content"] for item in board.bridge.questions(max_answers=REFERENCE_ANSWERS, limit=30)]

    def qa_posts(self, pid, course_id):
        platform = self.platform(pid)
        z = self.find_row(platform, course_id).get("zhs") or {}
        row = self.find_row(platform, course_id)
        mine = [p for p in self._posts() if str(p.get("course")) == str(z.get("courseId"))]
        return {"posts": mine[-50:][::-1], "room": self.qa_room(platform, row), "peers": self.sharing_qa()}

    # 站内互答 is opt-in both ways: answer classmates' questions first, and let them answer mine. A marker file next
    # to the person's records, so the site (Hub.peer_questions) can see who takes part without opening their settings.
    def _peers_marker(self):
        return Path(self.store.path).parent / PEERS_MARKER

    def sharing_qa(self):
        return self._peers_marker().exists()

    def set_qa_peers(self, pid, body):
        self.platform(pid)
        marker = self._peers_marker()
        if body.get("on") is True:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        else:
            marker.unlink(missing_ok=True)
        return {"peers": self.sharing_qa()}

    def _posts_path(self):
        return Path(self.store.path).parent / "zhs-qa-posts.json"

    def _posts(self):
        try:
            data = json.loads(self._posts_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _record_post(self, course_id, kind, question, text, post_id, qid=None, recruit=None):
        with self.lock:
            posts = self._posts()
            # recruit and qid let other people on the site find this class's questions and count who answered them.
            posts.append({"course": course_id, "recruit": recruit, "kind": kind, "question": question, "text": text,
                          "id": post_id, "qid": qid, "at": time.time()})
            path = self._posts_path()
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(posts[-QA_POSTS:], ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, path)

    # ------------------------------------------------------------ settings

    def _shared_config(self):
        if self.shared_store is not None and self.shared_store.get("enabled", False):
            return self.shared_store.ai_config()
        return None

    def ai_config(self):
        if self.shared_store is not None and self.store.get("use_shared_model", False):
            config = self._shared_config()
            if config is None:
                raise HttpError(409, "管理员共享模型已停用，请在设置里重新选择 AI 模型")
            return config
        return self.store.ai_config()

    def _model_profiles(self):
        profiles = self.store.ai_profiles()
        shared = self._shared_config()
        selected = self.shared_store is not None and self.store.get("use_shared_model", False)
        if selected:
            for profile in profiles:
                profile.active = False
        if shared is not None:
            profiles.insert(0, ModelProfile(SHARED_MODEL_ID, shared, selected))
        return profiles

    def _health_store(self, config):
        shared = self._shared_config()
        if shared is not None and self.store.ai_fingerprint(shared) == self.store.ai_fingerprint(config):
            return self.shared_store
        return self.store

    def settings(self):
        models = []
        for profile in self._model_profiles():
            config, health = profile.config, self._health_store(profile.config).ai_health(profile.config)
            shared = profile.id == SHARED_MODEL_ID
            models.append({"id": profile.id, "provider": config.provider_name, "model": config.model,
                           "base_url": config.base_url, "key_hint": "" if shared else key_hint(config.api_key),
                           "active": profile.active, "shared": shared,
                           "health": health.get("status") or "", "health_message": health.get("message") or "",
                           "deletable": profile.id not in (DEFAULT_PROFILE_ID, SHARED_MODEL_ID)})
        return {"models": models, "concurrency": self.store.concurrency()}

    def _profile(self, profile_id):
        for profile in self._model_profiles():
            if profile.id == profile_id:
                return profile
        raise HttpError(404, "没有找到这个模型")

    def _config(self, body, previous=None):
        key = str(body.get("api_key", "")).strip() or (previous.api_key if previous else "")
        try:
            return AIConfig(str(body.get("base_url", "")), str(body.get("model", "")), key,
                            provider_name=str(body.get("provider_name", "")),
                            api_format=str(body.get("api_format") or (previous.api_format if previous else "chat_completions")))
        except ValueError as exc:
            raise HttpError(400, str(exc))

    def add_model(self, body):
        try:
            profile_id = self.store.save_ai_profile(self._config(body))
            if body.get("activate", True):
                self.activate_model({"id": profile_id})
        except SettingsError as exc:
            raise HttpError(400, str(exc))
        return {"id": profile_id}

    def update_model(self, profile_id, body):
        profile = self._profile(profile_id)
        if profile_id == SHARED_MODEL_ID:
            raise HttpError(400, "共享模型由管理员统一配置")
        try:
            self.store.save_ai_profile(self._config(body, profile.config), profile_id)
        except SettingsError as exc:
            raise HttpError(400, str(exc))
        return {"id": profile_id}

    def delete_model(self, profile_id):
        self._profile(profile_id)
        if profile_id == SHARED_MODEL_ID:
            raise HttpError(400, "共享模型由管理员统一配置")
        try:
            self.store.delete_ai_profile(profile_id)
        except SettingsError as exc:
            raise HttpError(400, str(exc))
        return {}

    def test_model(self, profile_id):
        config = self._profile(profile_id).config
        previous = replace(config)
        store = self.shared_store if profile_id == SHARED_MODEL_ID else self.store
        client = AIClient(config)
        try:
            client.check_connection()
        except AIError as exc:
            store.save_ai_health(config, "error", str(exc))
            return {"health": "error", "message": str(exc)}
        finally:
            client.session.close()
        store.save_ai_health(config, "ok", previous_config=previous)
        return {"health": "ok", "message": "连接正常"}

    def activate_model(self, body):
        profile_id = str(body.get("id", ""))
        self._profile(profile_id)
        if profile_id == SHARED_MODEL_ID:
            self.store.set("use_shared_model", True)
            return {}
        try:
            self.store.activate_ai_profile(profile_id)
            if self.shared_store is not None:
                self.store.set("use_shared_model", None)
        except (KeyError, SettingsError, ValueError):
            raise HttpError(404, "没有找到这个模型")
        return {}

    def save_concurrency(self, body):
        try:
            self.store.save_concurrency(body.get("ai"), body.get("study"))
        except SettingsError as exc:
            raise HttpError(400, str(exc))
        return {}


class _EventLogHandler(logging.Handler):
    """Forward course and answer log lines (already redacted at the source) to the page."""

    PREFIXES = ("ZHSMain", "AIMoocMain", "ZYKMoocMain", "MoocMain", "ai.")

    def __init__(self, app=None):
        super().__init__(logging.INFO)
        self.app = app

    def emit(self, record):
        # A line belongs to the job whose thread (or a helper thread it named) logged it;
        # lines from any other thread are dropped rather than risk a leak between users.
        match = JOB_THREAD.match(record.threadName or "")
        app = JOBS.get(match[1]) if match else None
        if app is None or (self.app is not None and app is not self.app) or not record.name.startswith(self.PREFIXES):
            return
        try:
            level = "error" if record.levelno >= logging.ERROR else "warn" if record.levelno >= logging.WARNING else ""
            app.publish({"type": "log", "text": record.getMessage(), "level": level, "job": match[1]})
        except Exception:
            pass


STATUS_TEXT = {"success": "完成", "skipped": "跳过", "error": "出错", "submitted": "已交卷", "saved": "已保存答案", "preview": "已生成答案", "paused": "已暂停",
               "ai_error": "AI 出错", "submission_unknown": "交卷结果未知", "waiting": "等你操作"}


STATUS_LABEL = {"submitted": "已提交", "draft": "未提交", "not_started": "未作答", "unknown": "状态未知"}


def _task_id(pid, item, category):
    if pid == "cx":
        # 超星的作业 id 就是 jobid（work-<hash>），同一门课里唯一，跨章节也不会重复。
        return re.sub(r"[^\w-]", "_", str(item.get("jobid") or item.get("id")))
    if pid == "zhs":
        return str(item["id"])
    return "%s-%s" % (category, re.sub(r"[^\w-]", "_", paper_exam_id(item)))


def _mooc_catalog(client, row):
    """MOOC homework, exams and quizzes with their completion state, read in parallel."""
    api = ExamAPI(client)

    def status(exam_id):
        try:
            return api.completion_status(exam_id)
        except ExamError:
            return "unknown"

    with ThreadPoolExecutor(INTERACTIVE_WORKERS) as pool:
        categories = (1, 2, 3)
        listed = [(row, item, category) for category, items
                  in zip(categories, pool.map(lambda category: api.catalog(row, category), categories)) for item in items]
        open_ids = list(dict.fromkeys(paper_exam_id(item) for _, item, category in listed
                                      if task_availability(item, category).available))
        statuses = dict(zip(open_ids, pool.map(status, open_ids)))
    # Papers not checked (closed, attempts used up) still count as handed in when they carry a score.
    return [(course, dict(item, _completion_status=completion_with_score(item, statuses.get(paper_exam_id(item), "unknown"))), category)
            for course, item, category in listed]


class NeedsRetry(Exception):
    """A job stops and waits for the person, without the platform sign-in being dropped."""


def _tally(done, count, failed):
    """A Q&A job's summary: what it did, and failures only when there were some."""
    return done % count + ("，失败 %d 条" % failed if failed else "")


def _qa_number(body, name, default, low, high):
    value = body.get(name)
    if value is None or value == "":
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise HttpError(400, "问答区参数 %s 必须是整数" % name)
    if not low <= value <= high:
        raise HttpError(400, "问答区参数 %s 要在 %d–%d 之间" % (name, low, high))
    return value


def _beijing_day(stamp):
    """The platform's day (Beijing time), whatever the server's time zone: daily limits and its morning update follow it."""
    return time.strftime("%Y-%m-%d", time.gmtime(stamp + 8 * 3600))


def _qa_options(body):
    """问答区答题只让人选一次发几条（1–50）；挑哪些题、隔多久由 _Board 和 QA_PACE 决定。"""
    return {"limit": _qa_number(body, "limit", 10, 1, 50)}


def _ask_texts(body):
    """要发布的提问：1–5 条，每条清理后按问答区规则检查（question_flaw），不重复。"""
    raw = body.get("texts")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 5:
        raise HttpError(400, "一次发布 1–5 条提问")
    texts = []
    for number, item in enumerate(raw, 1):
        text = _clean_question(item if isinstance(item, str) else "")
        flaw = question_flaw(text)
        if flaw:  # the board's own rules: it would be judged invalid or deleted, and the answers under it too
            raise HttpError(400, "第 %d 条%s" % (number, flaw))
        if text not in texts:
            texts.append(text)
    return texts


class _Board:
    """The Q&A board for one job. Picks questions nobody has answered first, then those with a few
    answers; remembers each post (question and text) so the web page can list what went out."""

    def __init__(self, bridge, record, peers=lambda: []):
        self.bridge, self.record, self.peers, self.asked = bridge, record, peers, {}

    def __enter__(self):
        self.bridge.__enter__()
        return self

    def __exit__(self, *exc):
        self.bridge.__exit__(*exc)

    def questions(self, max_answers=0, limit=10, **_):
        # Classmates on this site first: each of their questions that gets 5 valid answers earns its asker a point,
        # and it is a real question, not one of the floods the board is full of. Not right after it went up, though:
        # a ring of accounts answering each other within minutes is exactly what the board cleans out.
        now = time.time()
        found = [{"id": q["id"], "content": q["text"], "answers": q["ours"], "at": q["at"] * 1000, "peer": True,
                  "seen": q.get("replies") or []}
                 for q in self.peers() if q["ours"] < PEER_ANSWERS and now - q["at"] > PEER_DELAY][:limit]
        have = {q["id"] for q in found}
        found += [q for q in self.bridge.questions(max_answers=0, limit=limit) if q["id"] not in have]
        if len(found) < limit:
            # Most quiet threads on a busy board are floods or junk that answer_questions passes over;
            # an ordinary thread with a few answers still scores the same.
            have = {q["id"] for q in found}
            found += [q for q in self.bridge.questions(max_answers=5, limit=limit) if q["id"] not in have]
        self.asked.update((q["id"], q["content"]) for q in found)
        return found[:limit]

    def publish(self, question_id, text):
        answer_id = self.bridge.publish(question_id, text)
        if answer_id:
            self.record("answer", self.asked.get(question_id, ""), text, answer_id, qid=question_id)
        return answer_id

    def ask(self, text):
        question_id = self.bridge.ask(text)
        if question_id:
            self.record("ask", text, "", question_id)
        return question_id


def _history_line(event):
    if event["type"] == "log":
        return {"text": event.get("text", ""), "level": event.get("level", ""), "time": time.strftime("%H:%M")}
    if event.get("kind") == "result":
        status = event.get("status")
        return {"text": "%s · %s" % (event.get("name"), STATUS_TEXT.get(status, status)), "done": True,
                "level": "error" if status in ("error", "ai_error") else "", "time": time.strftime("%H:%M")}
    return None


def _track(progress, event):
    """Keep the counters the page draws a job's progress bar from, so a page opened mid-run can redraw it.
    Mirrors handleEvent() in studia.js."""
    kind, name = event.get("kind"), event.get("name") or ""
    overall = progress.get("overall")
    if kind == "begin":
        progress["overall"] = {"name": name, "total": event.get("total") or 0, "done": 0, "unit": event.get("unit")}
    elif kind == "add_total" and overall:
        overall["total"] += event.get("total") or 0
    elif kind == "result" and overall:
        overall["done"] += 1
        progress.pop("sub", None)
    elif kind in ("resources", "questions"):
        unit = event.get("unit")
        progress["sub"] = {"name": name, "done": event.get("completed") or 0, "total": event.get("total") or 0,
                           "unit": "项" if unit == "项资源" else unit}
    elif kind == "detail":
        progress["detail"] = name
    elif kind == "unit" and event.get("status") == "running":
        progress["unit"] = {"name": name, "key": event.get("key")}
    elif kind == "unit" and (progress.get("unit") or {}).get("key") == event.get("key"):
        # That unit is over; what follows (MOOC's discussion and rating) is told by "detail", not by the last unit.
        progress.pop("unit")


def _job_public(job):
    if job is None:
        return None
    public = {key: job.get(key) for key in ("id", "kind", "title", "course", "course_name", "platform", "state",
                                      "summary", "started", "ended", "running_since", "lines", "progress", "hold")} | {"order": _order(job)}
    public["night_paused"] = bool(job.get("night_paused")) and job["state"] in ("running", "queued")
    if public["night_paused"]:
        public["state"] = "paused"
    return public


def _order(job):
    """A job's place in its person's queue: when it was started, unless an administrator moved it."""
    return job.get("order", job["started"])


def _data_url(image):
    mime = "image/png" if image[:4] == b"\x89PNG" else "image/jpeg"
    return "data:%s;base64,%s" % (mime, base64.b64encode(image).decode("ascii"))


def _course_id(pid, row):
    if pid == "cx":
        # 同一门课可以在多个班级里，courseId + clazzId 才是唯一的。
        return re.sub(r"[^\w-]", "_", "%s-%s" % (row.get("courseId"), row.get("clazzId")))
    if pid == "zhs":
        z = row.get("zhs") or {}
        return "%s-%s" % (z.get("kind"), z.get("courseId") or z.get("secret"))
    for key in ("courseId", "id", "courseInfoId", "openId"):
        if row.get(key) not in (None, ""):
            return re.sub(r"[^\w-]", "_", str(row[key]))
    return re.sub(r"[^\w-]", "_", str(row.get("courseName") or "course"))


def course_features(pid, row, shares=None):
    """What one course can run: the platform's features, narrowed by the course's assessment rule
    (ZHSMain.api.course_offers, shared with the terminal menu)."""
    spec = PLATFORMS[pid]
    offers = course_offers(row, shares) if pid == "zhs" else {"review": False, "qa": False}
    return {"study": bool(spec.get("study", True)),
            "answer": bool(spec["answer"]),
            "review": bool(spec["review"]) and offers["review"],
            "qa": bool(spec.get("qa")) and offers["qa"],
            "mastery": offers.get("mastery", False)}


def _course(pid, row, shares=None):
    z = row.get("zhs") or {}
    progress = row.get("studySpeed", z.get("progress"))
    try:
        progress = round(float(progress))
    except (TypeError, ValueError):
        progress = None
    return {"id": _course_id(pid, row), "platform": pid,
            "name": row.get("courseName") or row.get("title") or "未命名课程",
            "type": row.get("courseInfoName") or row.get("teacher") or "",
            "kind": z.get("kind") or pid, "progress": progress,
            "features": course_features(pid, row, shares), "shares": shares}


ROUTES = [
    ("GET", r"/api/state", lambda app, m, q, b: app.state()),
    ("POST", r"/api/platforms/(\w+)/login", lambda app, m, q, b: app.login_start(m[1], b)),
    ("GET", r"/api/platforms/(\w+)/login", lambda app, m, q, b: app.login_status(m[1], (q.get("id") or [""])[0])),
    ("POST", r"/api/platforms/(\w+)/logout", lambda app, m, q, b: app.logout(m[1])),
    ("GET", r"/api/platforms/(\w+)/courses", lambda app, m, q, b: app.courses(m[1])),
    ("GET", r"/api/platforms/(\w+)/units", lambda app, m, q, b: app.units(m[1], (q.get("course") or [""])[0], (q.get("fresh") or [""])[0] == "1")),
    ("GET", r"/api/platforms/(\w+)/qa", lambda app, m, q, b: app.qa_posts(m[1], (q.get("course") or [""])[0])),
    ("POST", r"/api/platforms/(\w+)/qa/peers", lambda app, m, q, b: app.set_qa_peers(m[1], b)),
    ("POST", r"/api/platforms/(\w+)/qa/draft", lambda app, m, q, b: app.qa_draft(m[1], b)),
    ("GET", r"/api/platforms/(\w+)/score", lambda app, m, q, b: app.score(m[1], (q.get("course") or [""])[0], (q.get("fresh") or [""])[0] == "1")),
    ("GET", r"/api/platforms/(\w+)/tasks", lambda app, m, q, b: app.tasks(m[1], (q.get("course") or [""])[0], (q.get("fresh") or [""])[0] == "1")),
    ("GET", r"/api/jobs", lambda app, m, q, b: app.jobs_state()),
    ("POST", r"/api/jobs", lambda app, m, q, b: app.start_job(b)),
    ("POST", r"/api/jobs/stop", lambda app, m, q, b: app.stop_job((b or {}).get("id"))),
    ("POST", r"/api/jobs/(pause|continue|move)", lambda app, m, q, b: app.act_job(m[1], str((b or {}).get("id", "")), b)),
    ("GET", r"/api/settings", lambda app, m, q, b: app.settings()),
    ("POST", r"/api/settings/models", lambda app, m, q, b: app.add_model(b)),
    ("POST", r"/api/settings/models/activate", lambda app, m, q, b: app.activate_model(b)),
    ("POST", r"/api/settings/models/([\w-]+)", lambda app, m, q, b: app.update_model(m[1], b)),
    ("POST", r"/api/settings/models/([\w-]+)/delete", lambda app, m, q, b: app.delete_model(m[1])),
    ("POST", r"/api/settings/models/([\w-]+)/test", lambda app, m, q, b: app.test_model(m[1])),
    ("POST", r"/api/settings/concurrency", lambda app, m, q, b: app.save_concurrency(b)),
]


class Reply:
    """A response other than plain JSON 200: custom status, headers or content type."""

    def __init__(self, body, status=200, headers=None, content_type="application/json; charset=utf-8"):
        self.body, self.status, self.headers, self.content_type = body, status, headers or {}, content_type


NOT_HANDLED = object()


class LocalAccess:
    """One person on this machine: loopback host names and a random token from the terminal."""

    mode = "local"

    def __init__(self, app, port):
        self.app = app
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def host_ok(self, host):
        # DNS rebinding: only our own host names; cross-site calls lack the token.
        return host in self.allowed_hosts

    def headers(self):
        return {}

    def public(self, request, method, path, query, body):
        if method == "GET" and path == "/api/mode":
            return {"mode": self.mode}
        return NOT_HANDLED

    def authorize(self, request, method, query):
        token = request.headers.get("X-Studia-Token") or (query.get("t") or [""])[0]
        if not hmac.compare_digest(token.encode(), self.app.token.encode()):
            raise HttpError(403, "访问令牌无效，请从终端给出的地址打开 Studia")
        return {"app": self.app}

    def private(self, request, method, path, query, body, ctx):
        return NOT_HANDLED


def make_handler(access):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Studia"
        # Keep-alive: behind a proxy every request otherwise pays a fresh TCP + TLS handshake.
        protocol_version = "HTTP/1.1"
        timeout = 30  # a stalled client cannot pin a worker thread forever

        def log_message(self, *args):
            pass

        def _send(self, status, body, content_type="application/json; charset=utf-8", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            headers = dict(access.headers(), **(extra or {}))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", headers.pop("Cache-Control", "no-store"))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _reply(self, result):
            if isinstance(result, Reply):
                body = result.body if isinstance(result.body, (bytes, dict, list)) else str(result.body).encode("utf-8")
                return self._send(result.status, body, result.content_type, result.headers)
            return self._send(200, result)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64 * 1024:
                self.close_connection = True  # the unread body would be parsed as the next request
                raise HttpError(413, "请求过大")
            if not length:
                return {}
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                raise HttpError(400, "请求格式不正确")
            if not isinstance(body, dict):
                raise HttpError(400, "请求格式不正确")
            return body

        def _static(self, path):
            target = (STATIC / (path.lstrip("/") or "index.html")).resolve()
            if STATIC not in target.parents or not target.is_file() or target.name == "mock.js":
                raise HttpError(404, "not found")
            # Revalidate instead of downloading the script and styles again on every visit.
            stat = target.stat()
            etag = '"%x-%x"' % (stat.st_mtime_ns, stat.st_size)
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or target.suffix == ".js":
                content_type = ("text/javascript" if target.suffix == ".js" else content_type) + "; charset=utf-8"
            self._send(200, target.read_bytes(), content_type, {
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                                           "img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; "
                                           "connect-src 'self'; frame-ancestors 'none'",
                "ETag": etag, "Cache-Control": "no-cache",
            })

        def _events(self, app):
            subscriber = queue.Queue(maxsize=500)
            app.subscribers.add(subscriber)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-transform")
                # Nginx buffers proxied responses by default: events then reached the page in batches, or not at all.
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")  # no length: the stream ends when the socket does
                self.end_headers()
                while True:
                    try:
                        event = subscriber.get(timeout=15)
                        chunk = "data: %s\n\n" % json.dumps(event, ensure_ascii=False)
                    except queue.Empty:
                        # A real message, not an SSE comment, so the page can tell a live stream from a dead one.
                        chunk = 'data: {"type": "ping"}\n\n'
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.subscribers.discard(subscriber)

        def _dispatch(self, method):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            try:
                if not access.host_ok(self.headers.get("Host")):
                    raise HttpError(403, "不允许用这个地址访问。服务器模式请把 STUDIA_PUBLIC_URL 设成浏览器地址栏里的地址"
                                         "（协议、域名或 IP、端口都要一致），然后重启。")
                body = self._body() if method == "POST" else {}
                result = access.public(self, method, url.path, query, body)
                if result is not NOT_HANDLED:
                    return self._reply(result)
                if not url.path.startswith("/api/"):
                    if method != "GET":
                        raise HttpError(405, "method not allowed")
                    return self._static(url.path)
                ctx = access.authorize(self, method, query)
                if method == "GET" and url.path == "/api/events":
                    return self._events(ctx["app"])
                result = access.private(self, method, url.path, query, body, ctx)
                if result is not NOT_HANDLED:
                    return self._reply(result)
                for route_method, pattern, handler in ROUTES:
                    match = re.fullmatch(pattern, url.path)
                    if route_method == method and match:
                        return self._reply(handler(ctx["app"], match, query, body))
                raise HttpError(404, "not found")
            except HttpError as exc:
                self._send(exc.status, {"error": str(exc)})
            except (PlatformUnavailable, ZHSError) as exc:
                self._send(502, {"error": str(exc) or "平台暂时无法访问"})
            except AIError as exc:  # e.g. drafting questions: the model timed out or the key was refused
                self._send(502, {"error": str(exc)})
            except AuthenticationExpired:
                self._send(401, {"error": "登录已过期，请重新登录"})
            except Exception:
                logger.exception("网页接口出错：%s", url.path)
                self._send(500, {"error": "Studia 内部错误，详情见日志"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def serve(port=0, open_browser=True, store=None):
    app = Studia(store)
    logging.getLogger().addHandler(_EventLogHandler(app))
    server = ThreadingHTTPServer(("127.0.0.1", port), None)
    server.daemon_threads = True
    server.RequestHandlerClass = make_handler(LocalAccess(app, server.server_address[1]))
    url = "http://127.0.0.1:%d/?t=%s" % (server.server_address[1], app.token)
    print("Studia 网页版已启动：%s" % url)
    print("只有本机能访问；按 Ctrl+C 退出。")
    if open_browser:
        webbrowser.open(url)
    threading.Thread(target=app.resume, daemon=True, name="studia-resume").start()
    threading.Thread(target=keep_passports, args=(lambda: [app],), daemon=True, name="studia-keepalive").start()
    stop = threading.Event()
    threading.Thread(target=watch_quiet_hours, args=(stop,), daemon=True, name="studia-quiet-hours").start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        app.shutdown()
        drain()
        server.server_close()


def watch_quiet_hours(stop):
    """One site-wide watcher, including jobs whose browser keeps playing without Python requests."""
    while not stop.is_set():
        for app in set(JOBS.copy().values()):
            try:
                app.enforce_quiet_hours()
            except Exception:
                logger.exception("夜间任务调度失败")
        stop.wait(1)


def keep_passports(apps, interval=KEEPALIVE, first=60, stop=None):
    """Every interval (the first time after `first` seconds, so a fresh deploy shows its state soon), touch the
    sign-in centre for everyone the apps() callable returns. Runs until stop is set; one person's error is logged
    and the rest carry on."""
    stop = stop or threading.Event()
    wait = first
    while not stop.wait(wait):
        if quiet_hours()["active"]:
            wait = min(interval, 60)
            continue
        for app in apps():
            if quiet_hours()["active"]:
                break
            try:
                app.keep_passport()
            except Exception:
                logger.warning("智慧树登录中心保活出错", exc_info=True)
        wait = interval


def drain(timeout=20):
    """Give stopping jobs a moment to finish their last request, e.g. saving how far a video got."""
    deadline = time.monotonic() + timeout
    while JOBS and time.monotonic() < deadline:
        time.sleep(0.2)
