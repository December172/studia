import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rich.console import Console

import theme
from ai.client import AIConfig
from app import Application
from base.auth import AuthenticationExpired
from ChaoxingMain.api import ChaoxingError, LoginError, WorkUnavailable
from settings import SettingsError, SettingsStore
from ui import TerminalUI
from web.server import HttpError, PLATFORMS, Studia

CX_COURSE = {"id": "course_1", "courseId": "111", "clazzId": "222", "cpi": "333",
             "title": "高等数学", "teacher": "张老师"}
CX_WORK = {"type": "workid", "jobid": "work-abc", "name": "1.1 章节测验", "enc": "e"}


def make_view(answers=()):
    output = io.StringIO()
    inputs = iter(answers)
    return TerminalUI(console=Console(file=output, width=100, theme=theme.THEME),
                      input_fn=lambda: next(inputs)), output


class FakeChaoxingApi:
    """Stands in for ChaoxingApi: login and the course list are the only calls the web makes here."""

    instances = []

    def __init__(self, username=None, password=None, cookies=None):
        self.username, self.password, self.cookies = username, password, cookies
        self.session = Mock()
        self.session.cookies.get_dict.return_value = {"UID": "1", "fid": "2"}
        self.closed = False
        self.rows = [dict(CX_COURSE)]
        self.fail = None
        FakeChaoxingApi.instances.append(self)

    def course_list(self):
        if self.fail is not None:
            raise self.fail
        return [dict(row) for row in self.rows]

    def close(self):
        self.closed = True


def make_app(store):
    with patch.dict(PLATFORMS, {"cx": dict(PLATFORMS["cx"], api=FakeChaoxingApi)}):
        return Studia(store, oauth=Mock(return_value=None))


class CookieSessionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SettingsStore(Path(temp.name) / "local")

    def test_cookie_login_is_saved_and_restored(self):
        client = Mock(access_token=None)
        client.session.cookies.get_dict.return_value = {"UID": "1"}
        self.store.save_session("cx", client)
        self.assertEqual(SettingsStore(self.store.directory).session("cx"), {"cookies": {"UID": "1"}})

    def test_empty_cookie_jar_is_not_a_login(self):
        client = Mock(access_token=None)
        client.session.cookies.get_dict.return_value = {}
        with self.assertRaises(SettingsError):
            self.store.save_session("cx", client)
        self.assertIsNone(self.store.session("cx"))


class WebLoginTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SettingsStore(Path(temp.name) / "local")
        FakeChaoxingApi.instances.clear()

    def test_password_login_keeps_only_the_cookie(self):
        with patch.dict(PLATFORMS, {"cx": dict(PLATFORMS["cx"], api=FakeChaoxingApi)}):
            app = Studia(self.store, oauth=Mock(return_value=None))
            result = app.login_start("cx", {"username": "13800000000", "password": "secret"})
            self.assertEqual(result["status"], "done")
            platform = app.platforms["cx"]
            self.assertIsInstance(platform.client, FakeChaoxingApi)
            self.assertEqual(platform.client.username, "13800000000")
            self.assertTrue(app.logged_in(platform))
            # 账号密码本身不落盘，只保存平台下发的 Cookie。
            self.assertEqual(self.store.session("cx"), {"cookies": {"UID": "1", "fid": "2"}})
            courses = app.courses("cx")["courses"]
            self.assertEqual(courses[0]["name"], "高等数学")
            self.assertEqual(courses[0]["id"], "111-222")
            self.assertEqual(courses[0]["type"], "张老师")
            self.assertTrue(courses[0]["features"]["study"])
            self.assertTrue(courses[0]["features"]["answer"])

    def test_missing_credentials_are_refused(self):
        app = make_app(self.store)
        with self.assertRaises(HttpError) as caught:
            app.login_start("cx", {"username": "", "password": ""})
        self.assertEqual(caught.exception.status, 400)

    def test_wrong_password_is_reported(self):
        class WrongPassword(FakeChaoxingApi):
            def __init__(self, *args, **kwargs):
                raise LoginError("超星登录失败：账号或密码不正确")

        with patch.dict(PLATFORMS, {"cx": dict(PLATFORMS["cx"], api=WrongPassword)}):
            app = Studia(self.store, oauth=Mock(return_value=None))
            with self.assertRaises(HttpError) as caught:
                app.login_start("cx", {"username": "u", "password": "p"})
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("账号或密码", str(caught.exception))

    def test_course_read_failure_closes_the_new_client(self):
        class BrokenList(FakeChaoxingApi):
            def course_list(self):
                raise ChaoxingError("超星平台返回 HTTP 500")

        with patch.dict(PLATFORMS, {"cx": dict(PLATFORMS["cx"], api=BrokenList)}):
            app = Studia(self.store, oauth=Mock(return_value=None))
            with self.assertRaises(HttpError) as caught:
                app.login_start("cx", {"username": "u", "password": "p"})
        self.assertEqual(caught.exception.status, 502)
        self.assertTrue(BrokenList.instances[-1].closed)

    def test_saved_cookies_restore_without_a_password(self):
        self.store.set("session_cx", {"cookies": {"UID": "7"}})
        with patch.dict(PLATFORMS, {"cx": dict(PLATFORMS["cx"], api=FakeChaoxingApi)}):
            app = Studia(self.store, oauth=Mock(return_value=None))
            platform = app.platforms["cx"]
            self.assertTrue(app.logged_in(platform))
            client = app.client(platform)
        self.assertEqual(client.cookies, {"UID": "7"})


class WebTaskTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SettingsStore(Path(temp.name) / "local")
        self.app = make_app(self.store)
        self.platform = self.app.platforms["cx"]
        self.platform.client = Mock()
        self.platform.rows = [dict(CX_COURSE)]
        self.item = dict(CX_WORK, id="work-abc", _point={"id": "123"},
                         _job_info={"knowledgeid": "123"}, _completion_status="unknown",
                         _hint="平台不提供这份作业的提交状态")

    def test_tasks_use_the_work_job_id_and_stay_available(self):
        with patch("web.server.cx_catalog", return_value=[(dict(CX_COURSE), self.item, 1)]):
            tasks = self.app.tasks("cx", "111-222")["tasks"]
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["id"], "work-abc")
        self.assertEqual(task["name"], "1.1 章节测验")
        self.assertEqual(task["category"], "作业")
        self.assertEqual(task["status"], "状态未知")
        self.assertIsNone(task["score"])
        self.assertTrue(task["available"])
        self.assertFalse(task["done"])
        self.assertIn("提交状态", task["hint"])

    def test_answer_job_runs_the_chaoxing_runner(self):
        job = {"id": "job1", "cancel": threading.Event()}
        runner = Mock()
        runner.run.return_value = {"paper": "1.1 章节测验", "status": "submitted", "message": "已交卷"}
        ai = Mock(config=AIConfig("https://example.org/v1", "model", "key"))
        with patch("web.server.ChaoxingExerciseRunner", return_value=runner) as factory, \
                patch.object(Studia, "_ai", return_value=ai):
            summary = self.app._answer(self.platform, [(dict(CX_COURSE), self.item, 1)], True, job)
        factory.assert_called_once()
        runner.run.assert_called_once_with(CX_COURSE, self.item["_point"], self.item, self.item["_job_info"])
        self.assertIn("1/1", summary)
        self.assertIn("份作业", summary)
        ai.session.close.assert_called_once()

    def test_unavailable_work_is_skipped_and_the_queue_continues(self):
        other = dict(self.item, id="work-def", jobid="work-def")
        runner = Mock()
        runner.run.side_effect = [WorkUnavailable("教师未创建完成该测验"),
                                  {"paper": "1.2", "status": "submitted", "message": "已交卷"}]
        ai = Mock(config=AIConfig("https://example.org/v1", "model", "key"))
        with patch("web.server.ChaoxingExerciseRunner", return_value=runner), \
                patch.object(Studia, "_ai", return_value=ai):
            summary = self.app._answer(self.platform, [(dict(CX_COURSE), self.item, 1),
                                                       (dict(CX_COURSE), other, 1)], True,
                                       {"id": "job1", "cancel": threading.Event()})
        self.assertEqual(runner.run.call_count, 2)
        self.assertIn("1/2", summary)

    def test_platform_error_stops_only_that_work(self):
        other = dict(self.item, id="work-def", jobid="work-def")
        runner = Mock()
        runner.run.side_effect = [ChaoxingError("超星平台返回 HTTP 500"),
                                  {"paper": "1.2", "status": "saved", "message": "已保存"}]
        ai = Mock(config=AIConfig("https://example.org/v1", "model", "key"))
        with patch("web.server.ChaoxingExerciseRunner", return_value=runner), \
                patch.object(Studia, "_ai", return_value=ai):
            summary = self.app._answer(self.platform, [(dict(CX_COURSE), self.item, 1),
                                                       (dict(CX_COURSE), other, 1)], True,
                                       {"id": "job1", "cancel": threading.Event()})
        self.assertIn("1/2", summary)

    def test_study_job_uses_the_learning_handler(self):
        self.platform.client = Mock()
        job = {"id": "job1", "cancel": threading.Event(), "course": "111-222"}
        handler = Mock(blocked=None)
        factory = Mock(return_value=handler)
        with patch.dict(self.platform.spec, {"handler": factory}):
            summary = self.app._study(self.platform, dict(CX_COURSE), 0, job)
        factory.assert_called_once()
        kwargs = factory.call_args.kwargs
        self.assertEqual(kwargs["course_rows"], [dict(CX_COURSE)])
        self.assertIs(kwargs["client"], self.platform.client)
        self.assertEqual(kwargs["max_workers"], self.app.store.concurrency()["study"])
        self.assertEqual(summary, "学习完成")


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


