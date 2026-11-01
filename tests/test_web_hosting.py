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
from web.accounts import AccountError, Accounts, Sessions, Throttle
from settings import SettingsError
from web.hosting import COOKIE, EncryptedSettingsStore, HostedAccess, Hub, _key_file, export_logs, load_key, main
from web.server import JOBS, Studia, _EventLogHandler, make_handler


class HostedServerTest(unittest.TestCase):
    def setUp(self):
        fast = patch.object(web.accounts, "ITERATIONS", 1000)
        fast.start()
        self.addCleanup(fast.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), None)
        self.port = self.server.server_address[1]
        self.hub = Hub(directory.name, "http://127.0.0.1:%d" % self.port, key=os.urandom(32))
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

    def test_first_visit_sets_up_the_admin_with_the_logged_code(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = Hub(directory, "http://127.0.0.1:%d" % self.port, key=os.urandom(32))
            self.server.RequestHandlerClass = make_handler(HostedAccess(hub, self.port))
            self.assertTrue(self.request("GET", "/api/mode")[1]["setup"])
            form = {"username": "owner", "password": "owner-password"}
            self.assertEqual(self.request("POST", "/api/auth/setup", dict(form, code="0000-0000-0000"))[0], 403)
            status, payload, token, _ = self.request("POST", "/api/auth/setup", dict(form, code=hub.setup_code.lower()))
            self.assertEqual((status, payload["user"]["role"]), (200, "admin"))
            self.assertTrue(token)
            self.assertFalse(self.request("GET", "/api/mode")[1]["setup"])
            self.assertEqual(self.request("POST", "/api/auth/setup", dict(form, code="x"))[0], 409)

    def login(self, username, password):
        status, _, token, _ = self.request("POST", "/api/auth/login", {"username": username, "password": password})
        self.assertEqual(status, 200)
        csrf = self.request("GET", "/api/auth/me", cookie=token)[1]["csrf"]
        return token, csrf

    def test_everything_but_login_needs_a_session(self):
        self.assertEqual(self.request("GET", "/api/mode")[1], {"mode": "hosted", "setup": False})
        self.assertEqual(self.request("GET", "/api/state")[0], 401)
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.request("GET", "/api/mode", host="evil.example")[0], 403)

    def test_admin_sets_job_limits_and_sees_everyones_jobs(self):
        token, csrf = self.login("admin", "admin-password")
        alice = self.hub.accounts.create_user("alice", "alice-password")
        post = lambda path, body: self.request("POST", path, body, cookie=token, csrf=csrf)
        self.assertEqual(self.request("GET", "/api/admin/settings", cookie=token)[1], {"max_jobs": 4})
        self.assertEqual(post("/api/admin/settings", {"max_jobs": 2})[1], {"max_jobs": 2})
        self.assertEqual(self.hub.job_limit(alice), 2)
        self.assertEqual(post("/api/admin/users/%s/limit" % alice, {"max_jobs": 6})[0], 200)
        self.assertEqual(self.hub.job_limit(alice), 6)
        self.assertEqual(post("/api/admin/users/%s/limit" % alice, {"max_jobs": ""})[0], 200)
        self.assertEqual(self.hub.job_limit(alice), 2)
        self.assertEqual(post("/api/admin/settings", {"max_jobs": 0})[0], 400)
        app = self.hub.app(alice)
        app.jobs = {"j1": {"id": "j1", "kind": "study", "title": "自动学习", "course": "c", "course_name": "课",
                           "platform": "zhs", "state": "queued", "summary": "", "started": 1, "ended": None,
                           "lines": [], "cancel": threading.Event()}}
        jobs = self.request("GET", "/api/admin/jobs", cookie=token)[1]["jobs"]
        self.assertEqual([(j["username"], j["state"]) for j in jobs], [("alice", "queued")])
        self.assertEqual(post("/api/admin/users/%s/jobs/j1/stop" % alice, {})[0], 200)
        self.assertTrue(app.jobs["j1"]["cancel"].is_set())
        status, text, _, _ = self.request("GET", "/api/admin/logs?kind=audit&from=%d&to=%d" % (time.time() - 3600, time.time() + 60), cookie=token)
        self.assertEqual(status, 200)
        self.assertIn("admin:limit", text)   # the limit changes above were recorded
        self.assertEqual(self.request("GET", "/api/admin/logs?kind=app&from=x&to=1", cookie=token)[0], 400)
        # A user cannot reach any of it.
        user_token, user_csrf = self.login("alice", "alice-password")
        self.assertEqual(self.request("POST", "/api/admin/settings", {"max_jobs": 20},
                                      cookie=user_token, csrf=user_csrf)[0], 403)

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
            app.lines = []
            app.publish = app.lines.append
        handler = _EventLogHandler()
        record = logging.LogRecord("ZHSMain.main", logging.INFO, __file__, 1, "进度", None, None)
        JOBS["a1"] = alice
        try:
            for name in ("studia-job-a1", "studia-job-a1/study_3", "MainThread", "studia-job-b2"):
                record.threadName = name
                handler.emit(record)
        finally:
            JOBS.pop("a1", None)
        self.assertEqual([line["job"] for line in alice.lines], ["a1", "a1"])
        self.assertEqual(bob.lines, [])

    def test_jobs_beyond_the_personal_limit_wait_their_turn(self):
        app = Studia.__new__(Studia)
        app.lock, app.history, app.events, app.jobs = threading.RLock(), [], [], {}
        app.publish = app.events.append
        app.job_limit = lambda: 1
        gate = threading.Event()

        def job(jid, started):
            app.jobs[jid] = {"id": jid, "title": jid, "course": jid, "course_name": jid, "platform": "zhs",
                             "state": "queued", "summary": "", "started": started, "ended": None, "lines": [],
                             "cancel": threading.Event()}
            with app.lock:
                app._take_turn(app.jobs[jid])
            return app.jobs[jid]

        first, second = job("a", 1), job("b", 2)
        self.assertEqual((first["state"], second["state"]), ("running", "queued"))
        runners = [threading.Thread(target=app._run_job, args=(first, lambda: gate.wait(5) and "完成", None)),
                   threading.Thread(target=app._run_job, args=(second, lambda: "完成", None))]
        for runner in runners:
            runner.start()
        time.sleep(1.3)
        self.assertEqual(second["state"], "queued")
        gate.set()
        for runner in runners:
            runner.join(5)
        self.assertEqual([(e["id"], e["state"]) for e in app.events], [("a", "done"), ("b", "running"), ("b", "done")])

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
            self.assertNotIn(b"alice-password", accounts.path.read_bytes())

    def test_sessions_survive_a_restart_and_json_accounts_migrate(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(web.accounts, "ITERATIONS", 1000):
            legacy = {"users": {"u1": {"username": "alice", "password": web.accounts.hash_password("alice-password"),
                                       "role": "admin", "disabled": False, "created": 1, "last_login": None}},
                      "invites": {"AAAA-BBBB-CCCC": {"created": 1, "created_by": "u1", "expires": 9e9, "used_by": None}}}
            Path(directory, "accounts.json").write_text(json.dumps(legacy))
            accounts = Accounts(directory)
            self.assertEqual(accounts.authenticate("ALICE", "alice-password")["id"], "u1")
            self.assertEqual([i["state"] for i in accounts.invites()], ["open"])
            self.assertTrue(Path(directory, "accounts.json.migrated").exists())
            token = Sessions(accounts).create("u1")
            restarted = Accounts(directory)
            self.assertEqual(Sessions(restarted).get(token)["uid"], "u1")
            with self.assertRaises(AccountError):
                restarted.setup_admin("mallory", "mallory-password")

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
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys" / "secret.key"
            first = _key_file(path)
            self.assertEqual((len(load_key(first)), _key_file(path)), (32, first))
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        with self.assertRaises(ValueError):
            load_key("")

    def test_answer_memory_is_shared_and_old_user_files_are_merged(self):
        from ZHSMain.answer import prepare
        from ZHSMain.memory import AnswerMemory
        question = prepare({"id": 1, "content": "1+1=?", "questionType": 1,
                            "optionVos": [{"id": 1, "content": "1"}, {"id": 2, "content": "2"}]})
        with tempfile.TemporaryDirectory() as directory:
            legacy = AnswerMemory(Path(directory) / "users" / "u1" / "zhs-answers.json")
            legacy.remember_correct(question, ["2"])
            legacy.save()
            hub = Hub(directory, "http://127.0.0.1:1", key=os.urandom(32))
            self.assertIs(hub.app("u1").memory, hub.app("u2").memory)
            self.assertEqual(hub.app("u2").memory.lookup(question)["correct"], ["2"])
            self.assertFalse(legacy.path.exists())
            self.assertTrue(legacy.path.with_name("zhs-answers.json.merged").exists())

    def test_data_root_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = Hub(Path(directory) / "data", "http://127.0.0.1:1", key=os.urandom(32))
            self.assertEqual(oct(hub.data.stat().st_mode & 0o777), "0o700")


if __name__ == "__main__":
    unittest.main()


class LogExportTest(unittest.TestCase):
    def test_only_lines_inside_the_window_and_their_tracebacks(self):
        with tempfile.TemporaryDirectory() as directory:
            day = Path(directory) / "icve-ai-assistant-log_2026-11-01.log"
            day.write_text("[2026-11-01 08:00:00,001][1][a.py][line: 1][INFO] :: early\n"
                           "[2026-11-01 09:30:00,001][1][a.py][line: 1][ERROR] :: boom\n"
                           "Traceback (most recent call last):\n"
                           "[2026-11-01 11:00:00,001][1][a.py][line: 1][INFO] :: late\n", encoding="utf-8")
            at = lambda text: time.mktime(time.strptime(text, "%Y-%m-%d %H:%M"))
            text = export_logs(directory, "app", at("2026-11-01 09:00"), at("2026-11-01 10:00"))
            self.assertEqual(text.splitlines(), ["[2026-11-01 09:30:00,001][1][a.py][line: 1][ERROR] :: boom",
                                                 "Traceback (most recent call last):"])
            Path(directory, "audit.log").write_text(
                json.dumps({"time": "2026-11-01 09:15:00", "action": "login"}) + "\n", encoding="utf-8")
            self.assertIn('"login"', export_logs(directory, "audit", at("2026-11-01 09:00"), at("2026-11-01 10:00")))
            self.assertEqual(export_logs(directory, "app", at("2026-11-02 09:00"), at("2026-11-02 10:00")),
                             "这段时间没有日志。\n")
