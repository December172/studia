import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
import theme

from ai.client import AIConfig, AIError
from app import Application
from course_flow import CourseWorkspace
from model_ui import ModelChoice, ModelEdit
from settings import SettingsStore, SettingsError
from ui import TerminalUI, Back


class ModelManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SettingsStore(Path(self.temp.name))
        default_patch = patch('ai.defaults.default_config', return_value=AIConfig(
            'https://mochif.com/v1', 'gemini-3.8-flash', 'test-shared-key'))
        default_patch.start()
        self.addCleanup(default_patch.stop)
        self.output = io.StringIO()
        self.view = TerminalUI(console=Console(file=self.output, theme=theme.THEME))
        self.app = Application(self.view, self.store)
        self.a = AIConfig('https://one.example/v1', 'first', 'key-one')
        self.b = AIConfig('https://two.example/v1', 'second', 'key-two')

    def test_text_editor_saves_four_fields_and_summary_omits_protocol(self):
        inputs = iter(['供应商', 'https://one.example/v1', 'model', ''])
        self.view.input_fn = lambda: next(inputs)
        self.view.secret_fn = lambda prompt: 'private-key'
        result = self.view.model_editor()
        self.assertEqual(result.action, 'save')
        self.assertEqual(result.config.api_format, 'auto')
        current = AIConfig('https://one.example/v1', 'model', 'private-key', api_format='responses')
        inputs = iter(['1', '新供应商', '', '', ''])
        self.view.secret_fn = lambda prompt: ''
        updated = self.view.model_editor(current)
        self.assertEqual(updated.config.api_format, 'responses')
        self.assertEqual(updated.config.api_key, 'private-key')
        self.view.input_fn = lambda: ''
        self.view.review_ai([], updated.config, {})
        rendered = self.output.getvalue()
        self.assertIn('新供应商', rendered)
        self.assertNotIn('接口格式', rendered)
        self.assertNotIn('Responses', rendered)
        self.assertNotIn('private-key', rendered)

    def test_legacy_configuration_custom_url_and_deepseek_sequences_use_auto(self):
        from ui import prompt_ai_config
        for values, expected in [(['1', 'https://one.example/v1', 'model'], 'auto'),
                                  (['1', 'https://one.example/v1/responses', 'model'], 'responses'),
                                  (['2', '1'], 'auto')]:
            with self.subTest(values=values):
                inputs = iter(values)
                self.view.input_fn = lambda: next(inputs)
                self.view.secret_fn = lambda prompt: 'private-key'
                config = self.view.ai_config()
                self.assertEqual(config.api_format, expected)
                inputs = iter(values)
                config = prompt_ai_config(lambda prompt: next(inputs), lambda prompt: 'private-key', lambda text: None)
                self.assertEqual(config.api_format, expected)

    def test_custom_provider_name_is_shown_in_picker_and_answer_summary(self):
        config = AIConfig('https://private.example/v1', 'same-model', 'private-key',
                          provider_name='课程供应商')
        self.store.save_ai_profile(config)
        self.view.input_fn = lambda: 'b'
        self.app.manage_models()
        self.view.input_fn = lambda: ''
        self.view.review_ai([], config, {})
        rendered = self.output.getvalue()
        self.assertGreaterEqual(rendered.count('课程供应商'), 2)
        self.assertIn('same-model', rendered)
        self.assertNotIn('private.example', rendered)
        self.assertNotIn('private-key', rendered)

    def test_default_picker_hides_provider_url_and_blocks_editor(self):
        self.store.save_ai_profile(self.a)
        self.store.ensure_default_model()
        default = next(p for p in self.store.ai_profiles() if p.config.model == 'gemini-3.8-flash')
        inputs = iter(['e 1', 'b'])
        self.view.input_fn = lambda: next(inputs)
        with patch.object(self.view, 'model_editor', side_effect=AssertionError('default editor must not open')):
            self.assertEqual(self.app.manage_models(), self.a)
        rendered = self.output.getvalue()
        self.assertIn('默认供应商', rendered)
        self.assertNotIn(default.config.base_url.split('://', 1)[-1], rendered)
        self.assertNotIn(default.config.api_key, rendered)
        self.assertIn('自定义供应商', rendered)
        self.assertIn('gemini-3.8-flash', rendered)
        self.assertNotIn('one.example/v1', rendered)
        self.assertLess(rendered.index('默认供应商'), rendered.index('自定义供应商'))

    def test_application_rejects_default_edit_even_if_picker_requests_it(self):
        self.store.ensure_default_model()
        default = self.store.ai_profiles()[0]
        before = self.store.path.read_bytes()
        with patch.object(self.view, 'model_picker', side_effect=[ModelChoice('edit', default.id), ModelChoice('back')]), \
             patch.object(self.view, 'model_editor', side_effect=AssertionError('default editor must not open')):
            self.assertEqual(self.app.manage_models(), default.config)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertIn('默认供应商', self.output.getvalue())

    def test_add_saves_tests_and_reuses_without_reentering_credentials(self):
        with patch.object(self.view, 'model_picker', side_effect=[ModelChoice('add'), ModelChoice('back')]), \
             patch.object(self.view, 'model_editor', return_value=ModelEdit('save', self.a)), \
             patch('app.AIClient.check_connection', return_value=None):
            self.assertEqual(self.app.ai_configuration(), self.a)
        self.assertEqual(self.store.ai_health(self.a)['status'], 'ok')
        self.assertEqual(Application(self.view, SettingsStore(self.store.directory)).ai_configuration(), self.a)
        self.assertNotIn('key-one', self.output.getvalue())

    def test_failed_test_keeps_new_profile_without_overwriting_current(self):
        self.store.save_ai_profile(self.a)
        with patch.object(self.view, 'model_picker', side_effect=[ModelChoice('add'), ModelChoice('back')]), \
             patch.object(self.view, 'model_editor', return_value=ModelEdit('save', self.b)), \
             patch('app.AIClient.check_connection', side_effect=AIError('网络超时')):
            self.assertEqual(self.app.manage_models(), self.a)
        self.assertEqual(len(self.store.ai_profiles()), 2)
        self.assertEqual(self.store.ai_health(self.b)['status'], 'error')

    def test_cancel_editor_or_delete_confirmation_leaves_settings_identical(self):
        first = self.store.save_ai_profile(self.a)
        before = self.store.path.read_bytes()
        with patch.object(self.view, 'model_picker', side_effect=[ModelChoice('edit', first),
                          ModelChoice('edit', first), ModelChoice('back')]), \
             patch.object(self.view, 'model_editor', side_effect=[ModelEdit('back'), ModelEdit('delete')]), \
             patch.object(self.view, 'confirm', return_value=False):
            self.assertEqual(self.app.manage_models(), self.a)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_save_failure_does_not_test_or_activate_unsaved_configuration(self):
        first = self.store.save_ai_profile(self.a)
        with patch.object(self.view, 'model_picker', side_effect=[ModelChoice('edit', first), ModelChoice('back')]), \
             patch.object(self.view, 'model_editor', return_value=ModelEdit('save', self.b)), \
             patch.object(self.store, 'save_ai_profile', side_effect=SettingsError('磁盘已满')), \
             patch('app.AIClient.check_connection', side_effect=AssertionError('must not test unsaved model')):
            self.assertEqual(self.app.manage_models(), self.a)
        self.assertEqual(self.store.ai_config(), self.a)
        self.assertIn('磁盘已满', self.output.getvalue())

    def test_picker_test_checks_the_highlighted_model_without_activating_it(self):
        first = self.store.save_ai_profile(self.a)
        second = self.store.save_ai_profile(self.b)
        tests = []

        def picker(rows, active_id, *, cursor_id=None, message="", locked_ids=(), test_connection=None):
            self.assertEqual(active_id, first)
            status = test_connection(second)
            tests.append(status)
            return ModelChoice('back')

        with patch.object(self.view, 'model_picker', side_effect=picker), \
             patch('app.AIClient.check_connection', autospec=True, return_value=None) as check:
            self.assertEqual(self.app.manage_models(), self.a)
        self.assertEqual(self.store.ai_config(), self.a)
        self.assertEqual(self.store.ai_health(self.b)['status'], 'ok')
        self.assertIn('通过', tests[0])
        self.assertEqual(check.call_args.args[0].config, self.b)
        self.assertNotIn('key-two', repr(tests))

    def test_picker_test_reports_failure_without_activating_or_leaking_reason(self):
        first = self.store.save_ai_profile(self.a)
        second = self.store.save_ai_profile(self.b)
        statuses = []
        with patch.object(self.view, 'model_picker', side_effect=lambda rows, active_id, **kwargs:
                          statuses.append(kwargs['test_connection'](second)) or ModelChoice('back')), \
             patch('app.AIClient.check_connection', autospec=True, side_effect=AIError('网络超时')):
            self.assertEqual(self.app.manage_models(), self.a)
        self.assertEqual(self.store.ai_config(), self.a)
        self.assertIn('网络超时', statuses[0])
        self.assertEqual(self.store.ai_health(self.b)['status'], 'error')

    def test_default_provider_can_be_tested(self):
        self.store.ensure_default_model()
        default = self.store.ai_profiles()[0]
        statuses = []
        with patch.object(self.view, 'model_picker', side_effect=lambda rows, active_id, **kwargs:
                          statuses.append(kwargs['test_connection'](default.id)) or ModelChoice('back')), \
             patch('app.AIClient.check_connection', autospec=True, return_value=None) as check:
            self.app.manage_models()
        self.assertIn('通过', statuses[0])
        self.assertEqual(check.call_args.args[0].config, default.config)

    def test_unexpected_test_error_is_safe_and_never_opens_a_live_renderer(self):
        profile = self.store.save_ai_profile(self.a)
        with patch('app.AIClient.check_connection', side_effect=OSError('socket token=never-show')), \
             patch.object(self.view, 'activity', side_effect=AssertionError('nested renderer')) as activity:
            status = self.app.test_profile_connection(profile)
        self.assertIn('失败', status)
        self.assertNotIn('never-show', status)
        activity.assert_not_called()

    def test_settings_menu_has_no_standalone_connection_test(self):
        entries = []
        self.view.input_fn = lambda: entries.append(True) or '0'
        self.app.manage_settings()
        rendered = self.output.getvalue()
        self.assertNotIn('测试 AI 连接', rendered)
        self.assertIn('模型配置', rendered)

    def test_background_health_save_cannot_overwrite_a_later_activation(self):
        self.store.save_ai_profile(self.a)
        second = self.store.save_ai_profile(self.b)
        other_store = SettingsStore(self.temp.name)
        writing = threading.Event()
        release = threading.Event()
        activated = threading.Event()
        original_write = self.store._write
        def slow_write(data):
            writing.set()
            release.wait(3)
            original_write(data)
        def activate():
            other_store.activate_ai_profile(second)
            activated.set()
        with patch.object(self.store, '_write', side_effect=slow_write):
            worker = threading.Thread(target=lambda: self.store.save_ai_health(self.a, 'ok'))
            worker.start()
            self.assertTrue(writing.wait(3))
            change = threading.Thread(target=activate)
            change.start()
            activated.wait(.1)
            release.set()
            worker.join(3)
            change.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(change.is_alive())
        self.assertEqual(self.store.ai_config(), self.b)

    def test_first_configuration_cancel_returns_without_answering(self):
        with patch.object(self.view, 'model_picker', return_value=ModelChoice('back')):
            with self.assertRaises(Back):
                self.app.ai_configuration()

    def test_switch_in_summary_tests_and_answers_new_model_with_same_tasks_and_options(self):
        self.store.save_ai_profile(self.a)
        second = self.store.save_ai_profile(self.b)
        selected = [({'id': 'ci', 'courseId': 'c'}, {'id': 'p', 'name': '试卷'}, 1)]
        inputs = iter(['s', '2', 'm', 't', ''])
        self.view.input_fn = lambda: next(inputs)
        state = {}
        seen = []

        def execute(client, tasks, config, auto_submit):
            seen.append((tasks, config, auto_submit))
            return []

        with patch.object(self.app, 'task_catalog', return_value=selected), \
             patch.object(self.view, 'papers', return_value=selected), \
             patch.object(self.view, 'model_picker', return_value=ModelChoice('select', second)), \
             patch('app.AIClient.check_connection', autospec=True, side_effect=lambda client: seen.append(client.config)), \
             patch.object(self.app, 'execute_ai', side_effect=execute), \
             patch.object(self.view, 'operation_result', return_value=1):
            CourseWorkspace(self.app).tasks([selected[0][0]], 2, state)
        self.assertEqual(seen, [self.b, (selected, self.b, True)])
        self.assertEqual(self.store.ai_config(), self.b)
        self.assertIn('second', self.output.getvalue())

    def test_deleting_current_in_summary_blocks_start_until_a_model_is_selected(self):
        inputs = iter(['m', '', 'b'])
        self.view.input_fn = lambda: next(inputs)
        options = {}
        with self.assertRaises(Back):
            self.view.review_ai([], self.a, options, manage_models=lambda: None)
        self.assertIsNone(options['config'])
        self.assertIn('选择模型', self.output.getvalue())
