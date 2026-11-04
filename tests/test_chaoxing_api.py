import base64
import hashlib
import json
import unittest

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from requests.cookies import RequestsCookieJar

from base.auth import AuthenticationExpired, PlatformUnavailable
from ChaoxingMain.api import (AES_KEY, ChaoxingApi, ChaoxingError, LoginError, WorkUnavailable,
                              encrypt, video_enc)


class FakeResponse:
    def __init__(self, text="", status_code=200, url="https://mooc1.chaoxing.com/page",
                 json_body=None, content=None):
        self.text = text
        self.status_code = status_code
        self.url = url
        self.json_body = json_body
        self.content = content if content is not None else text.encode()

    def json(self):
        if self.json_body is None:
            raise ValueError("not json")
        return self.json_body


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.headers = {}
        self.cookies = RequestsCookieJar()
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.handler("GET", url, kwargs)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.handler("POST", url, kwargs)

    def close(self):
        self.closed = True


def course_card(course_id, clazz_id, cpi, title):
    return ('<div class="course" id="course_%s" info="i" roleid="2">'
            '<a href="https://mooc1.chaoxing.com/x?courseId=%s&clazzid=%s&cpi=%s&ut=s">'
            '<span class="course-name" title="%s"></span></a>'
            '<input class="clazzId" value="%s"><input class="courseId" value="%s"></div>'
            % (course_id, course_id, clazz_id, cpi, title, clazz_id, course_id))


CARDS = """
<script>
mArg={"defaults":{"ktoken":"kt","cpi":"333","knowledgeid":"123"},"attachments":[
{"type":"workid","jobid":"work-abc","property":{"title":"1.1 章节测验"},"enc":"e"}]};
</script>
"""

COURSE = {"title": "高等数学", "courseId": "111", "clazzId": "222", "cpi": "333"}
POINT = {"id": "123", "title": "1.1 函数"}
JOB = {"type": "workid", "jobid": "work-abc", "name": "1.1 章节测验", "enc": "e"}
JOB_INFO = {"knowledgeid": "123", "ktoken": "kt", "cpi": "333"}


class LoginTest(unittest.TestCase):
    def test_encrypt_is_aes_cbc_with_platform_key(self):
        raw = base64.b64decode(encrypt("secret"))
        plain = unpad(AES.new(AES_KEY, AES.MODE_CBC, AES_KEY).decrypt(raw), 16)
        self.assertEqual(plain, b"secret")

    def test_login_uploads_encrypted_credentials(self):
        calls = []

        def handler(method, url, kwargs):
            calls.append((method, url, kwargs))
            return FakeResponse(json_body={"status": True})

        api = ChaoxingApi("13800000000", "secret", session=FakeSession(handler))
        self.assertTrue(api.logged_in)
        method, url, kwargs = calls[0]
        self.assertEqual((method, url), ("POST", "https://passport2.chaoxing.com/fanyalogin"))
        self.assertNotEqual(kwargs["data"]["uname"], "13800000000")
        self.assertEqual(kwargs["data"]["fid"], "-1")

    def test_login_failure_uses_platform_message(self):
        session = FakeSession(lambda *args: FakeResponse(json_body={"status": False, "msg2": "账号或密码错误"}))
        with self.assertRaises(LoginError) as caught:
            ChaoxingApi("u", "p", session=session)
        self.assertIn("账号或密码错误", str(caught.exception))

    def test_login_without_json_is_an_error(self):
        with self.assertRaises(LoginError):
            ChaoxingApi("u", "p", session=FakeSession(lambda *args: FakeResponse("<html>登录页</html>")))

    def test_cookies_skip_login(self):
        def handler(*args):
            raise AssertionError("cookie 登录不应该请求平台")

        api = ChaoxingApi(cookies={"UID": "1"}, session=FakeSession(handler))
        self.assertTrue(api.logged_in)


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
LOGIN_PAGE = ('<input type="hidden" value="u1" id="uuid"/>'
              '<input type="hidden" id="enc" value="e1"/>')


