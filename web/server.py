"""Local web interface: a 127.0.0.1-only HTTP server for every supported platform."""
from __future__ import annotations

import base64
import hmac
import json
import logging
import mimetypes
import queue
import re
import secrets
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from concurrent.futures import ThreadPoolExecutor

from AIMoocMain.api import AIMoocApi
from AIMoocMain.exam_api import ExamAPI, ExamError, ExamServiceError, TaskUnavailableError
from AIMoocMain.export_questions import paper_display_name, paper_exam_id
from AIMoocMain.main import AIMoocHandler
from ai.client import AIClient, AIConfig, AIError
from ai.runner import AnswerRunner
from base.concurrency import INTERACTIVE_WORKERS
from base.auth import AuthenticationExpired, PlatformUnavailable
from NewMoocMain.oauth_login import oauth_login
from settings import DEFAULT_PROFILE_ID, SettingsError, SettingsStore
from task_capabilities import task_availability, task_type_label
from task_metrics import score_text
from ZHSMain.answer import ZHSAnswerRunner, catalog
from ZHSMain.api import CaptchaRequired, StudyTimeLimit, ZHSApi, ZHSError
from ZHSMain.main import ZHSHandler
from ZHSMain.memory import SHARED as SHARED_ANSWERS, AnswerMemory
from ZYKMoocMain.api import ZYKMoocApi
from ZYKMoocMain.main import ZYKMoocHandler

STATIC = Path(__file__).resolve().parent / "static"
REVIEW_MINUTES = 30
QR_REFRESHES = 5
HISTORY = 20
JOB_THREADS = {}
logger = logging.getLogger(__name__)

