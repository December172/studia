import json
import tempfile
import time
import unittest
from unittest.mock import Mock

from settings import SettingsStore
from web.server import INTERRUPTED, JOBS, Studia, drain


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
        app.start_job.assert_called_once_with({"platform": "zhs", "type": "review", "course": "ai-c1"}, minutes=12, resumes=1)
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

    def test_history_survives_a_restart(self):
        app = self.app()
        app.platforms["zhs"].spec = dict(app.platforms["zhs"].spec, handler=lambda **kw: None)
        app.start_job({"platform": "zhs", "type": "study", "course": "ai-c1"})
        drain(5)
        self.assertEqual(self.app().history[0]["state"], "done")


if __name__ == "__main__":
    unittest.main()
