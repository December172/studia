import http.client
import json
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

from reporting import ProgressEvent
from settings import SettingsStore
from web.server import LocalAccess, Studia, _course, make_handler


class WebServerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = Studia(SettingsStore(directory.name), oauth=Mock(return_value=None))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), None)
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = make_handler(LocalAccess(self.app, self.port))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, *, token=True, host=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        headers = {"Host": host or "127.0.0.1:%d" % self.port}
        if token:
            headers["X-Studia-Token"] = self.app.token
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()

    def test_api_requires_token_and_own_host(self):
        self.assertEqual(self.request("GET", "/api/state", token=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/state", host="evil.example")[0], 403)
        self.assertEqual(self.request("GET", "/", host="evil.example")[0], 403)
        status, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual([p["id"] for p in state["platforms"]], ["mooc", "zhs"])
        self.assertFalse(any(p["logged_in"] for p in state["platforms"]))
        self.assertEqual(state["jobs"], [])

    def test_static_files_stay_inside_the_bundle(self):
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.request("GET", "/../settings.py")[0], 404)
        self.assertEqual(self.request("GET", "/%2e%2e/settings.py")[0], 404)
        self.assertEqual(self.request("GET", "/mock.js")[0], 404)

    def test_courses_need_login_and_jobs_validate_input(self):
        self.assertEqual(self.request("GET", "/api/platforms/zhs/courses")[0], 401)
        self.assertEqual(self.request("GET", "/api/platforms/nope/courses")[0], 404)
        self.assertEqual(self.request("POST", "/api/jobs", body={"platform": "zhs", "type": "rm", "course": "x"})[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", body={"platform": "mooc", "type": "review", "course": "x"})[0], 400)
        self.assertEqual(self.request("GET", "/api/platforms/zyk/courses")[0], 404)
        self.assertEqual(self.request("GET", "/api/platforms/mooc/tasks?course=x")[0], 401)

    def test_jobs_run_in_parallel_except_on_the_same_course_or_zhs_video(self):
        self.app.platforms["zhs"].rows = [{"courseName": "课", "zhs": {"kind": "shared", "courseId": 1}}]
        running = {"id": "a1", "kind": "study", "title": "自动学习", "platform": "zhs", "course": "shared-1",
                   "course_name": "网络课", "started": 0, "cancel": threading.Event()}
        self.app.jobs = {"a1": running}
        status, body = self.request("POST", "/api/jobs", body={"platform": "zhs", "type": "review", "course": "shared-1"})
        self.assertEqual(status, 409)
        self.assertIn("正在自动学习", json.loads(body)["error"])
        check = self.app._check_conflicts
        with self.assertRaisesRegex(Exception, "只能播放一个视频"):
            check("zhs", "shared-2", "study")
        check("zhs", "shared-1", "answer")       # answering the same course is fine
        check("mooc", "m1", "study")             # other platforms are independent
        self.app.jobs = {"a1": dict(running, platform="mooc", course="m1")}
        check("mooc", "m2", "study")             # MOOC courses study side by side
        with self.assertRaisesRegex(Exception, "正在自动学习"):
            check("mooc", "m1", "study")
        self.app.jobs = {str(i): dict(running, id=str(i), platform="mooc", course=str(i)) for i in range(20)}
        with self.assertRaisesRegex(Exception, "排队的任务已有"):
            check("mooc", "m99", "answer")

    def test_finished_job_lands_in_history_with_its_lines(self):
        platform = self.app.platforms["zhs"]
        platform.rows = [{"courseName": "网络课", "zhs": {"kind": "ai", "courseId": "c1"}}]
        platform.client = Mock()
        platform.spec = dict(platform.spec, handler=lambda **kwargs: kwargs["on_progress"](
            ProgressEvent("result", "第一节", status="success")))
        self.assertEqual(self.request("POST", "/api/jobs", body={"platform": "zhs", "type": "study", "course": "ai-c1"})[0], 200)
        for _ in range(50):
            if not self.app.jobs:
                break
            time.sleep(0.02)
        history = json.loads(self.request("GET", "/api/jobs")[1])["history"]
        self.assertEqual(history[0]["state"], "done")
        self.assertEqual((history[0]["platform"], history[0]["course_name"]), ("zhs", "网络课"))
        self.assertEqual(history[0]["lines"][0]["text"], "第一节 · 完成")

    def test_captcha_leaves_the_job_waiting_for_the_user(self):
        from ZHSMain.api import CaptchaRequired
        platform = self.app.platforms["zhs"]
        platform.rows = [{"courseName": "网络课", "zhs": {"kind": "ai", "courseId": "c1"}}]
        platform.client = Mock()
        platform.spec = dict(platform.spec, handler=lambda **kwargs: Mock(blocked=CaptchaRequired("请先完成滑块验证")),
                             units=lambda client, row: [{"name": "第一章", "units": []}])
        status, body = self.request("GET", "/api/platforms/zhs/units?course=ai-c1")
        self.assertEqual((status, json.loads(body)["chapters"][0]["name"]), (200, "第一章"))
        self.assertEqual(self.request("GET", "/api/platforms/zyk/units?course=x")[0], 404)
        self.assertEqual(self.request("POST", "/api/jobs", body={"platform": "zhs", "type": "study", "course": "ai-c1"})[0], 200)
        for _ in range(50):
            if not self.app.jobs:
                break
            time.sleep(0.02)
        job = json.loads(self.request("GET", "/api/jobs")[1])["history"][0]
        self.assertEqual((job["state"], job["summary"]), ("waiting", "请先完成滑块验证"))

    def test_model_test_records_health_and_default_model_cannot_be_deleted(self):
        from ai.client import AIConfig
        store = self.app.store
        store.ensure_default_model()
        profile_id = store.save_ai_profile(AIConfig("https://api.example.com/v1", "m", "sk-secret-value-1234"))
        with unittest.mock.patch("web.server.AIClient") as client:
            client.return_value.check_connection.side_effect = __import__("ai.client", fromlist=["AIError"]).AIError("401")
            status, body = self.request("POST", "/api/settings/models/%s/test" % profile_id)
        self.assertEqual((status, json.loads(body)["health"]), (200, "error"))
        models = {m["id"]: m for m in json.loads(self.request("GET", "/api/settings")[1])["models"]}
        self.assertEqual(models[profile_id]["health"], "error")
        default = next(m for m in models.values() if not m["deletable"])
        self.assertEqual(self.request("POST", "/api/settings/models/%s/delete" % default["id"])[0], 400)
        self.assertEqual(self.request("POST", "/api/settings/models/%s/delete" % profile_id)[0], 200)

    def test_mooc_tasks_are_listed_by_type_with_scores(self):
        platform = self.app.platforms["mooc"]
        row = {"courseName": "英语", "courseId": "c1"}
        platform.rows, platform.client = [row], Mock()
        entries = [(row, {"id": "e/1", "name": "第一单元作业", "score": 92, "_completion_status": "submitted"}, 1),
                   (row, {"id": "q2", "title": "随堂测验", "score": None, "_completion_status": "not_started"}, 3)]
        with unittest.mock.patch("web.server._mooc_catalog", return_value=entries):
            status, body = self.request("GET", "/api/platforms/mooc/tasks?course=c1")
        tasks = json.loads(body)["tasks"]
        self.assertEqual(status, 200)
        self.assertEqual([(t["id"], t["category"], t["score"]) for t in tasks], [("1-e_1", "作业", "92"), ("3-q2", "测验", None)])
        self.assertEqual(tasks[0]["status"], "已提交")
        self.assertIn("1-e_1", platform.entries["c1"])

    def test_settings_never_return_full_api_key(self):
        from ai.client import AIConfig
        store = self.app.store
        store.activate_ai_profile(store.save_ai_profile(
            AIConfig("https://api.example.com/v1", "m", "sk-secret-value-1234")))
        status, body = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertNotIn("secret-value", body.decode())
        self.assertIn("sk-…1234", body.decode())

    def test_course_mapping(self):
        row = {"courseName": "导论", "courseInfoName": "共享课", "studySpeed": "99.6",
               "zhs": {"kind": "shared", "courseId": 7}}
        self.assertEqual(_course("zhs", row), {"id": "shared-7", "platform": "zhs", "name": "导论", "type": "共享课",
                                               "kind": "shared", "progress": 100})
        self.assertEqual(_course("mooc", {"courseName": "高数", "courseId": "a/b"})["id"], "a_b")
        self.assertIsNone(_course("zhs", {"zhs": {"kind": "ai", "courseId": 8}})["progress"])

    def test_qr_login_completion_saves_session(self):
        client = Mock()
        client.login_qr_status.return_value = {"status": 1, "oncePassword": "once"}
        self.app.platforms["zhs"].login = {"client": client, "qr": "q", "refreshes": 0}
        self.app.store.save_session = Mock()
        self.assertEqual(self.app.login_status("zhs")["status"], "done")
        client.finish_qr_login.assert_called_once_with("once")
        self.app.store.save_session.assert_called_once_with("zhs", client)
        self.assertIs(self.app.platforms["zhs"].client, client)

    def test_browser_login_runs_in_background_and_reports_failure(self):
        self.assertEqual(self.app.login_start("mooc")["status"], "waiting")
        for _ in range(50):
            if self.app.platforms["mooc"].login["status"] != "waiting":
                break
            time.sleep(0.02)
        result = self.app.login_status("mooc")
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(self.app.platforms["mooc"].login)


if __name__ == "__main__":
    unittest.main()
