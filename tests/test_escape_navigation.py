import io
import tempfile
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.text import Text

import theme
import ui
from app import Application
from ai.client import AIConfig
from model_ui import ModelChoice, ModelEdit
from settings import SettingsStore


class EscapeNavigationTests(unittest.TestCase):
    def view(self, answers=None):
        stream = io.StringIO()
        inputs = iter(answers) if answers is not None else None
        view = ui.TerminalUI(console=Console(file=stream, theme=theme.THEME, force_terminal=True),
                             input_fn=(lambda: next(inputs)) if inputs is not None else None)
        return view, stream

    def test_escape_exits_native_prompt_without_enter_or_using_default(self):
        for secret in (False, True):
            with self.subTest(secret=secret), create_pipe_input() as pipe:
                pipe.send_text('\x1b')
                with self.assertRaises(ui.Back):
                    ui.read_terminal_line('输入', secret=secret, input=pipe, output=DummyOutput())

    def test_arrow_sequences_edit_input_instead_of_triggering_back(self):
        with create_pipe_input() as pipe:
            pipe.send_text('ac\x1b[Db\r')
            self.assertEqual(ui.read_terminal_line('输入', input=pipe, output=DummyOutput()), 'abc')

    def test_native_menu_and_summary_share_escape_input(self):
        view, output = self.view()
        with create_pipe_input() as pipe:
            pipe.send_text('\x1b')
            reader = partial(ui.read_terminal_line, input=pipe, output=DummyOutput())
            with patch('ui.read_terminal_line', side_effect=reader), patch('sys.stdin.isatty', return_value=True):
                with self.assertRaises(ui.Back):
                    view.review_ai([], AIConfig('https://example.org/v1', 'model', 'key'), {})
        # Render output carries ANSI styling; assert on the visible text only.
        plain = Text.from_ansi(output.getvalue()).plain
        self.assertIn('Esc 返回', plain)
        self.assertNotIn('b 返回', plain)

    def test_escape_in_scripted_secret_and_manual_input_also_navigates_back(self):
        view, _ = self.view(['\x1b'])
        with self.assertRaises(ui.Back):
            view.ask('答案', navigation=False)
        view.secret_fn = lambda prompt: '\x1b'
        with self.assertRaises(ui.Back):
            view.ask('Key', secret=True)

    def test_escape_from_connection_result_returns_to_settings(self):
        view, _ = self.view(['2', '\x1b', '0'])
        with tempfile.TemporaryDirectory() as directory:
            app = Application(view, SettingsStore(Path(directory)))
            with patch.object(app, 'test_ai_connection', return_value='未配置') as test_connection:
                self.assertFalse(app.manage_settings())
                test_connection.assert_called_once()

    def test_escape_delete_confirmation_returns_to_editor_without_deleting(self):
        view, _ = self.view(['\x1b'])
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory))
            config = AIConfig('https://example.org/v1', 'model', 'key')
            profile = store.save_ai_profile(config)
            app = Application(view, store)
            with patch.object(view, 'model_picker', side_effect=[ModelChoice('edit', profile), ModelChoice('back')]), \
                 patch.object(view, 'model_editor', side_effect=[ModelEdit('delete'), ModelEdit('back')]) as editor:
                self.assertEqual(app.manage_models(), config)
                self.assertEqual(editor.call_count, 2)
            self.assertEqual(store.ai_config(), config)
