import threading
import time
import unittest

from base.auth import AuthenticationExpired
from ChaoxingMain.api import ChaoxingError
from ChaoxingMain.study import ChaoxingStudyHandler

COURSE = {"title": "高等数学", "courseId": "111", "clazzId": "222", "cpi": "333"}


def point(point_id, finished=False):
    return {"id": point_id, "title": "章节" + point_id, "has_finished": finished}


def video(job_id):
    return {"type": "video", "jobid": job_id, "name": "视频" + job_id}


def work(job_id):
    return {"type": "workid", "jobid": job_id, "name": "作业" + job_id}


class FakeClient:
    def __init__(self, chapters, jobs=None, *, job_delay=0.0, task_delay=0.0,
                 job_error=None, not_open=False, video_results=None):
        self.chapters = chapters
        self.jobs = jobs or {}
        self.job_delay = job_delay
        self.task_delay = task_delay
        self.job_error = job_error
        self.not_open = not_open
        self.video_results = dict(video_results or {})
        self.lock = threading.Lock()
        self.course_points_calls = 0
        self.job_calls = []
        self.video_calls = []
        self.document_calls = []
        self.read_calls = []
        self.empty_pages = []
        self.active_chapters = 0
        self.max_chapters = 0
        self.active_tasks = 0
        self.max_tasks = 0

    def course_points(self, course):
        self.course_points_calls += 1
        return {"hasLocked": False, "points": list(self.chapters)}

    def job_list(self, course, point_info):
        with self.lock:
            self.job_calls.append(point_info["id"])
            self.active_chapters += 1
            self.max_chapters = max(self.max_chapters, self.active_chapters)
        try:
            time.sleep(self.job_delay)
            error = self.job_error(point_info) if self.job_error else None
            if error:
                raise error
            if self.not_open:
                return [], {"notOpen": True}
            return list(self.jobs.get(point_info["id"], [])), {"knowledgeid": "k", "cpi": "c", "ktoken": "kt"}
        finally:
            with self.lock:
                self.active_chapters -= 1

    def empty_page(self, course, point_info):
        self.empty_pages.append(point_info["id"])

    def study_video(self, course, job, *, speed=1.0, kind="Video", cancel_event=None):
        with self.lock:
            self.video_calls.append((job["jobid"], kind))
            self.active_tasks += 1
            self.max_tasks = max(self.max_tasks, self.active_tasks)
        try:
            time.sleep(self.task_delay)
            return self.video_results.get((job["jobid"], kind), "success")
        finally:
            with self.lock:
                self.active_tasks -= 1

    def study_document(self, course, job):
        self.document_calls.append(job["jobid"])
        return "success"

    def study_read(self, course, job, job_info):
        self.read_calls.append(job["jobid"])
        return "success"


def run(chapters, jobs=None, *, max_workers=1, client=None, notopen_action="retry", **kwargs):
    events = []
    fake = client or FakeClient(chapters, jobs, **kwargs)
    handler = ChaoxingStudyHandler(client=fake, course_rows=[COURSE], on_progress=events.append,
                                   max_workers=max_workers, sleep=lambda _: None,
                                   notopen_action=notopen_action)
    return fake, handler, events


class ParallelismTest(unittest.TestCase):
    def test_chapters_run_in_parallel(self):
        chapters = [point(str(index)) for index in range(1, 5)]
        jobs = {chapter["id"]: [video("v" + chapter["id"])] for chapter in chapters}
        fake, _, events = run(chapters, jobs, max_workers=2, job_delay=0.08)
        self.assertEqual(fake.max_chapters, 2)
        self.assertEqual([event.kind for event in events].count("result"), 1)

    def test_tasks_inside_a_chapter_run_in_parallel(self):
        chapters = [point("1")]
        jobs = {"1": [video("v%d" % index) for index in range(4)]}
        fake, _, _ = run(chapters, jobs, task_delay=0.06)
        self.assertGreaterEqual(fake.max_tasks, 2)
        self.assertEqual(len(fake.video_calls), 4)