class QrLoginTest(unittest.TestCase):
    def client(self, handler):
        return ChaoxingApi(session=FakeSession(handler), sleep=lambda _: None)

    def test_login_qr_reads_uuid_and_fetches_the_image(self):
        seen = []

        def handler(method, url, kwargs):
            seen.append((method, url, kwargs))
            if url.endswith("/login"):
                return FakeResponse(LOGIN_PAGE)
            if url.endswith("/createqr"):
                return FakeResponse(content=PNG)
            raise AssertionError(url)

        token, image = self.client(handler).login_qr()
        self.assertEqual(token, {"uuid": "u1", "enc": "e1"})
        self.assertEqual(image, PNG)
        self.assertEqual(seen[-1][2]["params"], {"uuid": "u1", "fid": -1})

    def test_login_qr_without_parameters_is_reported(self):
        with self.assertRaises(LoginError):
            self.client(lambda *args: FakeResponse("<html>登录页</html>")).login_qr()

    def status(self, payload):
        seen = []

        def handler(method, url, kwargs):
            seen.append((method, url, kwargs))
            return FakeResponse(json_body=payload)

        result = self.client(handler).login_qr_status({"uuid": "u", "enc": "e"})
        self.assertEqual(seen[0][1], "https://passport2.chaoxing.com/getauthstatus/v2")
        self.assertEqual(seen[0][2]["data"]["enc"], "e")
        return result

    def test_status_mapping(self):
        self.assertEqual(self.status({"status": True})["status"], 1)
        self.assertEqual(self.status({"status": False, "type": "3"})["status"], -1)
        self.assertEqual(self.status({"status": False, "type": 4, "nickname": "张三"}),
                         {"status": 0, "nickname": "张三"})
        self.assertEqual(self.status({"status": False, "type": "2"})["status"], 2)
        self.assertEqual(self.status({"status": False, "type": "7", "mes": "二维码失效"})["status"], 2)
        self.assertEqual(self.status({"status": False, "type": "6"})["status"], 3)
        two_factor = self.status({"status": True, "containTwoFactorLogin": True})
        self.assertEqual(two_factor["status"], 3)
        self.assertIn("二次验证", two_factor["message"])

    def test_refresh_gets_a_new_uuid_and_image(self):
        seen = []

        def handler(method, url, kwargs):
            seen.append((method, url, kwargs))
            if url.endswith("/refreshQRCode"):
                return FakeResponse(json_body={"uuid": "u2", "enc": "e2"})
            if url.endswith("/createqr"):
                return FakeResponse(content=PNG)
            raise AssertionError(url)

        token, image = self.client(handler).refresh_login_qr({"uuid": "u", "enc": "e"})
        self.assertEqual(token, {"uuid": "u2", "enc": "e2"})
        self.assertEqual(image, PNG)
        self.assertEqual(seen[0][1], "https://passport2.chaoxing.com/refreshQRCode")

    def test_finish_visits_mooc_and_verifies_the_session(self):
        urls = []

        def handler(method, url, kwargs):
            urls.append(url)
            if url.startswith("https://i.mooc.chaoxing.com"):
                return FakeResponse("<html>space</html>")
            if "courselistdata" in url:
                return FakeResponse("<html>courses</html>")
            raise AssertionError(url)

        api = self.client(handler)
        api.finish_qr_login()
        self.assertTrue(api.logged_in)
        self.assertIn("https://i.mooc.chaoxing.com/space/index", urls)

    def test_finish_reports_a_session_that_did_not_take(self):
        api = self.client(lambda *args: FakeResponse(status_code=401))
        with self.assertRaises(LoginError):
            api.finish_qr_login()
        self.assertFalse(api.logged_in)