# Order is the sidebar order. "answer"/"review" mark what the web UI can run today.
PLATFORMS = {
    "mooc": {"name": "MOOC", "short": "MOOC", "login": "browser", "api": AIMoocApi,
             "handler": AIMoocHandler, "answer": True, "review": False, "needs_ai": True},
    "zyk": {"name": "资源库", "short": "资源库", "login": "browser", "api": ZYKMoocApi,
            "handler": ZYKMoocHandler, "answer": False, "review": False, "needs_ai": False, "hidden": True},
    "zhs": {"name": "智慧树", "short": "智慧树", "login": "qr", "api": ZHSApi,
            "handler": ZHSHandler, "answer": True, "review": True, "needs_ai": False},
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
        self.login = None

    def public(self, logged_in):
        spec = self.spec
        return {"id": self.id, "name": spec["name"], "short": spec["short"], "login": spec["login"],
                "logged_in": logged_in, "features": {"answer": spec["answer"], "review": spec["review"]}}


class Studia:
    """All state behind the web UI; one running job at a time across platforms."""

    def __init__(self, store=None, oauth=oauth_login, *, reports_dir="ai_results", slots=None, oauth_url=None):
        self.store = store or SettingsStore()
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.RLock()
        self.platforms = {pid: Platform(pid) for pid, spec in PLATFORMS.items() if not spec.get("hidden")}
        self.oauth = oauth
        # Hosted mode: answer reports stay in the user's own directory, jobs share
        # a site-wide slot pool, and browser login returns to the server instead
        # of a localhost listener.
        self.reports_dir = reports_dir
        self.slots = slots
        self.oauth_url = oauth_url
        self.job = None
        self.history = []
        self.subscribers = set()

    # ------------------------------------------------------------ events

    def publish(self, event):
        job = self.job
        if job is not None and event.get("type") in ("log", "progress"):
            line = _history_line(event)
            if line:
                job["lines"].insert(0, line)
                del job["lines"][60:]
        for subscriber in list(self.subscribers):
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                pass

    def on_progress(self, event):
        self.publish(dict(asdict(event), type="progress"))

    def progress_event(self, kind, name, *, total=None, status="", unit="项"):
        self.publish({"type": "progress", "kind": kind, "name": name, "total": total,
                      "status": status, "unit": unit, "completed": None})

    # ------------------------------------------------------------ state

    def platform(self, pid):
        platform = self.platforms.get(pid)
        if platform is None:
            raise HttpError(404, "不支持这个平台")
        return platform

    def logged_in(self, platform):
        return platform.client is not None or bool(self.store.session(platform.id))

    def state(self):
        return {"platforms": [p.public(self.logged_in(p)) for p in self.platforms.values()],
                "job": _job_public(self.job), "history": [_job_public(j) for j in self.history]}

    def client(self, platform):
        with self.lock:
            if platform.client is None:
                saved = self.store.session(platform.id)
                if not saved:
                    raise HttpError(401, "请先登录%s" % platform.spec["name"])
                platform.client = platform.spec["api"](access_token=saved["access_token"])
            return platform.client

    def drop(self, platform):
        with self.lock:
            if platform.client is not None and hasattr(platform.client, "close"):
                platform.client.close()
            platform.client, platform.rows, platform.entries = None, [], {}

    def expire(self, platform):
        self.store.clear_session(platform.id)
        self.drop(platform)
        return HttpError(401, "%s 登录已过期，请重新登录" % platform.spec["name"])

    # ------------------------------------------------------------ login

    def login_start(self, pid):
        platform = self.platform(pid)
        with self.lock:
            if platform.spec["login"] == "qr":
                if platform.login and platform.login.get("client"):
                    platform.login["client"].close()
                client = ZHSApi()
                qr_token, image = client.login_qr()
                platform.login = {"client": client, "qr": qr_token, "refreshes": 0}
                return {"qr": _data_url(image)}
            if self.oauth_url is not None:
                platform.login = {"status": "waiting", "message": "请在新打开的页面登录职教云，完成后会自动回到这里"}
                return {"status": "waiting", "message": platform.login["message"], "open": self.oauth_url(pid)}
            if platform.login and platform.login.get("status") == "waiting":
                return {"status": "waiting", "message": "已在浏览器打开登录页"}
            platform.login = {"status": "waiting", "message": "已在浏览器打开职教云登录页，登录完成后会自动回到这里"}
            threading.Thread(target=self._browser_login, args=(platform,), daemon=True).start()
            return {"status": "waiting", "message": platform.login["message"]}

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
        except Exception as exc:
            logger.exception("%s 浏览器登录失败", platform.id)
            state.update(status="failed", message=str(exc) or "登录失败")

    def login_status(self, pid):
        platform = self.platform(pid)
        with self.lock:
            login = platform.login
            if login is None:
                raise HttpError(409, "请先开始登录")
            if platform.spec["login"] == "browser":
                if login["status"] != "waiting":
                    platform.login = None
                return {"status": login["status"], "message": login["message"]}
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
                client = login["client"]
                client.finish_qr_login(result.get("oncePassword", ""))
                try:
                    client.shared_exam_sso()
                except (AuthenticationExpired, PlatformUnavailable, ZHSError):
                    logger.warning("考试会话初始化失败，进入共享课试卷时将重新尝试")
                self.drop(platform)
                platform.client, platform.login = client, None
                self.store.save_session(platform.id, client)
                return {"status": "done", "message": "登录成功"}
            if status == 2:
                login["refreshes"] += 1
                if login["refreshes"] > QR_REFRESHES:
                    platform.login = None
                    return {"status": "failed", "message": "二维码多次过期，请点击刷新"}
                login["qr"], image = login["client"].login_qr()
                return {"status": "refreshed", "message": "二维码已过期，已自动刷新", "qr": _data_url(image)}
            if status == 3:
                platform.login = None
                return {"status": "failed", "message": "已在手机上取消登录"}
            return {"status": "waiting", "message": ""}

    def logout(self, pid):
        platform = self.platform(pid)
        self.store.clear_session(pid)
        self.drop(platform)
        return {}

    # ------------------------------------------------------------ courses

    def courses(self, pid):
        platform = self.platform(pid)
        client = self.client(platform)
        try:
            result = client.my_course_list(page_size=9999, strict=True) if pid != "zhs" else client.my_course_list()
        except AuthenticationExpired:
            raise self.expire(platform)
        rows = result.get("rows") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise PlatformUnavailable("课程列表读取失败，请稍后重试")
        with self.lock:
            platform.rows = rows
        return {"courses": [_course(pid, row) for row in rows]}

    def find_row(self, platform, course_id):
        if not platform.rows:
            self.courses(platform.id)
        for row in platform.rows:
            if _course_id(platform.id, row) == course_id:
                return row
        raise HttpError(404, "没有找到这门课程，请刷新课程列表")

    def tasks(self, pid, course_id):
        platform = self.platform(pid)
        if not platform.spec["answer"]:
            raise HttpError(400, "%s 的网页版答题还在接入中，请先用终端版" % platform.spec["name"])
        row = self.find_row(platform, course_id)
        client = self.client(platform)
        try:
            entries = catalog(client, row) if pid == "zhs" else _mooc_catalog(client, row)
        except AuthenticationExpired:
            raise self.expire(platform)
        except CaptchaRequired as exc:
            raise HttpError(409, str(exc))
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
                "name": (item.get("name") if pid == "zhs" else paper_display_name(item)) or "未命名试卷",
                "category": "作业" if pid == "zhs" and category == 1 else "考试" if pid == "zhs" else task_type_label(item, category),
                "status": availability.label or STATUS_LABEL.get(item.get("_completion_status"), "未作答"),
                "score": score,
                "available": availability.available,
                "done": done and not item.get("_redo"),
                "reason": availability.reason,
                "hint": item.get("_hint", ""),
                "redo_left": item.get("answerCount") if item.get("_redo") else None,
            })
        with self.lock:
            platform.entries[course_id] = listed
        return {"tasks": result}

    # ------------------------------------------------------------ jobs

    def start_job(self, body):
        platform = self.platform(str(body.get("platform", "")))
        kind = body.get("type")
        course_id = str(body.get("course", ""))
        allowed = {"study"} | ({"review"} if platform.spec["review"] else set()) | ({"answer"} if platform.spec["answer"] else set())
        if kind not in allowed:
            raise HttpError(400, "这个平台暂不支持该操作")
        row = self.find_row(platform, course_id)
        with self.lock:
            if self.job:
                raise HttpError(409, "已有任务在运行，请先停止")
            cancel = threading.Event()
            label = {"study": "自动学习", "review": "每日复习", "answer": "AI 答题"}[kind]
            if kind == "answer":
                selected = self._selected(platform, course_id, body.get("tasks") or [])
                target = lambda: self._answer(platform, selected, bool(body.get("auto_submit")), cancel)
            else:
                minutes = REVIEW_MINUTES if kind == "review" else 0
                target = lambda: self._study(platform, row, minutes, cancel)
            self.job = {"id": secrets.token_hex(4), "title": label, "course": course_id, "platform": platform.id,
                        "course_name": row.get("courseName") or "课程", "cancel": cancel, "state": "running",
                        "summary": "", "started": time.time(), "ended": None, "lines": []}
        self.publish(dict(_job_public(self.job), type="job"))
        threading.Thread(target=self._run_job, args=(target, platform), daemon=True, name="studia-job").start()
        return {"ok": True}

    def stop_job(self):
        job = self.job
        if job:
            job["cancel"].set()
        return {}

    def jobs(self):
        return {"job": _job_public(self.job), "history": [_job_public(j) for j in self.history]}

    def _selected(self, platform, course_id, task_ids):
        entries = platform.entries.get(course_id) or {}
        selected = [entries[str(task_id)] for task_id in task_ids if str(task_id) in entries]
        if not selected:
            raise HttpError(400, "请先选择试卷（试卷列表可能已过期，请刷新）")
        return selected

    def _run_job(self, target, platform):
        state, summary = "done", ""
        job = self.job
        JOB_THREADS[threading.get_ident()] = self
        acquired = False
        try:
            if self.slots is not None:
                # Site-wide limit: wait (cancellably) for a free slot before touching any platform.
                while not self.slots.acquire(timeout=1):
                    if job["cancel"].is_set():
                        raise KeyboardInterrupt
                    if job["state"] != "queued":
                        job["state"] = "queued"
                        self.publish(dict(_job_public(job), type="job"))
                acquired = True
                if job["state"] == "queued":
                    job["state"] = "running"
                    self.publish(dict(_job_public(job), type="job"))
            summary = target() or "已完成"
            if job["cancel"].is_set():
                state, summary = "stopped", "已停止"
        except KeyboardInterrupt:
            state, summary = "stopped", "已停止"
        except AuthenticationExpired:
            self.expire(platform)
            state, summary = "error", "%s 登录已过期，请重新登录" % platform.spec["name"]
        except Exception as exc:
            logger.exception("网页任务失败")
            state, summary = "error", str(exc) or "任务失败"
        finally:
            if acquired:
                self.slots.release()
            JOB_THREADS.pop(threading.get_ident(), None)
            with self.lock:
                job.update(state=state, summary=summary, ended=time.time())
                self.history.insert(0, job)
                del self.history[HISTORY:]
                self.job = None
            self.publish(dict(_job_public(job), type="job"))

    def _ai(self, cancel_message):
        config = self.store.ai_config()
        if config is None:
            raise HttpError(400, cancel_message)
        ai = AIClient(config)
        try:
            ai.check_connection()
        except AIError as exc:
            ai.session.close()
            self.store.save_ai_health(config, "error", str(exc))
            raise
        self.store.save_ai_health(config, "ok")
        return ai

    def _study(self, platform, row, minutes, cancel):
        spec = platform.spec
        kwargs = {"client": self.client(platform), "course_rows": [row], "on_progress": self.on_progress,
                  "cancel_event": cancel, "max_workers": self.store.concurrency()["study"]}
        ai = None
        if spec["needs_ai"]:
            ai = kwargs["activity_ai"] = self._ai("MOOC 学习会自动参与讨论和评价，请先在设置里添加 AI 模型")
        if minutes:
            kwargs["review_minutes"] = minutes
        try:
            spec["handler"](**kwargs)
        finally:
            if ai is not None:
                ai.session.close()
        return "今日复习完成" if minutes else "学习完成"

    def _answer(self, platform, selected, auto_submit, cancel):
        ai = self._ai("请先在设置里添加 AI 模型")
        try:
            workers = self.store.concurrency()["ai"]
            if platform.id == "zhs":
                runner = ZHSAnswerRunner(
                    self.client(platform), ai, auto_submit=auto_submit, on_progress=self.on_progress,
                    cancel_event=cancel, max_workers=workers,
                    memory=AnswerMemory(Path(self.store.path).parent / "zhs-answers.json", shared=SHARED_ANSWERS))
            else:
                # No manual-answer or confirm callbacks: unanswerable papers stay unsubmitted.
                runner = AnswerRunner(ExamAPI(self.client(platform)), ai, output_dir=self.reports_dir,
                                      auto_submit=auto_submit, on_progress=self.on_progress, max_workers=workers)
            self.progress_event("begin", "AI 答题", total=len(selected), unit="份试卷")
            done = 0
            for course, item, category in selected:
                if cancel.is_set():
                    break
                name = (item.get("name") if platform.id == "zhs" else paper_display_name(item)) or "试卷"
                try:
                    result = runner.run_paper(course, item, category)
                except (CaptchaRequired, StudyTimeLimit) as exc:
                    self.progress_event("result", name, status="error")
                    self.publish({"type": "log", "text": f"{name}：{exc}", "level": "error"})
                    break
                except TaskUnavailableError as exc:
                    result = {"status": "skipped", "message": str(exc)}
                except ExamError as exc:
                    result = {"status": "error", "message": str(exc), "stop_queue": isinstance(exc, ExamServiceError)}
                status = result["status"]
                if status == "ai_error":
                    self.store.save_ai_health(ai.config, "error", result.get("message", "AI 服务失败"))
                self.progress_event("result", name, status=status)
                if result.get("message"):
                    self.publish({"type": "log", "text": f"{name}：{result['message']}",
                                  "level": "error" if status in ("error", "ai_error") else ""})
                done += status in ("submitted", "saved", "preview")
                if result.get("stop_queue") or status in ("ai_error", "submission_unknown", "submitting", "paused"):
                    break
            return f"处理完成 {done}/{len(selected)} 份试卷"
        finally:
            ai.session.close()

    # ------------------------------------------------------------ settings

    def settings(self):
        models = []
        for profile in self.store.ai_profiles():
            config, health = profile.config, self.store.ai_health(profile.config)
            models.append({"id": profile.id, "provider": config.provider_name, "model": config.model,
                           "base_url": config.base_url, "key_hint": key_hint(config.api_key), "active": profile.active,
                           "health": health.get("status") or "", "health_message": health.get("message") or "",
                           "deletable": profile.id != DEFAULT_PROFILE_ID})
        return {"models": models, "concurrency": self.store.concurrency()}

    def _profile(self, profile_id):
        for profile in self.store.ai_profiles():
            if profile.id == profile_id:
                return profile
        raise HttpError(404, "没有找到这个模型")

    def _config(self, body, previous=None):
        key = str(body.get("api_key", "")).strip() or (previous.api_key if previous else "")
        try:
            return AIConfig(str(body.get("base_url", "")), str(body.get("model", "")), key,
                            provider_name=str(body.get("provider_name", "")),
                            api_format=previous.api_format if previous else "chat_completions")
        except ValueError as exc:
            raise HttpError(400, str(exc))

    def add_model(self, body):
        try:
            profile_id = self.store.save_ai_profile(self._config(body))
            if body.get("activate", True):
                self.store.activate_ai_profile(profile_id)
        except SettingsError as exc:
            raise HttpError(400, str(exc))
        return {"id": profile_id}

    def update_model(self, profile_id, body):
        profile = self._profile(profile_id)
        try:
            self.store.save_ai_profile(self._config(body, profile.config), profile_id)
        except SettingsError as exc:
            raise HttpError(400, str(exc))
        return {"id": profile_id}

    def delete_model(self, profile_id):
        self._profile(profile_id)
        try:
            self.store.delete_ai_profile(profile_id)
        except SettingsError as exc:
            raise HttpError(400, str(exc))
        return {}

    def test_model(self, profile_id):
        config = self._profile(profile_id).config
        client = AIClient(config)
        try:
            client.check_connection()
        except AIError as exc:
            self.store.save_ai_health(config, "error", str(exc))
            return {"health": "error", "message": str(exc)}
        finally:
            client.session.close()
        self.store.save_ai_health(config, "ok")
        return {"health": "ok", "message": "连接正常"}

    def activate_model(self, body):
        try:
            self.store.activate_ai_profile(str(body.get("id", "")))
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
        # Hosted mode has no single app: a line belongs to whichever user's job thread
        # logged it. Lines from helper threads are dropped rather than risk a leak.
        app = self.app or JOB_THREADS.get(record.thread)
        if app is None or not record.name.startswith(self.PREFIXES) or not app.job:
            return
        try:
            level = "error" if record.levelno >= logging.ERROR else "warn" if record.levelno >= logging.WARNING else ""
            app.publish({"type": "log", "text": record.getMessage(), "level": level})
        except Exception:
            pass


