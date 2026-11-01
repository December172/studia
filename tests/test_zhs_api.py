import json
import unittest
import urllib.parse
from unittest.mock import Mock, patch

import requests
from base.auth import AuthenticationExpired, PlatformUnavailable
from ZHSMain import api
from ZHSMain.api import (
    CaptchaRequired,
    KEYS,
    WatchPoints,
    ZHSApi,
    ZHSApiError,
    ZHSError,
    decrypt,
    encode_ev,
    encrypt,
)


def make_api():
    session = Mock()
    session.cookies = __import__("requests").cookies.RequestsCookieJar()
    session.request = Mock()
    session.headers = {}
    return ZHSApi(session=session)


def json_response(payload, status=200):
    response = Mock()
    response.status_code = status
    response.json.return_value = payload
    return response


class CryptoTest(unittest.TestCase):
    def test_encrypt_matches_platform(self):
        self.assertEqual(
            encrypt('{"recruitAndCourseId": "abc", "dateFormate": 1700000000000}', KEYS["video"]),
            "QhSgx0MEvvp0jRL20aaxWlqKuqDVmx7L/zSqUDNH1p0IgiNl5O4aghZhx93U7vK6sAmahUAv75/UtGVSZlqWDg==",
        )
        cipher = encrypt('{"name": "中文"}', KEYS["ai"])
        self.assertEqual(cipher, "Nx637e4NA8F2H8efwRHDPK+YUeeM2Xkhkj9YVbkb4AA=")
        self.assertEqual(decrypt(cipher, KEYS["ai"]), '{"name": "中文"}')

    def test_encode_ev(self):
        self.assertEqual(
            encode_ev([123, 45, 6, 789, 10, "0", 30, 95, "0:01:35", "abc-uuidzhs"]),
            "4b48434f405f5f4c41474c4d51554a41404f475a5f434f4b444e5a554049454f150807570f051d10100c09",
        )
        self.assertEqual(encode_ev([1, "0:00:00", "x"]), "4b41404e445a5e4a4a4b0c")

    def test_watch_points(self):
        points = WatchPoints()
        points.add(10)
        points.add(17)
        self.assertEqual(points.text(), "0,1,2,2,3,3,3,4,4,4,5")
        points.reset(40)
        points.add(45)
        self.assertEqual(points.text(), "0,1,10,10,10")


class SecureTest(unittest.TestCase):
    def test_form_post_and_decrypt_body(self):
        client = make_api()
        client.session.request.return_value = json_response({"code": 0, "data": {"ok": 1}})
        result = client.secure("https://x.test/path", {"a": 1})
        self.assertEqual(result["data"], {"ok": 1})
        _, kwargs = client.session.request.call_args
        self.assertIn("secretStr", kwargs["data"])
        self.assertIsNone(kwargs["json"])
        body = json.loads(decrypt(kwargs["data"]["secretStr"], KEYS["video"]))
        self.assertEqual(body["a"], 1)
        self.assertIn("dateFormate", body)

    def test_json_post(self):
        client = make_api()
        client.session.request.return_value = json_response({"code": 200})
        client.secure("https://x.test/path", {"a": 1}, key="ai", ok=200, as_json=True)
        _, kwargs = client.session.request.call_args
        self.assertIn("secretStr", kwargs["json"])
        self.assertIn("json", kwargs)

    def test_captcha_and_api_error(self):
        client = make_api()
        client.session.request.return_value = json_response({"code": -12})
        with self.assertRaises(CaptchaRequired):
            client.secure("https://x.test/path", {})
        client.session.request.return_value = json_response({"code": 5, "msg": "boom"})
        with self.assertRaises(ZHSApiError) as ctx:
            client.secure("https://x.test/path", {})
        self.assertEqual(ctx.exception.code, 5)
        self.assertIn("boom", str(ctx.exception))

    def test_missing_code_is_success(self):
        client = make_api()
        client.session.request.return_value = json_response({"data": 1})
        self.assertEqual(client.secure("https://x.test/path", {}), {"data": 1})

    def test_unauthorized(self):
        client = make_api()
        client.session.request.return_value = json_response({}, status=401)
        with self.assertRaises(AuthenticationExpired):
            client.secure("https://x.test/path", {})


