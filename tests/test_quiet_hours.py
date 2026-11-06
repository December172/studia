"""Offline clock-boundary checks: no course accounts or external requests."""
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import Mock, patch

from ai.runner import AttemptClock
from base.quiet_hours import BEIJING, CHECKPOINTS, quiet_hours
from reporting import child_thread_name
from settings import SettingsStore
from web.server import HttpError, JOBS, Studia, drain, keep_passports, watch_quiet_hours
from ZHSMain.api import ZHSApi
from ZHSMain.answer import ZHSAnswerRunner
from ZHSMain.qa import QaBridge


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=BEIJING).timestamp()


class QuietClockTest(unittest.TestCase):
    def test_boundaries_use_beijing_time_including_midnight(self):
        for text, active, until in (
            ("2026-11-06T06:59:59", True, 1),
            ("2026-11-06T07:00:00", False, 16 * 3600),
            ("2026-11-06T22:59:59", False, 1),
            ("2026-11-06T23:00:00", True, 8 * 3600),
            ("2026-11-07T00:00:00", True, 7 * 3600),
        ):
            with self.subTest(text=text):
                result = quiet_hours(at(text))
                self.assertEqual((result["active"], result["changes_in"]), (active, until))
        # The same instant in UTC is 15:00, regardless of the host's local timezone.
        self.assertTrue(quiet_hours(datetime.fromisoformat("2026-11-06T15:00:00+00:00").timestamp())["active"])

    def test_heartbeat_does_not_report_an_overnight_wait_as_answering_time(self):
        api = Mock()
        clock = AttemptClock(api, {}, "exam", "task", None, False)
        clock.started = 0
        clock.stop_event = Mock()
        clock.stop_event.wait.side_effect = [False, True]
        clock.stop_event.is_set.return_value = False
        with patch("ai.runner.time.monotonic", side_effect=[10, 8 * 3600 + 10]), patch("ai.runner.checkpoint"):
            clock._loop()
        api.heartbeat.assert_called_once_with({}, "exam", "task", 10)


