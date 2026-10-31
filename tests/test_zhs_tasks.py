"""智慧课程 AI 答题接入课程任务流程（离线）。"""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rich.console import Console

import theme
from app import Application
from base.auth import AuthenticationExpired
from course_flow import CourseWorkspace, task_identity
from settings import SettingsStore
from ui import Cancelled, TerminalUI
from ZHSMain.api import CaptchaRequired

COURSE = {"courseName": "课程", "zhs": {"courseId": 1, "classId": 2}}


def make_view(answers=()):
    output = io.StringIO()
    inputs = iter(answers)
    return TerminalUI(console=Console(file=output, theme=theme.THEME, width=120),
                      input_fn=lambda: next(inputs)), output


def task(name, item_id="a", category=1):
    return (COURSE, {"id": item_id, "name": name, "zhs": {}}, category)


class CourseMenuTest(unittest.TestCase):
    def menu(self, platform):
        view, _ = make_view(["q"])
        captured = []
        with patch.object(view, "choice", side_effect=lambda title, choices, **kw:
                          captured.extend(choices) or 1):
            view.course_menu([{"courseName": "课程"}], platform)
        return [name for _, name, _ in captured]

    def test_zhs_has_ai_answer_but_no_export(self):
        names = self.menu(2)
        self.assertIn("AI 答题", names)
        self.assertNotIn("导出题目", names)

    def test_resource_library_keeps_study_only(self):
        self.assertEqual(self.menu(1), ["自动学习"])


class CatalogTest(unittest.TestCase):
    def test_merges_multiple_courses(self):
        view, _ = make_view()
        app = Application(view, store=Mock())
        first = [(COURSE, {"id": "1", "name": "A"}, 1)]
        second = [(dict(COURSE, courseName="课程二"), {"id": "2", "name": "B"}, 2)]
        with patch("ZHSMain.answer.catalog", side_effect=[first, second]) as catalog:
            result = app.zhs_task_catalog(Mock(), [COURSE, dict(COURSE, courseName="课程二")])
        self.assertEqual(result, first + second)
        self.assertEqual(catalog.call_count, 2)


class ExecuteTest(unittest.TestCase):
    def setUp(self):
        view, _ = make_view()
        self.events = []
        view.on_progress = self.events.append
        self.store = Mock()
        self.store.path = Path(tempfile.mkdtemp()) / "settings.json"
        self.store.concurrency.return_value = {"ai": 5, "study": 1}
        self.app = Application(view, store=self.store)
        self.ai_session = Mock()
        self.ai_client = Mock(session=self.ai_session)
        self.app.verify_ai = Mock(return_value=self.ai_client)

    def run_execute(self, outcomes, auto_submit=True):
        runner = Mock()
        runner.run_paper.side_effect = outcomes
        with patch("app.ZHSAnswerRunner", return_value=runner) as factory:
            results = self.app.execute_zhs_ai(Mock(), [task("一"), task("二")], Mock(), auto_submit)
        self.runner = runner
        self.factory = factory
        return results

    def test_passes_ai_concurrency_to_runner(self):
        self.run_execute([{"paper": "一", "status": "submitted", "message": "ok"},
                          {"paper": "二", "status": "submitted", "message": "ok"}])
        self.assertEqual(self.factory.call_args.kwargs["max_workers"], 5)

    def test_two_papers_and_single_verification(self):
        results = self.run_execute([
            {"paper": "一", "status": "submitted", "message": "ok"},
            {"paper": "二", "status": "preview", "message": "ok"},
        ])
        self.assertEqual([r["status"] for r in results], ["submitted", "preview"])
        self.assertEqual(self.app.verify_ai.call_count, 1)
        self.assertEqual(self.runner.run_paper.call_count, 2)
        self.assertIn("result", [e.kind for e in self.events])
        self.ai_session.close.assert_called_once()

    def test_ai_error_stops_queue(self):
        results = self.run_execute([{"paper": "一", "status": "ai_error", "message": "bad"}])
        self.assertEqual(len(results), 1)
        self.assertEqual(self.runner.run_paper.call_count, 1)
        self.ai_session.close.assert_called_once()

    def test_submission_unknown_stops_queue(self):
        results = self.run_execute([{"paper": "一", "status": "submission_unknown", "message": "?"}])
        self.assertEqual(len(results), 1)
        self.assertEqual(self.runner.run_paper.call_count, 1)

    def test_captcha_records_error_and_stops(self):
        results = self.run_execute([CaptchaRequired("需要验证码")])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("验证码", results[0]["message"])
        self.assertEqual(self.runner.run_paper.call_count, 1)
        self.ai_session.close.assert_called_once()

    def test_authentication_expired_propagates(self):
        with self.assertRaises(AuthenticationExpired):
            self.run_execute([AuthenticationExpired("expired")])