class RequestTest(unittest.TestCase):
    def client(self, response):
        return ChaoxingApi(cookies={"UID": "1"}, session=FakeSession(lambda *args: response),
                           sleep=lambda _: None)

    def test_signed_in_reads_the_course_list_page(self):
        session = FakeSession(lambda *args: FakeResponse("<html></html>"))
        api = ChaoxingApi(cookies={"UID": "1"}, session=session, sleep=lambda _: None)
        self.assertTrue(api.signed_in())
        self.assertEqual(session.calls[0][1],
                         "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata")

    def test_signed_in_reports_expiry(self):
        api = ChaoxingApi(cookies={"UID": "1"},
                          session=FakeSession(lambda *args: FakeResponse(status_code=401)), sleep=lambda _: None)
        with self.assertRaises(AuthenticationExpired):
            api.signed_in()

    def test_unauthorized(self):
        with self.assertRaises(AuthenticationExpired):
            self.client(FakeResponse(status_code=401))._request("get", "https://x/")

    def test_redirect_to_passport_means_expired(self):
        response = FakeResponse(url="https://passport2.chaoxing.com/login?refer=x")
        with self.assertRaises(AuthenticationExpired):
            self.client(response)._request("get", "https://x/")

    def test_other_status_is_platform_error(self):
        with self.assertRaises(ChaoxingError):
            self.client(FakeResponse(status_code=500))._request("get", "https://x/")

    def test_forbidden_checks_login_without_repeating_the_business_request(self):
        session = FakeSession(lambda method, url, kwargs: FakeResponse(
            status_code=200 if url.endswith("/visit/courselistdata") else 403))
        api = ChaoxingApi(cookies={"UID": "1"}, session=session)
        with self.assertRaisesRegex(ChaoxingError, "HTTP 403"):
            api._request("post", "https://mooc1.chaoxing.com/write", data={"answer": "A"})
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(session.calls[-1][1].endswith("/visit/courselistdata"))
        self.assertTrue(api.logged_in)

    def test_forbidden_with_an_expired_login_propagates_expiry(self):
        for response in (FakeResponse(status_code=401),
                         FakeResponse(url="https://passport2.chaoxing.com/login")):
            with self.subTest(response=response):
                session = FakeSession(lambda method, url, kwargs: response
                                      if url.endswith("/visit/courselistdata") else FakeResponse(status_code=403))
                api = ChaoxingApi(cookies={"UID": "1"}, session=session)
                with self.assertRaises(AuthenticationExpired):
                    api._request("get", "https://mooc1.chaoxing.com/page")
                self.assertEqual(len(session.calls), 2)

    def test_forbidden_login_probe_does_not_recurse_or_claim_expiry(self):
        api = self.client(FakeResponse(status_code=403))
        with self.assertRaisesRegex(ChaoxingError, "HTTP 403"):
            api._request("get", "https://mooc1.chaoxing.com/page")
        self.assertEqual(len(api.session.calls), 2)

    def test_failed_login_probe_preserves_the_login(self):
        import requests

        def handler(method, url, kwargs):
            if url.endswith("/visit/courselistdata"):
                raise requests.Timeout()
            return FakeResponse(status_code=403)

        api = ChaoxingApi(cookies={"UID": "1"}, session=FakeSession(handler))
        with self.assertRaises(PlatformUnavailable):
            api._request("get", "https://mooc1.chaoxing.com/page")
        self.assertTrue(api.logged_in)


