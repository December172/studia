"""A 401/403 from one call only deletes the saved login once the platform confirms it is gone."""
import queue
import tempfile
import time
import unittest
from unittest.mock import Mock

import requests

from base.auth import AuthenticationExpired, PlatformUnavailable
from settings import SettingsStore
from web.server import HttpError, Studia, drain
from ZHSMain.api import ZHSApi


class ExpireTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SettingsStore(directory.name)
        self.app = Studia(self.store, oauth=Mock(return_value=None))
        self.platform = self.app.platforms["zhs"]
        self.platform.client = Mock(access_token="cookie")
        self.store.save_session("zhs", self.platform.client)

    def expire(self, **probe):
        self.platform.client.signed_in = Mock(**probe)
        return self.app.expire(self.platform)

    def test_a_login_the_platform_still_accepts_is_kept(self):
        error = self.expire(return_value=True)
        self.assertEqual(error.status, 503)
        self.assertIsNotNone(self.store.session("zhs"))
        self.assertIsNotNone(self.platform.client)

    def test_an_unreachable_check_keeps_the_login(self):
        self.expire(side_effect=PlatformUnavailable("连不上"))
        self.assertIsNotNone(self.store.session("zhs"))

    def test_a_confirmed_expiry_deletes_it(self):
        error = self.expire(return_value=False)
        self.assertEqual(error.status, 401)
        self.assertIsNone(self.store.session("zhs"))
        self.assertIsNone(self.platform.client)

    def test_other_tabs_hear_about_sign_in_changes(self):
        tab = queue.Queue()
        self.app.subscribers.add(tab)
        self.expire(return_value=True)
        self.assertTrue(tab.empty())
        self.app.logout("zhs")
        self.assertEqual(tab.get_nowait(), {"type": "account"})

    def test_mooc_asks_for_the_account(self):
        platform = self.app.platforms["mooc"]
        client = platform.client = Mock(spec=["get_info", "access_token", "close"], access_token="t")
        self.store.save_session("mooc", client)
        client.get_info.side_effect = AuthenticationExpired("过期")
        self.assertIsInstance(self.app.expire(platform), HttpError)
        self.assertIsNone(self.store.session("mooc"))
        client.get_info.assert_called_once_with(strict=True)

    def test_a_job_refused_once_waits_without_signing_out(self):
        self.platform.client.signed_in = Mock(return_value=True)
        self.platform.rows = [{"courseName": "网络课", "zhs": {"kind": "ai", "courseId": "c1"}}]

        def refused(**_):
            raise AuthenticationExpired("403")
        self.platform.spec = dict(self.platform.spec, handler=refused)
        self.app.start_job({"platform": "zhs", "type": "study", "course": "ai-c1"})
        for _ in range(50):
            if self.app.history:
                break
            time.sleep(0.1)
        self.assertEqual(self.app.history[0]["state"], "waiting")
        self.assertIn("暂时拒绝", self.app.history[0]["summary"])
        self.assertIsNotNone(self.store.session("zhs"))
        drain(5)


class SignedInTest(unittest.TestCase):
    def test_asks_a_business_api_not_the_passport(self):
        api = ZHSApi()
        api.ai_courses = Mock(return_value=[])
        self.assertTrue(api.signed_in())
        api.ai_courses.side_effect = AuthenticationExpired("401")
        self.assertFalse(api.signed_in())

    def test_network_failure_is_not_an_answer(self):
        api = ZHSApi()
        api.session.request = Mock(side_effect=requests.ConnectionError())
        with self.assertRaises(PlatformUnavailable):
            api.signed_in()


def setUpModule():
    # Task tests have a daytime clock; curfew boundaries live in test_quiet_hours.py.
    daytime = unittest.mock.patch("web.server.quiet_hours", return_value={"active": False})
    daytime.start()
    unittest.addModuleCleanup(daytime.stop)


if __name__ == "__main__":
    unittest.main()