class FailureDiagnosticsTest(unittest.TestCase):
    """Failed calls log endpoint, HTTP status, business code, platform message and field shape, nothing secret."""

    URL = "https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/saveDatabaseIntervalTimeV2?uuid=U-9&ticket=ST-1"

    def client(self, payload, status=200):
        client = make_api()
        client.session.cookies.set("CASLOGC", "secret-cookie-value-123", domain="zhihuishu.com")
        client.session.cookies.set("SESSION", "jsessionid-abcdef0123456789", domain="zhihuishu.com")
        client.session.request.return_value = json_response(payload, status)
        return client

    def assert_nothing_secret(self, text):
        for secret in ("secret-cookie-value-123", "jsessionid-abcdef0123456789", "uuid=", "ticket=", "ST-1",
                       "secretStr", "13800138000", "alice-password", "student-no-778899"):
            self.assertNotIn(secret, text)

    def test_decode_error_names_the_endpoint_code_message_and_fields(self):
        client = self.client({"code": -1, "message": "参数解码发生错误", "data": None, "success": False})
        with self.assertLogs("ZHSMain.api", "WARNING") as logs, self.assertRaises(ZHSApiError) as ctx:
            client.secure(self.URL, {"uuid": "U-9", "password": "alice-password", "studentNo": "student-no-778899"})
        self.assertEqual(ctx.exception.code, -1)            # behaviour unchanged
        line = logs.output[0]
        self.assertIn("POST studyservice-api.zhihuishu.com/gateway/t/v1/learning/saveDatabaseIntervalTimeV2", line)
        self.assertIn("HTTP 200", line)
        self.assertIn("业务码 -1", line)
        self.assertIn("提示：参数解码发生错误", line)
        self.assertIn("响应字段：code(int), message(str), data(null), success(bool)", line)
        self.assertNotIn("验证类型", line)
        self.assert_nothing_secret(line)

    def test_captcha_logs_the_platform_message_and_an_unknown_type_when_none_is_given(self):
        client = self.client({"code": -12, "message": "请完成安全验证 13800138000", "data": {"token": "abcdefghijklmnopqrstuvwxyz"}})
        with self.assertLogs("ZHSMain.api", "WARNING") as logs, self.assertRaises(CaptchaRequired):
            client.secure(self.URL, {})
        line = logs.output[0]
        self.assertIn("业务码 -12", line)
        self.assertIn("提示：请完成安全验证 ***", line)
        self.assertIn("验证类型：未知", line)
        self.assertIn("data(dict: token(str))", line)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", line)
        self.assert_nothing_secret(line)

    def test_captcha_type_is_logged_only_when_the_platform_names_it(self):
        client = self.client({"code": -12, "msg": "", "data": {"captchaType": "slide"}})
        with self.assertLogs("ZHSMain.api", "WARNING") as logs, self.assertRaises(CaptchaRequired):
            client.secure(self.URL, {})
        self.assertIn("验证类型：captchaType=slide", logs.output[0])

    def test_http_and_connection_failures_keep_their_errors_and_hide_the_query(self):
        client = self.client({}, status=502)
        with self.assertLogs("ZHSMain.api", "WARNING") as logs, self.assertRaises(PlatformUnavailable):
            client.secure(self.URL, {})
        self.assertIn("HTTP 502", logs.output[0])
        self.assert_nothing_secret(logs.output[0])
        client.session.request.side_effect = requests.ConnectionError("Max retries exceeded with url: " + self.URL)
        with self.assertLogs("ZHSMain.api", "WARNING") as logs, self.assertRaises(PlatformUnavailable):
            client.secure(self.URL, {})
        self.assertIn("HTTP 无响应 · 提示：ConnectionError", logs.output[0])
        self.assert_nothing_secret(logs.output[0])

    def test_shared_exam_slider_block_is_logged_and_still_asks_the_user(self):
        client = self.client({"status": "403", "msg": "请完成滑块验证", "rt": None})
        with self.assertLogs("ZHSMain.api", "WARNING") as logs, self.assertRaises(CaptchaRequired):
            client.exam_form("https://studentexam-api.zhihuishu.com/studentExam/gateway/t/v1/answer/saveStudentAnswer",
                             {"answer": "B", "uuid": "U-9"})
        line = logs.output[0]
        self.assertIn("POST studentexam-api.zhihuishu.com/studentExam/gateway/t/v1/answer/saveStudentAnswer", line)
        self.assertIn("业务码 403 · 提示：请完成滑块验证 · 验证类型：未知", line)
        self.assertNotIn('"B"', line)
        self.assert_nothing_secret(line)

    def test_the_line_reaches_the_web_job_log(self):
        import logging
        import threading
        from web.server import JOBS, _EventLogHandler
        app = Mock()
        handler = _EventLogHandler()
        log = logging.getLogger("ZHSMain.api")
        log.addHandler(handler)
        JOBS["ab12"] = app
        try:
            client = self.client({"code": -1, "message": "参数解码发生错误"})
            worker = threading.Thread(target=lambda: self.assertRaises(ZHSApiError, client.secure, self.URL, {}),
                                      name="studia-job-ab12")
            worker.start()
            worker.join(5)
        finally:
            log.removeHandler(handler)
            JOBS.pop("ab12", None)
        event = app.publish.call_args[0][0]
        self.assertEqual((event["job"], event["level"]), ("ab12", "warn"))
        self.assertIn("业务码 -1", event["text"])


