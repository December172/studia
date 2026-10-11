"""Animated identity must redraw without disrupting real terminal input."""
import asyncio
import io
import unittest
from unittest.mock import patch

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.input import create_pipe_input
from rich.console import Console

import checklist
import theme
import ui
from tests.test_checklist_layout import SizedOutput


class NameAnimationTests(unittest.TestCase):
    def capture(self, app, frames):
        screen = app.renderer.last_rendered_screen
        if screen:
            cells = [(y, x, c.char, c.style) for y, row in screen.data_buffer.items()
                     for x, c in row.items() if c.char in ('王', '治', '程')]
            if cells:
                frames.append(cells)

    def assert_animation(self, frames):
        self.assertGreaterEqual(len(frames), 3, 'name must refresh while waiting for input')
        for frame in frames:
            self.assertEqual(''.join(c[2] for c in frame), '王治程')
            self.assertEqual([c[1] for c in frame], [74, 76, 78])
        self.assertGreater(len({tuple(c[3] for c in f) for f in frames}), 1,
                           'name colors must change between frames')

    def test_course_prompt_animates_once_and_keeps_arrow_editing(self):
        frames = []
        with create_pipe_input() as pipe:
            def session(*args, **kwargs):
                kwargs.update(input=pipe, output=SizedOutput(80))
                result = PromptSession(*args, **kwargs)
                result.app.after_render += lambda app: self.capture(app, frames)
                result.app.pre_run_callables.append(
                    lambda: asyncio.get_running_loop().call_later(.2, pipe.send_text, 'ac\x1b[Db\r'))
                return result
            view = ui.TerminalUI(console=Console(file=io.StringIO(), force_terminal=True,
                                                 width=80, theme=theme.THEME))
            view.set_user({'displayName': '王治程'})
            with patch('sys.stdin.isatty', return_value=True), patch('prompt_toolkit.PromptSession', session):
                view.shortcuts('编号进入 · / 关键词搜索')
                view.shortcuts('Esc 返回 · q 退出', show_name=False)
                self.assertEqual(view.ask('选择编号'), 'abc')
            self.assert_animation(frames)

    def test_native_menu_animates_name_while_waiting(self):
        # The interactive menu now routes to native_ui.option_menu; the animated
        # identity must keep redrawing there too, without affecting selection.
        frames = []
        with create_pipe_input() as pipe:
            def application(*args, **kwargs):
                kwargs.update(input=pipe, output=SizedOutput(80))
                result = Application(*args, **kwargs,
                                     after_render=lambda app: self.capture(app, frames))
                result.pre_run_callables.append(
                    lambda: asyncio.get_running_loop().call_later(.2, pipe.send_text, '\x1b[B\r'))
                return result
            view = ui.TerminalUI(console=Console(file=io.StringIO(), force_terminal=True,
                                                 width=80, theme=theme.THEME))
            view.set_user({'displayName': '王治程'})
            with patch('sys.stdin.isatty', return_value=True), \
                 patch('native_ui.Application', application):
                self.assertEqual(view.choice('菜单', [(1, '学习', ''), (2, '答题', '')],
                                             help_text='Esc 返回 · q 退出'), 2)
            self.assert_animation(frames)

    def test_checklist_animates_without_changing_selection(self):
        frames = []
        with create_pipe_input() as pipe:
            def application(*args, **kwargs):
                result = Application(*args, **kwargs,
                                     after_render=lambda app: self.capture(app, frames))
                result.pre_run_callables.append(
                    lambda: asyncio.get_running_loop().call_later(.2, pipe.send_text, ' \x1b[B \x1b[A \r'))
                return result
            with patch('checklist.Application', application):
                result = checklist.choose_many('任务', [('甲', '', ''), ('乙', '', '')],
                                               account_name='王治程', input=pipe, output=SizedOutput(80))
            self.assertEqual(result.indexes, [1])
            self.assert_animation(frames)
