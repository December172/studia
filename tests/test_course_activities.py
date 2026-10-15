"""Offline activity flow: platform state gates every AI-generated submission."""
import threading
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from contextlib import nullcontext

from AIMoocMain.api import AIMoocApi
from app import Application

from AIMoocMain.activities import CourseActivities, discussion_open
from AIMoocMain.main import AIMoocHandler
from ai.client import AIError, AIServiceError
from base.auth import AuthenticationExpired
from base.api_client import RequestRateLimited

COURSE = {"id": "ci", "courseId": "c", "courseName": "数据结构"}
OPEN = {"id": "d", "title": "第一章讨论", "zt": 1}
DONE = {"star": 5, "content": "已有评价"}
AUTO = object()


class FakeAI:
    def __init__(self, answers=("快速排序平均效率较高。", "课程结构清晰，内容实用。"), action=None):
        self.answers = iter(answers)
        self.action = action
        self.questions = []

    def answer(self, question):
        self.questions.append(question)
        if self.action:
            self.action()
        answer = next(self.answers, None)
        if isinstance(answer, Exception):
            raise answer
        return {"answer": answer, "explanation": ""}


class ActivityClient:
    """Model the documented server endpoints, not the activity implementation."""
    def __init__(self, discussions=(OPEN,), evaluation=DONE):
        self.discussions = list(discussions)
        self.evaluation = evaluation
        self.joined = set()
        self.pages = []
        self.writes = []
        self.details = {row['id']: dict(row, content="比较快速排序和冒泡排序的效率。")
                        for row in discussions if isinstance(row, dict) and row.get('id')}
        self.list_response = self.evaluation_response = AUTO
        self.reply_response = self.submit_response = {"code": 200}

    def discussion_list(self, course_id, course_info_id, page_num=1, page_size=20):
        self.pages.append(page_num)
        if self.list_response is not AUTO:
            return self.list_response
        pending = [row for row in self.discussions if row.get('id') not in self.joined]
        start = (page_num - 1) * page_size
        return {"code": 200, "rows": pending[start:start + page_size], "total": len(pending)}

    def discussion_detail(self, discuss_id):
        return {"code": 200, "data": self.details.get(discuss_id)}

    def add_discussion_reply(self, course_info_id, course_id, discuss_id, content):
        self.writes.append(("reply", course_info_id, course_id, discuss_id, content))
        if self.reply_response == {"code": 200}:
            self.joined.add(discuss_id)
        return self.reply_response

    def course_evaluation(self, course_info_id):
        return ({"code": 200, "data": self.evaluation} if self.evaluation_response is AUTO
                else self.evaluation_response)

    def submit_course_evaluation(self, course_info_id, course_id, content, star=5):
        self.writes.append(("evaluation", course_info_id, course_id, content, star))
        if self.submit_response == {"code": 200}:
            self.evaluation = {"star": star, "content": content}
        return self.submit_response

    def study_design_list(self, *args):
        return []

    def study_record_list(self, *args):
        return []