STATUS_TEXT = {"success": "完成", "skipped": "跳过", "error": "出错", "submitted": "已交卷", "saved": "已保存答案", "preview": "已生成答案", "paused": "已暂停",
               "ai_error": "AI 出错", "submission_unknown": "交卷结果未知"}


STATUS_LABEL = {"submitted": "已提交", "draft": "未提交", "not_started": "未作答"}


def _task_id(pid, item, category):
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
    return [(course, dict(item, _completion_status=statuses.get(paper_exam_id(item), "unsupported")), category)
            for course, item, category in listed]


def _history_line(event):
    if event["type"] == "log":
        return {"text": event.get("text", ""), "level": event.get("level", ""), "time": time.strftime("%H:%M")}
    if event.get("kind") == "result":
        status = event.get("status")
        return {"text": "%s · %s" % (event.get("name"), STATUS_TEXT.get(status, status)), "done": True,
                "level": "error" if status in ("error", "ai_error") else "", "time": time.strftime("%H:%M")}
    return None


def _job_public(job):
    if job is None:
        return None
    return {key: job[key] for key in ("id", "title", "course", "course_name", "platform", "state",
                                      "summary", "started", "ended", "lines")}


def _data_url(image):
    mime = "image/png" if image[:4] == b"\x89PNG" else "image/jpeg"
    return "data:%s;base64,%s" % (mime, base64.b64encode(image).decode("ascii"))


