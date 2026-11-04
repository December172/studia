"""超星学习通接口：登录、课程/章节/任务点读取、作业页面与交卷。

与 studia 其他平台客户端一样，这里只负责 HTTP 和解析，不决定答题策略；整章读取、
覆盖率判断和提交都由 ChaoxingMain.exercise 决定。写操作（交卷）不自动重试：
超星返回的 ``status`` 就是平台自己的确认，和源码一样不再单独回查一份作业的状态。
"""
import base64
import re
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
    decode_work_jobs,
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
        """章节里的作业任务和这一章的公共参数。

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
            found, found_info = decode_work_jobs(page.text)
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