class CatalogTest(unittest.TestCase):
    def test_course_list_includes_custom_folders(self):
        def handler(method, url, kwargs):
            if url.endswith("/visit/interaction"):
                return FakeResponse('<ul class="file-list"><li fileid="42"><input class="rename-input" value="收藏"></li></ul>')
            folder = (kwargs.get("data") or {}).get("courseFolderId")
            if str(folder) == "42":
                return FakeResponse(course_card("222", "333", "444", "选修课"))
            return FakeResponse(course_card("111", "222", "333", "高等数学"))

        api = ChaoxingApi(cookies={"UID": "1"}, session=FakeSession(handler), sleep=lambda _: None)
        courses = api.course_list()
        self.assertEqual([course["title"] for course in courses], ["高等数学", "选修课"])
        self.assertEqual([course["courseId"] for course in courses], ["111", "222"])

    def test_job_list_stops_at_a_repeated_card(self):
        numbers, knowledge_ids = [], []

        def handler(method, url, kwargs):
            numbers.append(kwargs["params"]["num"])
            knowledge_ids.append(kwargs["params"]["knowledgeid"])
            return FakeResponse(CARDS)

        api = ChaoxingApi(cookies={"UID": "1"}, session=FakeSession(handler), sleep=lambda _: None)
        jobs, info = api.job_list(COURSE, POINT)
        # 每个 num 都返回同一张卡片时只保留一份，避免重复提交。
        self.assertEqual([job["jobid"] for job in jobs], ["work-abc"])
        self.assertEqual(info["knowledgeid"], "123")
        self.assertEqual(numbers, ["0", "1"])
        self.assertEqual(set(knowledge_ids), {"123"})

    def test_job_list_stops_at_an_empty_card(self):
        for empty in ("<html></html>", 'mArg={"defaults":{},"attachments":[]};'):
            with self.subTest(empty=empty):
                session = FakeSession(lambda method, url, kwargs: FakeResponse(
                    CARDS if kwargs["params"]["num"] == "0" else empty))
                api = ChaoxingApi(cookies={"UID": "1"}, session=session, sleep=lambda _: None)
                jobs, info = api.job_list(COURSE, POINT)
                self.assertEqual([job["jobid"] for job in jobs], ["work-abc"])
                self.assertEqual(info["knowledgeid"], "123")
                self.assertEqual(len(session.calls), 2)

    def test_completed_and_unsupported_cards_do_not_hide_later_work(self):
        for first in ({"type": "video", "jobid": "v1", "objectId": "o", "isPassed": True},
                      {"type": "live", "jobid": "l1"},
                      {"type": "video", "objectId": "non-task"}):
            for include_finished in (False, True):
                with self.subTest(first=first, include_finished=include_finished):
                    pages = ['mArg=%s;' % json.dumps({"attachments": [first]}), CARDS, ""]
                    session = FakeSession(lambda method, url, kwargs: FakeResponse(
                        pages[int(kwargs["params"]["num"])]))
                    api = ChaoxingApi(cookies={"UID": "1"}, session=session, sleep=lambda _: None)
                    jobs, _ = api.job_list(COURSE, POINT, include_finished=include_finished)
                    ids = [job["jobid"] for job in jobs]
                    self.assertIn("work-abc", ids)
                    self.assertEqual("v1" in ids, include_finished and first.get("jobid") == "v1")
                    self.assertEqual(len(session.calls), 3)

    def test_card_scan_is_bounded_and_deduplicates_overlapping_jobs(self):
        def handler(method, url, kwargs):
            number = kwargs["params"]["num"]
            return FakeResponse(CARDS.replace('"attachments":[', '"attachments":[{"type":"workid","jobid":"w%s"},' % number))

        session = FakeSession(handler)
        api = ChaoxingApi(cookies={"UID": "1"}, session=session, sleep=lambda _: None)
        jobs, _ = api.job_list(COURSE, POINT)
        self.assertEqual(len(session.calls), 10)
        self.assertEqual(len(jobs), 11)

    def test_job_list_includes_learning_tasks(self):
        from tests.test_chaoxing_decode import ALL_CARDS
        session = FakeSession(lambda *args: FakeResponse(ALL_CARDS))
        api = ChaoxingApi(cookies={"UID": "1"}, session=session, sleep=lambda _: None)
        jobs, _ = api.job_list(COURSE, POINT)
        self.assertEqual({job["type"] for job in jobs}, {"video", "document", "read", "workid"})

    def test_not_open_chapter_stops_reading(self):
        session = FakeSession(lambda *args: FakeResponse("章节未开放"))
        api = ChaoxingApi(cookies={"UID": "1"}, session=session, sleep=lambda _: None)
        jobs, info = api.job_list(COURSE, POINT)
        self.assertEqual(jobs, [])
        self.assertEqual(info, {"notOpen": True})
        self.assertEqual(len(session.calls), 1)


VIDEO_JOB = {"type": "video", "jobid": "work-v", "name": "视频1", "objectid": "obj",
             "otherinfo": "nodeId_123-a", "mid": "m1", "aid": "a1", "playTime": 30000,
             "rt": "0.9"}
JOB_INFO = {"knowledgeid": "123"}
VIDEO_BLOB = {
    "type": "video", "videoName": "视频1", "duration": 600, "otherInfo": "nodeId_123-a",
    "objectid": "obj", "jobid": "work-v", "fastforward": True, "vbegin": 10, "vend": 590,
    "playTime": 30, "reportTimeInterval": 15,
    "reportUrl": "https://mooc1.chaoxing.com/mooc-ans/multimedia/log/a/333/dt",
    "knowledgeid": "123", "courseid": "111", "clazzId": "222", "mid": "m1", "rt": 0.9,
    "cpi": "333", "aid": "a1",
}


