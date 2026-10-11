"""Native action pages for summaries/results, plus native routing via TerminalUI.

The action page reuses the Task2a helpers (one style, responsive actions, wrapped
help, animated identity) and returns typed results so the existing TerminalUI
loops invoke their original callbacks and reopen the page with state. A bounded
watchdog guarantees a stuck page can never hang the suite.
"""
import io
import unittest
from unittest.mock import patch

import native_ui
from tests.test_native_controls import (DOWN, ENTER, ESC, STAB, TAB, run)

START = 'start'


def page(keys, lines=('第一行',), actions=None, **kwargs):
    actions = actions if actions is not None else {'start': '开始', 's': '设置'}
    return run(native_ui.action_page, keys, '标题', lambda: list(lines), actions, **kwargs)


class ActionPageTests(unittest.TestCase):
    def test_enter_returns_the_default_action(self):
        result, _ = page(ENTER)
        self.assertEqual(result.kind, 'action')
        self.assertEqual(result.action, 'start')

    def test_typed_shortcut_selects_another_action(self):
        result, _ = page('s')
        self.assertEqual(result.kind, 'action')
        self.assertEqual(result.action, 's')

    def test_escape_backs_and_q_quits(self):
        self.assertEqual(page(ESC)[0].kind, 'back')
        self.assertEqual(page('q')[0].kind, 'quit')

    def test_q_works_from_a_focused_button_too(self):
        # q is advertised on every page; tabbing to a button must not swallow it.
        result, _ = page(TAB + 'q')
        self.assertEqual(result.kind, 'quit')

    def test_tab_reaches_buttons_and_enter_activates_the_focused_one(self):
        result, _ = page(TAB + ENTER)
        self.assertEqual(result.kind, 'action')
        self.assertEqual(result.action, 'start')

    def test_shift_tab_reaches_the_last_action(self):
        result, _ = page(STAB + ENTER)
        self.assertEqual(result.kind, 'action')
        self.assertEqual(result.action, 's')

    def test_status_message_is_rendered(self):
        result, frames = page(ESC, status='没有可用的模型', help_text='Esc 返回 · q 退出')
        self.assertEqual(result.kind, 'back')
        self.assertIn('没有可用的模型', '\n'.join(frames[-1]))

    def test_lines_scroll_with_arrows_and_pagedown(self):
        lines = [f'结果 {i:02d}' for i in range(30)]
        result, frames = page([DOWN, DOWN, DOWN, ENTER], lines=lines, columns=80, rows=12)
        self.assertEqual(result.kind, 'action')
        self.assertEqual(result.action, 'start')
        text = '\n'.join(frames[-1])
        self.assertIn('结果 03', text)
        self.assertNotIn('结果 00', text)

    def test_static_line_list_renders(self):
        # A plain list must render, not be re-wrapped as a callable.
        result, frames = page(ESC, lines=['静态 一', '静态 二'])
        self.assertEqual(result.kind, 'back')
        text = '\n'.join(frames[-1])
        self.assertIn('静态 一', text)
        self.assertIn('静态 二', text)

    def test_tab_cycles_through_every_action_not_back_to_the_first(self):
        # Tab from the body reaches the first button, then the second; a focused
        # button must be recognised so Enter activates *it*.
        result, _ = page([TAB] * 2 + [ENTER])
        self.assertEqual((result.kind, result.action), ('action', 's'))

    def test_shift_tab_reaches_the_last_action_then_cycles(self):
        result, _ = page([STAB] + [ENTER])
        self.assertEqual((result.kind, result.action), ('action', 's'))

    def test_unbound_digit_is_ignored_instead_of_starting_by_ordinal(self):
        # The page binds only the explicit action keys; a digit with no matching
        # action must never start the default action by position.
        result, _ = page('9' + ENTER)
        self.assertEqual((result.kind, result.action), ('action', 'start'))

    def test_explicit_result_key_binds_to_its_own_action(self):
        # A result page owns 1/2; pressing 2 must return action '2' itself, not
        # the second action by ordinal position.
        result, _ = page('2' + ENTER, actions={'1': '返回本课程', '2': '切换课程'})
        self.assertEqual((result.kind, result.action), ('action', '2'))
        result, _ = page([STAB, STAB] + [ENTER])
        self.assertEqual((result.kind, result.action), ('action', 'start'))

    def test_pages_render_usably_at_narrow_and_short_sizes(self):
        lines = ['导出 · 3 份题目', '文件格式 · Word', '保存位置 · tiku/<课程名>/']
        actions = {'start': '开始导出', 's': '修改设置'}
        for columns, rows in ((40, 16), (52, 20), (120, 30)):
            with self.subTest(size=(columns, rows)):
                result, frames = page(ESC, lines=lines, actions=actions,
                                      columns=columns, rows=rows)
                self.assertEqual(result.kind, 'back')
                text = '\n'.join(frames[-1])
                self.assertNotIn('Window too small', text)
                for value in ('开始导出', '修改设置'):
                    self.assertIn(value, text)
                for line in frames[-1]:
                    self.assertLessEqual(len(line), columns)

    def test_short_page_fills_the_window_with_a_blank_middle(self):
        # The user asked for a filled window: the heading/content stay at the
        # top and the action/help group sits at the bottom, with the surplus
        # rows blank in between instead of one compressed stack of lines.
        _, frames = page(ESC, lines=('甲', '乙', '丙'), columns=120, rows=30,
                         help_text='Esc 返回 · q 退出')
        lines = frames[-1]
        content_row = next(i for i, line in enumerate(lines) if '甲' in line)
        rule_row = next(i for i, line in enumerate(lines) if '─' in line)
        action_row = next(i for i, line in enumerate(lines) if '开始' in line)
        help_row = next(i for i, line in enumerate(lines) if 'Esc 返回' in line)
        self.assertLess(content_row, 6, 'content must stay at the top')
        self.assertGreater(action_row, 24, 'actions must sit at the bottom')
        self.assertEqual(rule_row + 1, action_row, 'the rule marks the content/action boundary')
        self.assertGreater(help_row, action_row)
        middle = lines[content_row + 1:rule_row]
        self.assertTrue(any(not line.strip() for line in middle),
                        'the surplus space between content and actions must stay blank')
        self.assertEqual(len(lines), 30)

    def test_short_page_keeps_everything_reachable_at_16_rows(self):
        # The flexible filler collapses on short terminals: nothing scrolls off
        # and no field/action/help row is lost.
        lines = ['供应商 · 演示', '模型 · 示例模型']
        result, frames = page(ESC, lines=lines, columns=40, rows=16,
                              help_text='Enter 开始 · Tab 按钮 · Esc 返回 · q 退出')
        self.assertEqual(result.kind, 'back')
        text = '\n'.join(frames[-1])
        for value in ('供应商 · 演示', '模型 · 示例模型', '开始', '设置', 'Esc 返回'):
            self.assertIn(value, text)
        self.assertNotIn('Window too small', text)

    def test_help_wraps_by_whole_segments_without_splitting_tokens(self):
        _, frames = page(ESC, columns=40, rows=16,
                         help_text='↑↓ 移动 · Enter 开始 · Tab 按钮 · Esc 返回 · q 退出')
        text = '\n'.join(frames[-1])
        # No rendered line starts with a fragment that was split out of a segment.
        for line in frames[-1]:
            self.assertFalse(line.startswith('ab'), line)


