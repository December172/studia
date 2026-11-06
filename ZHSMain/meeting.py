"""见面课回放：复用官方播放器计时、签名和上报，回查服务端签到结果。"""
from __future__ import annotations

import json
import math
import threading
from urllib.parse import urlsplit

from base.auth import AuthenticationExpired, PlatformUnavailable
from MoocMain.log import Logger
from reporting import Reporter
from ZHSMain.api import CaptchaRequired, StudyTimeLimit, ZHSError, _clean
from ZHSMain.qa import _playwright_cookies

logger = Logger(__name__).get_log()


def replay_url(meeting):
    url = str(meeting.get("replayCourseUrl") or "")
    if url.startswith("//"):
        url = "https:" + url
    parts = urlsplit(url)
    if parts.hostname != "lc.zhihuishu.com" or parts.path != "/live/vod_room.html":
        raise ZHSError("暂不支持这个见面课回放地址，请到平台查看")
    return parts._replace(scheme="https").geturl()


def resume_target(videos, history):
    """The player numbers minutes across videos, rounding each video's length up."""
    watched, offset = {str(value) for value in history}, 0
    for index, video in enumerate(videos):
        minutes = math.ceil(float(video["duration"]) / 60)
        if minutes <= 0:
            raise ZHSError("见面课视频时长异常")
        for minute in range(1, minutes + 1):
            if str(offset + minute) not in watched:
                return {"id": video["id"], "index": index, "position": (minute - 1) * 60}
        offset += minutes
    return None


class MeetingPlayer:
    def __init__(self, client, meeting):
        self.url = replay_url(meeting)
        self.cookies = _playwright_cookies(client.access_token)
        self._pw = self._browser = self.page = None
        self.error = None
        self.history_loaded = False

    def __enter__(self):
        from playwright.sync_api import sync_playwright, Error

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(
                headless=True, args=["--autoplay-policy=no-user-gesture-required"])
            context = self._browser.new_context()
            context.add_cookies(self.cookies)
            self.page = context.new_page()
            self.page.on("response", self._response)
            self.page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
            if urlsplit(self.page.url).hostname in ("passport.zhihuishu.com", "login.zhihuishu.com"):
                raise AuthenticationExpired("智慧树登录已过期，请重新扫码")
            # This legacy page replaces window.Map, breaking Playwright's DOM wait helpers.
            # Plain evaluate also lets the independent video/history requests finish together.
            for _ in range(30):
                self.wait()
                ready = self.page.evaluate(
                    "() => !!(window.videoFlag && window.videoInfo.length && "
                    "window.courseEndStatus !== null && window.videoStartEndmap.size() > 0)")
                if self.history_loaded and ready:
                    break
            else:
                raise PlatformUnavailable("见面课视频或观看记录未加载，请稍后重试")
        except BaseException as exc:
            self.close()
            if isinstance(exc, Error):
                logger.warning("见面课播放器初始化失败：%s", _clean(str(exc).splitlines()[0]))
                raise PlatformUnavailable(
                    "见面课播放器未能加载；请确认网络和 Chromium 已安装（playwright install --only-shell chromium）") from None
            raise
        return self

    def _response(self, response):
        path = urlsplit(response.url).path
        if not path.endswith(("/getWatchHistory", "/setWatchHistorySecret")):
            return
        if response.status in (401, 403):
            self.error = AuthenticationExpired("智慧树登录已过期，请重新扫码")
            return
        try:
            data = response.json()
            code = str(data.get("status", data.get("code", "")))
            if code == "401":
                self.error = AuthenticationExpired("智慧树登录已过期，请重新扫码")
            elif code in ("-12", "1010") or str(data.get("code")) == "1010":
                self.error = CaptchaRequired("见面课要求验证，请到平台完成后重试")
            elif response.status >= 400 or code not in ("", "0", "200") or data.get("success") is False:
                self.error = PlatformUnavailable("见面课观看进度读取或保存失败，请到平台核对后重试")
            elif path.endswith("/getWatchHistory"):
                self.history_loaded = True
        except Exception:
            self.error = PlatformUnavailable("见面课观看进度响应无法解析")

    def start(self):
        # The page replaces Map: Playwright cannot marshal objects in either direction.
        # Cross this boundary as JSON text, leaving the platform's legacy Map intact.
        data = json.loads(self.page.evaluate(
            "() => JSON.stringify({videos: videoInfo.map(v => ({id: v.id, duration: v.duration})), "
            "history: watchHistoryBack})"))
        target = resume_target(data["videos"], data["history"])
        if target is None:
            raise ZHSError("观看记录已满但平台尚未确认签到，请到平台核对")
        self.page.evaluate("text => { const {id, index, position} = JSON.parse(text); "
                           "onPaly(id, index); seekTime = position; }", json.dumps(target))

    def snapshot(self):
        if urlsplit(self.page.url).hostname in ("passport.zhihuishu.com", "login.zhihuishu.com"):
            raise AuthenticationExpired("智慧树登录已过期，请重新扫码")
        return json.loads(self.page.evaluate("""() => {
            const v = document.querySelector('#forFollowBackDiv video');
            if (!v) return JSON.stringify({key: null, position: 0});
            v.muted = true;
            v.playbackRate = 1;
            if (v.paused && !v.ended) v.play().catch(() => {});
            return JSON.stringify({key: String(curVideoId), position: Math.floor(v.currentTime),
                    percent: document.querySelector('.qiandao-num')?.textContent || '0'});
        }"""))

    def wait(self):
        # Dispatch the player's response callbacks while waiting, including failed progress saves.
        self.page.wait_for_timeout(1000)
        if self.error is not None:
            raise self.error

    def close(self):
        for close in (getattr(self._browser, "close", None), getattr(self._pw, "stop", None)):
            if close:
                try:
                    close()
                except Exception:
                    logger.debug("关闭见面课播放器失败", exc_info=True)

    def __exit__(self, *exc):
        self.close()