class FakeQrClient:
    status = {"status": 1}
    instances = []

    def __init__(self):
        self.session = Mock()
        self.session.cookies.get_dict.return_value = {"UID": "1"}
        self.closed = False
        self.finished = False
        FakeQrClient.instances.append(self)

    def login_qr(self):
        return ({"uuid": "u1", "enc": "e1"}, PNG)

    def login_qr_status(self, token):
        return dict(self.status)

    def refresh_login_qr(self, token):
        return ({"uuid": "u2", "enc": "e2"}, PNG)

    def finish_qr_login(self, result=None):
        self.finished = True

    def close(self):
        self.closed = True


class WebQrLoginTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SettingsStore(Path(temp.name) / "local")
        FakeQrClient.instances.clear()

    def start(self, api=FakeQrClient):
        with patch.dict(PLATFORMS, {"cx": dict(PLATFORMS["cx"], api=api)}):
            app = Studia(self.store, oauth=Mock(return_value=None))
            started = app.login_start("cx")
        return app, started

    def test_qr_start_returns_an_image_and_confirm_saves_the_session(self):
        app, started = self.start()
        self.assertTrue(started["qr"].startswith("data:image/png;base64,"))
        self.assertTrue(started["id"])
        result = app.login_status("cx", started["id"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.store.session("cx"), {"cookies": {"UID": "1"}})
        self.assertTrue(FakeQrClient.instances[-1].finished)
        self.assertTrue(app.logged_in(app.platforms["cx"]))

    def test_expired_code_is_refreshed_without_restarting_the_scan(self):
        class Expiring(FakeQrClient):
            def login_qr_status(self, token):
                return {"status": 2}

        app, started = self.start(Expiring)
        result = app.login_status("cx", started["id"])
        self.assertEqual(result["status"], "refreshed")
        self.assertIn("qr", result)
        self.assertEqual(app.platforms["cx"].qr[started["id"]]["qr"], {"uuid": "u2", "enc": "e2"})

    def test_cancel_message_reaches_the_page(self):
        class Cancelled(FakeQrClient):
            def login_qr_status(self, token):
                return {"status": 3, "message": "这个账号需要手机二次验证"}

        app, started = self.start(Cancelled)
        result = app.login_status("cx", started["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("二次验证", result["message"])

    def test_password_form_still_works_on_a_qr_platform(self):
        app, _ = self.start()
        with patch.dict(PLATFORMS, {"cx": dict(PLATFORMS["cx"], api=FakeChaoxingApi)}):
            app.platforms["cx"].spec.update(api=FakeChaoxingApi)
            result = app.login_start("cx", {"username": "13800000000", "password": "secret"})
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.store.session("cx"), {"cookies": {"UID": "1", "fid": "2"}})


class TerminalTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SettingsStore(Path(temp.name) / "local")
        self.item = dict(CX_WORK, id="work-abc", _point={"id": "123"}, _job_info={"knowledgeid": "123"})

    def test_platform_chooser_and_course_menu_expose_chaoxing(self):
        view, output = make_view(["3"])
        self.assertEqual(view.choose_platform(0), 3)
        self.assertIn("超星", output.getvalue())

        view, output = make_view(["1"])
        self.assertEqual(view.course_menu([CX_COURSE], 3), 1)
        self.assertIn("自动学习", output.getvalue())
        self.assertIn("AI 答题", output.getvalue())

    def test_saved_cookie_login_is_reused(self):
        view, _ = make_view([])
        app = Application(view, self.store)
        self.store.set("session_cx", {"cookies": {"UID": "1"}})
        with patch("app.ChaoxingApi") as api:
            api.return_value.course_list.return_value = [dict(CX_COURSE)]
            client, rows = app.cx_client_and_courses(choose=False)
        api.assert_called_once_with(cookies={"UID": "1"})
        self.assertIs(client, api.return_value)
        self.assertEqual(rows, [CX_COURSE])

    def test_qr_login_is_the_default(self):
        view, output = make_view(["1"])
        app = Application(view, self.store)
        fresh = Mock()
        fresh.course_list.return_value = [dict(CX_COURSE)]
        fresh.session.cookies.get_dict.return_value = {"UID": "9"}
        with patch("app.ChaoxingApi", return_value=fresh) as api, \
                patch("app.cx_qr_login", return_value=True) as qr:
            client, rows = app.cx_client_and_courses(choose=False)
        api.assert_called_once_with()
        qr.assert_called_once()
        self.assertIs(client, fresh)
        self.assertEqual(rows, [CX_COURSE])
        self.assertEqual(self.store.session("cx"), {"cookies": {"UID": "9"}})
        self.assertIn("扫码", output.getvalue())

    def test_expired_cookie_login_asks_for_the_password_again(self):
        view, _ = make_view(["2", "13800000000"])
        view.secret_fn = lambda _: "secret"
        app = Application(view, self.store)
        self.store.set("session_cx", {"cookies": {"UID": "1"}})
        expired = Mock()
        expired.course_list.side_effect = AuthenticationExpired("超星登录已过期")
        fresh = Mock()
        fresh.course_list.return_value = [dict(CX_COURSE)]
        fresh.session.cookies.get_dict.return_value = {"UID": "2"}
        with patch("app.ChaoxingApi", side_effect=[expired, fresh]) as api:
            client, rows = app.cx_client_and_courses(choose=False)
        self.assertEqual(api.call_args_list[-1].kwargs, {})
        self.assertEqual(api.call_args_list[-1].args, ("13800000000", "secret"))
        self.assertIs(client, fresh)
        self.assertEqual(rows, [CX_COURSE])
        self.assertEqual(self.store.session("cx"), {"cookies": {"UID": "2"}})

    def test_catalog_reads_each_course_through_the_chaoxing_catalog(self):
        view, _ = make_view([])
        app = Application(view, self.store)
        client = Mock()
        with patch("app.cx_catalog", return_value=[(CX_COURSE, self.item, 1)]) as catalog:
            found = app.cx_task_catalog(client, [CX_COURSE])
        self.assertEqual(found, [(CX_COURSE, self.item, 1)])
        catalog.assert_called_once_with(client, CX_COURSE)

    def test_execute_answers_each_selected_work_and_closes_the_ai_client(self):
        view, _ = make_view([])
        app = Application(view, self.store)
        client = Mock()
        runner = Mock()
        runner.run.return_value = {"paper": "1.1 章节测验", "status": "saved", "message": "已保存"}
        ai = Mock()
        with patch("app.ChaoxingExerciseRunner", return_value=runner), \
                patch.object(Application, "verify_ai", return_value=ai):
            results = app.execute_cx_ai(client, [(CX_COURSE, self.item, 1)], Mock(), True)
        runner.run.assert_called_once_with(CX_COURSE, self.item["_point"], self.item, self.item["_job_info"])
        self.assertEqual(results[0]["status"], "saved")
        ai.session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