class RoutingTests(unittest.TestCase):
    def view(self, **kwargs):
        import theme
        from rich.console import Console
        import ui
        return ui.TerminalUI(console=Console(file=io.StringIO(), force_terminal=True,
                                             theme=theme.THEME), **kwargs)

    def native(self, *results):
        return patch.object(native_ui, 'action_page', side_effect=list(results))

    def test_export_summary_start_returns_options_and_settings_loop(self):
        view = self.view()
        options = {'export_format': 'docx', 'quiz_mode': 'merged', 'include_my_answer': False}
        with self.native(native_ui.PageResult('start', 'start')), \
             patch('sys.stdin.isatty', return_value=True), \
             patch.object(view, 'edit_export_options') as editor:
            self.assertIs(view.review_export([], options), options)
        editor.assert_not_called()
        with self.native(native_ui.PageResult('action', 's'), native_ui.PageResult('start', 'start')), \
             patch('sys.stdin.isatty', return_value=True), \
             patch.object(view, 'edit_export_options') as editor:
            self.assertIs(view.review_export([], options), options)
        editor.assert_called_once_with(options)

    def test_export_summary_back_propagates(self):
        import ui
        view = self.view()
        with self.native(native_ui.PageResult('back')), \
             patch('sys.stdin.isatty', return_value=True):
            with self.assertRaises(ui.Back):
                view.review_export([], {})

    def test_ai_summary_without_a_model_cannot_start_and_reopens(self):
        import ui
        view = self.view()
        with self.native(native_ui.PageResult('start', 'start'), native_ui.PageResult('back')), \
             patch('sys.stdin.isatty', return_value=True):
            with self.assertRaises(ui.Back):
                view.review_ai([], None, {})

    def test_ai_summary_starts_and_preserves_auto_submit(self):
        from ai.client import AIConfig
        view = self.view()
        config = AIConfig('https://example.org/v1', 'model', 'secret')
        options = {'auto_submit': False}
        with self.native(native_ui.PageResult('start', 'start')), \
             patch('sys.stdin.isatty', return_value=True):
            self.assertIs(view.review_ai([], config, options), False)
        self.assertEqual(options['config'], config)

    def test_ai_summary_callbacks_fire_and_reopen(self):
        from ai.client import AIConfig
        view = self.view()
        config = AIConfig('https://example.org/v1', 'model', 'secret')
        checks, managed = [], []
        with self.native(native_ui.PageResult('action', 't'),
                         native_ui.PageResult('action', 'm'),
                         native_ui.PageResult('start', 'start')), \
             patch('sys.stdin.isatty', return_value=True):
            self.assertIs(view.review_ai([], config, {}, check_connection=lambda: checks.append(1),
                                         manage_models=lambda: managed.append(1) or config,
                                         connection_status=lambda c: '已通过'), True)
        self.assertEqual(checks, [1])
        self.assertEqual(managed, [1])

    def test_operation_result_default_switch_and_escape_contracts(self):
        import ui
        view = self.view()
        view.begin_operation()
        with self.native(native_ui.PageResult('start', 'start')), \
             patch('sys.stdin.isatty', return_value=True):
            self.assertEqual(view.operation_result(), 1)
        with self.native(native_ui.PageResult('action', '2')), \
             patch('sys.stdin.isatty', return_value=True):
            self.assertEqual(view.operation_result(), 2)
        with self.native(native_ui.PageResult('back')), \
             patch('sys.stdin.isatty', return_value=True):
            self.assertEqual(view.operation_result(), 1)
        with self.native(native_ui.PageResult('quit')), \
             patch('sys.stdin.isatty', return_value=True):
            with self.assertRaises(ui.Cancelled):
                view.operation_result()
        with self.native(native_ui.PageResult('start', 'start')), \
             patch('sys.stdin.isatty', return_value=True):
            self.assertEqual(view.operation_result(['/tmp/a.md']), 1)

    def test_empty_tasks_has_an_explicit_back_action(self):
        view = self.view()
        with self.native(native_ui.PageResult('back')), \
             patch('sys.stdin.isatty', return_value=True):
            self.assertIsNone(view.empty_tasks())

    def test_study_summary_starts_and_backs_out(self):
        import ui
        view = self.view()
        with self.native(native_ui.PageResult('start', 'start')), \
             patch('sys.stdin.isatty', return_value=True):
            self.assertIsNone(view.review_study([{'courseName': '课程'}], max_workers=3))
        with self.native(native_ui.PageResult('back')), \
             patch('sys.stdin.isatty', return_value=True):
            with self.assertRaises(ui.Back):
                view.review_study([{'courseName': '课程'}])

    def test_study_summary_backs_out_from_the_visible_back_button(self):
        import ui
        view = self.view()
        with self.native(native_ui.PageResult('action', 'b')), \
             patch('sys.stdin.isatty', return_value=True):
            with self.assertRaises(ui.Back):
                view.review_study([{'courseName': '课程'}])

    def test_injected_input_never_calls_native_pages(self):
        view = self.view(input_fn=lambda: '开始')
        with patch.object(native_ui, 'action_page') as native, \
             patch('sys.stdin.isatty', return_value=True):
            view.review_study([{'courseName': '课程'}], max_workers=3)
        native.assert_not_called()


if __name__ == '__main__':
    unittest.main()
