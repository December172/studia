import http.client
import json
import logging
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import web.accounts
from web.accounts import Accounts, Throttle
from settings import SettingsError
from web.hosting import COOKIE, EncryptedSettingsStore, HostedAccess, Hub, load_key, main
from web.server import JOB_THREADS, Studia, _EventLogHandler, make_handler


class HostedServerTest(unittest.TestCase):
    def setUp(self):
        fast = patch.object(web.accounts, "ITERATIONS", 1000)
        fast.start()
        self.addCleanup(fast.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), None)
        self.port = self.server.server_address[1]
        self.hub = Hub(directory.name, "http://127.0.0.1:%d" % self.port, key=os.urandom(32), max_jobs=1)
        self.server.RequestHandlerClass = make_handler(HostedAccess(self.hub, self.port))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.admin_id = self.hub.accounts.create_user("admin", "admin-password", role="admin")

    def request(self, method, path, body=None, cookie=None, csrf=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        headers = {"Host": host or "127.0.0.1:%d" % self.port}
        if cookie:
            headers["Cookie"] = "%s=%s" % (COOKIE, cookie)
        if csrf:
            headers["X-Studia-CSRF"] = csrf
        data = json.dumps(body) if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        set_cookie = response.getheader("Set-Cookie") or ""
        token = set_cookie.split(";")[0].split("=", 1)[1] if set_cookie.startswith(COOKIE + "=") else None
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw.decode()
        return response.status, payload, token, set_cookie

    def login(self, username, password):
        status, _, token, _ = self.request("POST", "/api/auth/login", {"username": username, "password": password})
        self.assertEqual(status, 200)
        csrf = self.request("GET", "/api/auth/me", cookie=token)[1]["csrf"]
        return token, csrf

    def test_everything_but_login_needs_a_session(self):
        self.assertEqual(self.request("GET", "/api/mode")[1], {"mode": "hosted"})
        self.assertEqual(self.request("GET", "/api/state")[0], 401)
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.request("GET", "/api/mode", host="evil.example")[0], 403)

    def test_login_cookie_csrf_and_logout(self):
        status, _, token, header = self.request("POST", "/api/auth/login", {"username": "ADMIN", "password": "admin-password"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=Lax", header)
        me = self.request("GET", "/api/auth/me", cookie=token)[1]
        self.assertEqual(me["user"]["role"], "admin")
        self.assertEqual(self.request("POST", "/api/auth/logout", {}, cookie=token)[0], 403)
        self.assertEqual(self.request("POST", "/api/auth/logout", {}, cookie=token, csrf=me["csrf"])[0], 200)
        self.assertEqual(self.request("GET", "/api/state", cookie=token)[0], 401)

    def test_repeated_failures_lock_the_login(self):
        for _ in range(5):
            self.assertEqual(self.request("POST", "/api/auth/login", {"username": "admin", "password": "nope"})[0], 401)
        self.assertEqual(self.request("POST", "/api/auth/login", {"username": "admin", "password": "admin-password"})[0], 429)

    def test_invite_registration_is_single_use(self):
        token, csrf = self.login("admin", "admin-password")
        code = self.request("POST", "/api/admin/invites", {"days": 3}, cookie=token, csrf=csrf)[1]["code"]
        status, payload, new_token, _ = self.request("POST", "/api/auth/register",
                                                     {"invite": code.lower(), "username": "alice", "password": "alice-password"})
        self.assertEqual((status, payload["user"]["role"]), (200, "user"))
        self.assertTrue(new_token)
        again = self.request("POST", "/api/auth/register", {"invite": code, "username": "bob", "password": "bob-password"})
        self.assertEqual(again[0], 400)
        invites = self.request("GET", "/api/admin/invites", cookie=token)[1]["invites"]
        self.assertEqual(invites[0]["state"], "used")

    def test_users_cannot_see_each_others_data(self):
        self.hub.accounts.create_user("alice", "alice-password")
        self.hub.accounts.create_user("bob", "bob-password")
        alice, alice_csrf = self.login("alice", "alice-password")
        bob, _ = self.login("bob", "bob-password")
        status = self.request("POST", "/api/settings/models", {"provider_name": "Alice 私有", "base_url": "https://a.example/v1",
                                                               "model": "m", "api_key": "sk-alice-secret-0001"},
                              cookie=alice, csrf=alice_csrf)[0]
        self.assertEqual(status, 200)
        alice_models = [m["provider"] for m in self.request("GET", "/api/settings", cookie=alice)[1]["models"]]
        bob_models = [m["provider"] for m in self.request("GET", "/api/settings", cookie=bob)[1]["models"]]
        self.assertIn("Alice 私有", alice_models)
        self.assertNotIn("Alice 私有", bob_models)
        uids = {u["username"]: u["id"] for u in self.hub.accounts.users()}
        self.assertNotEqual(self.hub.app(uids["alice"]).store.directory, self.hub.app(uids["bob"]).store.directory)
        self.assertEqual(self.request("GET", "/api/admin/users", cookie=bob)[0], 403)

    def test_user_settings_are_encrypted_on_disk(self):
        self.hub.accounts.create_user("alice", "alice-password")
        alice, csrf = self.login("alice", "alice-password")
        self.request("POST", "/api/settings/models", {"provider_name": "私有", "base_url": "https://a.example/v1",
                                                      "model": "m", "api_key": "sk-alice-secret-0001"}, cookie=alice, csrf=csrf)
        files = list((self.hub.data / "users").rglob("settings*"))
        self.assertEqual([f.name for f in files], ["settings.enc"])
        self.assertNotIn("sk-alice-secret", files[0].read_text())
        self.assertEqual(oct(files[0].stat().st_mode & 0o777), "0o600")

    def test_audit_log_records_sign_ins_and_admin_actions_without_secrets(self):
        self.request("POST", "/api/auth/login", {"username": "admin", "password": "not-it"})
        token, csrf = self.login("admin", "admin-password")
        self.request("POST", "/api/admin/invites", {"days": 1}, cookie=token, csrf=csrf)
        lines = [json.loads(l) for l in (self.hub.data / "audit.log").read_text().splitlines()]
        self.assertEqual([(l["action"], l["ok"]) for l in lines], [("login", False), ("login", True), ("admin:invites", True)])
        self.assertNotIn("not-it", (self.hub.data / "audit.log").read_text())
        self.assertEqual(oct((self.hub.data / "audit.log").stat().st_mode & 0o777), "0o600")
        status, _, _, _ = self.request("GET", "/api/mode")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/mode", headers={"Host": "127.0.0.1:%d" % self.port})
        self.assertEqual(conn.getresponse().getheader("X-Frame-Options"), "DENY")
        conn.close()

    def test_disabled_user_is_signed_out_immediately(self):
        uid = self.hub.accounts.create_user("alice", "alice-password")
        alice, _ = self.login("alice", "alice-password")
        token, csrf = self.login("admin", "admin-password")
        self.assertEqual(self.request("POST", "/api/admin/users/%s/disable" % uid, {}, cookie=token, csrf=csrf)[0], 200)
        self.assertEqual(self.request("GET", "/api/state", cookie=alice)[0], 401)
        self.assertEqual(self.request("POST", "/api/auth/login", {"username": "alice", "password": "alice-password"})[0], 403)
        self.assertEqual(self.request("POST", "/api/admin/users/%s/disable" % self.admin_id, {}, cookie=token, csrf=csrf)[0], 400)

    def test_oauth_callback_nonce_is_single_use_and_bound_to_its_user(self):
        url = self.hub.oauth_url(self.admin_id, "mooc")
        self.assertIn("redirect=http%3A%2F%2F127.0.0.1", url)
        nonce = url.rsplit("%2F", 1)[1]
        with patch.object(Studia, "finish_browser_login") as finish:
            status, page, _, _ = self.request("GET", "/oauth/callback/%s?token=abc" % nonce)
            self.assertEqual(status, 200)
            self.assertNotIn("abc", page)
            for _ in range(50):
                if finish.called:
                    break
                time.sleep(0.01)
            finish.assert_called_once_with("mooc", "abc")
            self.assertEqual(self.request("GET", "/oauth/callback/%s?token=abc" % nonce)[0], 400)
        other = self.hub.accounts.create_user("alice", "alice-password")
        alice, _ = self.login("alice", "alice-password")
        nonce = self.hub.oauth_url(other, "mooc").rsplit("%2F", 1)[1]
        admin, _ = self.login("admin", "admin-password")
        self.assertEqual(self.request("GET", "/oauth/callback/%s?token=abc" % nonce, cookie=admin)[0], 400)


class IsolationUnitTest(unittest.TestCase):
    def test_log_lines_go_only_to_the_job_thread_owner(self):
        alice, bob = Studia.__new__(Studia), Studia.__new__(Studia)
        for app in (alice, bob):
            app.job, app.lines = {"lines": []}, []
            app.publish = app.lines.append
        handler = _EventLogHandler()
        record = logging.LogRecord("ZHSMain.main", logging.INFO, __file__, 1, "进度", None, None)
        JOB_THREADS[record.thread] = alice
        try:
            handler.emit(record)
        finally:
            JOB_THREADS.pop(record.thread, None)
        handler.emit(record)
        self.assertEqual(len(alice.lines), 1)
        self.assertEqual(bob.lines, [])

    def test_jobs_queue_when_the_site_is_busy(self):
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        app = Studia.__new__(Studia)
        app.lock, app.slots, app.history, app.events = threading.RLock(), slots, [], []
        app.publish = app.events.append
        app.job = {"id": "j", "title": "t", "course": "c", "course_name": "c", "platform": "zhs", "state": "running",
                   "summary": "", "started": 0, "ended": None, "lines": [], "cancel": threading.Event()}
        runner = threading.Thread(target=app._run_job, args=(lambda: "完成", None))
        runner.start()
        time.sleep(1.3)
        self.assertEqual(app.events[0]["state"], "queued")
        slots.release()
        runner.join(5)
        self.assertEqual([e["state"] for e in app.events], ["queued", "running", "done"])

    def test_throttle_window(self):
        throttle = Throttle(limit=2, window=60)
        throttle.fail("k")
        self.assertEqual(throttle.blocked("k"), 0)
        throttle.fail("k")
        self.assertGreater(throttle.blocked("k"), 0)
        throttle.clear("k")
        self.assertEqual(throttle.blocked("k"), 0)

    def test_accounts_file_is_private(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(web.accounts, "ITERATIONS", 1000):
            accounts = Accounts(directory)
            accounts.create_user("alice", "alice-password")
            self.assertEqual(oct(accounts.path.stat().st_mode & 0o777), "0o600")
            self.assertNotIn("alice-password", accounts.path.read_text())

    def test_encrypted_store_round_trip_and_tamper_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            key = os.urandom(32)
            alice = EncryptedSettingsStore(Path(directory) / "alice", key, "alice")
            alice.set("session_zhs", {"access_token": "secret-cookie"})
            self.assertEqual(alice.session("zhs")["access_token"], "secret-cookie")
            sealed = alice.path.read_text()
            self.assertNotIn("secret-cookie", sealed)
            with self.assertRaises(SettingsError):
                EncryptedSettingsStore(Path(directory) / "alice", os.urandom(32), "alice").session("zhs")
            bob = EncryptedSettingsStore(Path(directory) / "bob", key, "bob")
            bob.path.parent.mkdir()
            bob.path.write_text(sealed)
            with self.assertRaises(SettingsError):
                bob.session("zhs")

    def test_plain_settings_are_migrated_and_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "settings.json").write_text(json.dumps({"version": 1, "concurrency": {"ai": 3, "study": 2}}))
            store = EncryptedSettingsStore(directory, os.urandom(32), "u")
            self.assertEqual(store.concurrency(), {"ai": 3, "study": 2})
            self.assertFalse(Path(directory, "settings.json").exists())
            self.assertTrue(store.path.exists())

    def test_server_refuses_to_start_without_a_valid_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"STUDIA_SECRET_KEY": "short"}):
            self.assertEqual(main(["--data", directory]), 1)
        self.assertEqual(len(load_key("A" * 43)), 32)
        with self.assertRaises(ValueError):
            load_key("")

    def test_data_root_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = Hub(Path(directory) / "data", "http://127.0.0.1:1", key=os.urandom(32))
            self.assertEqual(oct(hub.data.stat().st_mode & 0o777), "0o700")


if __name__ == "__main__":
    unittest.main()
