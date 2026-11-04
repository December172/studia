"""MOOC papers out of attempts, their order, and a running job's grid surviving a page reload."""
import tempfile
import unittest
from unittest.mock import Mock

from AIMoocMain.exam_api import ExamAPI, paper_number
from settings import SettingsStore
from task_capabilities import task_availability
from web.server import Studia, _track


class PapersTest(unittest.TestCase):
    def test_no_attempt_left_closes_every_kind_of_paper(self):
        for category in (1, 2, 3):
            availability = task_availability({"answerCount": -1, "status": 2}, category)
            self.assertFalse(availability.available)
            self.assertEqual(availability.label, "次数用完")
        self.assertTrue(task_availability({"answerCount": 1, "status": 0}, 1).available)

    def test_papers_follow_the_number_in_their_name(self):
        names = ["专题五 测验", "专题二 测验", "期末汇总", "专题四 测验", "专题十 测验", "专题一 测验"]
        api = ExamAPI.__new__(ExamAPI)
        api.request = Mock(return_value={"code": 200, "data": [{"id": n, "name": n} for n in names]})
        listed = [x["name"] for x in api.catalog({"id": "i", "courseId": "c"}, 1)]
        self.assertEqual(listed, ["专题一 测验", "专题二 测验", "专题四 测验", "专题五 测验", "专题十 测验", "期末汇总"])
        self.assertEqual(paper_number({"name": "第二十三次"}), 23)


class GridTest(unittest.TestCase):
    def test_finished_units_reach_the_cached_grid_and_leave_the_bar(self):
        app = Studia(SettingsStore(tempfile.mkdtemp()), oauth=Mock(return_value=None))
        grid = [{"name": "第一章", "units": [{"id": "u1", "state": "queued"}, {"id": "u2", "state": "queued"}]}]
        app.platforms["mooc"].cache[("units", "c")] = (0, grid)
        job = {"id": "j", "platform": "mooc", "course": "c", "lines": []}
        app.jobs["j"] = job
        for status in ("running", "done"):
            app.publish({"type": "progress", "kind": "unit", "job": "j", "key": "u1", "name": "视频", "status": status})
        self.assertEqual([u["state"] for u in grid[0]["units"]], ["done", "queued"])
        self.assertNotIn("unit", job["progress"])  # the bar now shows what comes next, not the last unit

    def test_a_running_unit_stays_on_the_bar(self):
        progress = {}
        _track(progress, {"kind": "unit", "key": "u1", "name": "视频", "status": "running"})
        _track(progress, {"kind": "unit", "key": "u0", "name": "旧", "status": "done"})
        self.assertEqual(progress["unit"]["key"], "u1")


if __name__ == "__main__":
    unittest.main()
