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
from task_metrics import nonnegative_number

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
STUONLINE = "https://stuonline.zhihuishu.com"
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
MASTERY = "https://aistudy.zhihuishu.com/gateway/t/v1"
MASTERY_PAGE = "https://studywisdomh5.zhihuishu.com"

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


# 考核项 → scoreAssessRuleDto 的字段，按成绩分析页的顺序。
SCORE_SHARES = (("progress", "progressScoreShare"), ("habit", "learnedHabitScoreShare"),
                ("interactive", "learnedInteractiveScoreShare"), ("knowledge", "learnedKnowledgeScoreShare"),
                ("test", "examScoreShare"), ("meet", "meetCourseScoreShare"), ("final", "finalExamScoreShare"))


def course_offers(row, shares=None):
    """这门课用不用得上每日复习、问答区：每日复习只攒学习习惯分，问答区只攒问答互动分，
    所以只给共享课，并且考核规则里这一项大于 0 才开。shares 读不到（None）时不收窄。"""
    shared = (row.get("zhs") or {}).get("kind") == "shared"
    earns = lambda name: shares is None or shares.get(name, 0) > 0
    return {"review": shared and earns("habit"), "qa": shared and earns("interactive"),
            "mastery": shared}


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

    def secure(self, url, payload, *, key="video", ok=0, as_json=False, method="POST",
               captcha_ev=None, captcha_vision=None, optional=False, origin=None):
        form = self._secure_form(payload, key)
        headers = {"origin": origin} if origin else {}
        if method.upper() == "GET":
            result = self._request("GET", url, params=form, **headers)
        elif as_json:
            result = self._request("POST", url, json_body=form, **headers)
        else:
            result = self._request("POST", url, data=form, **headers)

        code = result.get("code", ok)
        if code != ok and code == -12:
            # 先把平台真实返回记下来：验证类型决定能不能自动通过，
            # 没这条日志就只能靠猜。
            _log_failure(method, url, getattr(result, "http_status", None), code=code,
                         message=result.get("message") or result.get("msg") or "",
                         result=result, captcha=True)
            kind = _captcha_type(result).lower()
            captcha_type = "puzzle" if "jigsaw" in kind or "slide" in kind else "space"
            if (captcha_ev is not None and url.startswith(STUDY_GATEWAY + "/learning/") and
                    self.pass_captcha(captcha_ev, vision=captcha_vision, captcha_type=captcha_type)):
                form = self._secure_form(payload, key)
                if method.upper() == "GET":
                    result = self._request("GET", url, params=form, **headers)
                elif as_json:
                    result = self._request("POST", url, json_body=form, **headers)
                else:
                    result = self._request("POST", url, data=form, **headers)
                code = result.get("code", ok)
        if code != ok:
            message = result.get("message") or result.get("msg") or ""
            # An optional call refused by the platform is expected and handled by its caller;
            # warning about it put a scary line in the user's job log on every run.
            if not optional or code == -12:
                _log_failure(method, url, getattr(result, "http_status", None), code=code, message=message,
                             result=result, captcha=code == -12)
            if code == -12:
                raise CaptchaRequired("智慧树要求验证码，请到网页完成验证后重试")
            raise ZHSApiError(code, message)
        return result

    def pass_captcha(self, ev, *, vision=None, captcha_type="space"):
        """验证令牌须经智慧树确认后才允许重试。"""
        from ZHSMain import slider

        if not ev:
            logger.warning("缺少学习验证上下文，需要人工处理")
            return False
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain or "", "path": c.path or "/"}
            for c in self.session.cookies
        ]
        try:
            validate, _ = slider.solve(cookies, page_url="https://studyvideoh5.zhihuishu.com/stuStudy",
                                       captcha_type=captcha_type, vision=vision)
        except Exception as exc:
            logger.warning("自动验证未完成：%s", type(exc).__name__)
            return False
        if not validate:
            return False
        try:
            form = self._secure_form({"token": validate, "ev": ev, "checkType": 1}, "video")
            result = self._request("POST", STUDY_GATEWAY + "/learning/validateSlideToken", data=form)
        except (ZHSError, PlatformUnavailable, AuthenticationExpired):
            logger.warning("智慧树未能确认验证结果")
            return False
        data = result.get("data")
        accepted = isinstance(data, dict) and data.get("status") in (200, "200") and data.get("pass") is True
        if accepted:
            logger.info("智慧树已确认验证通过")
        else:
            logger.warning("智慧树未确认验证通过")
        return accepted

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

    def score_shares(self, course_id, recruit_id):
        """共享课考核规则（成绩分析页的 scoreAssessRuleDto）里各项占的分，满分 100。

        habit 是学习习惯分（每日复习攒的），interactive 是问答互动分（问答区攒的）。
        """
        result = self._request(
            "POST", STUONLINE + "/stuonline/json/stuLearnReportNew/loadStuLearingTab/",
            data={"courseId": course_id, "recruitId": recruit_id, "type": "l", "courseTypeEs": 1},
            origin=STUONLINE,
        )
        rule = result.get("scoreAssessRuleDto")
        if not isinstance(rule, dict):
            raise PlatformUnavailable("智慧树考核规则返回格式异常")
        return {name: _percent(rule.get(field)) or 0 for name, field in SCORE_SHARES}

    def score_earned(self, course_id, recruit_id):
        """共享课每项已经拿到的分（成绩分析页「我的得分」），和 score_shares 的各项对应；
        另有 usual 平时分、total 当前总分。习惯分和互动分各在自己的标签接口里。"""
        def read(path, **extra):
            return self._request("POST", STUONLINE + "/stuonline/json/stuLearnReportNew/" + path,
                                 data=dict(courseId=course_id, recruitId=recruit_id, **extra), origin=STUONLINE)
        ssd = read("loadStuLearnReport/", stuRankType="classRank").get("ssd")
        if not isinstance(ssd, dict):
            raise PlatformUnavailable("智慧树成绩返回格式异常")
        habit = read("loadStuHabit/", type="l", courseTypeEs=1)
        forum = read("loadCourseForum/", type="l", courseTypeEs=1)
        number = lambda value: float(nonnegative_number(value) or 0)
        mine = ((forum.get("qaStatisticsMap") or {}).get("myInteractionDto")) or {}
        count = lambda *names: int(next((number(mine[n]) for n in names if mine.get(n) is not None), 0))
        return {"progress": number(ssd.get("courseScore")), "habit": number(habit.get("learnedHabitScore")),
                "interactive": number(forum.get("learnedInteractiveScore")), "test": number(ssd.get("examScore")),
                "meet": number(ssd.get("meetScore")), "final": number(ssd.get("finalScore")),
                "usual": number(ssd.get("usualScore")), "total": number(ssd.get("totalScore")),
                # 「我的互动」：有效回答、有效提问、自己的提问收到的回答
                "qa": {"answers": count("validAnswerAmount", "effectiveAnswer"),
                       "questions": count("validQuestionAmount", "effectiveQuestion"),
                       "received": count("getAnswer")}}

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

    def shared_save_cache(self, payload, *, captcha_ev=None, captcha_vision=None):
        try:
            return self.secure(STUDY_GATEWAY + "/learning/saveCacheIntervalTimeV2", payload,
                               captcha_ev=captcha_ev, captcha_vision=captcha_vision, optional=True)
        except (AuthenticationExpired, CaptchaRequired):
            raise
        except Exception as exc:
            logger.debug("保存缓存进度失败：%s", exc)
            return None

    def shared_save_progress(self, payload, *, captcha_ev=None, captcha_vision=None):
        return self.secure(STUDY_GATEWAY + "/learning/saveDatabaseIntervalTimeV2", payload,
                           captcha_ev=captcha_ev, captcha_vision=captcha_vision)

    def _mastery_get(self, path, payload):
        return self.secure(MASTERY + path, payload, ok=200, method="GET", origin=MASTERY_PAGE)["data"]

    @staticmethod
    def _mastery_data(result):
        code = result.get("code")
        if code == -12:
            raise CaptchaRequired("智慧树要求验证码，请到网页完成验证后重试")
        if code in (401, 403):
            raise AuthenticationExpired("智慧树登录已过期，请重新扫码")
        if code != 200:
            raise ZHSApiError(code, result.get("message") or result.get("msg") or "掌握度请求失败")
        if result.get("data") is None or result.get("data") is False:
            raise PlatformUnavailable("智慧树未确认掌握度操作结果，请到平台核对")
        return result["data"]

    def mastery_nodes(self, course_id, recruit_id):
        """Read the official mastery map, including nodes outside chapter themes; never open a test."""
        info = self._mastery_get("/learning/queryCourseMapInfo", {"courseId": course_id, "recruitId": recruit_id})
        if not isinstance(info, dict) or not info.get("mapHashStr") or not info.get("mapStr"):
            raise PlatformUnavailable("智慧树掌握度图谱返回格式异常")
        tree = self._mastery_data(self._request(
            "POST", "https://share-course-map-service.zhihuishu.com/gateway/t/node/queryMasteryNodeInfo",
            json_body={"idHash": info["mapHashStr"], "idStr": info["mapStr"]}, origin=MASTERY_PAGE))
        if (not isinstance(tree, dict) or "nodeThemeRespVos" not in tree
                or tree["nodeThemeRespVos"] is not None and not isinstance(tree["nodeThemeRespVos"], list)
                or tree.get("randomNodes") is not None and not isinstance(tree["randomNodes"], list)):
            raise PlatformUnavailable("智慧树掌握度知识点返回格式异常")
        nodes = {}

        def visit(items):
            for node in items:
                children = node.get("children") or []
                if children:
                    visit(children)
                elif node.get("idHash"):
                    nodes[str(node["idHash"])] = node

        visit(tree["nodeThemeRespVos"] or [])
        visit(tree.get("randomNodes") or [])
        return list(nodes.values())

    def mastery_progress(self, recruit_id):
        result = self._mastery_get("/mastery/getAllMasteryInfos", {"recruitId": recruit_id})
        if not isinstance(result, list):
            raise PlatformUnavailable("智慧树掌握度进度返回格式异常")
        return result

    def mastery_start(self, course_id, recruit_id, knowledge_id):
        # The site's misspelling knowledeId is part of the wire protocol. This GET creates an attempt.
        return self._mastery_get("/exam/start", {
            "courseId": course_id, "recruitId": recruit_id, "knowledeId": knowledge_id})

    def mastery_submit(self, recruit_id, knowledge_id, exam_id, answers):
        # Unlike reads, the official /exam/submit sends plain JSON, not secretStr.
        return self._mastery_data(self._request("POST", MASTERY + "/exam/submit", json_body={
            "recruitId": recruit_id, "knowledgeId": knowledge_id, "examId": exam_id,
            "answerVos": answers, "dateFormate": int(time.time() * 1000)}, origin=MASTERY_PAGE))

    def mastery_result(self, recruit_id, knowledge_id, exam_id):
        return self._mastery_get("/exam/getAnswerMasteryInfo", {
            "recruitId": recruit_id, "knowledeId": knowledge_id, "examId": exam_id})

    def mastery_answers(self, recruit_id, knowledge_id, exam_id):
        return self._mastery_get("/exam/getUserAnswers", {
            "recruitId": recruit_id, "knowledeId": knowledge_id, "examId": exam_id})

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

    def passport_alive(self):
        """登录中心（passport）的票据还在不在。问答区每次进站都靠它认人，它过期了问答区就进不去，
        业务登录却照样能用。顺带「用」了它一次：票据像是没用一阵就过期，隔一会儿碰一下能续上。
        连不上抛 PlatformUnavailable。"""
        try:
            response = self._request(
                "GET", PASSPORT + "/cas/login",
                params={"service": HOMEWORK + "/studentExam/gateway/t/v1/student/getStudentHomework"},
                allow_redirects=False, expect_json=False)
        except AuthenticationExpired:
            return False
        return response.status_code == 302 and "ticket=" in (response.headers.get("Location") or "")

    def signed_in(self):
        """这份登录还能不能用：问一个学习时也要用的业务接口，没登录它回 401/403。

        不问 passport 单点登录：那张票据过两三个小时就失效，业务登录却还能用，
        拿它判断会把正在学习的账号当成掉线、删掉登录。连不上时抛 PlatformUnavailable。
        """
        try:
            self.ai_courses()
        except AuthenticationExpired:
            return False
        return True

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
            # 单点登录的票据比业务登录短命；它过期时，扫码时建好的考试会话通常还能用。
            # 不当成掉线：真不能用了，考试接口自己会回 401/403。
            logger.warning("智慧树单点登录已过期，沿用已有的考试会话")
            self._shared_sso_done = True
            return
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

    def shared_save_answer(self, answer_item, recruit_id, *, exam, captcha_vision=None):
        extra = {"source": "1"} if exam else None
        url = HOMEWORK_GATEWAY + "/answer/saveStudentAnswer"
        payload = {"stuExamAnswer": json.dumps([answer_item]), "recruitId": recruit_id}
        try:
            result = self.exam_form(url, payload, extra=extra)
        except CaptchaRequired as error:
            chapter_id = answer_item.get("examId")
            if exam or captcha_vision is None or not chapter_id or "滑块验证未通过" not in str(error):
                raise
            from ZHSMain import slider

            accepted = False
            try:
                cookies = [{"name": c.name, "value": c.value, "domain": c.domain or "", "path": c.path or "/"}
                           for c in self.session.cookies]
                token, _ = slider.solve(cookies, page_url=EXAM_PAGE + "/stuExamWeb.html",
                                        captcha_type="space", vision=captcha_vision)
                if token:
                    result = self.exam_form(HOMEWORK_GATEWAY + "/answer/validateSlideToken",
                                            {"recruitId": recruit_id, "token": token,
                                             "checkType": 1, "chapterId": chapter_id})
                    accepted = result.get("pass") is True
            except AuthenticationExpired:
                raise
            except Exception as exc:
                logger.warning("作业自动验证未完成：%s", type(exc).__name__)
            if accepted:
                logger.info("智慧树已确认作业验证通过")
                result = self.exam_form(url, payload, extra=extra)
            else:
                raise
        rt = result.get("rt")
        status = rt.get("statu") if isinstance(rt, dict) else None
        if str(status) == "-10":
            raise CaptchaRequired("智慧树要求进一步验证，请到网页完成验证后重试")
        if str(status) != "1":
            raise ZHSApiError(status, "平台未确认答案保存")
        return result

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