class LearningApiTest(unittest.TestCase):
    def client(self, handler):
        return ChaoxingApi(cookies={"UID": "99", "fid": "7"}, session=FakeSession(handler), sleep=lambda _: None)

    def blob_client(self, blob, reports, passed=lambda count, params: count >= 1):
        def handler(method, url, kwargs):
            if "getknowledgecurvideo" in url:
                self.assertEqual(kwargs["data"]["mid"], "m1")
                self.assertIn("mooc1-api.chaoxing.com", url)
                self.assertIn("Android", kwargs["headers"]["User-Agent"])
                return FakeResponse(json_body={"data": {"videoObject": json.dumps(blob)}})
            reports.append(kwargs["params"])
            return FakeResponse(json_body={"isPassed": bool(passed(len(reports), kwargs["params"]))})

        return self.client(handler)

    def test_video_enc_uses_the_clip_time(self):
        raw = "[222][99][work-v][obj][600000][d_yHJ!$pdA~5][600000][10_590]"
        self.assertEqual(video_enc(222, "work-v", "obj", 600, 600, 99, "10_590"),
                         hashlib.md5(raw.encode()).hexdigest())

    def test_video_info_reads_the_player_blob(self):
        api = self.blob_client(VIDEO_BLOB, [])
        info = api.video_info(COURSE, VIDEO_JOB, JOB_INFO)
        self.assertEqual(info["duration"], 600)
        self.assertEqual(info["end"], 590)              # 老师截取的结尾
        self.assertEqual(info["clip_time"], "10_590")
        self.assertTrue(info["fastforward"])
        self.assertEqual(info["rt"], 0.9)
        self.assertEqual(info["interval"], 15)
        self.assertEqual(info["playTime"], 30)
        self.assertEqual(info["reportUrl"], VIDEO_BLOB["reportUrl"])

    def test_live_player_seconds_match_card_milliseconds(self):
        # 2026-11-04 同一个已观看视频的只读接口采样；不含账号和签名信息。
        blob = dict(VIDEO_BLOB, duration="1315", playTime=1176)
        api = self.blob_client(blob, [])
        player = api.video_info(COURSE, VIDEO_JOB, JOB_INFO)
        fallback = self.client(lambda *args: FakeResponse(json_body={
            "status": "success", "duration": 1315, "dtoken": "dt"}))
        card = fallback._video_info_from_status(COURSE, dict(VIDEO_JOB, playTime=1176000))
        self.assertEqual(player["playTime"], 1176)
        self.assertEqual(player["playTime"], card["playTime"])

    def test_fastforward_video_drags_to_the_end_and_completes(self):
        reports = []
        api = self.blob_client(VIDEO_BLOB, reports, passed=lambda count, params: params["isdrag"] == "4")
        self.assertEqual(api.study_video(COURSE, VIDEO_JOB, job_info=JOB_INFO), "success")
        self.assertEqual([report["isdrag"] for report in reports], ["3", "1", "4"])
        drag, complete = reports[1], reports[2]
        self.assertEqual(drag["playingTime"], "30-590")       # drag 是 from-to
        self.assertEqual(complete["playingTime"], "590")
        self.assertEqual(complete["clipTime"], "10_590")
        self.assertEqual(complete["rt"], "0.9")
        self.assertEqual(complete["view"], "pc")
        self.assertEqual(complete["enc"], video_enc(222, "work-v", "obj", 590, 600, "99", "10_590"))

    def test_drag_that_completes_stops_before_the_complete_report(self):
        reports = []
        api = self.blob_client(VIDEO_BLOB, reports, passed=lambda count, params: params["isdrag"] == "1")
        self.assertEqual(api.study_video(COURSE, VIDEO_JOB, job_info=JOB_INFO), "success")
        self.assertEqual([report["isdrag"] for report in reports], ["3", "1"])

    def test_video_without_fastforward_reports_start_playing_complete(self):
        blob = dict(VIDEO_BLOB, fastforward=False, playTime=600, vend=0, vbegin=0)
        reports = []
        api = self.blob_client(blob, reports, passed=lambda count, params: params["isdrag"] == "4")
        self.assertEqual(api.study_video(COURSE, VIDEO_JOB, job_info=JOB_INFO), "success")
        self.assertEqual([report["isdrag"] for report in reports], ["3", "0", "4"])
        self.assertEqual(reports[-1]["playingTime"], "600")

    def test_non_task_video_is_skipped(self):
        blob = dict(VIDEO_BLOB, jobid="")
        reports = []
        api = self.blob_client(blob, reports)
        self.assertEqual(api.study_video(COURSE, VIDEO_JOB, job_info=JOB_INFO), "skipped")
        self.assertEqual(reports, [])

    def test_video_without_mid_uses_the_status_endpoint(self):
        job = {key: value for key, value in VIDEO_JOB.items() if key != "mid"}
        calls = []

        def handler(method, url, kwargs):
            calls.append((url, kwargs))
            return FakeResponse(json_body={"status": "success", "dtoken": "dt", "duration": 600})

        api = self.client(handler)
        info = api.video_info(COURSE, job, JOB_INFO)
        self.assertTrue(any("/knowledge/getnextknowledgevideo" in url for url, _ in calls))
        self.assertTrue(any("/ananas/status/obj" in url for url, _ in calls))
        self.assertEqual(info["duration"], 600)
        self.assertTrue(info["reportUrl"].endswith("/multimedia/log/a/333/dt"))

    def test_document_read_and_empty_page(self):
        seen = []

        def handler(method, url, kwargs):
            seen.append((url, kwargs))
            return FakeResponse("{}")

        api = self.client(handler)
        self.assertEqual(api.study_document(COURSE, {"jobid": "d1", "otherinfo": "nodeId_123-rt_1"}), "success")
        self.assertIn("/mooc-ans/job/document", seen[-1][0])
        params = seen[-1][1]["params"]
        self.assertEqual(params["knowledgeid"], "123")
        self.assertEqual(params["courseid"], "111")
        self.assertEqual(params["checkMicroTopic"], "true")
        self.assertEqual(params["microTopicId"], "undefined")
        self.assertEqual(params["courseEngineInfo"], "false")
        self.assertEqual(api.study_read(COURSE, {"jobid": "r1", "jtoken": "jt"}, {"knowledgeid": "123"}), "success")
        self.assertIn("/ananas/job/readv2", seen[-1][0])
        self.assertEqual(seen[-1][1]["params"]["jtoken"], "jt")
        self.assertEqual(api.empty_page(COURSE, {"id": "p1"}), "success")
        self.assertIn("/mycourse/studentstudyAjax", seen[-1][0])
        self.assertEqual(seen[-1][1]["params"]["chapterId"], "p1")

    def test_document_without_knowledgeid_is_an_error(self):
        api = self.client(lambda *a: FakeResponse("{}"))
        self.assertEqual(api.study_document(COURSE, {"jobid": "d1", "otherinfo": ""}), "error")

    def test_document_falls_back_to_the_chapter_knowledgeid(self):
        seen = []

        def handler(method, url, kwargs):
            seen.append(kwargs["params"])
            return FakeResponse(json_body={"status": True})

        api = self.client(handler)
        self.assertEqual(api.study_document(COURSE, {"jobid": "d1", "otherinfo": ""},
                                            {"knowledgeid": "123"}), "success")
        self.assertEqual(seen[0]["knowledgeid"], "123")

    def test_document_rejected_by_the_platform_is_an_error(self):
        api = self.client(lambda *a: FakeResponse(json_body={"status": False, "msg": "已过期"}))
        self.assertEqual(api.study_document(COURSE, {"jobid": "d1", "otherinfo": "nodeId_123-x"},
                                            {"knowledgeid": "123"}), "error")


