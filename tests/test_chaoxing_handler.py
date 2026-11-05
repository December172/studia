import unittest

from ai.client import AIResponseError, AIServiceError
from base.auth import AuthenticationExpired, PlatformUnavailable
from ChaoxingMain.api import WorkUnavailable
from ChaoxingMain.main import ChaoxingHandler, catalog, course_units

COURSE = {"title": "高等数学", "courseId": "111", "clazzId": "222", "cpi": "333"}
OTHER = {"title": "大学物理", "courseId": "999", "clazzId": "888", "cpi": "777"}
POINT = {"id": "123", "title": "1.1 函数"}
JOB = {"type": "workid", "jobid": "work-abc", "name": "1.1 章节测验", "enc": "e"}

PAGE = ('<form id="form1"><input type="hidden" name="courseId" value="111">'
        '<div class="singleQuesId" data="q1"><div class="TiMu" data="0">'
        '<input type="hidden" name="answerq1" value="">'
        '<div class="Zy_TItle"><span>1+1=?</span></div>'
        '<ul><li aria-label="A. 1"></li><li aria-label="B. 2"></li></ul></div></div></form>')


class FakeAI:
    def answer(self, question):
        return {"answer": "B", "explanation": ""}


class FakeClient:
    def __init__(self, courses=(COURSE,), points=(POINT,), jobs=(JOB,), page=PAGE, error=None,
                 confirmation=None, nodes=None, finished=(), unfinished=()):
        self.courses = list(courses)
        self.points = list(points)
        self.jobs = list(jobs)
        self.page = page
        self.error = error
        self.confirmation = confirmation
        self.nodes = dict(nodes or {})
        self.finished = set(finished)
        self.unfinished = set(unfinished)
        self.payloads = []
        self.closed = False

    def course_list(self):
        if isinstance(self.error, Exception):
            raise self.error
        return self.courses

    def course_points(self, course):
        return {"hasLocked": False, "points": self.points}

    def job_list(self, course, point, include_finished=False):
        if isinstance(self.error, Exception):
            raise self.error
        return self.jobs, {"knowledgeid": "123", "ktoken": "kt", "cpi": "333"}

    def course_nodes(self, course):
        return dict(self.nodes)

    def job_status_ids(self, course):
        return set(self.finished), set(self.unfinished)

    def work_page(self, course, job, job_info):
        return self.page

    def submit_work(self, payload):
        self.payloads.append(payload)
        return self.confirmation if self.confirmation is not None else {"status": True, "msg": "成功"}

    def close(self):
        self.closed = True


class CatalogTest(unittest.TestCase):
    def test_catalog_carries_what_the_runner_needs(self):
        entries = catalog(FakeClient(), COURSE)
        self.assertEqual(len(entries), 1)
        course, item, category = entries[0]
        self.assertIs(course, COURSE)
        self.assertEqual(item["jobid"], "work-abc")
        self.assertEqual(item["id"], "work-abc")
        self.assertEqual(item["name"], "1.1 章节测验")
        self.assertEqual(item["_point"], POINT)
        self.assertEqual(item["_job_info"]["knowledgeid"], "123")
        self.assertEqual(item["_completion_status"], "unknown")
        self.assertEqual(category, 1)

    def test_units_leave_out_chapter_works(self):
        # 作业在「AI 答题」里做；只有作业的章节不进学习格子。
        self.assertEqual(course_units(FakeClient(), COURSE), [])

    def test_completed_chapter_confirms_documents_when_mobile_status_is_unavailable(self):
        # 真实复测：文档上报成功后，章节已完成，但卡片仍是 isPassed=false。
        client = FakeClient(points=[dict(POINT, has_finished=True)], jobs=[
            {"type": "document", "jobid": "d1", "name": "讲义.ppt", "is_passed": False},
            {"type": "document", "jobid": "d2", "name": "讲义.pdf", "is_passed": False}])
        units = course_units(client, COURSE)[0]["units"]
        self.assertEqual([unit["state"] for unit in units], ["done", "done"])

    def test_current_node_counts_override_a_stale_completed_chapter(self):
        client = FakeClient(points=[dict(POINT, has_finished=True)], jobs=[
            {"type": "document", "jobid": "d1", "name": "讲义", "is_passed": False}],
            nodes={"123": {"finish": 0, "unfinish": 1}})
        self.assertEqual(course_units(client, COURSE)[0]["units"][0]["state"], "queued")

    def test_units_keep_learned_cards_and_use_platform_counts(self):
        jobs = [
            {"type": "video", "jobid": "v1", "name": "视频", "is_passed": True},
            {"type": "document", "jobid": "d1", "name": "PPT", "is_passed": False},
            {"type": "workid", "jobid": "w1", "name": "作业", "is_passed": False},
        ]
        client = FakeClient(jobs=jobs, nodes={"123": {"finish": 2, "unfinish": 0}})
        units = course_units(client, COURSE)[0]["units"]
        self.assertEqual([(unit["id"], unit["state"]) for unit in units], [("v1", "done"), ("d1", "done")])

    def test_units_use_finished_job_ids_for_documents(self):
        jobs = [
            {"type": "document", "jobid": "d1", "name": "PDF", "is_passed": False},
            {"type": "document", "jobid": "d2", "name": "PPT", "is_passed": False},
        ]
        client = FakeClient(jobs=jobs, finished={"d1"}, unfinished={"d2"})
        units = course_units(client, COURSE)[0]["units"]
        self.assertEqual([unit["state"] for unit in units], ["done", "queued"])