class ActivityTests(unittest.TestCase):
    def run_course(self, client=None, ai=None, cancel=None):
        return CourseActivities(client or ActivityClient(), ai or FakeAI(),
                                cancel_event=cancel).run_course(COURSE)

    def test_discussion_uses_topic_content_and_limits_plain_text_to_20_characters(self):
        client = ActivityClient()
        ai = FakeAI(["<b>快速排序</b>\n" + "效率较高" * 20])
        result = self.run_course(client, ai)
        self.assertEqual(result['discussion_posted'], 1)
        self.assertEqual(client.writes[0][:4], ("reply", "ci", "c", "d"))
        text = client.writes[0][4]
        self.assertLessEqual(len(text), 20)
        self.assertNotIn('<', text)
        self.assertNotIn('\n', text)
        self.assertIn('比较快速排序和冒泡排序', ai.questions[0]['context'])
        self.assertNotIn('courseInfoId', str(ai.questions))

    def test_pending_pages_are_all_read_before_posts_and_duplicate_ids_only_post_once(self):
        rows = [dict(OPEN, id=str(i)) for i in range(25)] + [dict(OPEN, id="0")]
        client = ActivityClient(rows)
        ai = FakeAI(["快速排序效率较高"] * 26,
                    action=lambda: self.assertEqual(client.pages, [1, 2]))
        result = self.run_course(client, ai)
        self.assertEqual(result['discussion_posted'], 25)
        self.assertEqual(len(client.writes), 25)

    def test_joined_discussions_and_existing_evaluation_do_not_generate_or_post(self):
        client = ActivityClient()
        client.joined.add('d')
        ai = FakeAI()
        result = self.run_course(client, ai)
        self.assertEqual(client.writes, [])
        self.assertEqual(ai.questions, [])
        self.assertEqual(result['evaluation'], 'evaluated')

    def test_missing_or_invalid_evaluation_state_never_posts(self):
        for response in (None, {}, {"code": 200}, {"code": 500, "data": None},
                         {"data": None}, {"code": 200, "data": {}}, {"code": 200, "data": False}):
            with self.subTest(response=response):
                client = ActivityClient([])
                client.evaluation_response = response
                result = self.run_course(client)
                self.assertEqual(client.writes, [])
                self.assertEqual(result['evaluation'], 'failed')

    def test_five_star_evaluation_uses_ai_and_does_not_repeat_on_next_run(self):
        client, ai = ActivityClient([], evaluation=None), FakeAI(["知识体系清晰，内容丰富。"])
        result = self.run_course(client, ai)
        self.assertEqual(result['evaluation'], 'submitted')
        self.assertEqual(client.writes, [("evaluation", "ci", "c", "知识体系清晰，内容丰富。", 5)])
        self.assertIn('数据结构', ai.questions[0]['context'])
        self.run_course(client, ai)
        self.assertEqual(len(client.writes), 1)

    def test_empty_nontext_or_failed_ai_counts_as_failure(self):
        for answer in (None, [], 5, "   ", "<p></p>", AIError('服务不可用')):
            with self.subTest(answer=answer):
                client = ActivityClient()
                result = self.run_course(client, FakeAI([answer]))
                self.assertEqual(client.writes, [])
                self.assertEqual(result['discussion_failed'], 1)
                client = ActivityClient([], evaluation=None)
                result = self.run_course(client, FakeAI([answer]))
                self.assertEqual(client.writes, [])
                self.assertEqual(result['evaluation'], 'failed')

    def test_incomplete_or_failed_directory_blocks_all_discussion_writes(self):
        for response in (None, {"code": 500, "rows": [], "total": 0},
                         {"code": 200, "rows": [OPEN], "total": 2},
                         {"code": 200, "rows": [], "total": 1},
                         {"code": 200, "rows": [OPEN]},
                         {"code": 200, "rows": [OPEN], "total": 0},
                         {"code": 200, "rows": ["bad"], "total": 1}):
            with self.subTest(response=response):
                client = ActivityClient()
                client.list_response = response
                result = self.run_course(client)
                self.assertEqual(client.writes, [])
                self.assertEqual(result['discussion_failed'], 1)

    def test_unknown_or_rejected_write_is_never_retried_or_successful(self):
        for response in (None, {}, {"msg": "unknown"}, {"code": 500}):
            with self.subTest(response=response):
                client = ActivityClient()
                client.reply_response = response
                result = self.run_course(client)
                self.assertEqual(len(client.writes), 1)
                self.assertEqual(result['discussion_failed'], 1)
                client = ActivityClient([], evaluation=None)
                client.submit_response = response
                result = self.run_course(client)
                self.assertEqual(len(client.writes), 1)
                self.assertEqual(result['evaluation'], 'failed')

    def test_cancellation_during_ai_never_reaches_either_write(self):
        for discussions, evaluation in (([OPEN], DONE), ([], None)):
            with self.subTest(discussions=discussions):
                cancel = threading.Event()
                client = ActivityClient(discussions, evaluation)
                self.run_course(client, FakeAI(action=cancel.set), cancel)
                self.assertEqual(client.writes, [])

    def test_window_is_checked_again_after_ai_generation(self):
        client = ActivityClient()
        with patch('AIMoocMain.activities.discussion_open', side_effect=[True, True, False]):
            self.run_course(client)
        self.assertEqual(client.writes, [])

    def test_authentication_and_rate_limits_stop_before_other_writes(self):
        for response, error in (({"code": 401}, AuthenticationExpired),
                                ({"code": 429}, RequestRateLimited)):
            with self.subTest(response=response):
                client = ActivityClient(evaluation=None)
                client.list_response = response
                with self.assertRaises(error):
                    self.run_course(client)
                self.assertEqual(client.writes, [])

    def test_resource_complete_course_still_runs_activities_and_ai_failure_marks_course_error(self):
        for answer, expected in (("快速排序效率较高", 'success'), (None, 'error')):
            client, events = ActivityClient(), []
            AIMoocHandler(client=client, course_rows=[COURSE], activity_ai=FakeAI([answer]),
                          on_progress=events.append)
            self.assertEqual([e.status for e in events if e.kind == 'result'], [expected])

    def test_ai_service_failure_stops_further_ai_requests_and_marks_course_error(self):
        client = ActivityClient([OPEN, dict(OPEN, id='d2')], evaluation=None)
        ai, events = FakeAI([AIServiceError('鉴权失败')]), []
        handler = AIMoocHandler(client=client, course_rows=[COURSE], activity_ai=ai,
                                on_progress=events.append)
        self.assertEqual(len(ai.questions), 1)
        self.assertTrue(handler.cancel_event.is_set())
        self.assertEqual(client.writes, [])
        self.assertEqual([e.status for e in events if e.kind == 'result'], ['error'])

    def test_cancel_during_detail_does_not_start_ai_generation(self):
        client, cancel, ai = ActivityClient(), threading.Event(), FakeAI()
        original = client.discussion_detail
        def detail(discuss_id):
            cancel.set()
            return original(discuss_id)
        client.discussion_detail = detail
        self.run_course(client, ai, cancel)
        self.assertEqual(ai.questions, [])
        self.assertEqual(client.writes, [])

    def test_app_to_real_platform_api_submits_discussion_and_evaluation_offline(self):
        api = object.__new__(AIMoocApi)
        api.default_base_url = 'https://platform.invalid/prod-api'
        api.session = Mock()
        payloads = {
            '/course/courseDesign/getStudentDesignList': {'code': 200, 'data': []},
            '/course/studyRecord/completed/courseware': {'code': 200, 'data': []},
            '/course/courseInfoDiscuss/list': {'code': 200, 'rows': [OPEN], 'total': 1},
            '/course/courseInfoDiscuss/d': {'code': 200, 'data': dict(OPEN, content='比较排序效率')},
            '/course/courseInfoEvaluation/getEvaluation': {'code': 200, 'data': None},
        }
        def get(url, **kwargs):
            response = Mock(status_code=200)
            response.json.return_value = payloads[url.removeprefix(api.default_base_url)]
            return response
        api.session.get.side_effect = get
        response = Mock(status_code=200)
        response.json.return_value = {'code': 200}
        api.session.post.return_value = response
        view, store, ai = Mock(), Mock(), FakeAI()
        view.activity.side_effect = lambda *args: nullcontext()
        store.concurrency.return_value = {'study': 1}
        ai.session = Mock()
        app = Application(view, store=store)
        with patch.object(app, 'ai_configuration', return_value=Mock()), \
             patch.object(app, 'verify_ai', return_value=ai):
            app.study(0, api, [COURSE])
        writes = api.session.post.call_args_list
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0].args[0], api.default_base_url + '/course/courseInfoReply/add')
        self.assertEqual(writes[0].kwargs['json'], {
            'courseInfoId': 'ci', 'courseId': 'c', 'discussId': 'd',
            'content': '快速排序平均效率较高。', 'typeId': 1})
        self.assertEqual(writes[1].kwargs['json'], {
            'courseInfoId': 'ci', 'courseId': 'c', 'content': '课程结构清晰，内容实用。', 'star': 5})
        self.assertEqual(view.on_progress.call_args.args[0].status, 'success')
        ai.session.close.assert_called_once()

    def test_beijing_windows_server_state_and_invalid_times_are_not_bypassed(self):
        now = datetime.fromisoformat('2026-10-15T22:00:00+08:00')
        cases = [({}, True), ({'startTime': '2026-10-15 22:00:00'}, True),
                 ({'endTime': '2026-10-15 22:00:00'}, True),
                 ({'startTime': '2026-10-15 22:00:01'}, False),
                 ({'endTime': '2026-10-15 21:59:59'}, False),
                 ({'startTime': '2026-10-15T14:00:01Z'}, False),
                 ({'endTime': 'bad'}, False), ({'startTime': 123}, False),
                 ({'startTime': '2026-10-16', 'endTime': '2026-10-14'}, False),
                 ({'zt': 0}, False), ({'zt': '2'}, False)]
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertEqual(discussion_open(row, now=now), expected)


if __name__ == '__main__':
    unittest.main()