class StatusApiTest(unittest.TestCase):
    def client(self, handler):
        return ChaoxingApi(cookies={"UID": "99", "fid": "7"}, session=FakeSession(handler), sleep=lambda _: None)

    def test_course_nodes_reads_gear_counts(self):
        seen = []

        def handler(method, url, kwargs):
            seen.append((url, kwargs["params"]))
            return FakeResponse(json_body={"data": {"course": {"knowledge": [
                {"id": "123", "name": "1.1 函数", "finishcount": 2, "unfinishcount": 1, "clickcount": 3},
                {"id": "456", "name": "1.2 极限", "jobFinishedCount": 1, "jobUnfinishedCount": 0},
            ]}}})

        nodes = self.client(handler).course_nodes(COURSE)
        self.assertIn("mooc1-api.chaoxing.com/gas/clazz", seen[0][0])
        self.assertEqual(seen[0][1]["id"], "222")
        self.assertEqual(seen[0][1]["personid"], "333")
        self.assertEqual(nodes["123"], {"name": "1.1 函数", "finish": 2, "unfinish": 1, "click": 3})
        self.assertEqual((nodes["456"]["finish"], nodes["456"]["unfinish"]), (1, 0))

    def test_finished_job_ids_pages_until_short(self):
        seen = []

        def handler(method, url, kwargs):
            seen.append((url, kwargs["params"]))
            if "/jobfinishedList" not in url:
                return FakeResponse(json_body={})
            page = int(kwargs["params"]["page"])
            rows = [{"jobid": "j%d" % (index + 1)} for index in range(20)] if page == 1 else [{"jobId": "j21"}]
            return FakeResponse(json_body={"data": {"list": rows}})

        api = self.client(handler)
        self.assertEqual(api.finished_job_ids(COURSE), {"j%d" % index for index in range(1, 22)})
        self.assertTrue(all("mooc1-api.chaoxing.com" in url for url, _ in seen))
        self.assertEqual([params["page"] for _, params in seen], [1, 2])

    def test_job_status_ids_returns_both_sets(self):
        def handler(method, url, kwargs):
            if "/jobfinishedList" in url:
                return FakeResponse(json_body={"data": [{"jobid": "done-1"}]})
            return FakeResponse(json_body={"data": {"list": [{"jobid": "todo-1"}]}})

        api = self.client(handler)
        self.assertEqual(api.job_status_ids(COURSE), ({"done-1"}, {"todo-1"}))

    def test_course_nodes_falls_back_to_empty_on_failure(self):
        api = self.client(lambda *a: FakeResponse("not json", status_code=500))
        self.assertEqual(api.course_nodes(COURSE), {})


