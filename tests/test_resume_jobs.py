import json
import tempfile
import time
import unittest
import threading
from unittest.mock import Mock, patch

from base.auth import AuthenticationExpired
from settings import SettingsStore
from web.server import INTERRUPTED, JOBS, HttpError, Studia, _qa_options, drain


class ResumeJobsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SettingsStore(directory.name)
        self.path = self.store.directory / "jobs.json"

    def app(self):
        app = Studia(self.store, oauth=Mock(return_value=None))
        platform = app.platforms["zhs"]
        platform.rows = [{"courseName": "网络课", "zhs": {"kind": "ai", "courseId": "c1"}}]
        platform.client = Mock()
        # A study job that plays until it is told to stop.
        platform.spec = dict(platform.spec, handler=lambda **kw: kw["cancel_event"].wait(5))
        return app

    def write(self, *active, stopped_ago=60):
        self.path.write_text(json.dumps({"stopped_at": time.time() - stopped_ago, "active": list(active), "history": []}))

    def test_an_update_stops_jobs_and_the_next_start_resumes_learning(self):
        app = self.app()
        app.start_job({"platform": "zhs", "type": "study", "course": "ai-c1"})
        time.sleep(0.1)
        app.shutdown()
        drain(5)
        self.assertFalse(JOBS)
        saved = json.loads(self.path.read_text())
        self.assertEqual([(e["type"], e["course"]) for e in saved["active"]], [("study", "ai-c1")])

        again = self.app()
        again.resume()
        (job,) = again.jobs.values()
        self.assertEqual((job["kind"], job["course"], job["resumes"]), ("study", "ai-c1", 1))
        self.assertEqual(job["lines"][0]["text"], "服务更新后自动继续")
        again.shutdown()
        drain(5)

    def test_answering_is_not_rerun_and_review_only_plays_what_is_left(self):
        self.write({"platform": "zhs", "type": "answer", "course": "ai-c1", "course_name": "网络课"},
                   {"platform": "zhs", "type": "review", "course": "ai-c1", "course_name": "网络课", "minutes": 12.4})
        app = self.app()
        app.start_job = Mock()
        app.resume()
        app.start_job.assert_called_once_with({"platform": "zhs", "type": "review", "course": "ai-c1"}, minutes=12, resumes=1, paused=False, restoring=True)
        self.assertEqual((app.history[0]["title"], app.history[0]["state"], app.history[0]["summary"]),
                         ("AI 答题", "stopped", INTERRUPTED))

    def test_stale_or_repeatedly_resumed_jobs_are_left_alone(self):
        study = {"platform": "zhs", "type": "study", "course": "ai-c1", "course_name": "网络课"}
        for active, stopped_ago in ((study, 2 * 3600), (dict(study, resumes=3), 60)):
            self.write(active, stopped_ago=stopped_ago)
            app = self.app()
            app.start_job = Mock()
            app.resume()
            app.start_job.assert_not_called()
            self.assertEqual(app.history[0]["summary"], INTERRUPTED)

    def test_only_back_to_back_restarts_count_toward_the_limit(self):
        app = self.app()
        app.start_job({"platform": "zhs", "type": "study", "course": "ai-c1"}, resumes=3)
        time.sleep(0.1)
        (job,) = app.jobs.values()
        with app.lock:
            app._save()
        self.assertEqual(json.loads(self.path.read_text())["active"][0]["resumes"], 3)  # restarted again at once
        job["running_since"] -= 3600  # studied for an hour since it came back
        with app.lock:
            app._save()
        self.assertEqual(json.loads(self.path.read_text())["active"][0]["resumes"], 0)
        app.shutdown()
        drain(5)

    def test_an_administrator_reorders_pauses_and_continues_without_losing_a_job(self):
        app = self.app()
        app.job_limit, app.zhs_parallel = (lambda: 1), (lambda: True)
        platform = app.platforms["zhs"]
        platform.rows = [{"courseName": c, "zhs": {"kind": "ai", "courseId": c}} for c in ("a", "b", "c")]
        runs = []

        def handler(course_rows, cancel_event, **_):
            runs.append(course_rows[0]["courseName"])
            cancel_event.wait(5)
        platform.spec = dict(platform.spec, handler=handler)
        ids = {c: app.start_job({"platform": "zhs", "type": "study", "course": "ai-" + c})["id"] for c in "abc"}
        line = lambda: [(j["course"][3:], j["state"]) for j in app._running()]
        wait = lambda want: self.assertTrue(any(line() == want or time.sleep(0.05) for _ in range(100)), line())
        wait([("a", "running"), ("b", "queued"), ("c", "queued")])

        # c moves to the front of the queue, then into a's slot: a steps aside and waits at the front.
        app.move_job(ids["c"], to=0)
        wait([("a", "running"), ("c", "queued"), ("b", "queued")])
        app.act_job("move", ids["c"], {"swap": ids["a"]})
        wait([("c", "running"), ("a", "queued"), ("b", "queued")])
        app.pause_job(ids["b"])                    # waiting: paused at once
        app.pause_job(ids["c"])                    # running: stops, keeps its place in the list
        wait([("a", "running"), ("c", "paused"), ("b", "paused")])   # c keeps the place it was moved to
        with self.assertRaises(HttpError):
            app.move_job(ids["b"], to=0)           # paused: continue it first
        app.continue_job(ids["c"])
        wait([("a", "running"), ("c", "queued"), ("b", "paused")])
        self.assertEqual(runs, ["a", "c", "a"])    # the same jobs ran again; none was dropped or duplicated

        # A pause outlasts an update, however long the service is down; the rest resume as before.
        app.shutdown()
        drain(5)
        saved = json.loads(self.path.read_text())
        self.assertEqual([(e["course"][3:], e["paused"]) for e in saved["active"]], [("a", False), ("b", True), ("c", False)])
        saved["stopped_at"] -= 2 * 3600
        self.path.write_text(json.dumps(saved))
        again = self.app()
        again.zhs_parallel = lambda: True
        again.platforms["zhs"].rows = platform.rows
        again.resume()
        self.assertEqual([(j["course"][3:], j["state"]) for j in again._running()], [("b", "paused")])
        again.stop_job()
        drain(5)
        self.assertEqual(again.history[0]["state"], "stopped")

    def test_qa_posting_is_not_rerun_after_a_restart(self):
        self.write({"platform": "zhs", "type": "qa", "course": "ai-c1", "course_name": "网络课"})
        app = self.app()
        app.start_job = Mock()
        app.resume()
        app.start_job.assert_not_called()
        self.assertEqual((app.history[0]["title"], app.history[0]["summary"]), ("问答区答题", INTERRUPTED))

    def test_qa_options_are_checked_and_stop_does_not_wait_out_the_interval(self):
        with self.assertRaises(HttpError):
            _qa_options({"limit": 0})
        with self.assertRaises(HttpError):
            _qa_options({"limit": "ten"})
        self.assertEqual(_qa_options({}), {"limit": 10})

        app = self.app()
        app.platforms["zhs"].rows = [{"courseName": "网络课", "zhs": {"kind": "shared", "courseId": "c1", "recruitId": "r1"}}]
        posted = threading.Event()

        class Board:
            def __enter__(self): return self
            def __exit__(self, *exc): pass
            def questions(self, **kw): return [{"id": i, "content": "第 %d 节的分层设计为什么更好维护？" % i} for i in range(3)]
            def publish(self, qid, text): posted.set(); return 100 + qid

        app._ai = Mock(return_value=Mock(answer=Mock(return_value={"answer": "这是一段足够长的回答，" * 5})))
        with patch("web.server.QaBridge", return_value=Board()):
            app.start_job({"platform": "zhs", "type": "qa", "course": "shared-c1", "interval": 600})
            self.assertTrue(posted.wait(5))
            started = time.time()
            app.stop_job()
            drain(5)
        self.assertLess(time.time() - started, 3)
        self.assertEqual(app.history[0]["state"], "stopped")

    def test_a_qa_page_bounced_to_sign_in_keeps_the_platform_login(self):
        self.store.save_session("zhs", Mock(access_token="still-good"))
        app = self.app()
        app.platforms["zhs"].rows = [{"courseName": "网络课", "zhs": {"kind": "shared", "courseId": "c1", "recruitId": "r1"}}]
        app._ai = Mock()
        with patch("web.server.QaBridge", side_effect=AuthenticationExpired("智慧树登录已过期，请重新扫码")):
            app.start_job({"platform": "zhs", "type": "qa", "course": "shared-c1"})
            drain(5)
        self.assertEqual(app.history[0]["state"], "waiting")
        self.assertIn("问答区没进去", app.history[0]["summary"])
        self.assertEqual(self.store.session("zhs")["access_token"], "still-good")
        self.assertIsNotNone(app.platforms["zhs"].client)

    def test_history_survives_a_restart(self):
        app = self.app()
        app.platforms["zhs"].spec = dict(app.platforms["zhs"].spec, handler=lambda **kw: None)
        app.start_job({"platform": "zhs", "type": "study", "course": "ai-c1"})
        drain(5)
        self.assertEqual(self.app().history[0]["state"], "done")


def setUpModule():
    # Task tests have a daytime clock; curfew boundaries live in test_quiet_hours.py.
    daytime = unittest.mock.patch("web.server.quiet_hours", return_value={"active": False})
    daytime.start()
    unittest.addModuleCleanup(daytime.stop)


if __name__ == "__main__":
    unittest.main()