class CookieTest(unittest.TestCase):
    def test_restored_expired_exam_cookie_is_not_sent(self):
        client = make_api()
        client.session.cookies.set("o_session_id", "expired", domain=".zhihuishu.com",
                                   path="/", expires=1, secure=True)
        restored = ZHSApi(access_token=client.access_token)
        self.addCleanup(restored.close)
        cookie = next(c for c in restored.session.cookies if c.name == "o_session_id")
        self.assertEqual(cookie.expires, 1)
        self.assertTrue(cookie.secure)
        prepared = restored.session.prepare_request(requests.Request(
            "GET", "https://studentexam-api.zhihuishu.com/"))
        self.assertNotIn("o_session_id=", prepared.headers.get("Cookie", ""))

    def test_access_token_roundtrip(self):
        client = make_api()
        saved = json.dumps([
            {"name": "CASLOGC", "value": "abc", "domain": ".zhihuishu.com", "path": "/"}
        ])
        client.load_cookies(saved)
        self.assertEqual(json.loads(client.access_token), json.loads(saved))

    def test_uuid_and_exit_record(self):
        client = make_api()
        raw = json.dumps({"uuid": "user-1"})
        client.load_cookies(json.dumps([
            {"name": "CASLOGC", "value": urllib.parse.quote(raw), "domain": ".zhihuishu.com", "path": "/"}
        ]))
        self.assertEqual(client.uuid, "user-1")
        self.assertEqual(client.session.cookies.get("exitRecod_user-1"), "2")

    def test_invalid_cookies(self):
        client = make_api()
        with self.assertRaises(AuthenticationExpired):
            client.load_cookies("not-json")

    def test_empty_access_token(self):
        self.assertEqual(make_api().access_token, "")


