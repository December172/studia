"""Offline UI and orchestration integration checks. Never access a real account."""
import io
import logging
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from rich.console import Console
from rich.cells import cell_len

import theme
from app import Application
from reporting import ProgressEvent, Reporter
from ui import TerminalUI, Cancelled, legacy_input, run_console_tool
from AIMoocMain.export_questions import QuizExportHandler
from AIMoocMain.main import AIMoocHandler
from ZYKMoocMain.main import ZYKMoocHandler


def view(width=100, **kwargs):
    output = io.StringIO()
    return TerminalUI(console=Console(file=output, width=width, theme=theme.THEME, color_system=None), **kwargs), output


class RenderingTests(unittest.TestCase):
    def test_progress_does_not_wrap_with_wide_terminal_symbol_glyphs(self):
        ui, output = view(120)
        ui.on_progress(ProgressEvent("begin", "等待浏览器登录", total=None))
        ui.console.print(ui.render())
        row = output.getvalue().splitlines()[0]
        # Some macOS fonts render braille/thick bar glyphs as two cells.
        # One extra cell wraps the line and leaves a ghost on each refresh.
        for double_width in ("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", "━╸╺"):
            physical_width = sum(2 if char in double_width else cell_len(char) for char in row)
            self.assertLessEqual(physical_width, 120)

    def test_progress_keeps_readable_task_name_and_one_row(self):
        ui, output = view(80)
        ui.on_progress(ProgressEvent("begin", "音乐课程\n模拟任务", total=12))
        ui.console.print(ui.render())
        self.assertIn("音乐课程 模拟任务", output.getvalue().splitlines()[0])
        self.assertEqual(len(output.getvalue().splitlines()), 11)

    def test_legacy_tool_input_pauses_live_and_cleanup_on_failure(self):
        ui, output = view(input_fn=Mock(return_value="answer"))
        def operation():
            with patch.object(ui.live, "stop", wraps=ui.live.stop) as stop, \
                 patch.object(ui.live, "start", wraps=ui.live.start) as start:
                self.assertEqual(legacy_input("旧脚本提示"), "answer")
                stop.assert_called_once()
                start.assert_called_once()
            raise ValueError("token=never-show-token")
        run_console_tool(operation, "辅助工具", ui)
        self.assertIsNone(ui.live)
        self.assertEqual(output.getvalue().count("总用时"), 1)
        self.assertIn("失败 1", output.getvalue())
        self.assertNotIn("never-show-token", output.getvalue())

    def test_eight_physical_log_rows_truncate_wide_text_and_literal_markup(self):
        ui, output = view(52)
        ui.on_progress(ProgressEvent("begin", "任务名称" * 60, total=20))
        for index in range(20):
            ui.log(f"ROW-{index:02d} " + "中文[b]文字[/b]" * 80)
        ui.console.print(ui.render())
        text = output.getvalue()
        self.assertNotIn("ROW-11", text)
        for index in range(12, 20):
            self.assertIn(f"ROW-{index:02d}", text)
        self.assertIn("[b]", text)
        self.assertEqual(len(text.splitlines()), 11)  # progress + 8 rows + 2 borders
        self.assertTrue(all(cell_len(line) <= 52 for line in text.splitlines()))

    def test_multiline_and_control_characters_cannot_grow_panel(self):
        ui, output = view()
        ui.log("\n".join(f"line{i}" for i in range(15)) + "\x1b[2J\x1b]0;evil\x07")
        self.assertEqual(len(ui.lines), 8)
        ui.console.print(ui.render())
        self.assertNotIn("\x1b", output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 11)

    def test_nonterminal_has_only_summary_after_activity_and_handler_restored(self):
        ui, output = view()
        root = logging.getLogger()
        before = list(root.handlers)
        with ui.session():
            with ui.activity("任务"):
                logging.getLogger("demo").warning("ONLY-IN-PANEL")
                ui.on_progress(ProgressEvent("result", "任务", status="error"))
            ui.finish()
            ui.finish()
        self.assertEqual(root.handlers, before)
        self.assertNotIn("ONLY-IN-PANEL", output.getvalue())
        self.assertEqual(len(output.getvalue().strip().splitlines()), 1)
        self.assertIn("成功 0 · 失败 1", output.getvalue())

    def test_summary_does_not_call_skipped_unknown_or_needs_review_success(self):
        ui, output = view(180)
        for status in ["preview", "submitted", "skipped", "needs_review", "submission_unknown"]:
            ui.on_progress(ProgressEvent("result", "任务", status=status))
        ui.finish()
        self.assertIn("成功 2 · 失败 2", output.getvalue())
        self.assertIn("跳过 1", output.getvalue())
        self.assertIn("未完成 1", output.getvalue())
        self.assertNotIn("待人工", output.getvalue())
        self.assertIn("结果未知 1", output.getvalue())

    def test_observer_failures_do_not_escape(self):
        observer = Reporter(Mock(side_effect=RuntimeError("render failed")))
        observer.begin("任务", 1)
        observer.result("任务", "success")

    def test_dev_log_redacts_credentials(self):
        ui, _ = view()
        ui.log('URL ?token=secret-token&x=1 password=secret-password Authorization: Bearer secret-key')
        text = str(ui.lines)
        for value in ("secret-token", "secret-password", "secret-key"):
            self.assertNotIn(value, text)

    def test_search_and_pagination_keep_original_identifiers(self):
        ui, _ = view(input_fn=Mock(side_effect=["n", "/课程14", "15"]))
        items = [{"courseName": f"课程{i}"} for i in range(25)]
        self.assertEqual(ui.courses(items), [items[14]])

    def test_cancel_and_hidden_key_never_echo(self):
        ui, output = view(input_fn=Mock(side_effect=["1", "https://example.org/v1", "model"]),
                          secret_fn=Mock(return_value="never-show-key"))
        self.assertEqual(ui.ai_config().api_key, "never-show-key")
        self.assertNotIn("never-show-key", output.getvalue())
        ui.input_fn = Mock(return_value="q")
        with self.assertRaises(Cancelled):
            ui.home()

    def test_interrupt_cleans_live_and_emits_one_summary(self):
        import StartWork
        ui, output = view()
        def interrupt():
            with ui.activity("打断测试"):
                raise KeyboardInterrupt()
        with patch("app.Application.run", side_effect=interrupt):
            StartWork.main(ui)
        self.assertIsNone(ui.live)
        self.assertIn("已取消", output.getvalue())
        self.assertEqual(output.getvalue().count("总用时"), 1)