def review_ai(selected, config, options, **kwargs):
    options["config"] = config
    return True


class WorkspaceTest(unittest.TestCase):
    def test_zhs_uses_zhs_catalog_and_executor(self):
        view, _ = make_view(["all", "1", "q"])
        store = Mock()
        store.get.side_effect = lambda key, default=None: default
        app = Application(view, store=store)
        app.execute_zhs_ai = Mock(return_value=[])
        app.execute_ai = Mock()
        app.task_catalog = Mock()
        app.report_path = Mock()
        catalog = [(COURSE, {"id": "h1", "name": "作业"}, 1)]
        app.zhs_task_catalog = Mock(return_value=catalog)
        app.ai_configuration = Mock(return_value=Mock())
        app.ai_status_text = Mock(return_value="未测试")
        workspace = CourseWorkspace(app)
        workspace.platform = 2
        view.review_ai = Mock(side_effect=review_ai)
        view.operation_result = Mock(return_value=1)
        workspace.tasks([COURSE], 2, {})
        app.zhs_task_catalog.assert_called_once()
        app.execute_zhs_ai.assert_called_once()
        app.task_catalog.assert_not_called()
        app.execute_ai.assert_not_called()

    def test_zhs_answer_returns_no_paths(self):
        view, _ = make_view()
        store = Mock()
        app = Application(view, store=store)
        captured = []

        def answer(operation, title=None):
            captured.extend(operation() or [])
            return 1
        workspace = CourseWorkspace(app)
        workspace.platform = 2
        workspace.execute = answer
        app.zhs_task_catalog = Mock(return_value=[(COURSE, {"id": "h1", "name": "作业"}, 1)])
        app.execute_zhs_ai = Mock(return_value=[])
        app.ai_configuration = Mock(return_value=Mock())
        app.ai_status_text = Mock(return_value="未测试")
        view.papers = Mock(return_value=[(COURSE, {"id": "h1", "name": "作业"}, 1)])
        view.review_ai = Mock(side_effect=review_ai)
        workspace.tasks([COURSE], 2, {})
        app.execute_zhs_ai.assert_called_once()
        self.assertEqual(captured, [])


class IdentityTest(unittest.TestCase):
    def test_zhs_courses_are_distinct(self):
        first = (dict(COURSE, zhs={"courseId": 1, "classId": 2}),
                 {"id": "hw-1-11-22"}, 1)
        second = (dict(COURSE, zhs={"courseId": 9, "classId": 2}),
                  {"id": "hw-1-11-22"}, 1)
        self.assertNotEqual(task_identity(first), task_identity(second))

    def test_different_knowledge_points_are_distinct(self):
        home = dict(COURSE, zhs={"courseId": 1, "classId": 2})
        one = (home, {"id": "hw-1-11-22"}, 1)
        two = (home, {"id": "hw-2-11-22"}, 1)
        self.assertNotEqual(task_identity(one), task_identity(two))


if __name__ == "__main__":
    unittest.main()
