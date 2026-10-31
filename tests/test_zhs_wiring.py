"""智慧树扫码登录与界面接线（离线、不打开浏览器）。"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlparse

import requests
from rich.console import Console

import theme
from app import Application
from base.auth import PlatformUnavailable
from course_flow import CourseWorkspace
from settings import SettingsStore
from ui import Cancelled, TerminalUI
from ZHSMain.api import ZHSApi
from ZHSMain.login import qr_login


def make_view(answers=()):
    output = io.StringIO()
    inputs = iter(answers)
    return TerminalUI(console=Console(file=output, theme=theme.THEME, width=120),
                      input_fn=lambda: next(inputs)), output


def page_path(uri):
    return unquote(urlparse(uri).path)


class QrLoginTests(unittest.TestCase):
    def setUp(self):
        self.opened = []
        self.client = Mock()
        self.client.login_qr.return_value = ("qr-token", b"\x89PNG-old")
        self.finished = []
        self.client.finish_qr_login.side_effect = self.finished.append
        self.statuses = []

    def run_login(self, statuses, *, timeout=300, open_page=None, on_status=None):
        self.statuses = list(statuses)

        def status(token):
            return self.statuses.pop(0)

        self.client.login_qr_status.side_effect = status
        return qr_login(self.client,
                        open_page=open_page or (lambda uri: self.opened.append(uri)),
                        sleep=lambda _: None, timeout=timeout, on_status=on_status)

    def test_scanned_and_confirmed_returns_true_and_cleans_up(self):
        messages = []
        read = []
        statuses = [{"status": -1}, {"status": 0}, {"status": 0},
                    {"status": 1, "oncePassword": "once"}]

        def page(uri):
            self.opened.append(uri)
            path = Path(page_path(uri))
            read.append(path.read_text(encoding="utf-8"))
            self.path = path

        self.assertTrue(self.run_login(statuses, open_page=page, on_status=messages.append))
        self.assertEqual(self.finished, ["once"])
        self.assertEqual(messages, ["已扫码，请在手机上确认登录"])
        self.assertEqual(len(self.opened), 1)
        self.assertTrue(self.opened[0].startswith("file://"))
        self.assertIn("智慧树扫码登录", read[0])
        self.assertNotIn("qr-token", read[0])
        self.assertNotIn("once", read[0])
        final = self.path.read_text(encoding="utf-8")
        self.assertNotIn("data:image", final)
        self.assertNotIn("refresh", final)
        self.assertIn("登录成功", final)

    def test_expired_qr_requests_a_new_code_and_rewrites_the_page(self):
        self.client.login_qr.side_effect = [("qr-token", b"old-image"), ("qr-new", b"new-image")]
        path = []

        def status(token):
            if len(path) == 0:
                path.append(page_path(self.opened[0]))
                return {"status": 2}
            self.assertIn("bmV3LWltYWdl", Path(path[0]).read_text(encoding="utf-8"))
            return {"status": 1, "oncePassword": "once"}

        self.client.login_qr_status.side_effect = status
        self.assertTrue(qr_login(self.client, open_page=lambda uri: self.opened.append(uri),
                                 sleep=lambda _: None, timeout=10))
        self.assertEqual(self.client.login_qr.call_count, 2)
        self.assertNotIn("data:image", Path(path[0]).read_text(encoding="utf-8"))

    def test_cancelled_on_phone_returns_false(self):
        self.assertFalse(self.run_login([{"status": 3}]))

    def test_timeout_returns_false(self):
        self.assertFalse(self.run_login([{"status": -1}] * 4, timeout=4))
        self.assertEqual(self.client.login_qr_status.call_count, 4)

    def test_network_blip_is_ignored(self):
        calls = []

        def status(token):
            calls.append(token)
            if len(calls) == 1:
                raise PlatformUnavailable("网络抖动")
            return {"status": 1, "oncePassword": "once"}

        self.client.login_qr_status.side_effect = status
        self.assertTrue(qr_login(self.client, open_page=lambda uri: self.opened.append(uri),
                                 sleep=lambda _: None, timeout=5))

    def test_confirmed_login_saves_exam_gateway_cookie(self):
        client = ZHSApi()
        self.addCleanup(client.close)
        client.login_qr = Mock(return_value=("qr", b"image"))
        client.login_qr_status = Mock(return_value={"status": 1, "oncePassword": "once"})

        def request(method, url, **kwargs):
            if url.endswith("/cas/login"):
                return Mock(status_code=302, headers={"Location": "https://studentexam-api.zhihuishu.com/landing?ticket=test"})
            if "studentexam-api.zhihuishu.com/landing" in url:
                client.session.cookies.set("o_session_id", "ticket", domain=".zhihuishu.com", path="/")
            else:
                client.session.cookies.set("CASLOGC", "login", domain=".zhihuishu.com", path="/")
            return Mock(status_code=200)

        client.session.request = request
        self.assertTrue(qr_login(client, open_page=lambda _: None, sleep=lambda _: None, timeout=1))
        restored = ZHSApi(access_token=client.access_token)
        self.addCleanup(restored.close)
        prepared = restored.session.prepare_request(requests.Request(
            "GET", "https://studentexam-api.zhihuishu.com/"))
        self.assertIn("o_session_id=ticket", prepared.headers.get("Cookie", ""))

    def test_exam_sso_unavailable_does_not_block_course_login(self):
        self.client.shared_exam_sso.side_effect = PlatformUnavailable("offline")
        messages = []
        self.assertTrue(self.run_login([{"status": 1, "oncePassword": "once"}], on_status=messages.append))
        self.assertTrue(any("考试" in message for message in messages))

    def test_qr_is_cleared_after_failure(self):
        self.assertFalse(self.run_login([{"status": 3}]))
        final = Path(page_path(self.opened[0])).read_text(encoding="utf-8")
        self.assertNotIn("data:image", final)
        self.assertIn("回到终端", final)


class ZhsApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SettingsStore(Path(self.temp.name) / "local")
        self.view, self.output = make_view()
        self.app = Application(self.view, self.store)
        self.courses = [{"courseName": "共享课", "zhs": {"kind": "shared"}}]

    def test_saved_zhs_login_is_reused(self):
        self.store.save_session("zhs", Mock(access_token='[{"name": "CASLOGC", "value": "x"}]'))
        client = Mock(access_token="cookies")
        with patch("ZHSMain.api.ZHSApi.my_course_list", return_value={"rows": self.courses}) as listed, \
             patch("app.qr_login") as qr:
            result, rows = self.app.client_and_courses(ZHSApi, choose=False)
        qr.assert_not_called()
        listed.assert_called_once()
        self.assertEqual(rows, self.courses)

    def test_missing_zhs_login_uses_qr_and_saves_session(self):
        cookies = json.dumps([{"name": "CASLOGC", "value": "x"}])
        with patch.object(self.app, "zhs_login", return_value=("", "", cookies)) as zhs, \
             patch.object(self.app, "login") as browser_login, \
             patch("ZHSMain.api.ZHSApi.my_course_list", return_value={"rows": self.courses}):
            client, rows = self.app.client_and_courses(ZHSApi, choose=False)
        zhs.assert_called_once()
        browser_login.assert_not_called()
        self.assertEqual(rows, self.courses)
        self.assertTrue(self.store.session("zhs")["access_token"])
        client.close()

    def test_zhs_login_uses_the_qr_flow_and_returns_the_cookie(self):
        client = Mock(access_token="cookies")
        with patch("app.ZHSApi", return_value=client), \
             patch("app.qr_login", return_value=True) as qr:
            self.assertEqual(self.app.zhs_login(), ("", "", "cookies"))
        qr.assert_called_once()
        self.assertIs(qr.call_args[0][0], client)

    def test_zhs_login_failure_closes_the_client_and_can_retry(self):
        client = Mock(access_token="cookies")
        with patch("app.ZHSApi", return_value=client), \
             patch("app.qr_login", return_value=False), \
             patch.object(self.view, "confirm", return_value=False):
            with self.assertRaises(Cancelled):
                self.app.zhs_login()
        client.close.assert_called_once()

    def test_study_version_two_uses_the_zhs_handler_without_ai(self):
        handler = Mock()
        client = Mock(access_token="cookies")
        with patch("app.ZHSHandler", handler) as cls, \
             patch.object(self.app, "verify_ai") as verify:
            self.app.study(2, client, self.courses)
        cls.assert_called_once()
        kwargs = cls.call_args.kwargs
        self.assertIs(kwargs["client"], client)
        self.assertEqual(kwargs["course_rows"], self.courses)
        self.assertEqual(kwargs["on_progress"], self.view.on_progress)
        self.assertEqual(kwargs["max_workers"], self.store.concurrency()["study"])
        verify.assert_not_called()

    def test_settings_option_five_clears_zhs_without_browser_logout(self):
        self.store.save_session("zhs", Mock(access_token="cookies"))
        self.view.input_fn = Mock(side_effect=["s", "5", "0"])
        self.assertFalse(self.app.manage_settings(current_platform="mooc"))
        self.assertIsNone(self.store.session("zhs"))
        self.assertFalse(self.app.browser_logout_pending)

    def test_logging_out_current_zhs_platform_restarts_login(self):
        self.store.save_session("zhs", Mock(access_token="cookies"))
        self.view.input_fn = Mock(side_effect=["s", "5"])
        self.assertTrue(self.app.manage_settings(current_platform="zhs"))
        self.assertFalse(self.app.browser_logout_pending)


class WiringTests(unittest.TestCase):
    def test_choose_platform_offers_zhihuishu(self):
        view, _ = make_view(["2"])
        self.assertEqual(view.choose_platform(0), 2)

    def test_settings_menu_offers_zhs_logout(self):
        view, output = make_view(["0"])
        view.settings_menu("path")
        self.assertIn("退出智慧树登录", output.getvalue())

    def test_course_workspace_platform_two_uses_zhs_api(self):
        app = Mock()
        app.view = Mock()
        workspace = CourseWorkspace(app)
        workspace.platform = 2
        with patch.object(app, "client_and_courses", side_effect=Cancelled) as load:
            with self.assertRaises(Cancelled):
                workspace.run()
        self.assertIs(load.call_args[0][0], ZHSApi)


if __name__ == "__main__":
    unittest.main()
