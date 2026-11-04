"""超星学习通接口：登录、课程/章节/任务点读取、学习任务、作业页面与交卷。

与 studia 其他平台客户端一样，这里只负责 HTTP 和解析，不决定调度策略；章节并行、
重试和答题覆盖率分别由 ChaoxingMain.study / exercise 决定。学习任务的接口和参数
来自 chaoxing 项目的 study_video / study_document / study_read / study_emptypage。
写操作（交卷、上报进度）不自动重试。
"""
import base64
import hashlib
import random
import re
import threading
import time

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from base.auth import AuthenticationExpired, PlatformUnavailable
from base.util import create_session
from ChaoxingMain.decode import (
    decode_course_folder,
    decode_course_list,
    decode_course_point,
    decode_jobs,
)
from MoocMain.log import Logger

logger = Logger(__name__).get_log()

BASE_URL = "https://mooc1.chaoxing.com"
MOOC2_URL = "https://mooc2-ans.chaoxing.com/mooc2-ans"
PASSPORT_URL = "https://passport2.chaoxing.com"
MOOC_HOME_URL = "https://i.mooc.chaoxing.com/space/index"
# 登录页上的扫码地址；fid=-1 是不指定学校，和网页登录框一致。
QR_LOGIN_PAGE = {"fid": "", "newversion": "true", "refer": "https%3A%2F%2Fi.chaoxing.com"}

# passport2 登录密码的 AES 密钥；这是页面前端自带、所有客户端共用的固定值。
AES_KEY = b"u2oh6Vu^HWe4_AES"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Microsoft Edge";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
COURSE_LIST_HEADERS = {
    "Referer": "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction?moocDomain=https://mooc1-1.chaoxing.com/mooc-ans",
}
SUBMIT_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": BASE_URL,
}
VIDEO_HEADERS = {"Referer": "https://mooc1.chaoxing.com/ananas/modules/video/index.html?v=2025-0725-1842"}
AUDIO_HEADERS = {"Referer": "https://mooc1.chaoxing.com/ananas/modules/audio/index_new.html?v=2025-0725-1842"}
VIDEO_STATUS_URL = BASE_URL + "/ananas/status/%s"
VIDEO_LOG_URL = BASE_URL + "/mooc-ans/multimedia/log/a/%s/%s"
DOCUMENT_URL = BASE_URL + "/ananas/job/document"
READ_URL = BASE_URL + "/ananas/job/readv2"
EMPTY_PAGE_URL = BASE_URL + "/mooc-ans/mycourse/studentstudyAjax"
# 上报视频进度时 enc 里的固定盐值，来自网页播放器。
VIDEO_ENC_SALT = "d_yHJ!$pdA~5"


def timestamp():
    return str(int(time.time() * 1000))


def video_enc(clazz_id, jobid, objectid, playing_time, duration, userid):
    """网页播放器上报进度用的 enc：md5("[课时][用户][任务][对象][毫秒播放][盐][毫秒时长][0_时长]")。"""
    raw = "[%s][%s][%s][%s][%s][%s][%s][0_%s]" % (
        clazz_id, userid, jobid, objectid, int(playing_time) * 1000,
        VIDEO_ENC_SALT, int(duration) * 1000, int(duration))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class ChaoxingError(RuntimeError):
    """平台返回了无法继续处理的结果。"""


class LoginError(ChaoxingError):
    pass


class WorkUnavailable(ChaoxingError):
    """这道作业本身不可作答（例如教师还没出完题），跳过它不影响其他作业。"""


def _hidden_value(html_text, name):
    """登录页里的 <input id="uuid" value="...">；属性顺序不固定，两种都认。"""
    for pattern in (r'<input[^>]*id="%s"[^>]*value="([^"]*)"' % name,
                    r'<input[^>]*value="([^"]*)"[^>]*id="%s"' % name):
        found = re.search(pattern, html_text or "")
        if found:
            return found.group(1)
    return ""


def encrypt(value):
    """passport2 登录用 AES-128-CBC，key 和 iv 相同，PKCS7 补位后 base64。"""
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_KEY)
    return base64.b64encode(cipher.encrypt(pad(value.encode("utf-8"), 16))).decode("ascii")