class QuietJobsTest(unittest.TestCase):
    def setUp(self):
        self.now = at("2026-11-06T22:59:59")
        clock = patch("web.server.quiet_hours", side_effect=lambda: quiet_hours(self.now))
        clock.start()
        self.addCleanup(clock.stop)
        self.apps = []
        self.stop = threading.Event()
        self.watcher = threading.Thread(target=watch_quiet_hours, args=(self.stop,), daemon=True)
        self.watcher.start()

    def tearDown(self):
        self.stop.set()
        self.watcher.join(3)
        for app in self.apps:
            app.stop_job()
        drain(5)
        self.assertFalse(JOBS)
        self.assertFalse(CHECKPOINTS)

    def app(self):
        directory = tempfile.TemporaryDirectory()
        # Cleanup jobs before deleting their persistence directory.
        self.addCleanup(directory.cleanup)
        app = Studia(SettingsStore(directory.name), zhs_parallel=lambda: True, job_limit=lambda: 1)
        self.apps.append(app)
        p = app.platforms["zhs"]
        p.rows = [{"courseName": c, "zhs": {"kind": "shared", "courseId": c, "recruitId": "r"}} for c in "abc"]
        p.client = Mock(access_token=None)
        p.spec = dict(p.spec, handler=lambda cancel_event, **_: cancel_event.wait(10))
        return app

    def start(self, app, course="a", kind="study"):
        return app.start_job({"platform": "zhs", "course": "shared-" + course, "type": kind})["id"]

    def wait_for(self, predicate):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("task did not reach the expected state")

    def night(self):
        self.now = at("2026-11-06T23:00:00")

    def morning(self):
        self.now = at("2026-11-07T07:00:00")

    def test_no_user_or_task_type_can_start_or_continue_at_night(self):
        for app in (self.app(), self.app()):
            app.find_row = Mock(side_effect=AssertionError("must reject before reading the platform"))
            self.night()
            for kind in ("study", "review", "answer", "qa", "ask"):
                with self.subTest(kind=kind), self.assertRaises(HttpError) as error:
                    self.start(app, kind=kind)
                self.assertEqual(error.exception.status, 409)
                self.assertIn("07:00", str(error.exception))
            for action in ("continue", "move"):
                with self.assertRaises(HttpError) as error:
                    app.act_job(action, "not-even-created", who="管理员")
                self.assertEqual(error.exception.status, 409)
            app.find_row.assert_not_called()
            self.assertFalse(app.jobs)

    def test_slow_validation_crossing_2300_cannot_create_a_job(self):
        app = self.app()
        row = app.platforms["zhs"].rows[0]
        app.find_row = Mock(side_effect=lambda *_: self.night() or row)
        with self.assertRaises(HttpError):
            self.start(app)
        self.assertFalse(app.jobs)

    def test_all_users_pause_and_queue_waits_manual_pause_survives_morning(self):
        app, other = self.app(), self.app()
        runs = []
        app.platforms["zhs"].spec["handler"] = lambda course_rows, cancel_event, **_: (
            runs.append(course_rows[0]["courseName"]), cancel_event.wait(10))
        running = self.start(app)
        queued = self.start(app, "b")
        manual = self.start(app, "c")
        app.pause_job(manual)
        other_id = self.start(other)
        self.wait_for(lambda: runs == ["a"])
        self.night()
        self.wait_for(lambda: app.jobs[running]["state"] == "queued" and other.jobs[other_id]["state"] == "queued")
        public = {j["id"]: j for j in app._running()}
        self.assertTrue(public[running]["night_paused"])
        self.assertTrue(public[queued]["night_paused"])
        self.assertEqual(public[running]["state"], "paused")
        self.assertFalse(public[manual]["night_paused"])
        self.assertEqual(runs, ["a"])
        # A stopped queue entry must not spring back to life at 07:00.
        app.stop_job(queued)
        self.wait_for(lambda: queued not in app.jobs)
        self.morning()
        self.wait_for(lambda: runs == ["a", "a"] and other.jobs[other_id]["state"] == "running")
        self.assertEqual(app.jobs[manual]["state"], "paused")
        self.assertEqual(app.history[0]["state"], "stopped")

    def test_post_waits_in_place_and_is_sent_once_after_morning(self):
        app = self.app()
        entered, release = threading.Event(), threading.Event()
        board = QaBridge.__new__(QaBridge)
        board._page = Mock()

        def ask(*_):
            entered.set()
            release.wait(5)
            return board._eval("publish", {}) and "已发布"

        app._ask = Mock(side_effect=ask)
        app.qa_room = Mock(return_value={"ask": 10})
        job_id = app.start_job({"platform": "zhs", "course": "shared-a", "type": "ask",
                               "texts": ["边缘计算和云计算在工厂里应该怎样分工？"]})["id"]
        self.assertTrue(entered.wait(3))
        self.night()
        release.set()
        self.wait_for(lambda: app.jobs[job_id].get("night_paused"))
        board._page.evaluate.assert_not_called()
        self.morning()
        self.wait_for(lambda: job_id not in app.jobs)
        app._ask.assert_called_once()
        board._page.evaluate.assert_called_once_with("publish", {})
        self.assertEqual(app.history[0]["state"], "done")

    def test_nested_helper_requests_wait_and_night_stop_wakes_them(self):
        app = self.app()
        entered, release = threading.Event(), threading.Event()
        api = ZHSApi(session=Mock())
        api.session.request.return_value = Mock(status_code=200, json=lambda: {"code": 0})

        def ask(*_):
            entered.set()
            release.wait(5)
            with ThreadPoolExecutor(1, thread_name_prefix=child_thread_name("answer")) as pool:
                return pool.submit(api.secure, "https://example.invalid", {}).result()

        app._ask = Mock(side_effect=ask)
        app.qa_room = Mock(return_value={"ask": 10})
        job_id = app.start_job({"platform": "zhs", "course": "shared-a", "type": "ask",
                               "texts": ["边缘计算和云计算在工厂里应该怎样分工？"]})["id"]
        self.assertTrue(entered.wait(3))
        self.night()
        release.set()
        self.wait_for(lambda: app.jobs[job_id].get("night_paused"))
        api.session.request.assert_not_called()
        app.stop_job(job_id)
        self.wait_for(lambda: job_id not in app.jobs)
        self.assertEqual(app.history[0]["state"], "stopped")
        api.session.request.assert_not_called()

    def test_nightly_restart_keeps_learning_even_when_older_than_resume_window(self):
        app = self.app()
        self.night()
        path = app.store.directory / "jobs.json"
        path.write_text(json.dumps({"stopped_at": time.time() - 8 * 3600, "active": [
            {"platform": "zhs", "course": "shared-a", "type": "study", "night_paused": True, "resumes": 3},
            {"platform": "zhs", "course": "shared-b", "type": "study", "paused": True},
            {"platform": "zhs", "course": "shared-c", "type": "answer", "night_paused": True},
        ]}))
        app.resume()
        self.assertEqual([(j["course"], j["state"]) for j in app.jobs.values()],
                         [("shared-a", "queued"), ("shared-b", "paused")])
        self.assertEqual(app.history[0]["kind"], "answer")
        self.assertEqual(app.history[0]["state"], "stopped")
        app.enforce_quiet_hours()
        app.shutdown()
        drain(5)
        saved = json.loads(path.read_text())
        self.assertTrue(saved["active"][0]["night_paused"])
        self.assertTrue(saved["active"][1]["paused"])

    def test_a_finished_papers_heartbeat_exits_while_its_job_still_waits(self):
        app = self.app()
        entered, release, finish, ended = (threading.Event() for _ in range(4))
        api = ZHSApi(session=Mock())

        def ask(*args):
            entered.set()
            release.wait(5)
            runner = ZHSAnswerRunner(api, Mock(), auto_submit=False, heartbeat_interval=0.01)
            runner._start_heartbeat("test", "paper")
            finish.wait(5)
            runner._stop_heartbeat()
            ended.set()
            args[-1]["cancel"].wait(5)

        app._ask = Mock(side_effect=ask)
        app.qa_room = Mock(return_value={"ask": 10})
        job_id = app.start_job({"platform": "zhs", "course": "shared-a", "type": "ask",
                               "texts": ["边缘计算和云计算在工厂里应该怎样分工？"]})["id"]
        self.assertTrue(entered.wait(3))
        self.night()
        release.set()
        self.wait_for(lambda: app.jobs[job_id].get("night_paused"))
        finish.set()
        self.assertTrue(ended.wait(3))
        self.assertIn(job_id, app.jobs)
        api.session.request.assert_not_called()

    def test_keepalive_does_not_touch_platforms_at_night(self):
        self.night()
        app = Mock()
        stop = Mock()
        stop.wait.side_effect = [False, True]
        keep_passports(lambda: [app], first=0, stop=stop)
        app.keep_passport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
