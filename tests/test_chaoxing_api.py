import base64
import unittest

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from requests.cookies import RequestsCookieJar

from base.auth import AuthenticationExpired
from ChaoxingMain.api import AES_KEY, ChaoxingApi, ChaoxingError, LoginError, WorkUnavailable, encrypt


class FakeResponse:
    def __init__(self, text="", status_code=200, url="https://mooc1.chaoxing.com/page", json_body=None):
        self.text = text
        self.status_code = status_code
        self.url = url
        self.json_body = json_body

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

    def test_job_list_tries_every_card_page(self):
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
        self.assertEqual(numbers, list("0123456789"))
        self.assertEqual(set(knowledge_ids), {"123"})

    def test_not_open_chapter_stops_reading(self):
        session = FakeSession(lambda *args: FakeResponse("章节未开放"))
        api = ChaoxingApi(cookies={"UID": "1"}, session=session, sleep=lambda _: None)
        jobs, info = api.job_list(COURSE, POINT)
        self.assertEqual(jobs, [])
        self.assertEqual(info, {"notOpen": True})
        self.assertEqual(len(session.calls), 1)


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