class ObservationTests(unittest.TestCase):
    def export(self, tmp, events, merged=False):
        api = Mock()
        api.user_id = "user"
        api.get_exam_list_by_student.return_value = {"rows": [{"id": "a", "name": "卷A"}, {"id": "b", "name": "卷B"}]}
        api.get_exam_paper.return_value = {"questions": [{"id": "q", "typeId": 3, "title": "判断", "stdAnswer": "0"}]}
        api.get_exam_student_records.return_value = []
        handler = QuizExportHandler(client=api, course_rows=[{"id": "ci", "courseId": "c", "courseName": "课程"}],
                                    category_ids=[3], output_dir=tmp, on_progress=events.append,
                                    quiz_mode="merged" if merged else "per_chapter", export_format="both")
        return api, handler

    def test_export_success_only_after_all_formats_written(self):
        for merged in (False, True):
            with self.subTest(merged=merged), tempfile.TemporaryDirectory() as tmp:
                events = []
                self.export(tmp, events, merged)
                self.assertEqual([e.status for e in events if e.kind == "result"], ["success", "success"])
                self.assertTrue(list(Path(tmp).rglob("*.docx")))
            with self.subTest(failed=merged), tempfile.TemporaryDirectory() as tmp:
                events = []
                with patch("AIMoocMain.export_questions.write_questions_docx", side_effect=OSError("disk full")):
                    self.export(tmp, events, merged)
                self.assertNotIn("success", [e.status for e in events if e.kind == "result"])
                self.assertEqual(sum(e.status == "error" for e in events), 2 if merged else 1)

    def test_study_observer_preserves_calls_and_partial_failure(self):
        for cls, module, kind in [(AIMoocHandler, "AIMoocMain.main", "ppt"), (ZYKMoocHandler, "ZYKMoocMain.main", "zip")]:
            for callback_fails in (False, True):
                with self.subTest(cls=cls, callback_fails=callback_fails):
                    api = Mock()
                    course = {"id": "ci", "courseInfoId": "ci", "courseId": "c", "courseName": "课程"}
                    nodes = [{"id": "a", "courseId": "c", "courseInfoId": "ci", "parentId": "p", "fileType": kind, "name": "第一节"},
                             {"id": "b", "courseId": "c", "courseInfoId": "ci", "parentId": "p", "fileType": kind, "name": "第二节"}]
                    api.study_design_list.return_value = [{"id": "p"}] if cls is AIMoocHandler else nodes
                    api.study_record_list.return_value = []
                    api.get_cell_list.return_value = nodes
                    api.course_content.return_value = {}
                    api.study_record.side_effect = [{"msg": "ok"}, RuntimeError("failed")]
                    events = []
                    callback = Mock(side_effect=RuntimeError()) if callback_fails else events.append
                    with patch(module + ".time.sleep"), patch(module + ".random.randint", return_value=1), patch(module + ".random.uniform", return_value=1):
                        cls(client=api, course_rows=[course], on_progress=callback)
                    self.assertEqual(api.study_record.call_count, 2)
                    if not callback_fails:
                        self.assertEqual([e.status for e in events if e.kind == "result"], ["error"])

    def test_ai_queue_still_stops_on_unknown_and_does_not_call_next_paper(self):
        ui, _ = view()
        course = {"id": "ci", "courseId": "c", "courseName": "课程"}
        selected = [(course, {"id": "a", "name": "A"}, 1), (course, {"id": "b", "name": "B"}, 1)]
        store = Mock()
        store.concurrency.return_value = {'ai': 8, 'study': 8}
        from ai.client import AIConfig
        store.ai_config.return_value = AIConfig('https://example.org/v1', 'test-model', 'synthetic-key')
        app = Application(ui, store=store)
        runner = Mock(reused=True)
        runner.run_paper.return_value = {"status": "submission_unknown", "paper": "A"}
        with patch.object(app, 'client_and_courses', return_value=(Mock(), [course])), \
             patch.object(ui, 'categories', return_value=[1]), patch.object(ui, 'papers', return_value=selected), \
             patch.object(ui, 'ai_config', return_value=Mock()), patch.object(ui, 'answer_settings', return_value=(True, .8)), \
             patch('app.ExamAPI') as api_cls, patch('app.AIClient'), patch('app.AnswerRunner', return_value=runner):
            api_cls.return_value.catalog.return_value = []
            app.ai()
        runner.run_paper.assert_called_once()
        self.assertEqual(ui.counts['submission_unknown'], 1)
        self.assertEqual(ui.counts['skipped'], 0)


if __name__ == '__main__':
    unittest.main()
