from __future__ import annotations

import base64
import json
import re
import time
import urllib.parse

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from MoocMain.log import Logger
from base.auth import AuthenticationExpired, PlatformUnavailable

logger = Logger(__name__).get_log()

IV = b"1g3qqdh4jvbskb9x"
KEYS = {
    "home": b"7q9oko0vqb3la20r",
    "video": b"azp53h0kft7qi78q",
    "ai": b"hw2fdlwcj4cs1mx7",
    "exam": b"onbfhdyvz8x7otrp",
}
EV_KEY = "zzpttjd"
BASE = "https://onlineservice-api.zhihuishu.com"
PASSPORT = "https://passport.zhihuishu.com"
STUDY = "https://studyservice-api.zhihuishu.com"
AI = "https://kg-ai-run.zhihuishu.com"
AI_TASK = "https://kg-run-student.zhihuishu.com"
EXAM = "https://studentexamtest.zhihuishu.com"
NEWBASE = "https://newbase.zhihuishu.com"
HOMEWORK = "https://studentexam-api.zhihuishu.com"
TAURUS = "https://taurusexam-api.zhihuishu.com"
# 共享课试卷请求使用答题页的来源信息。
EXAM_PAGE = "https://onlineexamh5new.zhihuishu.com"

HOMEWORK_GATEWAY = HOMEWORK + "/studentExam/gateway/t/v1"
TAURUS_GATEWAY = TAURUS + "/taurusExam/gateway/t/v1"

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
    "Origin": "https://studyh5.zhihuishu.com",
    "Referer": "https://studyh5.zhihuishu.com/",
}

STUDY_GATEWAY = STUDY + "/gateway/t/v1"
AI_GATEWAY = AI + "/run/gateway/t/stu"

# ---------------------------------------------------------------- 失败诊断
# One warning line per failed call, so a log shows which endpoint failed and what the
# platform said. Never the request, the full response, cookies, secretStr or account data.

# Response fields that name the kind of verification, when the platform sends one.
CAPTCHA_TYPE_FIELDS = ("captchaType", "verifyType", "validateType", "checkType")
# Long tokens, long digit runs (phone, student, ticket numbers) and e-mail addresses.
_SENSITIVE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\d{7,}|[A-Za-z0-9+/=_-]{16,}")


class _Json(dict):
    """A decoded JSON object that remembers the HTTP status it came with."""
    http_status = None


def _endpoint(url):
    parts = urllib.parse.urlsplit(url)
    return parts.netloc + parts.path


def _clean(text, limit=200):
    text = " ".join(str(text if text is not None else "").split())[:limit]
    return _SENSITIVE.sub("***", text)


def _type(value):
    if value is None:
        return "null"
    return {bool: "bool", int: "int", float: "float", str: "str", list: "list", dict: "dict"}.get(type(value), "other")


def _shape(result):
    """Field names and value types, one level into `data`; never the values."""
    fields = []
    for name, value in list(result.items())[:30]:
        if isinstance(value, dict):
            inner = ", ".join("%s(%s)" % (_clean(k, 40), _type(v)) for k, v in list(value.items())[:30])
            fields.append("%s(dict: %s)" % (_clean(name, 40), inner))
        else:
            fields.append("%s(%s)" % (_clean(name, 40), _type(value)))
    return ", ".join(fields) or "（空）"


def _captcha_type(result):
    """The verification type only when the platform names it in a field; never guessed."""
    data = result.get("data") if isinstance(result, dict) else None
    for source in (result, data):
        if isinstance(source, dict):
            for field in CAPTCHA_TYPE_FIELDS:
                value = source.get(field)
                if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                    return "%s=%s" % (field, _clean(value, 40))
    return "未知"


def _log_failure(method, url, status, *, code=None, message=None, result=None, captcha=False):
    try:
        parts = ["智慧树接口失败：%s %s" % (method.upper(), _endpoint(url)), "HTTP %s" % (status or "无响应")]
        if code is not None:
            parts.append("业务码 %s" % _clean(code, 20))
        if message is not None:
            parts.append("提示：%s" % (_clean(message) or "（空）"))
        if captcha:
            parts.append("验证类型：%s" % _captcha_type(result))
        if isinstance(result, dict):
            parts.append("响应字段：%s" % _shape(result))
        logger.warning(" · ".join(parts))
    except Exception:
        # A diagnostic must never change how the request fails.
        logger.debug("记录智慧树接口诊断失败", exc_info=True)


