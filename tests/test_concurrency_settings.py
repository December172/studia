"""Concurrency settings are user-local, validated, and used by the next run."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rich.console import Console

import theme
from app import Application
from settings import SettingsError, SettingsStore
from ui import TerminalUI


class ConcurrencySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SettingsStore(Path(self.temp.name))

    def view(self, answers):
        output = io.StringIO()
        inputs = iter(answers)
        view = TerminalUI(console=Console(file=output, width=100, theme=theme.THEME), input_fn=lambda: next(inputs))
        return view, output

    def test_worker_bounds_reject_bool_float_strings_and_out_of_range(self):
        from base.concurrency import validate_workers
        for value in (1, 2, 8):
            self.assertEqual(validate_workers(value), value)
        for value in (True, False, 1.0, '2', None, 0, -1, 9, 100):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_workers(value)

    def test_absent_settings_default_to_eight_without_creating_file(self):
        self.assertEqual(self.store.concurrency(), {'ai': 8, 'study': 8})
        self.assertFalse(self.store.path.exists())

    def test_restart_preserves_separate_values_and_other_settings(self):
        self.store.set('confidence', .9)
        self.store.save_concurrency(8, 2)
        restarted = SettingsStore(self.temp.name)
        self.assertEqual(restarted.concurrency(), {'ai': 8, 'study': 2})
        self.assertEqual(restarted.get('confidence'), .9)
        restarted.save_concurrency(1, 8)
        self.assertEqual(self.store.concurrency(), {'ai': 1, 'study': 8})

    def test_invalid_save_never_changes_previous_settings(self):
        self.store.save_concurrency(3, 4)
        before = self.store.path.read_bytes()
        for bad in (True, 0, 9, '8', 2.5, None):
            for values in ((bad, 2), (2, bad)):
                with self.subTest(values=values), self.assertRaises(SettingsError):
                    self.store.save_concurrency(*values)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_malformed_saved_value_falls_back_per_field_without_rewrite(self):
        for saved, expected in ((None, {'ai': 8, 'study': 8}),
                                ([], {'ai': 8, 'study': 8}),
                                ({'ai': 8}, {'ai': 8, 'study': 8}),
                                ({'ai': 99, 'study': 4}, {'ai': 8, 'study': 4}),
                                ({'ai': True, 'study': '8'}, {'ai': 8, 'study': 8})):
            self.store.set('concurrency', saved)
            before = self.store.path.read_bytes()
            self.assertEqual(self.store.concurrency(), expected)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_settings_menu_omits_read_only_execution_rhythm(self):
        self.store.save_concurrency(8, 8)
        before = self.store.path.read_bytes()
        view, output = self.view(['0'])
        Application(view, self.store).manage_settings()
        self.assertNotIn('执行节奏', output.getvalue())
        self.assertNotIn('测试 AI 连接', output.getvalue())
        self.assertIn('并发设置', output.getvalue())
        self.assertNotIn('并发数（', output.getvalue())
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_execution_uses_separate_saved_worker_counts(self):
        self.store.save_concurrency(8, 2)
        view, output = self.view([])
        app = Application(view, self.store)
        course = {'id': 'ci', 'courseId': 'c', 'courseName': 'course'}
        client = Mock()
        with patch('app.AIMoocHandler') as mooc, patch('app.ZYKMoocHandler') as zyk:
            app.study(0, client, [course])
            app.study(1, client, [course])
        self.assertEqual(mooc.call_args.kwargs['max_workers'], 2)
        self.assertEqual(zyk.call_args.kwargs['max_workers'], 2)
        self.assertIn('最多 2 项同时处理', output.getvalue())
        with patch.object(app, 'verify_ai', return_value=Mock()), patch('app.AnswerRunner') as runner:
            runner.return_value.run_paper.return_value = {'status': 'preview'}
            app.execute_ai(client, [(course, {'id': 'exam'}, 3)], Mock(), False, api=Mock())
            self.assertEqual(runner.call_args.kwargs['max_workers'], 8)
            self.store.save_concurrency(1, 1)
            app.execute_ai(client, [(course, {'id': 'exam'}, 3)], Mock(), False, api=Mock())
            self.assertEqual(runner.call_args.kwargs['max_workers'], 1)
        # The AI pipeline applies the saved count without explaining it at runtime.
        self.assertNotIn('题同时处理', output.getvalue())

    def test_cancel_does_not_save_and_failed_save_preserves_draft(self):
        view, _ = self.view([])
        app = Application(view, self.store)
        self.store.save_concurrency(8, 8)
        with patch.object(view, 'concurrency_editor', return_value=None):
            self.assertFalse(app.manage_concurrency())
        self.assertEqual(self.store.concurrency(), {'ai': 8, 'study': 8})
        draft = {'ai': 3, 'study': 5}
        original_save = self.store.save_concurrency
        calls = []
        def save(ai, study):
            calls.append((ai, study))
            if len(calls) == 1:
                raise SettingsError('保存失败')
            original_save(ai, study)
        with patch.object(view, 'concurrency_editor', side_effect=[draft, draft]) as editor, \
             patch.object(self.store, 'save_concurrency', side_effect=save):
            self.assertTrue(app.manage_concurrency())
        self.assertEqual(editor.call_args.args[0], draft)
        self.assertIn('保存失败', editor.call_args.kwargs['message'])
        self.assertEqual(self.store.concurrency(), draft)

    def test_confirmation_pages_state_execution_without_worker_internals(self):
        view, output = self.view(['', ''])
        view.review_study([{'courseName': '课程'}], max_workers=3)
        from ai.client import AIConfig
        view.review_ai([], AIConfig('https://example.org/v1', 'model', 'key'), {})
        rendered = output.getvalue()
        self.assertIn('完成课程学习任务', rendered)
        self.assertIn('通过校验后自动交卷', rendered)
        self.assertNotIn('同时处理', rendered)
