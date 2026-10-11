import io
import threading
import time
import unittest
from unittest.mock import patch

from prompt_toolkit.application import Application
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.data_structures import Size

from ai.client import AIConfig

ROWS = [('a', '供应商一', 'same', '?'), ('b', '供应商二', 'same', '?')]


class ModelUITests(unittest.TestCase):
    def picker(self, keys, active='a', rows=None, **kwargs):
        from model_ui import choose_model
        rows = rows if rows is not None else [('a', '供应商一', 'same', '✓'),
                                              ('b', '供应商二', 'same', '?')]
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            return choose_model(rows, active, input=pipe, output=DummyOutput(), **kwargs)

    def terminal_picker(self, keys, active='a', rows=None, **kwargs):
        from model_ui import choose_model
        rows = rows if rows is not None else ROWS
        stream = io.StringIO()
        output = Vt100_Output(stream, lambda: Size(rows=24, columns=80),
                              term='xterm-256color', default_color_depth=ColorDepth.DEPTH_8_BIT,
                              enable_cpr=False)
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            result = choose_model(rows, active, input=pipe, output=output, **kwargs)
        return result, stream.getvalue()

    def form(self, keys, current=None):
        from model_ui import edit_model
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            return edit_model(current, input=pipe, output=DummyOutput())

    def test_four_fields_save_without_protocol_selection_and_preserve_detected_format(self):
        old = AIConfig('https://one.example/v1', 'old', 'secret-key', api_format='responses')
        result = self.form('供应商\t\t\t\t\r\x03', old)
        self.assertEqual(result.action, 'save')
        self.assertEqual(result.config.provider_name, '供应商')
        self.assertEqual(result.config.api_format, 'responses')
        self.assertEqual(result.config.api_key, 'secret-key')
        self.assertEqual(old.provider_name, '')

    def test_draft_retains_detected_format_only_when_connection_fields_are_unchanged(self):
        from model_ui import draft_config
        old = AIConfig('https://one.example/v1', 'old', 'secret-key', api_format='responses')
        renamed = draft_config(' https://one.example/v1/ ', ' old ', '', old, provider_name='新名称')
        self.assertEqual(renamed.api_format, 'responses')
        self.assertEqual(renamed.api_key, 'secret-key')
        for url, model, key in [('https://one.example/v1', 'new', ''),
                                ('https://one.example/v1', 'old', 'new-key'),
                                ('https://two.example/v1', 'old', 'new-key')]:
            with self.subTest(url=url, model=model):
                updated = draft_config(url, model, key, old)
                self.assertEqual(updated.api_format, 'auto')
        with self.assertRaisesRegex(ValueError, '新 API Key'):
            draft_config('https://two.example/v1', 'new', '', old)

    def test_new_draft_uses_auto_but_complete_endpoint_specifies_format(self):
        from model_ui import draft_config
        self.assertEqual(draft_config('https://one.example/v1', 'model', 'key').api_format, 'auto')
        self.assertEqual(draft_config('https://one.example/v1/responses', 'model', 'key').api_format,
                         'responses')

    def test_provider_name_is_independent_from_connection_fields_and_cancel_discards_it(self):
        result = self.form('  我的供应商  \thttps://two.example/v1\tmodel-b\tnew-key\t\r')
        self.assertEqual(result.action, 'save')
        self.assertEqual(result.config.provider_name, '我的供应商')
        self.assertEqual(result.config.base_url, 'https://two.example/v1')
        old = result.config
        cancelled = self.form('changed\x1b', old)
        self.assertEqual(cancelled.action, 'back')
        self.assertIsNone(cancelled.config)
        self.assertEqual(old.provider_name, '我的供应商')
        renamed = self.form('\x01\x0b新名称\t\t\t\t\r', old)
        self.assertEqual(renamed.config.provider_name, '新名称')
        self.assertEqual(renamed.config.base_url, old.base_url)
        self.assertEqual(renamed.config.model, old.model)
        self.assertEqual(renamed.config.api_key, old.api_key)
        self.assertEqual(old.provider_name, '我的供应商')

    def test_enter_switches_only_highlighted_profile_and_keeps_active_initial_cursor(self):
        self.assertEqual(self.picker('\x1b[B\r').profile_id, 'b')
        self.assertEqual(self.picker('\r', active='b').profile_id, 'b')

    def test_add_row_edit_and_escape_are_distinct_actions(self):
        self.assertEqual(self.picker('\x1b[B\x1b[B\r').action, 'add')
        result = self.picker('\x1b[Be')
        self.assertEqual((result.action, result.profile_id), ('edit', 'b'))
        self.assertEqual(self.picker('\x1b').action, 'back')
        self.assertEqual(self.picker('\r', rows=[]).action, 'add')

    def test_default_picker_ignores_edit_but_still_allows_selection(self):
        from model_ui import choose_model
        rows = [('builtin-default', '默认供应商', 'shared-model', '?'), ('custom', '自定义供应商', 'same', '?')]
        for keys, expected in (('e\r', ('select', 'builtin-default')),
                               ('e\x1b[Be', ('edit', 'custom'))):
            with self.subTest(keys=keys), create_pipe_input() as pipe:
                pipe.send_text(keys)
                result = choose_model(rows, 'builtin-default', locked_ids={'builtin-default'},
                                      input=pipe, output=DummyOutput())
                self.assertEqual((result.action, result.profile_id), expected)

    def test_form_tabs_edit_model_and_blank_key_reuses_same_service(self):
        old = AIConfig('https://one.example/v1', 'old', 'secret-key')
        result = self.form('\t\t\x01\x0bnew-model\t\t\r', old)
        self.assertEqual(result.action, 'save')
        self.assertEqual(result.config.model, 'new-model')
        self.assertEqual(result.config.api_key, 'secret-key')
        self.assertEqual(old.model, 'old')

    def test_new_configuration_and_cancel_dont_share_mutable_state(self):
        result = self.form('\thttps://two.example/v1\tmodel-b\tnew-key\t\r')
        self.assertEqual(result.config, AIConfig('https://two.example/v1', 'model-b', 'new-key'))
        old = AIConfig('https://one.example/v1', 'old', 'secret-key')
        result = self.form('\t\x01\x0bhttps://changed.example/v1\x1b', old)
        self.assertEqual(result.action, 'back')
        self.assertIsNone(result.config)
        self.assertEqual(old.base_url, 'https://one.example/v1')

    def test_changed_service_requires_new_key_and_preserves_typed_fields_on_error(self):
        old = AIConfig('https://one.example/v1', 'old', 'secret-key')
        result = self.form('\t\x01\x0bhttps://two.example/v1\t\t\t\r\x1b[Znew-key\t\r', old)
        self.assertEqual(result.config.base_url, 'https://two.example/v1')
        self.assertEqual(result.config.api_key, 'new-key')

    def test_delete_exists_only_in_edit_form(self):
        old = AIConfig('https://one.example/v1', 'old', 'secret-key')
        self.assertEqual(self.form('\t\t\t\t\t\t\r', old).action, 'delete')
        self.assertEqual(self.form('\t\t\t\t\t\r').action, 'back')

    def wait_until(self, predicate, description):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('timed out waiting for ' + description)

    def running_picker(self, keys, rows=None, **kwargs):
        from model_ui import choose_model
        stream = io.StringIO()
        output = Vt100_Output(stream, lambda: Size(rows=24, columns=80),
                              term='xterm-256color', enable_cpr=False)
        context = create_pipe_input()
        pipe = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        state = {}
        def run():
            try:
                state['value'] = choose_model(rows or ROWS, 'a', input=pipe, output=output, **kwargs)
            except BaseException as exc:
                state['error'] = exc
            finally:
                state['done'] = True
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(pipe.send_text, '\x03')
        pipe.send_text(keys)
        return pipe, state, stream

    def test_pending_test_prevents_duplicates_and_retains_selection(self):
        release = threading.Event()
        self.addCleanup(release.set)
        calls = []
        def check(profile):
            calls.append(profile)
            release.wait(3)
            return '连接通过'
        pipe, state, stream = self.running_picker('\x1b[Btt\x1b[A\r', test_connection=check)
        self.wait_until(lambda: '测试中' in stream.getvalue(), 'pending frame')
        self.assertEqual(calls, ['b'])
        self.assertFalse(state.get('done'))
        release.set()
        self.wait_until(lambda: '连接通过' in stream.getvalue(), 'result frame')
        self.assertIn('秒', stream.getvalue())
        pipe.send_text('\r')
        self.wait_until(lambda: state.get('done'), 'selection')
        self.assertNotIn('error', state)
        self.assertEqual(state['value'].profile_id, 'b')

    def test_visible_test_button_and_retest_do_not_activate_or_exit(self):
        calls = []
        pipe, state, stream = self.running_picker('\x1b[B\t\t\r',
            test_connection=lambda profile: calls.append(profile) or '连接通过')
        self.wait_until(lambda: '连接通过' in stream.getvalue(), 'button test result')
        self.assertEqual(calls, ['b'])
        self.assertFalse(state.get('done'))
        pipe.send_text('t')
        self.wait_until(lambda: len(calls) == 2, 'retest')
        pipe.send_text('\x1b')
        self.wait_until(lambda: state.get('done'), 'back')
        self.assertEqual(state['value'].action, 'back')
        for label in ('使用此模型', '测试连接', '编辑'):
            self.assertIn(label, stream.getvalue())

    def test_builtin_can_be_tested_and_failures_are_inline(self):
        calls = []
        pipe, state, stream = self.running_picker('t', rows=[('a', '默认供应商', 'model', '?')],
            locked_ids={'a'}, test_connection=lambda profile: calls.append(profile) or '连接失败：网络超时')
        self.wait_until(lambda: '网络超时' in stream.getvalue(), 'failed test')
        self.assertEqual(calls, ['a'])
        self.assertFalse(state.get('done'))
        pipe.send_text('e\r')
        self.wait_until(lambda: state.get('done'), 'selection after locked edit')
        self.assertEqual(state['value'].action, 'select')

    def test_back_during_pending_test_closes_without_worker_exception(self):
        release = threading.Event()
        done = threading.Event()
        self.addCleanup(release.set)
        def check(profile):
            release.wait(3)
            done.set()
            raise RuntimeError('token=never-show')
        pipe, state, stream = self.running_picker('t', test_connection=check)
        self.wait_until(lambda: '测试中' in stream.getvalue(), 'pending frame')
        pipe.send_text('\x1b')
        self.wait_until(lambda: state.get('done'), 'back during pending')
        release.set()
        self.assertTrue(done.wait(3))
        self.assertEqual(state['value'].action, 'back')
        self.assertNotIn('never-show', stream.getvalue())

    def context_screen(self, keys, rows=None, **kwargs):
        """Full rendered screen after the keys are processed, as plain lines."""
        from model_ui import choose_model
        import model_ui
        screens = []

        def application(*args, **kw):
            def capture(app):
                screen = app.renderer.last_rendered_screen
                if screen:
                    screens.append([
                        ''.join(screen.data_buffer[y][x].char for x in sorted(screen.data_buffer[y])).rstrip()
                        for y in sorted(screen.data_buffer)])
            return Application(*args, **kw, after_render=capture)

        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            with patch.object(model_ui, 'Application', side_effect=application):
                choose_model(rows or ROWS, 'a', input=pipe, output=DummyOutput(), **kwargs)
        return '\n'.join(screens[-1]) if screens else ''

    def test_context_help_matches_the_visible_actions(self):
        # Add row: only Enter/Tab/Esc, never edit or test hints.
        add = self.context_screen('\x1b[B\x1b[B\x1b')
        self.assertIn('Enter 添加', add)
        self.assertNotIn('e 编辑', add)
        self.assertNotIn('t 测试', add)
        # Built-in row: testable and usable, but never editable.
        builtin = self.context_screen('\x1b', rows=[('a', '默认供应商', 'm', '?')],
                                      locked_ids={'a'})
        self.assertIn('Enter 使用', builtin)
        self.assertIn('t 测试', builtin)
        self.assertNotIn('e 编辑', builtin)

    def test_custom_row_exposes_a_visible_back_button(self):
        # Every row context offers an explicit primary and return action; the
        # custom row keeps Use/Test/Edit order and adds 返回 after Edit.
        from model_ui import ModelChoice, choose_model
        with create_pipe_input() as pipe:
            pipe.send_text('\t\t\t\t\r')  # body → 使用 → 测试 → 编辑 → 返回
            result = choose_model(ROWS, 'a', input=pipe, output=DummyOutput())
        self.assertEqual(result.action, 'back')

    def test_custom_row_back_button_is_escape_and_keeps_focus_order(self):
        from model_ui import choose_model, ModelChoice
        import model_ui
        screens = []

        def application(*args, **kw):
            def capture(app):
                screen = app.renderer.last_rendered_screen
                if screen:
                    screens.append('\n'.join(
                        ''.join(screen.data_buffer[y][x].char for x in sorted(screen.data_buffer[y]))
                        for y in sorted(screen.data_buffer)))
            return Application(*args, **kw, after_render=capture)

        with create_pipe_input() as pipe:
            pipe.send_text('\t\t\t\t\x1b')
            with patch.object(model_ui, 'Application', side_effect=application):
                result = choose_model(ROWS, 'a', input=pipe, output=DummyOutput())
        self.assertIsInstance(result, ModelChoice)
        self.assertTrue(any('返回' in frame for frame in screens))

    def test_passwords_are_masked_in_real_terminal_rendering(self):
        from model_ui import edit_model
        old = AIConfig('https://one.example/v1', 'old', 'old-private-key')
        for width in (40, 52, 80):
            with self.subTest(width=width), create_pipe_input() as pipe:
                stream = io.StringIO()
                output = Vt100_Output(stream, lambda: Size(rows=24, columns=width),
                                      term='xterm-256color', default_color_depth=ColorDepth.DEPTH_8_BIT,
                                      enable_cpr=False)
                pipe.send_text('\t\t\tnew-private-key\t\r\x03')
                result = edit_model(old, input=pipe, output=output)
                self.assertEqual(result.action, 'save')
                self.assertEqual(result.config.api_key, 'new-private-key')
                self.assertNotIn('old-private-key', stream.getvalue())
                self.assertNotIn('new-private-key', stream.getvalue())
                self.assertIn('保存并测试', stream.getvalue())
                self.assertNotIn('Window too small', stream.getvalue())
