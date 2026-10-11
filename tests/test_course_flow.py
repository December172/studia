"""Course-first interaction checks with scripted input and offline clients."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rich.console import Console

import theme
import ui as ui_module
from ui import TerminalUI, Cancelled
from app import Application
from settings import SettingsStore
from ai.client import AIConfig
from reporting import ProgressEvent


def make_view(answers, width=100):
    output = io.StringIO()
    inputs = iter(answers)
    view = TerminalUI(console=Console(file=output, width=width, theme=theme.THEME),
                      input_fn=lambda: next(inputs))
    return view, output


COURSES = [{"id": "ci1", "courseId": "c1", "courseName": "英语", "studySpeed": 82},
           {"id": "ci2", "courseId": "c2", "courseName": "计算机", "studySpeed": 36}]


class SelectionTests(unittest.TestCase):
    def test_search_all_only_selects_matching_rows(self):
        view, output = make_view(["/英语", "all"])
        self.assertEqual(view.courses(COURSES), [COURSES[0]])
        self.assertIn("1 项", output.getvalue())

    def test_empty_enter_does_not_cancel_selection(self):
        view, _ = make_view(["", "2"])
        self.assertEqual(view.courses(COURSES), [COURSES[1]])

    def test_back_is_distinct_from_quit(self):
        view, _ = make_view(["b", "q"])
        with self.assertRaises(Exception) as raised:
            view.ask("选择")
        self.assertEqual(type(raised.exception).__name__, "Back")
        with self.assertRaises(Cancelled):
            view.ask("选择")

    def test_single_course_rejects_multiple_then_enters_course(self):
        view, _ = make_view(["1,2", "2"])
        self.assertTrue(hasattr(view, "library"), "missing course-first library")
        self.assertEqual(view.library(COURSES, 0, {}), [COURSES[1]])

    def test_empty_search_all_does_not_select_hidden_courses(self):
        view, _ = make_view(["/不存在", "all", "/", "1"])
        self.assertEqual(view.courses(COURSES), [COURSES[0]])

    def test_task_filters_and_search_combine(self):
        catalog = [(COURSES[0], {"id": "a", "name": "第一章"}, 1),
                   (COURSES[0], {"id": "b", "name": "第一章"}, 3),
                   (COURSES[0], {"id": "c", "name": "第二章"}, 3)]
        view, _ = make_view(["f3", "/第一", "all"])
        self.assertEqual(view.papers(catalog), [catalog[1]])

    def test_returned_task_selection_is_default_and_filter_is_preserved(self):
        catalog = [(COURSES[0], {"id": "a", "name": "第一章"}, 1),
                   (COURSES[0], {"id": "b", "name": "第二章"}, 3)]
        view, output = make_view(["f3", "2", ""])
        state = {}
        self.assertTrue(hasattr(ui_module, "Back"), "missing navigation support")
        selected = view.papers(catalog, state=state)
        self.assertEqual(view.papers(catalog, selected=selected, state=state), [catalog[1]])
        self.assertIn("回车保留", output.getvalue())

    def test_narrow_library_keeps_platform_and_refresh_shortcuts_visible(self):
        view, output = make_view(["q"], width=52)
        with self.assertRaises(Cancelled):
            view.library(COURSES, 0, {})
        self.assertIn("t 切换平台", output.getvalue())
        self.assertIn("r 刷新", output.getvalue())

    def test_library_distinguishes_same_named_courses_by_term(self):
        courses = [{"courseName": "英语", "courseInfoName": "2026 秋季"},
                   {"courseName": "英语", "courseInfoName": "2025 秋季"}]
        view, output = make_view(["2"])
        self.assertEqual(view.library(courses, 0, {}), [courses[1]])
        self.assertIn("2026 秋季", output.getvalue())
        self.assertIn("2025 秋季", output.getvalue())


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.selected = [(COURSES[0], {"id": "a", "name": "第一章"}, 3)]

    def test_export_enter_uses_saved_options_without_more_questions(self):
        view, output = make_view([""])
        self.assertTrue(hasattr(view, "review_export"))
        options = {"export_format": "docx", "quiz_mode": "merged", "include_my_answer": True}
        self.assertEqual(view.review_export(self.selected, options), options)
        self.assertIn("Word", output.getvalue())
        self.assertIn("按课程合并", output.getvalue())

    def test_back_from_setting_editor_keeps_changes_in_summary(self):
        view, _ = make_view(["s", "1", "2", "b", ""])
        self.assertTrue(hasattr(view, "review_export"))
        options = view.review_export(self.selected, {})
        self.assertEqual(options["export_format"], "docx")

    def test_ai_summary_defaults_to_submission_and_keeps_only_execution_mode(self):
        config = AIConfig("https://example.org/v1", "model", "secret")
        view, output = make_view(["", "s", "1", ""])
        self.assertTrue(hasattr(view, "review_ai"))
        self.assertIs(view.review_ai(self.selected, config, {}), True)
        self.assertIs(view.review_ai(self.selected, config, {"auto_submit": False}), False)
        self.assertIn("不交卷，无成绩", output.getvalue())
        self.assertNotIn("secret", output.getvalue())

    def test_legacy_ai_settings_choose_only_execution_mode(self):
        view, _ = make_view(["", ""])
        self.assertIs(view.answer_settings(), True)
        view, _ = make_view(["1", ""])
        self.assertIs(view.answer_settings(), False)

    def test_local_answers_result_explains_no_platform_grade(self):
        view, output = make_view([""])
        view.begin_operation()
        view.on_progress(ProgressEvent("result", "第一章", status="preview"))
        self.assertEqual(view.operation_result(), 1)
        self.assertIn("答案已生成 · 未交卷", output.getvalue())
        self.assertIn("不会产生成绩", output.getvalue())
        self.assertIn("答题并自动交卷", output.getvalue())

    def test_result_page_shows_paths_errors_and_resets_next_operation(self):
        view, output = make_view([""])
        self.assertTrue(hasattr(view, "begin_operation"))
        view.begin_operation()
        view.on_progress(ProgressEvent("result", "第一章", status="error"))
        view.log("磁盘已满 token=never-show", "error")
        self.assertEqual(view.operation_result(["/tmp/课程/答案.md"]), 1)
        self.assertIn("磁盘已满", output.getvalue())
        self.assertIn("/tmp/课程/答案.md", output.getvalue())
        self.assertNotIn("never-show", output.getvalue())
        view.begin_operation()
        self.assertEqual(dict(view.counts), {})
        self.assertEqual(view.results, [])
        self.assertEqual(view.errors, [])

    def test_ai_configuration_back_returns_to_previous_field(self):
        view, output = make_view(["1", "https://first.example/v1", "b", "https://second.example/v1", "model"])
        view.secret_fn = lambda _: "new-key"
        try:
            config = view.ai_config()
        except ui_module.Back:
            self.fail("b at Model ID left the whole configuration instead of returning to URL")
        self.assertEqual(config.base_url, "https://second.example/v1")
        self.assertNotIn("new-key", output.getvalue())


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SettingsStore(Path(self.temp.name) / "settings")
        self.store.save_session("mooc", Mock(access_token="cached"))
        self.store.save_ai(AIConfig("https://example.org/v1", "model", "secret"))
        self.client = Mock(access_token="cached", user_id="user", default_base_url="https://platform.invalid")
        self.client.my_course_list.return_value = {"rows": COURSES}

    def run_workspace(self, answers, catalog=(), result=None):
        view, output = make_view(answers)
        app = Application(view, self.store)
        api = Mock()
        api.catalog.side_effect = lambda course, category: [item for owner, item, cat in catalog
                                                          if owner == course and cat == category]
        api.user_id = "user"
        runs = []

        def study(**kwargs):
            runs.append(kwargs["course_rows"])
            view.on_progress(ProgressEvent("result", "课程", status="success"))

        with patch.object(app, "login", side_effect=ui_module.Back()), \
             patch("app.AIMoocApi", return_value=self.client), \
             patch("app.ZYKMoocApi") as zyk, patch("app.AIMoocHandler", side_effect=study), \
             patch("app.ExamAPI", return_value=api), patch("app.AIClient"), \
             patch("app.AnswerRunner") as runner:
            runner.return_value.run_paper.return_value = result or {"status": "preview", "paper": "第一章"}
            runner.return_value.reused = False
            with self.assertRaises(Cancelled):
                app.run()
        return view, output.getvalue(), runs, runner

    def test_startup_enters_course_library_without_function_menu(self):
        _, text, _, _ = self.run_workspace(["q"])
        self.assertIn("我的课程", text)
        self.assertNotIn("选择功能", text)
        self.client.my_course_list.assert_called_once()

    def test_two_operations_reuse_course_and_reset_counts(self):
        view, text, runs, _ = self.run_workspace(["1", "1", "", "", "1", "", "", "b", "q"])
        self.assertEqual(runs, [[COURSES[0]], [COURSES[0]]])
        self.assertEqual(view.counts["success"], 1)
        self.assertEqual(text.count("成功 1"), 2)
        self.client.my_course_list.assert_called_once()

    def test_back_from_ai_summary_preserves_selected_papers(self):
        catalog = [(COURSES[0], {"id": "a", "name": "第一章"}, 3)]
        _, text, _, runner = self.run_workspace(["1", "2", "1", "\x1b", "", "", "", "q"], catalog)
        self.assertIn("回车保留", text)
        self.assertEqual(runner.return_value.run_paper.call_count, 1)

    def test_report_only_override_returns_to_auto_submit_next_operation(self):
        catalog = [(COURSES[0], {"id": "a", "name": "第一章"}, 3)]
        _, text, _, runner = self.run_workspace(
            ["1", "2", "1", "s", "1", "", "", "2", "1", "", "", "q"], catalog)
        self.assertEqual([call.kwargs["auto_submit"] for call in runner.call_args_list], [False, True])
        self.assertIsNone(self.store.get("auto_submit"))

    def test_unfinished_question_is_reported_without_asking_for_retry_notice(self):
        catalog = [(COURSES[0], {"id": "a", "name": "第一章"}, 3)]
        view, text, _, runner = self.run_workspace(["1", "2", "1", "", "", "q"], catalog,
            {"status": "needs_review", "paper": "第一章",
             "answers": [{"error": "单题重试 2 次后仍失败：bad JSON"}]})
        runner.return_value.run_paper.assert_called_once()
        # The run continues to the next paper instead of pausing for a human.
        self.assertNotIn("待补答", text)
        self.assertIn("bad JSON", text)

    def test_settings_logout_invalidates_live_client(self):
        _, text, _, _ = self.run_workspace(["s", "3", "\x1b", "q"])
        self.assertIsNone(self.store.session("mooc"))
        self.assertTrue("登录" in text)

    def test_empty_library_still_allows_settings(self):
        self.client.my_course_list.return_value = {"rows": []}
        _, text, _, _ = self.run_workspace(["s", "b", "q"])
        self.assertIn("我的课程", text)
        self.assertIn("本机设置", text)

    def test_no_tasks_returns_to_same_course_without_running_ai(self):
        _, text, _, runner = self.run_workspace(["1", "2", "", "q"])
        self.assertIn("暂无作业", text)
        runner.assert_not_called()

    def test_courses_open_one_at_a_time_without_batch_shortcut(self):
        _, text, runs, _ = self.run_workspace(["m", "1", "1", "", "2", "q"])
        self.assertEqual(runs, [[COURSES[0]]])
        self.assertNotIn("批量选择", text)
        self.assertNotIn("批量操作请使用 m", text)

    def test_export_workspace_writes_only_chosen_paper_and_returns_to_course(self):
        from AIMoocMain.export_questions import QuizExportHandler
        import course_flow
        catalog = [(COURSES[0], {"id": "a", "name": "第一章"}, 3),
                   (COURSES[0], {"id": "b", "name": "第二章"}, 3)]
        self.client.get_exam_paper.return_value = {"questions": [{"id": "q", "typeId": 3, "title": "判断题", "stdAnswer": "0"}]}
        self.client.get_exam_student_records.return_value = []
        target = Path(self.temp.name) / "exports"
        def export(**kwargs):
            return QuizExportHandler(output_dir=str(target), **kwargs)
        with patch.object(course_flow, "QuizExportHandler", side_effect=export):
            _, text, _, _ = self.run_workspace(["1", "3", "2", "", "", "q"], catalog)
        files = list(target.rglob("*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("第二章", files[0].read_text())
        self.assertNotIn("第一章", files[0].read_text())
        self.assertIn(str(files[0].resolve()), text.replace("\n", ""))

    def test_stopped_ai_queue_does_not_list_old_reports_for_unprocessed_tasks(self):
        catalog = [(COURSES[0], {"id": "a", "name": "第一章"}, 3),
                   (COURSES[0], {"id": "b", "name": "第二章"}, 3)]
        root = Path(self.temp.name)
        (root / "a.md").write_text("结果未知")
        (root / "b.md").write_text("旧报告")
        with patch("course_flow.report_path", side_effect=lambda directory, user, course, exam: root / (exam + ".json")):
            _, text, _, runner = self.run_workspace(["1", "2", "all", "", "", "q"], catalog,
                                                    {"status": "submission_unknown", "paper": "第一章"})
        self.assertEqual(runner.return_value.run_paper.call_count, 1)
        self.assertNotIn("b.md", text)
        self.assertIn("a.md", text)

    def test_fatal_error_after_completed_operation_is_not_silenced(self):
        import StartWork
        view, output = make_view([])
        view.workspace = True
        view.begin_operation()
        view.on_progress(ProgressEvent("result", "上一轮", status="success"))
        view.finish()
        with patch("app.Application.run", side_effect=RuntimeError("读取配置失败")):
            StartWork.main(view)
        self.assertIn("读取配置失败", output.getvalue())


if __name__ == "__main__":
    unittest.main()