def _course_id(pid, row):
    if pid == "zhs":
        z = row.get("zhs") or {}
        return "%s-%s" % (z.get("kind"), z.get("courseId") or z.get("secret"))
    for key in ("courseId", "id", "courseInfoId", "openId"):
        if row.get(key) not in (None, ""):
            return re.sub(r"[^\w-]", "_", str(row[key]))
    return re.sub(r"[^\w-]", "_", str(row.get("courseName") or "course"))


def _course(pid, row):
    z = row.get("zhs") or {}
    progress = row.get("studySpeed", z.get("progress"))
    try:
        progress = round(float(progress))
    except (TypeError, ValueError):
        progress = None
    return {"id": _course_id(pid, row), "platform": pid, "name": row.get("courseName") or "未命名课程",
            "type": row.get("courseInfoName") or "", "kind": z.get("kind") or pid, "progress": progress}


ROUTES = [
    ("GET", r"/api/state", lambda app, m, q, b: app.state()),
    ("POST", r"/api/platforms/(\w+)/login", lambda app, m, q, b: app.login_start(m[1])),
    ("GET", r"/api/platforms/(\w+)/login", lambda app, m, q, b: app.login_status(m[1])),
    ("POST", r"/api/platforms/(\w+)/logout", lambda app, m, q, b: app.logout(m[1])),
    ("GET", r"/api/platforms/(\w+)/courses", lambda app, m, q, b: app.courses(m[1])),
    ("GET", r"/api/platforms/(\w+)/tasks", lambda app, m, q, b: app.tasks(m[1], (q.get("course") or [""])[0])),
    ("GET", r"/api/jobs", lambda app, m, q, b: app.jobs()),
    ("POST", r"/api/jobs", lambda app, m, q, b: app.start_job(b)),
    ("POST", r"/api/jobs/stop", lambda app, m, q, b: app.stop_job()),
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
        timeout = 30  # a stalled client cannot pin a worker thread forever

        def log_message(self, *args):
            pass

        def _send(self, status, body, content_type="application/json; charset=utf-8", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in dict(access.headers(), **(extra or {})).items():
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
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or target.suffix == ".js":
                content_type = ("text/javascript" if target.suffix == ".js" else content_type) + "; charset=utf-8"
            self._send(200, target.read_bytes(), content_type, {
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                                           "img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; "
                                           "connect-src 'self'; frame-ancestors 'none'",
            })

        def _events(self, app):
            subscriber = queue.Queue(maxsize=500)
            app.subscribers.add(subscriber)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while True:
                    try:
                        event = subscriber.get(timeout=15)
                        chunk = "data: %s\n\n" % json.dumps(event, ensure_ascii=False)
                    except queue.Empty:
                        chunk = ": ping\n\n"
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
                    raise HttpError(403, "forbidden")
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_job()
        server.server_close()