class CourseListTest(unittest.TestCase):
    def test_shared_courses_pagination(self):
        client = make_api()
        pages = {
            1: {"code": 200, "result": {"totalCount": 7, "courseOpenDtos": [{"id": 1}]}},
            2: {"code": 200, "result": {"totalCount": 7, "courseOpenDtos": [{"id": 2}]}},
        }
        calls = []

        def request(method, url, **kwargs):
            body = json.loads(decrypt(kwargs["data"]["secretStr"], KEYS["home"]))
            calls.append(body["pageNo"])
            return json_response(pages[body["pageNo"]])

        client.session.request.side_effect = request
        self.assertEqual(client.shared_courses(), [{"id": 1}, {"id": 2}])
        self.assertEqual(calls, [1, 2])

    def test_my_course_list_rows(self):
        client = make_api()
        client.shared_courses = Mock(return_value=[
            {"recruitAndCourseId": "s1", "courseName": "共享", "recruitId": "9",
             "courseId": 0, "courseInfo": {"courseId": 3}, "progress": 12.6},
            {"courseName": "坏数据", "recruitId": "0"},
        ])
        client.ai_courses = Mock(return_value=[
            {"courseName": "", "courseId": "7", "classId": "8"},
            {"courseName": "坏", "courseId": "x", "classId": 1},
        ])
        rows = client.my_course_list()["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["courseInfoName"], "共享课")
        self.assertEqual(rows[0]["zhs"], {"kind": "shared", "secret": "s1", "recruitId": 9, "courseId": 3})
        self.assertEqual(rows[0]["studySpeed"], 13)
        self.assertEqual(rows[1]["courseName"], "未命名课程")
        self.assertEqual(rows[1]["zhs"], {"kind": "ai", "courseId": 7, "classId": 8})

    def test_live_list_shapes(self):
        # Field names as returned by the live platform (secret, percent strings, string ids).
        client = make_api()
        client.shared_courses = Mock(return_value=[
            {"secret": "abc", "courseName": "新一代信息技术导论", "recruitId": 444872,
             "courseId": 1000091138, "progress": "45.5%"},
        ])
        client.ai_courses = Mock(return_value=[
            {"courseName": "通信基础", "courseId": "1234567890123456789", "classId": 326950, "schedule": 58},
        ])
        shared, ai = client.my_course_list()["rows"]
        self.assertEqual(shared["zhs"], {"kind": "shared", "secret": "abc", "recruitId": 444872,
                                         "courseId": 1000091138})
        self.assertEqual(shared["studySpeed"], 46)
        self.assertEqual(ai["zhs"]["courseId"], 1234567890123456789)
        self.assertEqual(ai["studySpeed"], 58)

    def test_one_side_failure(self):
        client = make_api()
        client.shared_courses = Mock(side_effect=PlatformUnavailable("down"))
        client.ai_courses = Mock(return_value=[{"courseName": "AI", "courseId": 1, "classId": 2}])
        self.assertEqual(len(client.my_course_list()["rows"]), 1)

    def test_both_side_failure(self):
        client = make_api()
        client.shared_courses = Mock(side_effect=PlatformUnavailable("down"))
        client.ai_courses = Mock(side_effect=PlatformUnavailable("down"))
        with self.assertRaises(PlatformUnavailable):
            client.my_course_list()

    def test_auth_error_propagates(self):
        client = make_api()
        client.shared_courses = Mock(side_effect=AuthenticationExpired("expired"))
        client.ai_courses = Mock(return_value=[])
        with self.assertRaises(AuthenticationExpired):
            client.my_course_list()


class LoginTest(unittest.TestCase):
    def test_login_qr(self):
        client = make_api()
        client.session.request.side_effect = [
            json_response({}),
            json_response({"qrToken": "qt", "img": "aGk="}),
        ]
        token, image = client.login_qr()
        self.assertEqual(token, "qt")
        self.assertEqual(image, b"hi")
        first, second = client.session.request.call_args_list
        self.assertTrue(first.args[1].endswith("/login"))
        self.assertEqual(first.kwargs["params"]["service"], api.BASE + "/login/gologin")
        self.assertTrue(second.args[1].endswith("/qrCodeLogin/getLoginQrImg"))

    def test_login_qr_status(self):
        client = make_api()
        client.session.request.return_value = json_response({"status": 0})
        self.assertEqual(client.login_qr_status("qt")["status"], 0)
        _, kwargs = client.session.request.call_args
        self.assertEqual(kwargs["params"], {"qrToken": "qt"})

    def test_finish_qr_login(self):
        client = make_api()
        client.session.request.return_value = json_response({})
        client.session.cookies.set("CASLOGC", "abc", domain=".zhihuishu.com", path="/")
        client.finish_qr_login("once")
        _, kwargs = client.session.request.call_args
        self.assertEqual(kwargs["params"], {"service": api.BASE + "/login/gologin", "pwd": "once"})
        self.assertTrue(kwargs["allow_redirects"])
        client.session.cookies.clear()
        with self.assertRaises(ZHSError):
            client.finish_qr_login("once")


class ExamApiTest(unittest.TestCase):
    def test_exam_sheet_uses_exam_key_and_host(self):
        client = make_api()
        client.session.request.side_effect = [
            json_response({"code": 0, "data": {"partSheetVos": [
                {"questionSheetVos": [{"questionId": "1"}, {"questionId": 2, "version": 3}]}]}}),
            json_response({"code": 0, "data": {"partSheetVos": [
                {"questionSheetVos": [{"questionId": 3}]},
                {"questionSheetVos": [{"questionId": 4, "version": "2"}]}]}}),
        ]
        self.assertEqual(client.exam_sheet(11, 22, all_parts=False),
                         [{"questionId": 1, "version": 1}, {"questionId": 2, "version": 3}])
        self.assertEqual(client.exam_sheet(11, 22, all_parts=True),
                         [{"questionId": 3, "version": 1}, {"questionId": 4, "version": 2}])
        first, second = client.session.request.call_args_list
        self.assertEqual(first.args[0], "GET")
        self.assertTrue(first.args[1].startswith(api.EXAM + "/gateway/t/v1/"))
        self.assertTrue(first.args[1].endswith("/exam/user/getExamSheetInfo"))
        self.assertTrue(second.args[1].endswith("/exam/user/getAnswerSheetInformation"))
        body = json.loads(decrypt(first.kwargs["params"]["secretStr"], KEYS["exam"]))
        self.assertEqual(body["examTestId"], 11)
        self.assertEqual(body["examPaperId"], 22)

    def test_exam_submit_without_body(self):
        client = make_api()
        empty = Mock()
        empty.status_code = 200
        client.session.request.return_value = empty
        client.exam_submit({"examTestId": 1}, expect_body=False)
        _, kwargs = client.session.request.call_args
        self.assertIsNone(kwargs["json"])
        body = json.loads(decrypt(kwargs["data"]["secretStr"], KEYS["exam"]))
        self.assertEqual(body["examTestId"], 1)
        self.assertIn("dateFormate", body)

        client.session.request.return_value = Mock(status_code=500)
        with self.assertRaises(PlatformUnavailable):
            client.exam_submit({"examTestId": 1}, expect_body=False)

    def test_ai_exam_tasks_filters_type(self):
        client = make_api()
        client.session.request.return_value = json_response({"code": 200, "data": [
            {"taskType": 1, "id": 1}, {"taskType": 2, "id": 2}, "bad"]})
        self.assertEqual(client.ai_exam_tasks(9), [{"taskType": 1, "id": 1}])
        _, kwargs = client.session.request.call_args
        body = json.loads(decrypt(kwargs["json"]["secretStr"], KEYS["ai"]))
        self.assertEqual(body["courseId"], "9")
        self.assertEqual(body["taskType"], 1)

    def test_ai_homework_info_shape(self):
        client = make_api()
        client.session.request.return_value = json_response({"code": 200, "data": {"examTestId": 1}})
        self.assertEqual(client.ai_homework_info(1, 2, 3), {"examTestId": 1})
        client.session.request.return_value = json_response({"code": 200, "data": []})
        with self.assertRaises(PlatformUnavailable):
            client.ai_homework_info(1, 2, 3)


class SharedExamApiTest(unittest.TestCase):
    def test_exam_origin_does_not_change_following_study_request(self):
        client = ZHSApi()
        self.addCleanup(client.close)
        with patch.object(client.session, "send", return_value=json_response({"status": "200", "code": 0})) as send:
            client.exam_form(api.HOMEWORK_GATEWAY + "/student/getStudentHomework", {})
            client.secure(api.STUDY_GATEWAY + "/learning/queryCourse", {})
        exam_headers, study_headers = [call.args[0].headers for call in send.call_args_list]
        self.assertEqual(exam_headers["Origin"], "https://onlineexamh5new.zhihuishu.com")
        self.assertEqual(exam_headers["Referer"], "https://onlineexamh5new.zhihuishu.com/")
        self.assertEqual(study_headers["Origin"], "https://studyh5.zhihuishu.com")
        self.assertEqual(study_headers["Referer"], "https://studyh5.zhihuishu.com/")

    def test_exam_form_has_no_date_formate(self):
        client = make_api()
        client.session.request.return_value = json_response({"status": "200", "rt": {"ok": 1}})
        result = client.exam_form("https://x.test/path", {"a": 1})
        self.assertEqual(result["rt"], {"ok": 1})
        _, kwargs = client.session.request.call_args
        self.assertNotIn("dateFormate", kwargs["data"])
        body = json.loads(decrypt(kwargs["data"]["secretStr"], KEYS["exam"]))
        self.assertEqual(body, {"a": 1})

    def test_empty_homework_page_has_no_list(self):
        # The live platform answers an empty page with {"next": ...} and no list key.
        client = make_api()
        client.session.request.return_value = json_response({"status": "200", "rt": {"next": 0}})
        self.assertEqual(client.shared_homework_list(1, 2, 2), [])
        client.session.request.return_value = json_response({"status": "200", "rt": None})
        with self.assertRaises(PlatformUnavailable):
            client.shared_homework_list(1, 2, 2)

    def test_exam_form_extra_and_status(self):
        client = make_api()
        client.session.request.return_value = json_response({"status": 200})
        client.exam_form("https://x.test/path", {"a": 1}, extra={"source": "1"})
        _, kwargs = client.session.request.call_args
        self.assertEqual(kwargs["data"]["source"], "1")
        client.session.request.return_value = json_response({"status": "500", "msg": "boom"})
        with self.assertRaises(ZHSApiError) as ctx:
            client.exam_form("https://x.test/path", {})
        self.assertEqual(ctx.exception.code, "500")
        self.assertIn("boom", str(ctx.exception))

    def test_exam_form_captcha(self):
        client = make_api()
        client.session.request.return_value = json_response({"status": "500", "msg": "请拖动滑块验证"})
        with self.assertRaises(CaptchaRequired):
            client.exam_form("https://x.test/path", {})

    def test_shared_exam_sso(self):
        client = make_api()
        redirect = Mock(status_code=302, headers={"Location": "https://x.test/landing?ticket=t1"})
        landing = Mock(status_code=200)
        client.session.request.side_effect = [redirect, landing]
        client.shared_exam_sso()
        first, second = client.session.request.call_args_list
        self.assertTrue(first.args[1].startswith(api.PASSPORT + "/cas/login"))
        self.assertFalse(first.kwargs["allow_redirects"])
        self.assertEqual(first.kwargs["params"]["service"],
                         api.HOMEWORK + "/studentExam/gateway/t/v1/student/getStudentHomework")
        self.assertEqual(second.args[1], "https://x.test/landing?ticket=t1")
        self.assertTrue(second.kwargs["allow_redirects"])
        # Completed once; a second call makes no request.
        client.shared_exam_sso()
        self.assertEqual(client.session.request.call_count, 2)

    def test_shared_exam_sso_expired(self):
        client = make_api()
        client.session.request.return_value = Mock(status_code=200, headers={})
        with self.assertRaises(AuthenticationExpired):
            client.shared_exam_sso()

    def test_shared_submit_exam_checks_statu(self):
        client = make_api()
        client.session.request.return_value = json_response({"status": "200", "rt": {"statu": "1"}})
        self.assertEqual(client.shared_submit_exam(1, 2, 3, 4)["statu"], "1")
        client.session.request.return_value = json_response(
            {"status": "200", "rt": {"statu": "0", "msg": "no"}})
        with self.assertRaises(ZHSApiError) as ctx:
            client.shared_submit_exam(1, 2, 3, 4)
        self.assertEqual(ctx.exception.code, "statu")
        self.assertIn("no", str(ctx.exception))

    def test_shared_redo_homework(self):
        client = make_api()
        client.session.request.return_value = json_response({"status": "200", "rt": {"ok": 1}})
        result = client.shared_redo_homework(9, 200, 3)
        self.assertEqual(result["rt"], {"ok": 1})
        _, kwargs = client.session.request.call_args
        self.assertIn(api.HOMEWORK_GATEWAY + "/student/saveCourseTran",
                      client.session.request.call_args.args[1])
        body = json.loads(decrypt(kwargs["data"]["secretStr"], KEYS["exam"]))
        self.assertEqual(body, {"recruitId": 9, "examId": 200, "description": "", "courseId": 3})

    def test_shared_submit_exam_uses_taurus(self):
        client = make_api()
        client.session.request.return_value = json_response({"status": "200", "rt": {"statu": "1"}})
        client.shared_submit_exam(1, 2, 3, 4)
        _, kwargs = client.session.request.call_args
        self.assertIn(api.TAURUS + "/taurusExam/gateway/t/v1/answer/submit", client.session.request.call_args.args[1])
        body = json.loads(decrypt(kwargs["data"]["secretStr"], KEYS["exam"]))
        self.assertEqual(body["achieveCount"], "4")


if __name__ == "__main__":
    unittest.main()
