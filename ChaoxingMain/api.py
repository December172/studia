"""超星学习通接口：登录、课程/章节/任务点读取、学习任务、作业页面与交卷。

与 studia 其他平台客户端一样，这里只负责 HTTP 和解析，不决定调度策略；章节并行、
重试和答题覆盖率分别由 ChaoxingMain.study / exercise 决定。学习任务的接口和参数
来自 chaoxing 项目的 study_video / study_document / study_read / study_emptypage。
写操作（交卷、上报进度）不自动重试。
"""
import base64
import hashlib
import json
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
VIDEO_CUR_URL = BASE_URL + "/knowledge/getknowledgecurvideo"
VIDEO_NEXT_URL = BASE_URL + "/knowledge/getnextknowledgevideo"
DOCUMENT_URL = BASE_URL + "/mooc-ans/job/document"
READ_URL = BASE_URL + "/ananas/job/readv2"
EMPTY_PAGE_URL = BASE_URL + "/mooc-ans/mycourse/studentstudyAjax"
# 上报视频进度时 enc 里的固定盐值，来自网页播放器。
VIDEO_ENC_SALT = "d_yHJ!$pdA~5"


def timestamp():
    return str(int(time.time() * 1000))


def video_enc(clazz_id, jobid, objectid, playing_time, duration, userid, clip_time=""):
    """网页播放器上报进度用的 enc。

    md5("[clazzId][uid][jobid][objectid][播放秒*1000][盐][时长秒*1000][clipTime]")；
    clipTime 是老师截取的 vbegin_vend，普通视频才是 0_duration。
    """
    clip = clip_time or ("0_%d" % int(duration))
    raw = "[%s][%s][%s][%s][%s][%s][%s][%s]" % (
        clazz_id, userid, jobid, objectid, int(playing_time) * 1000,
        VIDEO_ENC_SALT, int(duration) * 1000, clip)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _flag_value(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


def _format_rt(value):
    """Java Double.toString 风格：整数值保留 .0（rt 是任务点要求的观看比例）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "1.0"
    return ("%.1f" % number) if number == int(number) else str(number)


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

    def _json(self, response):
        try:
            return response.json()
        except ValueError:
            return None

    def _video_throttle(self):
        """上报进度很容易触发 403：和源码一样每次先随机停 0–2 秒，再保证 2 秒间隔。"""
        self.sleep(random.uniform(0, 2))
        with self._video_lock:
            wait = self._video_next - time.time()
            if wait > 0:
                self.sleep(wait)
            self._video_next = time.time() + self.VIDEO_LOG_INTERVAL

    def _video_object(self, payload):
        """取视频 blob：{data|msg:{videoObject: json 字符串或对象}}。"""
        if not isinstance(payload, dict):
            return None
        data = payload.get("data") or payload.get("msg")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                data = None
        if not isinstance(data, dict):
            data = payload
        blob = data.get("videoObject")
        if isinstance(blob, str):
            try:
                blob = json.loads(blob)
            except ValueError:
                return None
        return blob if isinstance(blob, dict) else None

    def video_info(self, course, job, job_info=None, kind="Video"):
        """视频任务点的完整播放信息。

        优先取网页播放器实际使用的 ``knowledge/getknowledgecurvideo``（带 mid）或
        ``getnextknowledgevideo``（带附件 id），里面有 reportUrl、fastforward、
        vbegin/vend、rt、reportTimeInterval 等字段；取不到时退回 ananas/status
        的旧数据（只有 dtoken/duration，按“允许拖动”处理）。
        """
        knowledge = str((job_info or {}).get("knowledgeid") or job.get("knowledgeid") or "")
        blob = None
        if job.get("mid"):
            try:
                response = self._request("post", VIDEO_CUR_URL, data={
                    "courseid": course["courseId"], "clazzid": course["clazzId"],
                    "knowledgeid": knowledge, "cpi": course["cpi"], "mid": job["mid"]})
                blob = self._video_object(self._json(response))
            except (ChaoxingError, PlatformUnavailable):
                blob = None
        if blob is None and job.get("aid"):
            try:
                response = self._request("get", VIDEO_NEXT_URL, params={
                    "clazzId": course["clazzId"], "courseId": course["courseId"],
                    "knowledgeId": knowledge, "cardId": str((job_info or {}).get("cardid") or ""),
                    "cpi": course["cpi"], "attachmentId": str(job["aid"]), "isPhone": 1})
                blob = self._video_object(self._json(response))
            except (ChaoxingError, PlatformUnavailable):
                blob = None
        if blob:
            info = self._normalize_video(blob, course, job)
            if info["duration"] > 0 and info["reportUrl"]:
                return info
        return self._video_info_from_status(course, job, kind)

    def _normalize_video(self, blob, course, job):
        def number(key, default=0):
            value = blob.get(key)
            if value in (None, ""):
                return default
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return default

        duration = number("duration")
        vbegin = number("vbegin")
        vend = number("vend")
        # 老师可能只截取视频的一段：clipTime 和完成位置都以截取窗口为准。
        clip_end = vend if vend > 0 and (duration == 0 or vend < duration) else duration
        clip_time = "0_%d" % duration if (vbegin < 0 or vend <= 0) else "%d_%d" % (vbegin, vend)
        rt = blob.get("rt")
        try:
            rt = float(rt)
        except (TypeError, ValueError):
            rt = 0.0
        return {
            # blob 里 jobid 为空 = 非任务点视频（平台不记录完成），不要用卡片 jobid 顶替。
            "jobid": str(blob.get("jobid") or blob.get("jobId") or ""),
            "objectid": str(blob.get("objectid") or blob.get("objectId") or job.get("objectid") or ""),
            "otherInfo": str(blob.get("otherInfo") or job.get("otherinfo") or ""),
            "duration": duration,
            "end": clip_end,
            "vbegin": vbegin,
            "vend": vend,
            "clip_time": clip_time,
            "playTime": number("playTime"),
            "reportUrl": str(blob.get("reportUrl") or ""),
            "fastforward": _flag_value(blob.get("fastforward")),
            "rt": max(0.0, min(1.0, rt)),
            "interval": number("reportTimeInterval", 60) or 60,
            "clazzId": str(blob.get("clazzId") or blob.get("clazzid") or course["clazzId"]),
            "cpi": str(blob.get("cpi") or course["cpi"]),
            "attDuration": job.get("attDuration", ""),
            "attDurationEnc": job.get("attDurationEnc", ""),
            "videoFaceCaptureEnc": job.get("videoFaceCaptureEnc", ""),
        }

    def _video_status(self, job, kind="Video"):
        headers = VIDEO_HEADERS if kind == "Video" else AUDIO_HEADERS
        response = self._raw("get", VIDEO_STATUS_URL % job["objectid"],
                             params={"k": self.fid(), "flag": "normal"}, headers=headers)
        if response.status_code != 200:
            return None
        data = self._json(response)
        return data if isinstance(data, dict) and data.get("status") == "success" else None

    def _video_info_from_status(self, course, job, kind="Video"):
        """旧接口兜底：只有 dtoken/duration，没有 fastforward/clip 信息。"""
        data = self._video_status(job, kind)
        if data is None:
            return None
        try:
            duration = int(data.get("duration") or 0)
            dtoken = data["dtoken"]
        except (KeyError, TypeError, ValueError):
            return None
        if duration <= 0:
            return None
        fast = job.get("fastforward")
        return {
            "jobid": str(job["jobid"]), "objectid": str(job.get("objectid") or ""),
            "otherInfo": str(job.get("otherinfo") or ""), "duration": duration,
            "end": duration, "vbegin": 0, "vend": 0, "clip_time": "0_%d" % duration,
            "playTime": int(job.get("playTime") or 0) // 1000,
            "reportUrl": VIDEO_LOG_URL % (course["cpi"], dtoken),
            "fastforward": True if fast is None else _flag_value(fast),
            "rt": float(job.get("rt") or 0) or 0.0,
            "interval": 60,
            "clazzId": course["clazzId"], "cpi": course["cpi"],
            "attDuration": job.get("attDuration", ""), "attDurationEnc": job.get("attDurationEnc", ""),
            "videoFaceCaptureEnc": job.get("videoFaceCaptureEnc", ""),
        }

    def video_report(self, info, playing_time, isdrag, *, drag_from=None, kind="Video"):
        """按网页播放器的格式上报一次进度，返回 (isPassed, HTTP 状态码)。

        isdrag：0=playing、1=drag（playingTime 为 from-to）、2=pause、3=start、
        4=complete。enc 里的最后一段是 clipTime，不是固定的 0_duration。
        """
        url = info.get("reportUrl") or ""
        if not url:
            return False, 0
        self._video_throttle()
        seconds = max(0, int(playing_time))
        if isdrag == 1:
            playing = "%d-%d" % (max(0, int(drag_from or 0)), seconds)
        else:
            playing = str(seconds)
        clip = info["clip_time"]
        params = {
            "otherInfo": info["otherInfo"],
            "playingTime": playing,
            "duration": str(info["duration"]),
            "akid": "null",
            "jobid": info["jobid"],
            "clipTime": clip,
            "clazzId": info["clazzId"],
            "objectId": info["objectid"],
            "userid": self.user_id(),
            "isdrag": str(isdrag),
            "enc": video_enc(info["clazzId"], info["jobid"], info["objectid"],
                             seconds, info["duration"], self.user_id(), clip),
            "rt": _format_rt(info["rt"]),
            "dtype": kind,
            "view": "pc",
        }
        for key in ("attDuration", "attDurationEnc", "videoFaceCaptureEnc"):
            if info.get(key):
                params[key] = info[key]
        headers = VIDEO_HEADERS if kind == "Video" else AUDIO_HEADERS
        response = self._raw("get", url, params=params, headers=headers)
        if response.status_code == 200:
            data = self._json(response)
            return bool(isinstance(data, dict) and data.get("isPassed")), 200
        return False, response.status_code

    def study_video(self, course, job, *, speed=1.0, kind="Video", cancel_event=None, job_info=None):
        """完成一个视频/音频任务点，返回 success / skipped / forbidden / error / cancelled。

        网页播放器的协议（flutter_chaoxing 的验证记录）：
        - fastforward=true 时可以直接上报 isdrag=4（complete）到结尾，一步完成；
        - fastforward=false 时不允许拖过已看位置，只能按真实时间播放，
          期间用 isdrag=0（playing）心跳，最后再用 isdrag=4 收尾；
        - 老师截取的片段（vbegin/vend）以截取结尾为完成位置，enc 里用对应的 clipTime。
        """
        info = self.video_info(course, job, job_info, kind)
        if info is None:
            return "error"
        name = job.get("name") or job["jobid"]
        if not info["jobid"]:
            logger.info("%s：不是任务点视频（没有 jobid），跳过", name)
            return "skipped"
        duration, end = info["duration"], info["end"]
        if duration <= 0 or end <= 0:
            return "error"
        start = min(end, max(info["playTime"], info["vbegin"] if info["vbegin"] > 0 else 0))
        play_time = float(start)
        passed, state = False, 200
        if info["fastforward"]:
            # 允许拖动：先直接“看到结尾”，不行再模拟一次拖动后收尾。
            passed, state = self.video_report(info, end, 4, kind=kind)
            if not passed:
                passed, state = self.video_report(info, end, 1, drag_from=int(play_time), kind=kind)
                if not passed:
                    passed, state = self.video_report(info, end, 4, kind=kind)
            if passed:
                logger.info("%s：直接跳到结尾完成", name)
                return "success"
            logger.info("%s：平台没有接受直接跳到结尾，按真实时间播放", name)
        else:
            logger.info("%s：视频不允许拖动（fastforward=false），按真实时间播放", name)
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        self.video_report(info, play_time, 3, kind=kind)  # start
        last_log = play_time
        last_iter = time.time()
        interval = max(1, info["interval"])
        forbidden = 0
        end_attempts = 0
        while not passed:
            if cancel_event is not None and cancel_event.is_set():
                return "cancelled"
            if play_time - last_log >= interval or play_time >= end:
                passed, state = self.video_report(info, int(play_time), 0, kind=kind)  # playing
                if state == 403:
                    if forbidden >= 2:
                        logger.warning("%s：视频进度连续 403，跳过", name)
                        return "forbidden"
                    forbidden += 1
                    self.sleep(random.uniform(2, 4))
                    refreshed = self.video_info(course, job, job_info, kind)
                    if refreshed is not None:
                        info = refreshed
                        duration, end = info["duration"], info["end"]
                        play_time = min(end, max(float(info["playTime"]), 0.0))
                        last_log = play_time
                        continue
                elif not passed and state != 200:
                    return "error"
                last_log = play_time
                if play_time >= end and not passed:
                    end_attempts += 1
                    if end_attempts > 5:
                        logger.warning("%s：到达结尾多次仍未确认完成，跳过", name)
                        return "forbidden"
                    passed, state = self.video_report(info, end, 4, kind=kind)  # complete
                    if passed:
                        break
                    self.sleep(2)
            now = time.time()
            play_time = min(end, play_time + (now - last_iter) * speed)
            last_iter = now
            self.sleep(1)
        logger.info("%s：播放完成", name)
        return "success"

    def study_document(self, course, job, job_info=None):
        """完成文档/PPT/PDF 任务点：和网页 H5 阅读器一样打一次 mooc-ans/job/document。

        文档任务不需要像视频那样等时长，平台通常收到这次上报就直接标记完成；
        少数卡片的 otherInfo 里没有 nodeId，退回本章的 knowledgeid。
        """
        found = re.findall(r"nodeId_(.*?)-", job.get("otherinfo") or "")
        knowledge = found[0] if found else str((job_info or {}).get("knowledgeid") or "")
        if not knowledge:
            logger.warning("%s：文档任务缺少 knowledgeid，无法标记完成", job.get("name") or job["jobid"])
            return "error"
        # flutter_chaoxing 验证过的 H5 文档阅读完成 ping：mooc-ans/job/document，
        # 比旧版的 ananas/job/document 多了 checkMicroTopic / courseEngineInfo 参数。
        response = self._request("get", DOCUMENT_URL, params={
            "jobid": job["jobid"], "knowledgeid": knowledge,
            "courseid": course["courseId"], "clazzid": course["clazzId"],
            "jtoken": job.get("jtoken", ""), "checkMicroTopic": "true",
            "microTopicId": "undefined", "courseEngineInfo": "false",
        })
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("status") is False:
            logger.warning("%s：平台未确认文档完成：%s", job.get("name") or job["jobid"], payload.get("msg") or "")
            return "error"
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
