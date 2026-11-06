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

    def signed_in(self, alive, hours=3):
        self.platform.client = Mock(access_token="cookies", passport_alive=Mock(**alive), signed_in=Mock(return_value=True))
        self.store.save_session("zhs", self.platform.client)
        self.store.set("zhs_signed_in_at", time.time() - hours * 3600)

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


class RescanTest(KeepPassportTest):
    """A lapsed ticket shows on the page as a request to scan again, until a scan or sign-out clears it."""
    def rescan(self):
        return next(p for p in self.app.state()["platforms"] if p["id"] == "zhs")["rescan"]

    def test_the_page_asks_for_a_scan_while_the_ticket_is_gone_and_hears_each_change_once(self):
        self.app.publish = Mock()
        self.signed_in({"return_value": True})
        self.app.keep_passport()
        self.assertFalse(self.rescan())
        self.app.publish.assert_not_called()
        self.signed_in({"return_value": False})
        self.app.keep_passport()
        self.app.keep_passport()
        self.assertTrue(self.rescan())
        self.assertEqual(self.app.publish.call_count, 1)
        self.signed_in({"return_value": True})
        self.app.keep_passport()
        self.assertFalse(self.rescan())
        self.assertEqual(self.app.publish.call_count, 2)
        self.assertFalse(next(p for p in self.app.state()["platforms"] if p["id"] == "cx")["rescan"])

    def test_a_scan_or_signing_out_clears_it(self):
        self.signed_in({"return_value": False})
        self.app.keep_passport()
        self.app.logout("zhs")
        self.assertFalse(self.rescan())
        self.assertIsNone(self.store.get("zhs_passport_lapsed"))
        self.signed_in({"return_value": False})
        self.app.keep_passport()
        scanner = Mock(access_token="new", login_qr_status=Mock(return_value={"status": 1, "oncePassword": "x"}))
        self.platform.qr["q"] = {"client": scanner, "qr": "t", "refreshes": 0}
        self.assertEqual(self.app.login_status("zhs", "q")["status"], "done")
        self.assertFalse(self.rescan())

    def test_a_refused_board_says_whether_a_scan_will_help(self):
        self.signed_in({"return_value": False})
        self.assertIn("重新扫码后问答区才能进", self.app.board_refused(self.platform))
        self.assertTrue(self.rescan())
        self.signed_in({"return_value": True})
        self.assertIn("稍后重试", self.app.board_refused(self.platform))
        self.signed_in({"side_effect": PlatformUnavailable("连不上")})
        self.assertIn("稍后重试", self.app.board_refused(self.platform))


class EightHoursTest(KeepPassportTest):
    """The ticket lives eight hours from the scan however often it is touched."""
    def zhs(self):
        return next(p for p in self.app.state()["platforms"] if p["id"] == "zhs")

    def test_the_page_hears_when_the_ticket_runs_out_and_asks_for_a_scan_after(self):
        self.signed_in({"return_value": True}, hours=3)
        self.assertAlmostEqual(self.zhs()["rescan_at"], time.time() + 5 * 3600, delta=5)
        self.assertFalse(self.zhs()["rescan"])
        self.signed_in({"return_value": True}, hours=8.1)
        self.assertEqual((self.zhs()["rescan"], self.zhs()["rescan_at"]), (True, None))
        self.store.set("zhs_signed_in_at", None)  # scanned before the server kept the time
        self.assertEqual((self.zhs()["rescan"], self.zhs()["rescan_at"]), (False, None))
        self.assertIsNone(next(p for p in self.app.state()["platforms"] if p["id"] == "cx")["rescan_at"])

    def test_a_ticket_gone_past_eight_hours_is_not_asked_about_again(self):
        self.signed_in({"return_value": False}, hours=8.5)
        with self.assertLogs("web.server", "WARNING"):
            self.assertFalse(self.app.keep_passport())
        self.platform.client.passport_alive.reset_mock()
        self.assertFalse(self.app.keep_passport())
        self.platform.client.passport_alive.assert_not_called()

    def test_one_gone_early_is_asked_again_in_case_it_was_a_blip(self):
        self.signed_in({"return_value": False}, hours=2)
        self.app.keep_passport()
        self.platform.client.passport_alive.return_value = True
        self.assertTrue(self.app.keep_passport())
        self.assertFalse(self.zhs()["rescan"])

    def test_the_business_login_is_touched_and_deleted_once_confirmed_gone(self):
        self.signed_in({"return_value": True})
        self.app.keep_passport()
        self.platform.client.signed_in.assert_called()
        self.platform.client.signed_in.return_value = False
        with self.assertLogs("web.server", "WARNING") as logs:
            self.assertIsNone(self.app.keep_passport())
        self.assertIn("登录已失效", logs.output[-1])
        self.assertIsNone(self.store.session("zhs"))

    def test_scanning_another_platform_leaves_the_clock_alone(self):
        self.signed_in({"return_value": True}, hours=3)
        before = self.store.get("zhs_signed_in_at")
        scanner = Mock(login_qr_status=Mock(return_value={"status": 1, "oncePassword": "x"}), spec_set=[
            "login_qr_status", "finish_qr_login", "close", "cookies", "session"])
        self.app.platforms["cx"].qr["q"] = {"client": scanner, "qr": "t", "refreshes": 0}
        self.app.store.save_session = Mock()
        self.assertEqual(self.app.login_status("cx", "q")["status"], "done")
        self.assertEqual(self.store.get("zhs_signed_in_at"), before)


class LoopTest(unittest.TestCase):
    def test_everyone_is_touched_and_one_error_does_not_stop_the_rest(self):
        stop = threading.Event()
        broken, fine = Mock(), Mock()
        broken.keep_passport.side_effect = RuntimeError("boom")
        fine.keep_passport.side_effect = lambda: stop.set()
        thread = threading.Thread(target=keep_passports, args=(lambda: [broken, fine],),
                                  kwargs={"interval": 0.01, "first": 0, "stop": stop}, daemon=True)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        broken.keep_passport.assert_called()
        fine.keep_passport.assert_called()


def setUpModule():
    # Task tests have a daytime clock; curfew boundaries live in test_quiet_hours.py.
    daytime = unittest.mock.patch("web.server.quiet_hours", return_value={"active": False})
    daytime.start()
    unittest.addModuleCleanup(daytime.stop)


if __name__ == "__main__":
    unittest.main()