class WorkTest(unittest.TestCase):
    def test_work_page_params(self):
        session = FakeSession(lambda *args: FakeResponse("<html>题目</html>"))
        api = ChaoxingApi(cookies={"UID": "1"}, session=session, sleep=lambda _: None)
        self.assertEqual(api.work_page(COURSE, JOB, JOB_INFO), "<html>题目</html>")
        params = session.calls[0][2]["params"]
        self.assertEqual(params["workId"], "abc")
        self.assertEqual(params["jobid"], "work-abc")
        self.assertEqual(params["knowledgeid"], "123")
        self.assertEqual(params["clazzId"], "222")
        self.assertEqual(params["courseid"], "111")

    def test_missing_test_is_skippable(self):
        api = ChaoxingApi(cookies={"UID": "1"},
                          session=FakeSession(lambda *args: FakeResponse("教师未创建完成该测验")), sleep=lambda _: None)
        with self.assertRaises(WorkUnavailable):
            api.work_page(COURSE, JOB, JOB_INFO)

    def test_submit_posts_form_and_returns_confirmation(self):
        session = FakeSession(lambda *args: FakeResponse(json_body={"status": True, "msg": "提交成功"}))
        api = ChaoxingApi(cookies={"UID": "1"}, session=session, sleep=lambda _: None)
        self.assertEqual(api.submit_work({"pyFlag": ""}), {"status": True, "msg": "提交成功"})
        self.assertEqual(session.calls[0][1],
                         "https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew")
        self.assertEqual(session.calls[0][2]["headers"]["X-Requested-With"], "XMLHttpRequest")

    def test_submit_without_json_is_platform_error(self):
        api = ChaoxingApi(cookies={"UID": "1"},
                          session=FakeSession(lambda *args: FakeResponse("<html>错误</html>")), sleep=lambda _: None)
        with self.assertRaises(ChaoxingError):
            api.submit_work({"pyFlag": ""})


if __name__ == "__main__":
    unittest.main()