class HandlerTest(unittest.TestCase):
    def handler(self, client, **kwargs):
        self.events = []
        return ChaoxingHandler(client=client, ai_client=kwargs.pop("ai", FakeAI()),
                               on_progress=self.events.append, sleep=lambda _: None, **kwargs)

    def test_runs_every_work_and_reports_progress(self):
        client = FakeClient()
        handler = self.handler(client, auto_submit=True)
        self.assertEqual(handler.counts["submitted"], 1)
        self.assertEqual(len(client.payloads), 1)
        self.assertEqual(client.payloads[0]["pyFlag"], "")
        kinds = [event.kind for event in self.events]
        self.assertIn("begin", kinds)
        self.assertIn("result", kinds)
        self.assertIn("unit", kinds)
        begin = next(event for event in self.events if event.kind == "begin")
        self.assertEqual((begin.name, begin.total), ("超星 · 章节作业", 1))
        result = next(event for event in self.events if event.kind == "result")
        self.assertEqual((result.name, result.status), ("高等数学", "success"))
        unit = next(event for event in self.events if event.kind == "unit" and event.status == "done")
        self.assertEqual(unit.name, "1.1 章节测验")

    def test_course_id_filter(self):
        client = FakeClient(courses=(COURSE, OTHER))
        handler = self.handler(client, course_ids=["999"], auto_submit=True)
        self.assertEqual(handler.counts["submitted"], 1)
        self.assertEqual(len(client.payloads), 1)

    def test_skip_word_filter(self):
        client = FakeClient(courses=(COURSE, OTHER))
        handler = self.handler(client, skip=["物理"], auto_submit=True)
        self.assertEqual(handler.counts["submitted"], 1)

    def test_no_matching_course(self):
        client = FakeClient(courses=(COURSE,))
        handler = self.handler(client, course_ids=["404"])
        self.assertEqual(handler.counts["submitted"], 0)
        self.assertEqual(client.payloads, [])
        self.assertTrue(any(event.kind == "result" and event.status == "skipped" for event in self.events))

    def test_unavailable_work_is_skipped_not_failed(self):
        client = FakeClient()
        client.work_page = lambda *args: (_ for _ in ()).throw(WorkUnavailable("教师未创建完成该测验"))
        handler = self.handler(client, auto_submit=True)
        self.assertEqual(handler.counts["skipped"], 1)
        self.assertEqual(handler.counts["error"], 0)
        result = next(event for event in self.events if event.kind == "result")
        self.assertEqual(result.status, "skipped")

    def test_ai_failure_stops_the_run(self):
        client = FakeClient()

        class FailingAI:
            def answer(self, question):
                raise AIServiceError("AI 服务不可用")

        with self.assertRaises(AIServiceError):
            self.handler(client, ai=FailingAI(), auto_submit=True)
        self.assertEqual(client.payloads, [])

    def test_unanswered_question_saves_instead_of_submitting(self):
        client = FakeClient()

        class FailingAI:
            def answer(self, question):
                raise AIResponseError("格式错误")

        handler = self.handler(client, ai=FailingAI(), auto_submit=True)
        self.assertEqual(handler.counts["error"], 1)
        self.assertEqual(client.payloads, [])

    def test_expired_login_propagates(self):
        client = FakeClient(error=AuthenticationExpired("登录过期"))
        with self.assertRaises(AuthenticationExpired):
            self.handler(client, auto_submit=True)

    def test_network_failure_stops_the_run(self):
        client = FakeClient()

        def fail(*args):
            raise PlatformUnavailable("暂时无法连接超星平台")

        client.work_page = fail
        with self.assertRaises(PlatformUnavailable):
            self.handler(client, auto_submit=True)
        self.assertEqual(client.payloads, [])

    def test_injected_client_is_not_closed(self):
        client = FakeClient()
        self.handler(client)
        self.assertFalse(client.closed)

    def test_non_work_jobs_are_ignored(self):
        client = FakeClient(jobs=({"type": "video", "jobid": "v1"},))
        handler = self.handler(client, auto_submit=True)
        self.assertEqual(handler.counts["submitted"], 0)
        self.assertEqual(client.payloads, [])


if __name__ == "__main__":
    unittest.main()
