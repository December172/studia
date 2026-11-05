"""The server touches the Zhihuishu sign-in centre for everyone signed in, and logs whether its ticket holds."""
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

import requests

from base.auth import PlatformUnavailable
from settings import SettingsStore
from web.server import Studia, keep_passports
from ZHSMain.api import ZHSApi


class PassportAliveTest(unittest.TestCase):
    def client(self, status, location=""):
        api = ZHSApi()
        api.session.request = Mock(return_value=Mock(status_code=status, headers={"Location": location}))
        return api

    def test_a_ticket_handed_back_means_it_holds(self):
        self.assertTrue(self.client(302, "https://x/?ticket=ST-1").passport_alive())
        self.assertFalse(self.client(200).passport_alive())   # the sign-in page
        self.assertFalse(self.client(302, "https://passport.zhihuishu.com/login").passport_alive())
        self.assertFalse(self.client(403).passport_alive())

    def test_no_connection_is_not_an_answer(self):
        api = ZHSApi()
        api.session.request = Mock(side_effect=requests.ConnectionError())
        with self.assertRaises(PlatformUnavailable):
            api.passport_alive()


class KeepPassportTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SettingsStore(directory.name)
        self.app = Studia(self.store, oauth=Mock(return_value=None))
        self.platform = self.app.platforms["zhs"]

    def signed_in(self, alive):
        self.platform.client = Mock(access_token="cookies", passport_alive=Mock(**alive))
        self.store.save_session("zhs", self.platform.client)
        self.store.set("zhs_signed_in_at", time.time() - 3 * 3600)

    def test_logs_whether_the_ticket_holds_with_hours_since_the_scan(self):
        self.signed_in({"return_value": True})
        with self.assertLogs("web.server", "INFO") as logs:
            self.assertTrue(self.app.keep_passport())
        self.assertIn("票据有效", logs.output[0])
        self.assertIn("扫码后 3.0 小时", logs.output[0])
        self.signed_in({"return_value": False})
        with self.assertLogs("web.server", "WARNING") as logs:
            self.assertFalse(self.app.keep_passport())
        self.assertIn("问答区要重新扫码", logs.output[0])

    def test_nothing_to_do_without_a_login_and_a_blip_is_not_an_expiry(self):
        self.assertIsNone(self.app.keep_passport())
        self.signed_in({"side_effect": PlatformUnavailable("连不上")})
        self.assertIsNone(self.app.keep_passport())
        self.assertIsNotNone(self.store.session("zhs"))  # never signs anyone out


class BoardCheckTest(KeepPassportTest):
    def test_the_board_check_is_logged_next_to_the_ticket(self):
        from unittest.mock import patch
        from base.auth import AuthenticationExpired
        self.signed_in({"return_value": True})
        self.platform.client.shared_courses.return_value = [{"courseId": 1, "recruitId": 7}]
        with patch("web.server.QaBridge") as bridge, self.assertLogs("web.server", "INFO") as logs:
            self.app.keep_passport(board=True)
        self.assertIn("问答区能进（登录中心票据有效）", logs.output[-1])
        self.assertEqual(bridge.call_args.kwargs, {"course_id": 1, "recruit_id": 7})
        self.signed_in({"return_value": False})
        self.platform.client.shared_courses.return_value = [{"courseId": 1, "recruitId": 7}]
        with patch("web.server.QaBridge") as bridge, self.assertLogs("web.server", "INFO") as logs:
            bridge.return_value.__enter__.side_effect = AuthenticationExpired("登录页")
            self.app.keep_passport(board=True)
        self.assertIn("问答区进不去（登录中心票据已失效）", logs.output[-1])


class LoopTest(unittest.TestCase):
    def test_everyone_is_touched_and_one_error_does_not_stop_the_rest(self):
        stop = threading.Event()
        broken, fine = Mock(), Mock()
        broken.keep_passport.side_effect = RuntimeError("boom")
        fine.keep_passport.side_effect = lambda **_: stop.set()
        thread = threading.Thread(target=keep_passports, args=(lambda: [broken, fine],),
                                  kwargs={"interval": 0.01, "first": 0, "stop": stop}, daemon=True)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        broken.keep_passport.assert_called()
        fine.keep_passport.assert_called()
        self.assertEqual(fine.keep_passport.call_args_list[0].kwargs, {"board": True})  # the first round opens the board


if __name__ == "__main__":
    unittest.main()