class MeetingHandler:
    def __init__(self, *, client, course_rows, on_progress=None, cancel_event=None, **_):
        self.client = client
        self.progress = Reporter(on_progress)
        self.cancel = cancel_event if cancel_event is not None else threading.Event()
        self.blocked = None
        self.confirmed = self.pending = self.failed = 0
        self.progress.begin("见面课", len(course_rows), "门课程")
        for row in course_rows:
            self.check_cancel()
            name = row.get("courseName") or "未知课程"
            try:
                status = self.study(row)
            except (AuthenticationExpired, KeyboardInterrupt):
                raise
            except (CaptchaRequired, StudyTimeLimit) as error:
                self.blocked = error
                logger.warning("%s：%s", name, error)
                self.progress.result(name, "waiting")
                break
            except Exception as error:
                self.failed += 1
                logger.error("%s：见面课失败：%s", name, error)
                status = "error"
            self.progress.result(name, status)

    @property
    def summary(self):
        return f"见面课：已确认签到 {self.confirmed} 节 · 待处理 {self.pending} 节 · 失败 {self.failed} 项"

    def check_cancel(self):
        if self.cancel.is_set():
            raise KeyboardInterrupt

    def study(self, row):
        z = row.get("zhs") or {}
        if z.get("kind") != "shared":
            return "skipped"
        name = row.get("courseName") or "未知课程"
        self.progress.resources(f"{name} · 读取见面课", 0)
        meetings = self.client.meeting_list(z["recruitId"])
        before = self.failed, self.pending
        self.progress.resources(f"{name} · 见面课", 0, len(meetings))
        for index, meeting in enumerate(meetings):
            self.check_cancel()
            title = meeting.get("taskName") or "未命名见面课"
            try:
                reason = ""
                if str(meeting.get("taskType")) not in ("1", "4"):
                    reason = "讨论或实践课，请按教师要求完成"
                else:
                    state = self.client.meeting_status(meeting)
                    if str(state["signInStatus"]) == "2":
                        self.confirmed += 1
                        logger.info("%s · %s：平台已确认签到，跳过", name, title)
                        self.progress.resources(f"{name} · {title} · 已签到", index + 1, len(meetings))
                        continue
                    if str(state.get("courseEndStatus")) != "1":
                        reason = "课程学习时间已结束"
                    elif not (str(state.get("openStatus")) == "2" or str(state.get("courseLeaveStatus")) == "1"):
                        reason = "需要现场签到，线上观看不计分"
                    elif str(meeting.get("taskStatus")) != "3":
                        reason = "尚未结束，请在回放开放后重试"
                    elif not meeting.get("videoId"):
                        reason = "老师尚未上传回放"
                if reason:
                    self.pending += 1
                    logger.warning("%s · %s：%s", name, title, reason)
                else:
                    self.watch(meeting, f"{name} · {title}")
                    self.confirmed += 1
            except (AuthenticationExpired, CaptchaRequired, StudyTimeLimit, KeyboardInterrupt):
                raise
            except Exception as error:
                self.failed += 1
                logger.error("%s · %s：%s", name, title, error)
            self.progress.resources(f"{name} · {title}", index + 1, len(meetings))
        return ("error" if self.failed > before[0] else "waiting" if self.pending > before[1]
                else "success" if meetings else "skipped")

    def watch(self, meeting, title):
        logger.info("%s：开始按 1 倍速续看回放", title)
        with MeetingPlayer(self.client, meeting) as player:
            self.check_cancel()
            player.start()
            previous, idle = None, 0
            for elapsed in range(1, 24 * 60 * 60):
                self.check_cancel()
                player.wait()
                state = player.snapshot()
                current = state["key"], state["position"]
                idle = idle + 1 if current == previous else 0
                previous = current
                if elapsed % 5 == 0:
                    self.progress.detail(f"{title} · 签到进度 {state.get('percent', '0')}%")
                if elapsed % 30 == 0:
                    status = self.client.meeting_status(meeting)
                    if str(status["signInStatus"]) == "2":
                        logger.info("%s：服务端确认签到完成", title)
                        return
                    if str(status.get("courseEndStatus")) != "1":
                        raise ZHSError("课程学习时间已结束，停止播放")
                if idle >= 180:
                    raise ZHSError("播放器连续 3 分钟没有推进且平台未确认签到，请到平台查看")
            # ponytail: cap one replay at 24 hours; revisit if the platform introduces longer sessions.
            raise ZHSError("见面课播放超时，平台未确认签到")
