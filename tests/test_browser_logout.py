"""Explicit logout must clear the browser's shared SSO session (issue #9).

Clearing only the local cache leaves the account's token cookie in the shared
default browser, so the next /sso/auth visit silently restores the old account.
The official /loginOut route clears that cookie and forwards to the login page.
"""
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from rich.console import Console

import theme
from NewMoocMain.oauth_login import OAuthLoginHandler, oauth_login
from app import Application
from settings import SettingsStore, SettingsError
from ui import Cancelled, TerminalUI

AUTH = "https://sso.icve.com.cn/sso/auth"
LOGOUT = "https://sso.icve.com.cn/loginOut"
CALLBACK = "http://localhost:45678/login"


def query(url):
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


class LoginUrlTests(unittest.TestCase):
    """The URLs the production code actually opens in the browser."""

    def open_url(self, **kwargs):
        handler = OAuthLoginHandler(port=45678)
        with patch("NewMoocMain.oauth_login.webbrowser.open") as opened:
            handler.open_login_page(**kwargs)
        return opened.call_args[0][0]

    def test_regular_login_keeps_sso_auth_contract(self):
        url = self.open_url()
        self.assertTrue(url.startswith(AUTH + "?"))
        self.assertEqual(query(url), {"mode": "simple", "source": "2", "redirect": CALLBACK})

    def test_forced_logout_uses_official_route_then_returns_to_login(self):
        url = self.open_url(force_logout=True)
        self.assertTrue(url.startswith(LOGOUT + "?"))
        self.assertEqual(query(url), {"source": "2", "redirect": CALLBACK})

    def test_official_route_redirects_straight_to_the_local_callback(self):
        """官方 /loginOut 用 ?|& 正则取首个 redirect/source，未编码的值必须只有一个。"""
        url = self.open_url(force_logout=True)
        stripped = url.split("?", 1)[1]
        match = re.search(r"[?|&]redirect=([^&;]+?)(&|#|;|$)", stripped)
        self.assertIsNotNone(match)
        # 官方路由拼出的下一个地址，取到的 redirect 必须仍是本地回调。
        next_url = "/sso/auth?source=%s&redirect=%s" % (query(url)["source"], match.group(1))
        self.assertEqual(parse_qs(urlparse(next_url).query)["redirect"], [CALLBACK])
        self.assertEqual(next_url.count("redirect="), 1)
        self.assertEqual(next_url.count("source="), 1)

    def test_convenience_entry_forwards_force_logout_to_the_browser(self):
        with patch("NewMoocMain.oauth_login.webbrowser.open") as opened, \
             patch.object(OAuthLoginHandler, "wait_for_token", return_value="synthetic"):
            oauth_login(timeout=1, force_logout=True)
        self.assertTrue(opened.call_args[0][0].startswith(LOGOUT + "?"))


class BrowserLogoutTests(unittest.TestCase):
    """Logout intent survives failure/retry and is consumed only by a real login."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SettingsStore(Path(self.temp.name) / "local")
        self.output = io.StringIO()
        self.ui = TerminalUI(console=Console(file=self.output, theme=theme.THEME, width=120))
        self.app = Application(self.ui, self.store)

    def login_calls(self):
        calls = []

        def fake(timeout=300, **kwargs):
            calls.append(kwargs)
            return "browser-token"

        return calls, patch("app.oauth_login", side_effect=fake)

    def next_login_forces_logout(self):
        calls, login = self.login_calls()
        with login:
            self.app.login()
        return calls[0].get("force_logout", False)

    def logout(self, action, current_platform="mooc"):
        self.ui.input_fn = Mock(side_effect=[str(action), "0"])
        self.app.manage_settings(current_platform=current_platform)

    def test_logout_then_next_login_clears_browser_sso(self):
        self.store.save_session("mooc", Mock(access_token="stale"))
        client = Mock(access_token="fresh")
        client.my_course_list.return_value = {"rows": []}
        self.ui.input_fn = Mock(side_effect=["s", "3", "q"])
        with patch("app.AIMoocApi", return_value=client), patch("app.oauth_login") as login:
            with self.assertRaises(Cancelled):
                self.app.run()
        login.assert_called_once()
        self.assertTrue(login.call_args.kwargs["force_logout"])

    def test_regular_login_never_touches_browser_sso(self):
        self.store.save_session("mooc", Mock(access_token="stale"))
        self.assertFalse(self.next_login_forces_logout())

    def test_successful_forced_login_consumes_the_intent(self):
        self.store.save_session("mooc", Mock(access_token="stale"))
        self.logout(3)
        self.assertTrue(self.next_login_forces_logout())
        self.assertFalse(self.next_login_forces_logout())

    def test_retry_keeps_logout_intent(self):
        self.store.save_session("mooc", Mock(access_token="stale"))
        self.logout(3)
        attempts = []

        def flaky(timeout=300, **kwargs):
            attempts.append(kwargs.get("force_logout"))
            return None if len(attempts) == 1 else "browser-token"

        with patch("app.oauth_login", side_effect=flaky), \
             patch.object(self.ui, "confirm", return_value=True):
            self.app.login()
        self.assertEqual(attempts, [True, True])

    def test_cancelling_login_keeps_logout_intent(self):
        self.store.save_session("mooc", Mock(access_token="stale"))
        self.logout(3)
        attempts = []

        def failing(timeout=300, **kwargs):
            attempts.append(kwargs.get("force_logout"))
            return None

        with patch("app.oauth_login", side_effect=failing), \
             patch.object(self.ui, "confirm", return_value=False):
            with self.assertRaises(Cancelled):
                self.app.login()
        self.assertEqual(attempts, [True])
        self.assertTrue(self.next_login_forces_logout())

    def test_failed_logout_write_does_not_clear_browser_sso(self):
        self.store.save_session("mooc", Mock(access_token="stale"))
        with patch.object(self.store, "clear_session", side_effect=SettingsError("写入失败")):
            self.logout(3)
        self.assertIsNotNone(self.store.session("mooc"))
        self.assertFalse(self.next_login_forces_logout())

    def test_resource_library_logout_also_clears_browser_sso(self):
        from ai.client import AIConfig
        config = AIConfig("https://example.org/v1", "model", "secret-key")
        self.store.save_ai(config)
        self.store.save_session("mooc", Mock(access_token="mooc"))
        self.store.save_session("zyk", Mock(access_token="zyk"))
        self.logout(4, current_platform="zyk")
        self.assertIsNone(self.store.session("zyk"))
        self.assertIsNotNone(self.store.session("mooc"))
        self.assertEqual(self.store.ai_config(), config)
        self.assertTrue(self.next_login_forces_logout())


if __name__ == "__main__":
    unittest.main()