class ChaoxingApi:
    def __init__(self, username="", password="", *, cookies=None, session=None, sleep=None):
        self.session = session if session is not None else create_session()
        self.session.headers.update(HEADERS)
        self.sleep = sleep if sleep is not None else time.sleep
        self.logged_in = False
        # 视频进度上报的节流（多个章节同时学习时也共用）。
        self._video_lock = threading.Lock()
        self._video_next = 0.0
        self.VIDEO_LOG_INTERVAL = 2.0
        if cookies:
            for name, value in cookies.items():
                self.session.cookies.set(name, value)
            self.logged_in = True
        elif username and password:
            try:
                self.login(username, password)
            except BaseException:
                self.session.close()
                raise

    def close(self):
        self.session.close()

    # ------------------------------------------------------------------ 登录

    def login(self, username, password):
        data = {
            "fid": "-1",
            "uname": encrypt(username),
            "password": encrypt(password),
            "refer": "https%3A%2F%2Fi.chaoxing.com",
            "t": True,
            "forbidotherlogin": 0,
            "validate": "",
            "doubleFactorLogin": 0,
            "independentId": 0,
        }
        try:
            response = self.session.post(PASSPORT_URL + "/fanyalogin", data=data, headers=HEADERS, timeout=(10, 30))
        except requests.RequestException as error:
            raise PlatformUnavailable("暂时无法连接超星登录：%s" % error) from None
        if response.status_code != 200:
            raise LoginError("超星登录返回 HTTP %s" % response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise LoginError("超星登录返回了无法解析的结果") from None
        if not isinstance(payload, dict) or payload.get("status") is not True:
            reason = (payload.get("msg2") or payload.get("msg")) if isinstance(payload, dict) else ""
            raise LoginError("超星登录失败：%s" % (reason or "账号或密码不正确"))
        self.logged_in = True
        logger.info("超星登录成功")

    def login_qr(self):
        """学习通 App 扫码登录：取登录页里的 uuid/enc，再拿二维码图片。

        返回值可以原样交给 login_qr_status()；网页版会把它保存在待扫码会话里。
        """
        page = self._request("get", PASSPORT_URL + "/login", params=QR_LOGIN_PAGE)
        token = {"uuid": _hidden_value(page.text, "uuid"), "enc": _hidden_value(page.text, "enc")}
        if not token["uuid"] or not token["enc"]:
            raise LoginError("超星登录页没有返回二维码参数，请稍后重试")
        return token, self._qr_image(token)

    def refresh_login_qr(self, token):
        """二维码过期时向平台换一张，不重新走登录页（避免换掉整个扫码会话）。"""
        response = self._request("post", PASSPORT_URL + "/refreshQRCode")
        try:
            payload = response.json()
        except ValueError:
            raise ChaoxingError("超星没有返回新的二维码") from None
        if not isinstance(payload, dict) or not payload.get("uuid") or not payload.get("enc"):
            raise ChaoxingError("超星没有返回新的二维码")
        token = {"uuid": str(payload["uuid"]), "enc": str(payload["enc"])}
        return token, self._qr_image(token)

    def login_qr_status(self, token):
        """扫码状态，沿用项目里智慧树已经用到的约定：

        -1 等待扫码、0 已扫码待手机确认、1 已确认（Cookie 已写入）、
        2 二维码失效（可刷新）、3 手机取消或需要二次验证（跑不通）。
        """
        response = self._request("post", PASSPORT_URL + "/getauthstatus/v2", data={
            "enc": token.get("enc", ""), "uuid": token.get("uuid", ""),
            "doubleFactorLogin": 0, "forbidotherlogin": 0,
        })
        try:
            data = response.json()
        except ValueError:
            raise ChaoxingError("超星没有返回扫码状态") from None
        if not isinstance(data, dict):
            raise ChaoxingError("超星返回了未知的扫码状态")
        if data.get("status") in (True, "true"):
            if data.get("containTwoFactorLogin"):
                return {"status": 3, "message": "这个账号需要手机二次验证，请改用账号密码登录"}
            return {"status": 1, "nickname": data.get("nickname") or ""}
        kind = str(data.get("type") or "")
        if kind == "4":
            return {"status": 0, "nickname": data.get("nickname") or ""}
        if kind in ("2", "7"):
            # 2 = 二维码/会话失效，7 = 平台给出 mes 的错误；都按「换一张」处理。
            return {"status": 2, "message": data.get("mes") or "二维码已失效"}
        if kind == "6":
            return {"status": 3, "message": "已在手机上取消登录"}
        return {"status": -1, "message": "等待扫码"}

    def finish_qr_login(self, result=None):
        """扫码确认后把会话落到课程域名上，并确认 Cookie 真的可用。

        手机确认时 Cookie 写在 passport 域；浏览器登录后会跳到 i.mooc，这里照做，
        再用一次只读请求确认，避免把没生效的会话当成登录成功。
        """
        try:
            self._request("get", MOOC_HOME_URL)
        except (AuthenticationExpired, ChaoxingError):
            raise LoginError("扫码已确认，但登录状态没有生效，请重试") from None
        try:
            self.signed_in()
        except AuthenticationExpired:
            raise LoginError("扫码已确认，但登录状态没有生效，请重试") from None
        self.logged_in = True

    def _qr_image(self, token):
        response = self._request("get", PASSPORT_URL + "/createqr",
                                 params={"uuid": token["uuid"], "fid": -1})
        if not response.content:
            raise LoginError("超星没有返回二维码图片，请稍后重试")
        return response.content

    # ------------------------------------------------------------------ 请求

    def _request(self, method, url, *, params=None, data=None, headers=None):
        merged = dict(HEADERS)
        merged.update(headers or {})
        try:
            response = getattr(self.session, method)(url, params=params, data=data, headers=merged, timeout=(10, 30))
        except requests.RequestException as error:
            raise PlatformUnavailable("暂时无法连接超星平台：%s" % error) from None
        if response.status_code in (401, 403):
            raise AuthenticationExpired("超星登录已过期或无权访问")
        if response.status_code != 200:
            raise ChaoxingError("超星平台返回 HTTP %s" % response.status_code)
        # 被重定向到登录页说明 cookie 已经失效，不能把登录页当成数据页解析。
        # 登录相关的请求本来就打在 passport 上，不能按这条判断。
        if not url.startswith(PASSPORT_URL) and str(response.url).startswith(PASSPORT_URL):
            raise AuthenticationExpired("超星登录已过期，请重新登录")
        return response

    def signed_in(self):
        """只读地确认 cookie 还被平台接受；失效时抛 AuthenticationExpired。

        网页版用它区分「真的掉线」和「某次业务请求被限流/拒绝」，避免误删登录。
        """
        self._request("post", MOOC2_URL + "/visit/courselistdata",
                      data={"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0},
                      headers=COURSE_LIST_HEADERS)
        return True

    # ------------------------------------------------------------------ 目录

    def course_list(self):
        """当前账号的课程（含用户自建文件夹里的课程）。"""
        payload = {"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0}
        response = self._request("post", MOOC2_URL + "/visit/courselistdata", data=payload,
                                 headers=COURSE_LIST_HEADERS)
        courses = decode_course_list(response.text)
        folders = decode_course_folder(self._request("get", MOOC2_URL + "/visit/interaction").text)
        for folder in folders:
            payload = {"courseType": 1, "courseFolderId": folder["id"], "query": "", "superstarClass": 0}
            page = self._request("post", MOOC2_URL + "/visit/courselistdata", data=payload,
                                 headers=COURSE_LIST_HEADERS)
            courses += decode_course_list(page.text)
        return courses

    def course_points(self, course):
        """课程的章节点：``{"hasLocked": bool, "points": [...]}``。"""
        params = {"courseid": course["courseId"], "clazzid": course["clazzId"], "cpi": course["cpi"], "ut": "s"}
        return decode_course_point(self._request("get", MOOC2_URL + "/mycourse/studentcourse", params=params).text)

    def job_list(self, course, point):
        """章节里的任务点（视频/文档/阅读/作业）和这一章的公共参数。

        页面用 num=0..9 分批返回任务卡片，数量事先不知道，所以和源码一样逐个请求，
        直到没有新卡片；这一步是纯读取。
        """
        self.sleep(0.5)
        jobs, job_info = [], {}
        params = {
            "clazzid": course["clazzId"],
            "courseid": course["courseId"],
            "knowledgeid": point["id"],
            "ut": "s",
            "cpi": course["cpi"],
            "v": "2025-0424-1038-3",
            "mooc2": 1,
        }
        for number in "0123456789":
            params["num"] = number
            page = self._request("get", BASE_URL + "/mooc-ans/knowledge/cards", params=params)
            found, found_info = decode_jobs(page.text)
            if found_info.get("notOpen"):
                return [], found_info
            jobs += found
            job_info.update(found_info)
        # 不同 num 可能返回同一张卡片；按 jobid 去重，避免同一份作业被提交两次。
        unique, seen = [], set()
        for job in jobs:
            if job["jobid"] in seen:
                continue
            seen.add(job["jobid"])
            unique.append(job)
        return unique, job_info

    # ------------------------------------------------------------------ 学习

    def user_id(self):
        return str(self.session.cookies.get("_uid") or self.session.cookies.get("UID") or "")

    def fid(self):
        return str(self.session.cookies.get("fid") or "")

    def _raw(self, method, url, *, params=None, headers=None):
        merged = dict(HEADERS)
        merged.update(headers or {})
        try:
            return getattr(self.session, method)(url, params=params, headers=merged,
                                                 timeout=(10, 30), allow_redirects=True)
        except requests.RequestException as error:
            raise PlatformUnavailable("暂时无法连接超星平台：%s" % error) from None

    def _video_throttle(self):
        """上报进度很容易触发 403：和源码一样每次先随机停 0–2 秒，再保证 2 秒间隔。"""
        self.sleep(random.uniform(0, 2))
        with self._video_lock:
            wait = self._video_next - time.time()
            if wait > 0:
                self.sleep(wait)
            self._video_next = time.time() + self.VIDEO_LOG_INTERVAL

    def video_status(self, job, kind="Video"):
        """视频/音频的最新元数据（dtoken、duration、playTime）；失败返回 None。"""
        headers = VIDEO_HEADERS if kind == "Video" else AUDIO_HEADERS
        response = self._raw("get", VIDEO_STATUS_URL % job["objectid"],
                             params={"k": self.fid(), "flag": "normal"}, headers=headers)
        if response.status_code != 200:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) and data.get("status") == "success" else None

    @staticmethod
    def _passed(response):
        try:
            return bool(response.json().get("isPassed"))
        except (ValueError, AttributeError):
            return False

    def video_log(self, course, job, dtoken, duration, playing_time, kind="Video"):
        """上报一次播放进度，返回 (isPassed, HTTP 状态码)；403 交给调用方决定是否重试。"""
        self._video_throttle()
        headers = VIDEO_HEADERS if kind == "Video" else AUDIO_HEADERS
        params = {
            "clazzId": course["clazzId"],
            "playingTime": int(playing_time),
            "duration": int(duration),
            "clipTime": "0_%d" % int(duration),
            "objectId": job["objectid"],
            "otherInfo": job["otherinfo"],
            "courseId": course["courseId"],
            "jobid": job["jobid"],
            "userid": self.user_id(),
            "isdrag": "3",
            "view": "pc",
            "enc": video_enc(course["clazzId"], job["jobid"], job["objectid"],
                             playing_time, duration, self.user_id()),
            "dtype": kind,
        }
        for key in ("videoFaceCaptureEnc", "attDuration", "attDurationEnc"):
            if job.get(key):
                params[key] = job[key]
        url = VIDEO_LOG_URL % (course["cpi"], dtoken)

        def send(rt):
            params.update({"rt": rt, "_t": timestamp()})
            return self._raw("get", url, params=dict(params), headers=headers)

        rt = job.get("rt") or ""
        if not rt:
            found = re.search(r"-rt_([1d])", job.get("otherinfo") or "")
            if found:
                rt = "0.9" if found.group(1) == "d" else "1"
        if rt:
            response = send(rt)
            return self._passed(response), response.status_code
        # 没有 rt 时两个值都试，和源码一致。
        response = None
        for candidate in ("0.9", "1"):
            response = send(candidate)
            if response.status_code == 200:
                return self._passed(response), 200
            if response.status_code != 403:
                return False, response.status_code
        return False, response.status_code if response is not None else 0

    def study_video(self, course, job, *, speed=1.0, kind="Video", cancel_event=None):
        """完成一个视频/音频任务点，返回 success / forbidden / error / cancelled。

        先直接“拖到结尾”上报一次（isdrag=3）：平台允许拖拽的课程会立刻完成，不用等
        真实播放时间；被拒绝时再按真实时间累计进度（speed 倍速），每 30–90 秒
        （视频时间）上报一次，和源码的视频心跳一致。
        """
        info = self.video_status(job, kind)
        if info is None:
            return "error"
        try:
            dtoken = info["dtoken"]
            duration = int(info.get("duration") or 0)
        except (KeyError, TypeError, ValueError):
            return "error"
        if duration <= 0:
            return "error"
        play_time = int(job.get("playTime") or 0) // 1000
        name = job.get("name") or job["jobid"]
        # 直接拖到结尾。允许拖拽的课程（不少课程/视频不要求真实观看时长）一步完成，
        # 不必每个视频都按真实时间播放。
        passed, _ = self.video_log(course, job, dtoken, duration, duration, kind)
        if passed:
            logger.info("%s：直接拖到结尾完成", name)
            return "success"
        # 拖拽被拒时补报当前进度（源码是先报当前再试结尾，这里先试结尾）。
        if self.video_log(course, job, dtoken, duration, play_time, kind)[0]:
            return "success"
        logger.info("%s：平台不允许拖到结尾，按真实时间播放（%.0f 倍速）", name, speed)

        last_log, last_iter = 0, time.time()
        wait = random.randint(30, 90)
        forbidden = 0
        try:
            while not passed:
                if cancel_event is not None and cancel_event.is_set():
                    return "cancelled"
                if play_time - last_log >= wait or play_time >= duration:
                    passed, state = self.video_log(course, job, dtoken, duration, int(play_time), kind)
                    if state == 403:
                        if forbidden >= 2:
                            logger.warning("%s：视频进度连续 403，跳过", job.get("name") or job["jobid"])
                            return "forbidden"
                        forbidden += 1
                        self.sleep(random.uniform(2, 4))
                        refreshed = self.video_status(job, kind)
                        if refreshed is not None:
                            dtoken = refreshed.get("dtoken", dtoken)
                            duration = int(refreshed.get("duration") or duration)
                            play_time = min(duration, int(refreshed.get("playTime") or 0) // 1000)
                            last_iter = time.time()
                            continue
                    elif not passed and state != 200:
                        return "error"
                    wait, last_log = random.randint(30, 90), play_time
                now = time.time()
                play_time = min(duration, play_time + (now - last_iter) * speed)
                last_iter = now
                self.sleep(1)
        except PlatformUnavailable:
            raise
        return "success"

    def study_document(self, course, job):
        found = re.findall(r"nodeId_(.*?)-", job.get("otherinfo") or "")
        if not found:
            logger.warning("%s：文档任务缺少 knowledgeid", job.get("name") or job["jobid"])
            return "error"
        self._request("get", DOCUMENT_URL, params={
            "jobid": job["jobid"], "knowledgeid": found[0],
            "courseid": course["courseId"], "clazzid": course["clazzId"],
            "jtoken": job.get("jtoken", ""), "_dc": timestamp(),
        })
        return "success"

    def study_read(self, course, job, job_info):
        self._request("get", READ_URL, params={
            "jobid": job["jobid"], "knowledgeid": job_info.get("knowledgeid", ""),
            "jtoken": job.get("jtoken", ""), "courseid": course["courseId"],
            "clazzid": course["clazzId"],
        })
        return "success"

    def empty_page(self, course, point):
        """没有任务点的空章节：点一下完成，和源码 study_emptypage 一致。"""
        self._request("get", EMPTY_PAGE_URL, params={
            "courseId": course["courseId"], "clazzid": course["clazzId"],
            "chapterId": point["id"], "cpi": course["cpi"], "verificationcode": "",
            "mooc2": 1, "microTopicId": 0, "editorPreview": 0,
        })
        return "success"

    # ------------------------------------------------------------------ 作业

    def work_page(self, course, job, job_info):
        """打开（或恢复）这份作业的答题页，返回 HTML。

        这个 GET 会创建平台侧草稿，因此不能当作无副作用的查询重试。
        """
        params = {
            "api": "1",
            "workId": job["jobid"].replace("work-", ""),
            "jobid": job["jobid"],
            "originJobId": job["jobid"],
            "needRedirect": "true",
            "skipHeader": "true",
            "knowledgeid": str(job_info.get("knowledgeid", "")),
            "ktoken": job_info.get("ktoken", ""),
            "cpi": job_info.get("cpi", ""),
            "ut": "s",
            "clazzId": course["clazzId"],
            "type": "",
            "enc": job.get("enc", ""),
            "mooc2": "1",
            "courseid": course["courseId"],
        }
        text = self._request("get", BASE_URL + "/mooc-ans/api/work", params=params).text
        if "教师未创建完成该测验" in text:
            raise WorkUnavailable("教师未创建完成该测验")
        return text

    def submit_work(self, payload):
        """交卷或暂存；payload 里 pyFlag 为空表示提交，为 1 表示只保存。"""
        response = self._request("post", BASE_URL + "/mooc-ans/work/addStudentWorkNew", data=payload,
                                 headers=SUBMIT_HEADERS)
        try:
            result = response.json()
        except ValueError:
            raise ChaoxingError("超星没有返回交卷结果") from None
        if not isinstance(result, dict):
            raise ChaoxingError("超星返回了未知格式的交卷结果")
        return result