class ZHSError(RuntimeError):
    pass


class ZHSApiError(ZHSError):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"智慧树接口返回 {code}：{message}")


class CaptchaRequired(ZHSError):
    pass


class StudyTimeLimit(ZHSError):
    pass


def encrypt(text, key):
    cipher = AES.new(key, AES.MODE_CBC, IV)
    data = pad(text.encode("utf-8"), AES.block_size)
    return base64.b64encode(cipher.encrypt(data)).decode("ascii")


def decrypt(text, key):
    cipher = AES.new(key, AES.MODE_CBC, IV)
    data = cipher.decrypt(base64.b64decode(text))
    return unpad(data, AES.block_size).decode("utf-8")


def encode_ev(values):
    text = ";".join(str(value) for value in values)
    parts = []
    for index, char in enumerate(text):
        mixed = ord(char) ^ ord(EV_KEY[index % len(EV_KEY)])
        parts.append(format(mixed, "02x")[-4:])
    return "".join(parts)


def _percent(value):
    """Course progress arrives as 58, "58.3" or "58.3%"; None when absent."""
    try:
        return round(float(str(value).strip().rstrip("%")))
    except (TypeError, ValueError):
        return None


class WatchPoints:
    def __init__(self, start=0):
        self.points = [0, 1]
        self.last = int(start) or 1

    def add(self, end):
        for point in range(self.last, int(end) + 1, 2):
            self.points.append(point // 5 + 2)
        self.last = int(end)

    def text(self):
        return ",".join(str(point) for point in self.points)

    def reset(self, start=0):
        self.__init__(start)


class ZHSApi:
    def __init__(self, access_token=None, token=None, username=None, password=None, session=None):
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)
        self.username = username
        self.password = password
        self.user_info = None
        self._shared_sso_done = False
        saved = access_token or token
        if saved:
            self.load_cookies(saved)

    @property
    def access_token(self):
        cookies = [
            {"name": cookie.name, "value": cookie.value, "domain": cookie.domain, "path": cookie.path,
             **({"expires": cookie.expires} if cookie.expires is not None else {}),
             **({"secure": True} if cookie.secure else {})}
            for cookie in self.session.cookies
        ]
        return json.dumps(cookies, ensure_ascii=False) if cookies else ""

    def load_cookies(self, text):
        try:
            cookies = json.loads(text)
            for cookie in cookies:
                self.session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain") or "",
                    path=cookie.get("path") or "/",
                    expires=cookie.get("expires"),
                    secure=cookie.get("secure", False),
                )
        except (ValueError, TypeError, KeyError):
            raise AuthenticationExpired("保存的智慧树登录无效，请重新扫码") from None
        account = self.uuid
        if account:
            self.session.cookies.set("exitRecod_" + account, "2", domain="zhihuishu.com")
        self._shared_sso_done = False

    @property
    def uuid(self):
        value = self.session.cookies.get("CASLOGC")
        if not value:
            return None
        try:
            return json.loads(urllib.parse.unquote(value)).get("uuid")
        except (ValueError, AttributeError):
            return None

    def close(self):
        self.session.close()

    def _request(self, method, url, *, params=None, data=None, json_body=None,
                 allow_redirects=True, expect_json=True, origin=None):
        headers = None
        if origin:
            headers = {"Origin": origin, "Referer": origin + "/"}
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                data=data,
                json=json_body,
                headers=headers,
                timeout=(10, 30),
                allow_redirects=allow_redirects,
            )
        except requests.RequestException as exc:
            # The exception text can contain the full URL with its query; only its class is logged.
            _log_failure(method, url, None, message=type(exc).__name__)
            raise PlatformUnavailable("暂时无法连接智慧树，请稍后重试") from None
        if response.status_code in (401, 403):
            _log_failure(method, url, response.status_code)
            raise AuthenticationExpired("智慧树登录已过期，请重新扫码")
        if response.status_code >= 400:
            _log_failure(method, url, response.status_code)
            raise PlatformUnavailable(f"智慧树返回 HTTP {response.status_code}，请稍后重试")
        if not expect_json:
            return response
        try:
            result = response.json()
        except ValueError:
            _log_failure(method, url, response.status_code, message="响应不是 JSON")
            raise PlatformUnavailable("智慧树响应无法解析，请稍后重试") from None
        if not isinstance(result, dict):
            _log_failure(method, url, response.status_code, message="响应是 %s，不是对象" % _type(result))
            raise PlatformUnavailable("智慧树返回了未知格式，请稍后重试")
        result = _Json(result)
        result.http_status = response.status_code
        return result

    def _secure_form(self, payload, key):
        body = dict(payload or {})
        timestamp = int(time.time() * 1000)
        body["dateFormate"] = timestamp
        return {
            "secretStr": encrypt(json.dumps(body), KEYS[key]),
            "dateFormate": str(timestamp),
        }

    def secure(self, url, payload, *, key="video", ok=0, as_json=False, method="POST"):
        form = self._secure_form(payload, key)
        if method.upper() == "GET":
            result = self._request("GET", url, params=form)
        elif as_json:
            result = self._request("POST", url, json_body=form)
        else:
            result = self._request("POST", url, data=form)

        code = result.get("code", ok)
        if code != ok and code == -12:
            # 先把平台真实返回记下来：验证类型决定能不能自动通过，
            # 没这条日志就只能靠猜。
            _log_failure(method, url, getattr(result, "http_status", None), code=code,
                         message=result.get("message") or result.get("msg") or "",
                         result=result, captcha=True)
            if self.pass_captcha(result):
                if method.upper() == "GET":
                    result = self._request("GET", url, params=form)
                elif as_json:
                    result = self._request("POST", url, json_body=form)
                else:
                    result = self._request("POST", url, data=form)
                code = result.get("code", ok)
        if code != ok:
            message = result.get("message") or result.get("msg") or ""
            _log_failure(method, url, getattr(result, "http_status", None), code=code, message=message,
                         result=result, captcha=code == -12)
            if code == -12:
                raise CaptchaRequired("智慧树要求验证码，请到网页完成验证后重试")
            raise ZHSApiError(code, message)
        return result

    def pass_captcha(self, result):
        """按平台报出的验证类型自动通过。目前只实现了易盾滑块（jigsaw）。"""
        from ZHSMain import slider

        kind = _captcha_type(result)
        if "jigsaw" not in kind and "slider" not in kind:
            logger.warning("验证类型 %s 暂不支持自动通过，需要人工处理", kind)
            return False
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain or "", "path": c.path or "/"}
            for c in self.session.cookies
        ]
        try:
            validate, fresh = slider.solve(cookies, page_url=EXAM_PAGE)
        except Exception as exc:
            logger.warning("滑块自动验证未完成：%s", exc)
            return False
        for cookie in fresh:
            if cookie.get("domain") and "zhihuishu" in cookie["domain"]:
                self.session.cookies.set(cookie["name"], cookie["value"],
                                         domain=cookie["domain"], path=cookie.get("path") or "/")
        if validate:
            logger.info("滑块验证已通过，已换回浏览器 cookie")
            return True
        return False

    def login_qr(self):
        self._request(
            "GET",
            PASSPORT + "/login",
            params={"service": BASE + "/login/gologin"},
            expect_json=False,
        )
        result = self._request("GET", PASSPORT + "/qrCodeLogin/getLoginQrImg")
        if not result.get("qrToken") or not result.get("img"):
            raise ZHSError("二维码获取失败")
        return result["qrToken"], base64.b64decode(result["img"])

    def login_qr_status(self, qr_token):
        return self._request(
            "GET",
            PASSPORT + "/qrCodeLogin/getLoginQrInfo",
            params={"qrToken": qr_token},
        )

    def finish_qr_login(self, once_password):
        self._request(
            "GET",
            PASSPORT + "/login",
            params={"service": BASE + "/login/gologin", "pwd": once_password},
            allow_redirects=True,
            expect_json=False,
        )
        if len(self.session.cookies) == 0:
            raise ZHSError("登录后未获得 cookie")

    def shared_courses(self):
        url = BASE + "/gateway/t/v1/student/course/share/queryShareCourseInfo"
        courses = []
        total_pages = 1
        page = 1
        while page <= total_pages and page <= 200:
            result = self.secure(
                url,
                {"status": 0, "pageNo": page, "pageSize": 5},
                key="home",
                ok=200,
            )
            data = result.get("result")
            if not isinstance(data, dict) or not isinstance(data.get("courseOpenDtos"), list):
                raise PlatformUnavailable("智慧树课程列表返回格式异常，请稍后重试")
            if page == 1:
                try:
                    total = int(data.get("totalCount") or 0)
                except (TypeError, ValueError):
                    total = 0
                total_pages = max(1, (total + 4) // 5)
            courses.extend(data["courseOpenDtos"])
            page += 1
        return courses

    def ai_courses(self):
        result = self.secure(
            BASE + "/gateway/t/v1/student/queryStudentAICourseList",
            {"status": 3},
            key="home",
            ok=0,
        )
        courses = result.get("rt")
        if not isinstance(courses, list):
            raise PlatformUnavailable("智慧树课程列表返回格式异常，请稍后重试")
        return courses

    def my_course_list(self, page_size=9999, strict=False):
        rows = []
        failures = 0

        try:
            shared = self.shared_courses()
        except AuthenticationExpired:
            raise
        except Exception as exc:
            failures += 1
            logger.warning("读取智慧树共享课失败：%s", exc)
        else:
            for item in shared:
                row = self._shared_row(item)
                if row:
                    rows.append(row)

        try:
            ai_courses = self.ai_courses()
        except AuthenticationExpired:
            raise
        except Exception as exc:
            failures += 1
            logger.warning("读取智慧树智慧课程失败：%s", exc)
        else:
            for item in ai_courses:
                row = self._ai_row(item)
                if row:
                    rows.append(row)

        if failures >= 2:
            raise PlatformUnavailable("智慧树课程列表读取失败，请稍后重试")
        return {"rows": rows}

    def _shared_row(self, item):
        # The live list names the course key "secret"; older responses used recruitAndCourseId.
        secret = item.get("secret") or item.get("recruitAndCourseId")
        try:
            recruit_id = int(item.get("recruitId"))
        except (TypeError, ValueError):
            recruit_id = 0
        if not secret or recruit_id <= 0:
            logger.warning("跳过缺少共享课标识的课程：%s", item.get("courseName"))
            return None
        course_id = self._int_or_zero(item.get("courseId"))
        if course_id <= 0:
            course_info = item.get("courseInfo") or {}
            course_id = self._int_or_zero(course_info.get("courseId"))
        row = {
            "courseName": item.get("courseName") or "未命名课程",
            "courseInfoName": "共享课",
            "zhs": {
                "kind": "shared",
                "secret": secret,
                "recruitId": recruit_id,
                "courseId": course_id,
            },
        }
        progress = _percent(item.get("progress"))
        if progress is not None:
            row["studySpeed"] = progress
        return row

    def _ai_row(self, item):
        course_id = self._int_or_zero(item.get("courseId"))
        class_id = self._int_or_zero(item.get("classId"))
        if course_id <= 0 or class_id <= 0:
            return None
        row = {
            "courseName": item.get("courseName") or "未命名课程",
            "courseInfoName": "智慧课程",
            "zhs": {"kind": "ai", "courseId": course_id, "classId": class_id},
        }
        progress = _percent(item.get("schedule"))
        if progress is not None:
            row["studySpeed"] = progress
        return row

    @staticmethod
    def _int_or_zero(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def shared_gologin(self, secret):
        return self._request(
            "GET",
            STUDY + "/login/gologin",
            params={
                "fromurl": "https://studyh5.zhihuishu.com/videoStudy.html#/studyVideo?recruitAndCourseId="
                + secret
            },
            expect_json=False,
        )

    def shared_course(self, secret):
        return self.secure(STUDY_GATEWAY + "/learning/queryCourse", {"recruitAndCourseId": secret})["data"]

    def shared_videos(self, secret):
        return self.secure(STUDY_GATEWAY + "/learning/videolist", {"recruitAndCourseId": secret})["data"]

    def shared_study_info(self, lesson_ids, video_ids, recruit_id):
        return self.secure(
            STUDY_GATEWAY + "/learning/queryStuyInfo",
            {"lessonIds": lesson_ids, "lessonVideoIds": video_ids, "recruitId": recruit_id},
        )["data"]

    def shared_last_video(self, recruit_id):
        try:
            return self.secure(
                STUDY_GATEWAY + "/learning/queryUserRecruitIdLastVideoId",
                {"recruitId": recruit_id},
            )
        except (AuthenticationExpired, CaptchaRequired):
            raise
        except Exception as exc:
            logger.debug("读取上次观看视频失败：%s", exc)
            return None

    def shared_prelearning(self, payload):
        return self.secure(STUDY_GATEWAY + "/learning/prelearningNote", payload)["data"]

    def shared_question_points(self, payload):
        return self.secure(STUDY_GATEWAY + "/popupAnswer/loadVideoPointerInfo", payload)["data"]

    def shared_popup_question(self, payload):
        return self.secure(STUDY_GATEWAY + "/popupAnswer/lessonPopupExam", payload)["data"]

    def shared_save_popup(self, payload):
        return self.secure(STUDY_GATEWAY + "/popupAnswer/saveLessonPopupExamSaveAnswer", payload)

    def shared_courseware(self, video_id):
        try:
            return self.secure(
                STUDY_GATEWAY + "/course/threeDimensionalCourseWare",
                {"videoId": video_id},
                method="GET",
            )
        except (AuthenticationExpired, CaptchaRequired):
            raise
        except Exception as exc:
            logger.debug("读取课件信息失败：%s", exc)
            return None

    def shared_save_cache(self, payload):
        try:
            return self.secure(STUDY_GATEWAY + "/learning/saveCacheIntervalTimeV2", payload)
        except (AuthenticationExpired, CaptchaRequired):
            raise
        except Exception as exc:
            logger.debug("保存缓存进度失败：%s", exc)
            return None

    def shared_save_progress(self, payload):
        return self.secure(STUDY_GATEWAY + "/learning/saveDatabaseIntervalTimeV2", payload)

    def ai_course_basic(self, course_id, class_id):
        return self.secure(
            AI_GATEWAY + "/knowledge-study/course-basic",
            {"courseId": course_id, "classId": class_id},
            key="ai",
            ok=200,
            as_json=True,
        )["data"]

    def ai_resources(self, course_id, class_id, knowledge_id):
        data = self.secure(
            AI_GATEWAY + "/resources/list-knowledge-resource",
            {"courseId": course_id, "classId": class_id, "knowledgeId": knowledge_id},
            key="ai",
            ok=200,
            as_json=True,
        )["data"]
        resources = data.get("resourceList") if isinstance(data, dict) else None
        if not isinstance(resources, list):
            raise PlatformUnavailable("智慧树课程资源返回格式异常，请稍后重试")
        return resources

    def ai_complete(self, course_id, class_id, knowledge_id, resources_uid):
        return self.secure(
            AI_GATEWAY + "/studyRecord/completed",
            {
                "courseId": course_id,
                "classId": class_id,
                "knowledgeId": knowledge_id,
                "resourcesUid": resources_uid,
                "watchUId": 1,
            },
            key="ai",
            ok=200,
            as_json=True,
        )

    def ai_video_length(self, course_id, class_id, file_id):
        data = self.secure(
            AI_GATEWAY + "/resources-lab/get-video-time",
            {"courseId": course_id, "classId": class_id, "videoIdList": [file_id]},
            key="ai",
            ok=200,
            as_json=True,
        )["data"]
        return int(data[0]["time"])

    def ai_report(self, course_id, class_id, file_id, knowledge_id, position):
        return self.secure(
            AI_GATEWAY + "/studyRecord/report",
            {
                "courseId": course_id,
                "classId": class_id,
                "fileId": file_id,
                "knowledgeId": knowledge_id,
                "lastWatchTime": position,
                "studyTotalTime": 10,
                "shareCourseId": "",
                "nodeType": 0,
                "watchUId": 1,
            },
            key="ai",
            ok=200,
            as_json=True,
        )

    def open_video(self, video_id=None, file_id=None):
        try:
            if video_id:
                response = requests.get(
                    NEWBASE + "/video/initVideo",
                    params={
                        "jsonpCallBack": "result",
                        "videoID": str(video_id),
                        "_": int(time.time() * 1000),
                    },
                    headers=HEADERS,
                    timeout=(10, 30),
                )
                text = response.text
                start = text.find("(")
                end = text.rfind(")")
                if start < 0 or end <= start:
                    return
                data = json.loads(text[start + 1:end])
                lines = (data.get("result") or {}).get("lines") or []
                line_url = lines[0].get("lineUrl") if lines else None
                if not line_url:
                    return
                with requests.get(line_url, headers=HEADERS, stream=True, timeout=(10, 30)) as stream:
                    stream.raw.read(64 * 1024)
            elif file_id:
                requests.get(
                    NEWBASE + "/video/initVideo",
                    params={"fileId": file_id},
                    headers=HEADERS,
                    timeout=(10, 30),
                )
        except Exception as exc:
            logger.debug("视频流预请求失败：%s", exc)

    # ------------------------------------------------------------ 智慧课程试卷

    @staticmethod
    def _exam_url(path):
        return EXAM + "/gateway/t/v1" + path

    def ai_homework_info(self, course_id, class_id, knowledge_id):
        data = self.secure(
            AI + "/run/gateway/t/stu/exam/questions-paper",
            {
                "scMapId": course_id,
                "courseId": class_id,
                "classId": class_id,
                "knowledgeId": knowledge_id,
            },
            key="ai",
            ok=200,
            as_json=True,
        ).get("data")
        if not isinstance(data, dict):
            raise PlatformUnavailable("智慧树作业信息返回格式异常，请稍后重试")
        return data

    def ai_exam_tasks(self, course_id):
        data = self.secure(
            AI_TASK + "/student/gateway/t/task/taskList",
            {"courseId": str(course_id), "taskType": 1, "taskName": "", "status": 0},
            key="ai",
            ok=200,
            as_json=True,
        ).get("data")
        if not isinstance(data, list):
            raise PlatformUnavailable("智慧树考试任务返回格式异常，请稍后重试")
        return [task for task in data if isinstance(task, dict) and task.get("taskType") == 1]

    def ai_exam_pull_paper(self, course_id, class_id, exam_test_id):
        return self.secure(
            AI + "/run/gateway/t/task/exam/start-pull-paper",
            {
                "courseId": course_id,
                "classId": class_id,
                "examTestId": exam_test_id,
                "pageNumber": 1,
                "pageSize": 10,
            },
            key="ai",
            ok=200,
            as_json=True,
        )

    def ai_exam_detail(self, payload):
        data = self.secure(
            AI + "/run/gateway/t/task/exam/openExamDetail",
            payload,
            key="ai",
            ok=200,
            as_json=True,
        ).get("data")
        return data if isinstance(data, dict) else {}

    def exam_open(self, payload):
        return self.secure(self._exam_url("/exam/user/openExam"), payload, key="exam", ok=0)

    def exam_sheet(self, exam_test_id, exam_paper_id, *, all_parts):
        payload = {"examTestId": exam_test_id, "examPaperId": exam_paper_id}
        if all_parts:
            data = self.secure(
                self._exam_url("/exam/user/getAnswerSheetInformation"),
                payload,
                key="exam",
                ok=0,
                method="GET",
            ).get("data")
            parts = data.get("partSheetVos") if isinstance(data, dict) else None
        else:
            data = self.secure(
                self._exam_url("/exam/user/getExamSheetInfo"),
                payload,
                key="exam",
                ok=0,
                method="GET",
            ).get("data")
            parts = (data.get("partSheetVos") or [])[:1] if isinstance(data, dict) else None
            if isinstance(data, dict) and not parts:
                parts = [{"questionSheetVos": data.get("questionSheetVos")}]
        questions = []
        for part in parts or []:
            if isinstance(part, dict):
                questions.extend(part.get("questionSheetVos") or [])
        result = []
        for question in questions:
            if not isinstance(question, dict):
                continue
            try:
                qid = int(question.get("questionId"))
                version = int(question.get("version") or 1)
            except (TypeError, ValueError):
                continue
            result.append({"questionId": qid, "version": version})
        return result

    def exam_question(self, exam_test_id, exam_paper_id, question_id, version):
        return self.secure(
            self._exam_url("/question/getExamQuestionInfo"),
            {
                "examTestId": exam_test_id,
                "examPaperId": exam_paper_id,
                "questionId": question_id,
                "version": version,
            },
            key="exam",
            ok=0,
            method="GET",
        ).get("data")

    def exam_save_answer(self, payload):
        return self.secure(self._exam_url("/answer/saveAnswer"), payload, key="exam", ok=0)

    def exam_save_batch(self, payload):
        return self.secure(self._exam_url("/answer/saveBatchAnswer"), payload, key="exam", ok=0)

    def exam_heartbeat(self, exam_test_id, exam_paper_id, seconds):
        return self.secure(
            self._exam_url("/exam/user/updateUserUsedTime"),
            {
                "examTestId": exam_test_id,
                "examPaperId": exam_paper_id,
                "heartbeatTime": seconds,
            },
            key="exam",
            ok=0,
        )

    def exam_submit(self, payload, *, expect_body=True):
        if not expect_body:
            # The submit endpoint answers with an empty body; HTTP status is the only signal.
            self._request(
                "POST",
                self._exam_url("/exam/user/submit"),
                data=self._secure_form(payload, "exam"),
                expect_json=False,
            )
            return {}
        return self.secure(self._exam_url("/exam/user/submit"), payload, key="exam", ok=0)

    # ------------------------------------------------------------ 共享课试卷

    def exam_form(self, url, payload, *, extra=None):
        """Shared-course exam endpoints: no dateFormate, plain status "200".

        Use the answering page's origin without changing the study session headers.
        """
        form = {"secretStr": encrypt(json.dumps(dict(payload or {})), KEYS["exam"])}
        if extra:
            form.update(extra)
        result = self._request("POST", url, data=form, origin=EXAM_PAGE)
        if str(result.get("status")) != "200":
            message = result.get("msg") or result.get("message") or ""
            captcha = "滑块" in message or "验证" in message
            _log_failure("POST", url, getattr(result, "http_status", None), code=result.get("status"),
                         message=message, result=result, captcha=captcha)
            if captcha:
                raise CaptchaRequired(
                    "智慧树拒绝了本次作答请求（%s），请到网页打开该试卷完成验证后重试" % message)
            raise ZHSApiError(result.get("status"), message)
        return result

    def shared_exam_sso(self):
        if self._shared_sso_done:
            return
        response = self._request(
            "GET",
            PASSPORT + "/cas/login",
            params={"service": HOMEWORK + "/studentExam/gateway/t/v1/student/getStudentHomework"},
            allow_redirects=False,
            expect_json=False,
        )
        location = response.headers.get("Location") or ""
        if response.status_code != 302 or "ticket=" not in location:
            raise AuthenticationExpired("智慧树登录已过期，请重新扫码")
        self._request("GET", location, allow_redirects=True, expect_json=False)
        self._shared_sso_done = True

    def shared_homework_list(self, recruit_id, course_id, flag):
        rt = self.exam_form(
            HOMEWORK_GATEWAY + "/student/getStudentHomework",
            {"recruitId": recruit_id, "courseId": course_id, "flag": flag,
             "pageSize": 200, "pageNum": 0},
        ).get("rt")
        if not isinstance(rt, dict):
            raise PlatformUnavailable("智慧树作业列表返回格式异常，请稍后重试")
        # An empty page is {"next": ...} with no list at all, not an empty list.
        rows = rt.get("studentHomeworkList")
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise PlatformUnavailable("智慧树作业列表返回格式异常，请稍后重试")
        return rows

    def shared_exam_list(self, recruit_id, course_id, flag):
        rt = self.exam_form(
            TAURUS_GATEWAY + "/student/getStudentFinalExam",
            {"courseId": course_id, "recruitId": recruit_id, "flag": flag},
        ).get("rt")
        if not isinstance(rt, list):
            raise PlatformUnavailable("智慧树考试列表返回格式异常，请稍后重试")
        return rt

    def shared_school_id(self):
        rt = self.exam_form(
            HOMEWORK_GATEWAY + "/exam/getLoginSchoolInfo", {}).get("rt")
        if not isinstance(rt, dict) or rt.get("schoolId") in (None, ""):
            return ""
        return str(rt["schoolId"])

    def shared_answer_lock(self, exam_id, recruit_id):
        rt = self.exam_form(
            HOMEWORK_GATEWAY + "/answer/getSaveAnswerLockResult",
            {"examId": exam_id, "recruitId": recruit_id},
        ).get("rt")
        state = rt.get("state") if isinstance(rt, dict) else None
        try:
            return int(state)
        except (TypeError, ValueError):
            raise PlatformUnavailable("智慧树答案锁状态返回异常，请稍后重试") from None

    def shared_do_homework(self, recruit_id, exam_id, stu_exam_id, school_id, course_id):
        return self.exam_form(
            HOMEWORK_GATEWAY + "/student/doHomework",
            {"recruitId": recruit_id, "examId": exam_id, "studentExamId": stu_exam_id,
             "schoolId": school_id, "courseId": course_id},
        ).get("rt")

    def shared_do_exam(self, recruit_id, exam_id, stu_exam_id, school_id, course_id):
        return self.exam_form(
            TAURUS_GATEWAY + "/student/doExam",
            {"recruitId": recruit_id, "examId": exam_id, "studentExamId": stu_exam_id,
             "schoolId": school_id, "courseId": course_id, "deviceId": ""},
        ).get("rt")

    def shared_save_answer(self, answer_item, recruit_id, *, exam):
        extra = {"source": "1"} if exam else None
        return self.exam_form(
            HOMEWORK_GATEWAY + "/answer/saveStudentAnswer",
            {"stuExamAnswer": json.dumps([answer_item]), "recruitId": recruit_id},
            extra=extra,
        )

    def shared_redo_homework(self, recruit_id, exam_id, course_id):
        return self.exam_form(
            HOMEWORK_GATEWAY + "/student/saveCourseTran",
            {"recruitId": recruit_id, "examId": exam_id, "description": "",
             "courseId": course_id},
        )

    def shared_submit_homework(self, recruit_id, exam_id, stu_exam_id, count):
        return self.exam_form(
            HOMEWORK_GATEWAY + "/answer/submit",
            {"recruitId": recruit_id, "examId": exam_id, "stuExamId": stu_exam_id,
             "achieveCount": str(count)},
        ).get("rt")

    def shared_submit_exam(self, recruit_id, exam_id, stu_exam_id, count):
        rt = self.exam_form(
            TAURUS_GATEWAY + "/answer/submit",
            {"recruitId": recruit_id, "examId": exam_id, "stuExamId": stu_exam_id,
             "achieveCount": str(count)},
        ).get("rt")
        if not isinstance(rt, dict):
            raise PlatformUnavailable("智慧树交卷结果返回格式异常，请稍后重试")
        if str(rt.get("statu")) != "1":
            raise ZHSApiError("statu", rt.get("msg"))
        return rt

    def shared_answer_info(self, recruit_id, stu_exam_id, exam_id, school_id, course_id,
                           question_ids):
        return self.exam_form(
            HOMEWORK_GATEWAY + "/answer/getStuAnswerInfo",
            {"recruitId": recruit_id, "stuExamId": stu_exam_id, "examId": exam_id,
             "schoolId": school_id, "courseId": course_id,
             "questionIds": ",".join(str(qid) for qid in question_ids)},
        ).get("rt")

    def shared_look_homework(self, recruit_id, stu_exam_id, exam_id, school_id, course_id):
        return self.exam_form(
            HOMEWORK_GATEWAY + "/student/lookHomework",
            {"recruitId": recruit_id, "studentExamId": stu_exam_id, "examId": exam_id,
             "schoolId": school_id, "courseId": course_id},
        ).get("rt")
