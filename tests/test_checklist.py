"""Exercise real key bindings with an offline terminal input stream."""
import unittest

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import native_ui


class ChecklistTests(unittest.TestCase):
    def test_row_fitting_preserves_checkbox_indent_and_status_alignment(self):
        from checklist import fit
        from prompt_toolkit.utils import get_cwidth
        rendered = fit('  [x] 作业    已提交', 24)
        self.assertTrue(rendered.startswith('  [x] 作业    已提交'))
        self.assertEqual(get_cwidth(rendered), 24)
        self.assertLessEqual(get_cwidth(fit('很长的任务名称' * 20, 12)), 12)

    def choose(self, keys, count=3, selected=()):
        import checklist
        self.assertTrue(hasattr(checklist, 'choose_many'), 'keyboard checklist is not implemented')
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            return checklist.choose_many('任务', [(f'任务{i}', '未作答', '作业') for i in range(count)],
                                         selected=selected, refresh=True, input=pipe, output=DummyOutput())

    def test_space_toggles_without_confirming_and_enter_returns_checked_rows(self):
        result = self.choose(' \x1b[B \x1b[A \r')
        self.assertEqual(result.indexes, [1])
        self.assertIsNone(result.action)

    def test_all_row_selects_and_clears_every_row_across_long_lists(self):
        self.assertEqual(self.choose('\x1b[A \r', count=30).indexes, list(range(30)))
        self.assertEqual(self.choose('\x1b[A \x1b[B \r', selected=(0, 1, 2)).indexes, [0])

    def test_enter_with_nothing_checked_does_not_confirm(self):
        self.assertEqual(self.choose('\r \r').indexes, [0])

    def test_refresh_keeps_new_checkmarks_and_cursor_for_caller(self):
        result = self.choose(' \x1b[B r')
        self.assertEqual(result.action, 'r')
        self.assertEqual(result.indexes, [0, 1])
        self.assertEqual(result.cursor, 2)

    def test_back_and_quit_are_distinct(self):
        self.assertEqual(self.choose('\x1b').action, 'back')
        self.assertEqual(self.choose('b').action, 'back')
        self.assertEqual(self.choose('q').action, 'quit')
        self.assertEqual(self.choose('\x03').action, 'quit')

    def test_tab_focuses_the_confirm_button_and_enter_activates_it(self):
        # Space checks the row while the list owns focus; Tab then reaches the
        # Confirm button, and a focused button must be recognised (not reset to
        # the first control on every Tab press).
        result = self.choose(' \t\r')
        self.assertIsNone(result.action)
        self.assertEqual(result.indexes, [0])

    def test_shift_tab_reaches_the_back_button(self):
        self.assertEqual(self.choose('\x1b[Z\r').action, 'back')

    def test_space_is_inert_once_a_button_owns_focus(self):
        # Tab moves focus off the list; Space must not toggle background rows.
        result = self.choose('\t \x1b')
        self.assertEqual(result.action, 'back')
        self.assertEqual(result.indexes, [])

    def test_selected_rows_remain_checked_when_reopened(self):
        self.assertEqual(self.choose('\r', selected=(1, 2)).indexes, [1, 2])

    def test_workspace_keeps_space_selection_through_refresh(self):
        import io
        from functools import partial
        from unittest.mock import Mock, patch
        from rich.console import Console
        import checklist
        import theme
        from app import Application
        from course_flow import CourseWorkspace
        from ui import TerminalUI, Cancelled
        course = {'id': 'ci', 'courseId': 'c', 'courseName': '课程'}
        initial = [(course, {'id': 'a', 'name': '任务', '_completion_status': 'not_started'}, 1)]
        updated = [(course, dict(initial[0][1], _completion_status='submitted'), 1)]
        view = TerminalUI(console=Console(file=io.StringIO(), force_terminal=True, theme=theme.THEME))
        store = Mock()
        store.get.side_effect = lambda key, default=None: default
        app = Application(view, store)
        state = {}
        with create_pipe_input() as pipe:
            pipe.send_text(' r\r')
            native = partial(checklist.choose_many, input=pipe, output=DummyOutput())
            with patch('checklist.choose_many', side_effect=native), \
                 patch('native_ui.action_page',
                       return_value=native_ui.PageResult('quit')), \
                 patch('sys.stdin.isatty', return_value=True), \
                 patch('ui.read_terminal_line', return_value='q'), \
                 patch.object(app, 'task_catalog', side_effect=[initial, updated]):
                with self.assertRaises(Cancelled):
                    CourseWorkspace(app).tasks([course], 3, state)
        self.assertEqual(state.get('selected'), updated)


if __name__ == '__main__':
    unittest.main()