class RetryTest(unittest.TestCase):
    def test_chapter_error_is_retried(self):
        failures = {"1": 1}

        def job_error(point_info):
            if failures.get(point_info["id"]):
                failures[point_info["id"]] -= 1
                return ChaoxingError("临时错误")
            return None

        fake, _, events = run([point("1")], {"1": [video("v1")]}, job_error=job_error)
        self.assertEqual(fake.job_calls, ["1", "1"])
        self.assertEqual([event.status for event in events if event.kind == "result"], ["success"])

    def test_not_open_can_be_skipped(self):
        fake, _, events = run([point("1")], not_open=True, notopen_action="continue")
        self.assertEqual(fake.job_calls, ["1"])
        self.assertEqual([event.status for event in events if event.kind == "result"], ["skipped"])

    def test_not_open_is_retried_by_default(self):
        fake, _, _ = run([point("1")], not_open=True)
        self.assertEqual(len(fake.job_calls), 5)  # MAX_TRIES

    def test_authentication_error_propagates(self):
        def job_error(point_info):
            return AuthenticationExpired("登录过期")

        with self.assertRaises(AuthenticationExpired):
            run([point("1")], job_error=job_error)


class TaskTest(unittest.TestCase):
    def test_finished_chapter_is_skipped(self):
        fake, _, events = run([point("1", finished=True)])
        self.assertEqual(fake.job_calls, [])
        self.assertEqual([event.status for event in events if event.kind == "result"], ["skipped"])

    def test_empty_chapter_is_completed(self):
        fake, _, events = run([point("1")], {"1": []})
        self.assertEqual(fake.empty_pages, ["1"])
        self.assertEqual([event.status for event in events if event.kind == "result"], ["success"])

    def test_chapter_with_only_exercises_is_left_to_the_answer_job(self):
        fake, _, events = run([point("1")], {"1": [work("w1")]})
        self.assertEqual(fake.video_calls, [])
        self.assertEqual([event.status for event in events if event.kind == "result"], ["skipped"])
        self.assertEqual([event for event in events if event.kind == "unit"], [])

    def test_video_failure_falls_back_to_audio(self):
        fake, _, _ = run([point("1")], {"1": [video("v1")]},
                         video_results={("v1", "Video"): "error", ("v1", "Audio"): "success"})
        self.assertEqual(fake.video_calls, [("v1", "Video"), ("v1", "Audio")])

    def test_cancel_before_start_does_nothing(self):
        cancel = threading.Event()
        cancel.set()
        fake = FakeClient([point("1")])
        ChaoxingStudyHandler(client=fake, course_rows=[COURSE], cancel_event=cancel,
                             max_workers=2, sleep=lambda _: None)
        self.assertEqual(fake.course_points_calls, 0)
        self.assertEqual(fake.job_calls, [])

    def test_document_and_read_use_the_client(self):
        jobs = {"1": [{"type": "document", "jobid": "d1", "name": "讲义"},
                      {"type": "read", "jobid": "r1", "name": "阅读"}]}
        fake, _, _ = run([point("1")], jobs)
        self.assertEqual(fake.document_calls, ["d1"])
        self.assertEqual(fake.read_calls, ["r1"])

    def test_progress_reports_units_and_chapters(self):
        fake, _, events = run([point("1"), point("2")],
                              {"1": [video("v1")], "2": [video("v2")]}, max_workers=2)
        units = [(event.key, event.name, event.status) for event in events if event.kind == "unit"]
        self.assertIn(("v1", "视频v1", "done"), units)
        self.assertIn(("v2", "视频v2", "done"), units)
        resources = [event for event in events if event.kind == "resources"]
        self.assertTrue(resources)
        self.assertEqual(resources[-1].total, 2)
        self.assertEqual(resources[-1].completed, 2)


if __name__ == "__main__":
    unittest.main()
