"""MOOC course activities: fixed API wrappers, entry wiring, and summary wording."""
import io
import unittest
from unittest.mock import Mock, patch

from rich.console import Console

import native_ui
import theme
from AIMoocMain.api import AIMoocApi
from app import Application
from ui import Back, TerminalUI

RAW_BODY = {"code": 200, "msg": "ok", "data": {"total": 1, "rows": [{"id": "d1"}]}}


def make_api(body=None):
    """A real AIMoocApi that never logs in; the HTTP session is a fake transport."""
    api = object.__new__(AIMoocApi)
    api.session = Mock()
    api.default_base_url = "https://example.invalid/prod-api"
    api.access_token = None
    api.user_id = "u1"
    response = Mock(status_code=200)
    response.json.return_value = RAW_BODY if body is None else body
    api.session.get.return_value = response
    api.session.post.return_value = response
    return api


class DiscussionApiTests(unittest.TestCase):
    def test_discussion_list_sends_fixed_filters_and_returns_raw_envelope(self):
        api = make_api()
        result = api.discussion_list("c1", "ci1", 3, 50)
        url, = api.session.get.call_args.args
        self.assertTrue(url.endswith("/course/courseInfoDiscuss/list"))
        self.assertEqual(api.session.get.call_args.kwargs["params"], {
            "courseId": "c1", "courseInfoId": "ci1", "pageNum": 3, "pageSize": 50,
            "discussType": 4, "queryUser": 2, "joinStatus": 0,
        })
        # Raw response: the whole envelope, not the unwrapped data field.
        self.assertEqual(result, RAW_BODY)
        self.assertNotEqual(result, RAW_BODY["data"])

    def test_discussion_list_defaults_to_first_page_of_twenty(self):
        api = make_api()
        api.discussion_list("c1", "ci1")
        self.assertEqual(api.session.get.call_args.kwargs["params"]["pageNum"], 1)
        self.assertEqual(api.session.get.call_args.kwargs["params"]["pageSize"], 20)

    def test_discussion_detail_uses_the_id_in_the_path(self):
        api = make_api()
        result = api.discussion_detail("99")
        url, = api.session.get.call_args.args
        self.assertTrue(url.endswith("/course/courseInfoDiscuss/99"))
        self.assertIsNone(api.session.get.call_args.kwargs["params"])
        self.assertEqual(result, RAW_BODY)

    def test_add_discussion_reply_payload(self):
        api = make_api()
        api.add_discussion_reply("ci1", "c1", "d1", "一条回复")
        url, = api.session.post.call_args.args
        self.assertTrue(url.endswith("/course/courseInfoReply/add"))
        self.assertEqual(api.session.post.call_args.kwargs["json"], {
            "courseInfoId": "ci1", "courseId": "c1", "discussId": "d1",
            "content": "一条回复", "typeId": 1,
        })

    def test_course_evaluation_read_and_submit_payloads(self):
        api = make_api()
        api.course_evaluation("ci1")
        url, = api.session.get.call_args.args
        self.assertTrue(url.endswith("/course/courseInfoEvaluation/getEvaluation"))
        self.assertEqual(api.session.get.call_args.kwargs["params"], {"courseInfoId": "ci1"})
        api.submit_course_evaluation("ci1", "c1", "很好", star=5)
        url, = api.session.post.call_args.args
        self.assertTrue(url.endswith("/course/courseInfoEvaluation/add"))
        self.assertEqual(api.session.post.call_args.kwargs["json"], {
            "courseInfoId": "ci1", "courseId": "c1", "star": 5, "content": "很好",
        })


class StudyWiringTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.view = TerminalUI(console=Console(file=self.output, theme=theme.THEME))
        self.store = Mock()
        self.store.concurrency.return_value = {"ai": 1, "study": 1}
        self.app = Application(self.view, store=self.store)
        self.client = Mock(access_token="cached")
        self.courses = [{"id": "ci1", "courseId": "c1", "courseName": "英语"}]

    def test_mooc_injects_verified_ai_and_closes_its_session(self):
        config, ai_client = Mock(), Mock()
        with patch.object(self.app, "ai_configuration", return_value=config) as configured, \
             patch.object(self.app, "verify_ai", return_value=ai_client) as verified, \
             patch("app.AIMoocHandler") as handler:
            self.app.study(0, self.client, self.courses)
        configured.assert_called_once_with()
        verified.assert_called_once_with(config)
        self.assertEqual(handler.call_args.kwargs["activity_ai"], ai_client)
        self.assertEqual(handler.call_args.kwargs["client"], self.client)
        self.assertEqual(handler.call_args.kwargs["course_rows"], self.courses)
        ai_client.session.close.assert_called_once()

    def test_mooc_closes_the_ai_session_when_studying_fails(self):
        ai_client = Mock()
        with patch.object(self.app, "ai_configuration", return_value=Mock()), \
             patch.object(self.app, "verify_ai", return_value=ai_client), \
             patch("app.AIMoocHandler", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.app.study(0, self.client, self.courses)
        ai_client.session.close.assert_called_once()

    def test_failed_connection_closes_the_new_ai_session(self):
        from ai.client import AIError, AIConfig
        ai_client = Mock()
        ai_client.check_connection.side_effect = AIError('鉴权失败')
        with patch.object(self.app, 'ai_configuration',
                          return_value=AIConfig('https://example.invalid/v1', 'model', 'fake')), \
             patch('app.AIClient', return_value=ai_client):
            with self.assertRaises(AIError):
                self.app.study(0, self.client, self.courses)
        ai_client.session.close.assert_called_once()

    def test_library_never_touches_ai_configuration(self):
        with patch.object(self.app, "ai_configuration") as configured, \
             patch.object(self.app, "verify_ai") as verified, \
             patch("app.ZYKMoocHandler") as handler:
            self.app.study(1, self.client, self.courses)
        configured.assert_not_called()
        verified.assert_not_called()
        self.assertNotIn("activity_ai", handler.call_args.kwargs)

    def test_ai_configuration_failure_is_raised_not_silently_skipped(self):
        with patch.object(self.app, "ai_configuration", side_effect=RuntimeError("无配置")), \
             patch("app.AIMoocHandler") as handler:
            with self.assertRaises(RuntimeError):
                self.app.study(0, self.client, self.courses)
        handler.assert_not_called()

    def test_ai_verification_failure_is_raised_not_silently_skipped(self):
        from ai.client import AIError
        with patch.object(self.app, "ai_configuration", return_value=Mock()), \
             patch.object(self.app, "verify_ai", side_effect=AIError("连接失败")), \
             patch("app.AIMoocHandler") as handler:
            with self.assertRaises(AIError):
                self.app.study(0, self.client, self.courses)
        handler.assert_not_called()

    def test_cancelled_selection_never_tests_ai(self):
        with patch.object(self.app, "ai_configuration") as configured, \
             patch("app.AIMoocHandler") as handler:
            self.app.study(0, self.client, [])
        configured.assert_not_called()
        handler.assert_not_called()
        self.assertTrue(self.view.cancelled)


class SummaryWordingTests(unittest.TestCase):
    def view(self, answers=("开始",)):
        output = io.StringIO()
        values = iter(answers)
        return TerminalUI(console=Console(file=output, theme=theme.THEME),
                          input_fn=lambda: next(values)), output

    def test_line_input_summary_states_activity_scope_only_for_mooc(self):
        view, output = self.view()
        view.review_study([{"courseName": "课程"}], max_workers=3, activities=True)
        rendered = output.getvalue()
        self.assertIn("讨论由AI生成，最多20字；课程评价五星，评语由AI生成", rendered)
        self.assertIn("完成课程学习任务", rendered)
        self.assertNotIn("同时处理", rendered)

        view, output = self.view()
        view.review_study([{"courseName": "课程"}], max_workers=3)
        self.assertNotIn("讨论由AI生成", output.getvalue())

    def test_native_summary_states_activity_scope_only_for_mooc(self):
        console = Console(file=io.StringIO(), force_terminal=True, theme=theme.THEME)
        view = TerminalUI(console=console)
        for activities, expected in ((True, True), (False, False)):
            with self.subTest(activities=activities), \
                 patch("sys.stdin.isatty", return_value=True), \
                 patch.object(native_ui, "action_page") as page:
                page.return_value = native_ui.PageResult("start", "start")
                view.review_study([{"courseName": "课程"}], activities=activities)
            lines = page.call_args.args[1]
            text = "\n".join(lines() if callable(lines) else lines)
            self.assertEqual("讨论由AI生成，最多20字；课程评价五星，评语由AI生成" in text, expected)

    def test_course_loop_passes_platform_activity_flag(self):
        import course_flow
        for platform, expected in ((0, True), (1, False)):
            with self.subTest(platform=platform):
                view = Mock()
                view.course_menu.side_effect = [1, Back()]
                view.operation_result.return_value = 1
                app = Mock()
                app.store.concurrency.return_value = {"ai": 1, "study": 1}
                app.study.return_value = None
                workspace = course_flow.CourseWorkspace(app)
                workspace.view = view
                workspace.store = app.store
                workspace.platform = platform
                workspace.course_loop([{"courseName": "课程"}])
                self.assertIs(view.review_study.call_args.kwargs["activities"], expected)


if __name__ == "__main__":
    unittest.main()
