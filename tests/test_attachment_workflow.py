"""Unsupported attachment homework stays visible but cannot start an attempt."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rich.console import Console

import theme
from app import Application
from ai.runner import AnswerRunner
from AIMoocMain.exam_api import ExamError
from AIMoocMain.export_questions import QuizExportHandler
from settings import SettingsStore
from ui import TerminalUI, Back


COURSE = {'id': 'ci', 'courseId': 'c', 'courseName': '课程'}
ATTACHMENT = {'id': 'attachment', 'name': '项目计划书', 'typeId': 3}
NORMAL = {'id': 'normal', 'name': '普通作业', 'typeId': 1}


class AttachmentWorkflowTests(unittest.TestCase):
    def view(self, answers, width=120):
        out, inputs = io.StringIO(), iter(answers)
        return TerminalUI(console=Console(file=out, theme=theme.THEME, width=width),
                          input_fn=lambda: next(inputs)), out

    def test_line_all_ignores_attachment_but_still_explains_it(self):
        catalog = [(COURSE, ATTACHMENT, 1), (COURSE, NORMAL, 1)]
        for width in (52, 120):
            with self.subTest(width=width):
                view, out = self.view(['all'], width)
                self.assertEqual(view.papers(catalog), [catalog[1]])
                self.assertIn('附件作业', out.getvalue())
                self.assertIn('暂不支持', out.getvalue())

    def test_explicit_mixed_indexes_and_stale_default_cannot_include_attachment(self):
        catalog = [(COURSE, ATTACHMENT, 1), (COURSE, NORMAL, 1)]
        view, _ = self.view(['1,2', '2'])
        self.assertEqual(view.papers(catalog), [catalog[1]])
        view, _ = self.view([''])
        self.assertEqual(view.papers(catalog, selected=catalog), [catalog[1]])

    def test_only_attachments_cannot_start_and_can_return(self):
        view, out = self.view(['all', '\x1b'])
        with self.assertRaises(Back):
            view.papers([(COURSE, ATTACHMENT, 1)])
        self.assertIn('暂不支持', out.getvalue())

    def test_task_catalog_uses_catalog_type_without_opening_attachment_history_or_paper(self):
        view, _ = self.view([])
        app = Application(view, Mock())
        api = Mock()
        api.catalog.side_effect = lambda course, category: [ATTACHMENT, NORMAL] if category == 1 else []
        api.completion_status.return_value = 'not_started'
        with patch('app.ExamAPI', return_value=api):
            catalog = app.task_catalog(Mock(), [COURSE])
        self.assertEqual(catalog[0][1]['_completion_status'], 'unsupported')
        api.completion_status.assert_called_once_with('normal')
        api.paper.assert_not_called()

    def test_application_skips_attachment_before_model_check_or_opening_attempt(self):
        view, _ = self.view([])
        with tempfile.TemporaryDirectory() as directory:
            app = Application(view, SettingsStore(directory))
            with patch.object(app, 'verify_ai') as verify, patch('app.AnswerRunner') as runner:
                reports = app.execute_ai(Mock(), [(COURSE, ATTACHMENT, 1)], Mock(), True)
        self.assertEqual(reports[0]['status'], 'skipped')
        self.assertIn('附件作业', reports[0]['message'])
        verify.assert_not_called()
        runner.assert_not_called()

    def test_mixed_ai_queue_skips_only_attachment(self):
        view, _ = self.view([])
        with tempfile.TemporaryDirectory() as directory:
            app = Application(view, SettingsStore(directory))
            with patch.object(app, 'verify_ai'), patch('app.AnswerRunner') as factory:
                factory.return_value.run_paper.return_value = {'status': 'preview', 'paper': '普通作业'}
                reports = app.execute_ai(Mock(), [(COURSE, ATTACHMENT, 1), (COURSE, NORMAL, 1)],
                                         Mock(), False, api=Mock())
        self.assertEqual([r['status'] for r in reports], ['skipped', 'preview'])
        factory.return_value.run_paper.assert_called_once_with(COURSE, NORMAL, 1)

    def test_direct_runner_rejects_attachment_before_any_platform_or_ai_call(self):
        with tempfile.TemporaryDirectory() as directory:
            api, ai = Mock(), Mock()
            runner = AnswerRunner(api, ai, directory)
            with self.assertRaisesRegex(ExamError, '附件作业'):
                runner.run_paper(COURSE, ATTACHMENT, 1)
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(api.mock_calls, [])
            ai.answer.assert_not_called()

    def test_export_guard_applies_to_single_merged_and_catalog_entry(self):
        for mode in ('per_chapter', 'merged'):
            for explicit in (True, False):
                with self.subTest(mode=mode, explicit=explicit), tempfile.TemporaryDirectory() as directory:
                    api, events = Mock(), []
                    api.get_exam_list_by_student.return_value = {'rows': [ATTACHMENT]}
                    handler = QuizExportHandler(client=api, course_rows=[COURSE], category_ids=[3],
                        selected_papers=[(COURSE, ATTACHMENT, 1)] if explicit else None,
                        output_dir=directory, quiz_mode=mode, on_progress=events.append)
                    api.get_exam_paper.assert_not_called()
                    self.assertEqual(handler.stats['papers'], 0)
                    self.assertEqual([e.status for e in events if e.kind == 'result'], ['skipped'])
