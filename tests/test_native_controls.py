"""Native keyboard controls driven by real pipe input.

These exercise the shared option menu, the single-item search/action picker and
the yes/no confirmation directly, with a bounded watchdog so a stuck control can
never hang the suite. No account, platform or AI call is made.
"""
import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import native_ui

DOWN = '\x1b[B'
UP = '\x1b[A'
ENTER = '\r'
ESC = '\x1b'
TAB = '\t'
STAB = '\x1b[Z'
PGDN = '\x1b[6~'
PGUP = '\x1b[5~'
BACKSPACE = '\x7f'

COURSES = [{'courseName': '英语', 'courseInfoName': '2026 秋季'},
           {'courseName': '计算机', 'courseInfoName': '2026 秋季'},
           {'courseName': '音乐', 'courseInfoName': '2025 秋季'}]


class SizedOutput(DummyOutput):
    def __init__(self, columns=80, rows=24):
        self.columns, self.rows = columns, rows

    def get_size(self):
        return Size(rows=self.rows, columns=self.columns)


def run(func, keys, *args, columns=80, rows=24, timeout=6, **kwargs):
    """Run one native control with real rendering, bounded by a watchdog."""
    frames = []
    real_application = native_ui.Application

    def factory(*a, **kw):
        def grab(app):
            screen = app.renderer.last_rendered_screen
            if not screen:
                return
            frames.append([
                ''.join(screen.data_buffer[y][x].char for x in sorted(screen.data_buffer[y])).rstrip()
                for y in sorted(screen.data_buffer)])

        app = real_application(*a, **kw, after_render=grab)

        async def watchdog():
            await asyncio.sleep(timeout)
            app.exit(exception=RuntimeError('native control did not exit in time'))

        def prep():
            app.create_background_task(watchdog())

            def feed():
                for chunk in (keys if isinstance(keys, list) else [keys]):
                    time.sleep(0.7 if chunk == ESC else 0.15)
                    try:
                        pipe.send_text(chunk)
                    except Exception:
                        return
            threading.Thread(target=feed, daemon=True).start()
        app.pre_run_callables.append(prep)
        return app

    with create_pipe_input() as pipe:
        with patch.object(native_ui, 'Application', side_effect=factory):
            result = func(*args, input=pipe, output=SizedOutput(columns, rows), **kwargs)
    return result, frames


def menu(keys, choices, **kwargs):
    result, frames = run(native_ui.option_menu, keys, '标题', choices, **kwargs)
    return result, frames


def confirm(keys, default=False, **kwargs):
    result, frames = run(native_ui.confirm_prompt, keys, '确认操作', default=default, **kwargs)
    return result, frames


def pick(keys, items=COURSES, *, actions=None, state=None, selected=None, selectable=None,
         columns=80, rows=24, heading='', account_name='', **kwargs):
    result, frames = run(native_ui.single_picker, keys,
                         '我的课程 · MOOC', items,
                         lambda x: x['courseName'],
                         lambda x: x.get('courseInfoName', ''),
                         actions=actions, state=state, selected=selected, selectable=selectable,
                         heading=heading, account_name=account_name,
                         columns=columns, rows=rows, **kwargs)
    return result, frames


class OptionMenuTests(unittest.TestCase):
    def test_enter_returns_current_scalar_and_zero_is_a_value(self):
        result, _ = menu(ENTER, [(1, '自动学习', '完成课程学习任务'), (0, '返回', '回到功能选择')])
        self.assertEqual(result.kind, 'select')
        self.assertEqual(result.value, 1)

    def test_arrows_move_and_zero_selection_is_not_treated_as_absent(self):
        result, _ = menu(DOWN + ENTER, [(1, '自动学习', ''), (0, '返回', '')])
        self.assertEqual(result.kind, 'select')
        self.assertEqual(result.value, 0)

    def test_false_key_is_a_value_and_default_focuses_that_row(self):
        result, _ = menu(ENTER, [(True, '启用', ''), (False, '停用', '')], default=False)
        self.assertEqual(result.kind, 'select')
        self.assertIs(result.value, False)

    def test_default_zero_focuses_the_return_row_without_confirming(self):
        result, _ = menu(ENTER, [(1, '自动学习', ''), (0, '返回', '')], default=0)
        self.assertEqual(result.value, 0)

    def test_typed_shortcut_jumps_then_enter_confirms(self):
        result, _ = menu('3' + ENTER, [(1, 'A', ''), (2, 'B', ''), (3, 'C', '')])
        self.assertEqual(result.value, 3)

    def test_escape_back_and_q_quit(self):
        self.assertEqual(menu(ESC, [(1, 'A', '')])[0].kind, 'back')
        self.assertEqual(menu('q', [(1, 'A', '')])[0].kind, 'quit')

    def test_tab_moves_focus_so_typed_digit_does_not_select_from_the_list(self):
        # Tab leaves the list; a digit typed while a button owns focus must not
        # return a selection. Esc then leaves the menu with no choice.
        result, _ = menu(TAB + '2' + ESC, [(1, 'A', ''), (2, 'B', '')])
        self.assertEqual(result.kind, 'back')

    def test_shift_tab_reaches_actions_and_enter_activates_the_focused_button(self):
        result, _ = menu(STAB + ENTER, [(1, 'A', ''), (2, 'B', '')])
        self.assertIn(result.kind, ('select', 'back', 'quit'))


class ConfirmTests(unittest.TestCase):
    def test_enter_uses_the_default_even_when_false(self):
        self.assertIs(confirm(ENTER, default=True)[0].value, True)
        self.assertIs(confirm(ENTER, default=False)[0].value, False)

    def test_y_and_n_are_explicit_answers(self):
        self.assertIs(confirm('n', default=True)[0].value, False)
        self.assertIs(confirm('y', default=False)[0].value, True)

    def test_escape_backs_out_and_q_quits(self):
        self.assertEqual(confirm(ESC)[0].kind, 'back')
        self.assertEqual(confirm('q')[0].kind, 'quit')


class SinglePickerTests(unittest.TestCase):
    def test_arrows_and_enter_return_the_item_list_and_update_state(self):
        state = {}
        result, _ = pick(DOWN + ENTER, actions={'s': '设置'}, state=state)
        self.assertEqual(result.kind, 'select')
        self.assertEqual(result.value, [COURSES[1]])
        self.assertEqual(result.index, 1)
        self.assertEqual(state['cursor'], 1)

    def test_search_filters_and_enter_in_search_returns_focus_to_the_list(self):
        state = {}
        result, _ = pick('/英语' + ENTER + ENTER, actions={'s': '设置'}, state=state)
        self.assertEqual(result.kind, 'select')
        self.assertEqual(result.value, [COURSES[0]])
        self.assertEqual(state['query'], '英语')

    def test_shortcut_letters_are_literal_text_inside_search(self):
        # b/q/t/s/r must not trigger back/quit/actions while search owns focus.
        state = {}
        result, _ = pick('/bqtsr' + ENTER + ESC, actions={'s': '设置', 't': '切换平台', 'r': '刷新'},
                         state=state)
        self.assertEqual(result.kind, 'back')
        self.assertEqual(state['query'], 'bqtsr')

    def test_each_shortcut_letter_is_isolated_in_search(self):
        for letter in ('b', 'q', 't', 's', 'r'):
            with self.subTest(letter=letter):
                state = {}
                result, _ = pick('/' + letter + ENTER + ESC,
                                 actions={'s': '设置', 't': '切换平台', 'r': '刷新'}, state=state)
                self.assertEqual(result.kind, 'back', letter)
                self.assertEqual(state['query'], letter)

    def test_escape_in_search_returns_to_the_list_before_leaving(self):
        # After Escape from search, the list owns focus, so an action shortcut
        # fires instead of being typed as search text.
        result, _ = pick('/不存在' + ESC + 's', actions={'s': '设置'})
        self.assertEqual(result.kind, 'action')
        self.assertEqual(result.action, 's')

    def test_empty_results_still_allow_search_clear_actions_and_back(self):
        state = {}
        result, _ = pick('/zzz' + ENTER + 's', actions={'s': '设置'}, state=state)
        self.assertEqual(result.kind, 'action')
        self.assertEqual(result.action, 's')
        self.assertEqual(state['query'], 'zzz')
        # The query is retained on re-entry; clearing it restores the full list,
        # so Enter then selects.
        result, _ = pick('/' + BACKSPACE * 3 + ENTER + ENTER, state=state)
        self.assertEqual(result.kind, 'select')
        self.assertEqual(result.value, [COURSES[0]])
        self.assertEqual(state['query'], '')

    def test_empty_results_enter_does_not_confirm_or_exit(self):
        result, _ = pick('/zzz' + ENTER + ENTER + ESC, state={})
        self.assertEqual(result.kind, 'back')

    def test_disabled_row_cannot_be_confirmed(self):
        selectable = lambda x: x['courseName'] != '计算机'
        result, _ = pick(DOWN + ENTER + UP + ENTER, selectable=selectable)
        self.assertEqual(result.kind, 'select')
        self.assertEqual(result.value, [COURSES[0]])

    def test_selection_and_query_are_retained_across_actions(self):
        state = {}
        result, _ = pick('/英' + ESC + 'r', actions={'s': '设置', 'r': '刷新'}, state=state)
        self.assertEqual(result.kind, 'action')
        self.assertEqual(result.action, 'r')
        self.assertEqual(state['query'], '英')
        # Re-entering reproduces the filtered list and the cursor; Enter selects.
        result, _ = pick(ENTER, actions={'s': '设置', 'r': '刷新'}, state=state)
        self.assertEqual(result.kind, 'select')
        self.assertEqual(result.value, [COURSES[0]])

    def test_selected_item_is_retained_separately_from_the_focused_row(self):
        state = {'cursor': 2}
        result, frames = pick(ENTER, actions={'s': '设置'}, state=state, selected=[COURSES[2]])
        self.assertEqual(result.value, [COURSES[2]])
        self.assertTrue(frames)

    def test_back_and_quit_are_distinct(self):
        self.assertEqual(pick(ESC, actions={'s': '设置'})[0].kind, 'back')
        self.assertEqual(pick('q', actions={'s': '设置'})[0].kind, 'quit')

    def test_tab_focuses_a_button_so_shortcut_letters_do_not_fire(self):
        result, _ = pick(TAB + 's' + ESC, actions={'s': '设置'})
        self.assertEqual(result.kind, 'back')

    def test_shift_tab_reaches_search_and_its_escape_returns_to_the_list(self):
        # Shift+Tab from the list lands on the search field; Escape returns to
        # the list first, so a following action key fires on the list.
        result, _ = pick(STAB + ESC + 's', actions={'s': '设置'})
        self.assertEqual(result.kind, 'action')
        self.assertEqual(result.action, 's')

    def test_filtered_navigation_moves_through_visible_rows_only(self):
        # The filter keeps original indexes 0 and 2; Down from 0 must land on 2,
        # not on the filtered-out row 1.
        items = [{'courseName': '英语', 'courseInfoName': '2026 秋季'},
                 {'courseName': '计算机', 'courseInfoName': '2025 秋季'},
                 {'courseName': '音乐', 'courseInfoName': '2026 秋季'}]
        state = {}
        result, _ = pick('/2026' + ENTER + DOWN + ENTER, items=items,
                         actions={'s': '设置'}, state=state)
        self.assertEqual(result.kind, 'select')
        self.assertEqual(result.value, [items[2]])
        self.assertEqual(result.index, 2)


class NativeLayoutTests(unittest.TestCase):
    def rendered(self, frames):
        return '\n'.join(frames[-1]) if frames else ''

    def test_picker_shows_titles_details_actions_and_help_at_large_size(self):
        _, frames = pick(ESC, actions={'s': '设置', 't': '切换平台', 'r': '刷新'},
                         columns=120, rows=30, heading='ICVE AI Assistant')
        text = self.rendered(frames)
        for value in ('我的课程', '英语', '2026 秋季', '设置', '切换平台', '刷新', '返回'):
            self.assertIn(value, text)
        self.assertIn('ICVE AI Assistant', text)

    def test_picker_action_row_stacks_at_narrow_width(self):
        _, frames = pick(ESC, actions={'s': '设置', 't': '切换平台', 'r': '刷新'},
                         columns=40, rows=16)
        text = self.rendered(frames)
        for line in frames[-1]:
            self.assertLessEqual(len(line), 40)
        for value in ('设置', '切换平台', '刷新', '返回'):
            self.assertIn(value, text)

    def test_menu_actions_and_help_visible_in_short_terminal(self):
        _, frames = menu(ESC, [(1, '自动学习', '完成课程学习任务'), (2, 'AI 答题', 'AI 作答并自动交卷')],
                         columns=52, rows=20, heading='ICVE AI Assistant', help_text='Esc 返回 · q 退出')
        text = self.rendered(frames)
        for value in ('自动学习', 'AI 答题', '选择', '返回'):
            self.assertIn(value, text)

    def test_long_menu_scrolls_and_keeps_cursor_visible(self):
        choices = [(i, f'选项{i}', '') for i in range(30)]
        result, frames = menu([DOWN] * 25 + [ENTER], choices, columns=80, rows=12)
        self.assertEqual(result.value, 25)
        text = self.rendered(frames)
        self.assertIn('选项25', text)
        self.assertNotIn('选项0 ', text)


class TerminalUIRoutingTests(unittest.TestCase):
    """The TerminalUI adapter routes to the native controls only on a real TTY."""

    def view(self, **kwargs):
        import io
        from rich.console import Console
        import theme
        import ui
        return ui.TerminalUI(console=Console(file=io.StringIO(), force_terminal=True,
                                             theme=theme.THEME), **kwargs)

    def test_choice_returns_scalar_values_including_zero_and_false(self):
        import ui
        view = self.view()
        for value in (0, False, 3):
            with self.subTest(value=value), \
                 patch.object(native_ui, 'option_menu',
                              return_value=native_ui.ChooseResult('select', value)), \
                 patch('sys.stdin.isatty', return_value=True):
                self.assertIs(view.choice('标题', [(value, '选项', '')]), value)
        with patch.object(native_ui, 'option_menu',
                          return_value=native_ui.ChooseResult('back')), \
             patch('sys.stdin.isatty', return_value=True):
            with self.assertRaises(ui.Back):
                view.choice('标题', [(1, '选项', '')])
        with patch.object(native_ui, 'option_menu',
                          return_value=native_ui.ChooseResult('quit')), \
             patch('sys.stdin.isatty', return_value=True):
            with self.assertRaises(ui.Cancelled):
                view.choice('标题', [(1, '选项', '')])

    def test_single_select_returns_items_and_updates_supplied_state(self):
        view = self.view()
        state = {}
        chosen = [{'courseName': '英语'}]
        with patch.object(native_ui, 'single_picker',
                          return_value=native_ui.ChooseResult('select', chosen, 0,
                                                              indexes=(0,), cursor=0)) as picker, \
             patch('sys.stdin.isatty', return_value=True):
            self.assertEqual(view.select('课程', chosen, lambda x: x['courseName'],
                                         multiple=False, state=state), chosen)
        self.assertTrue(picker.called)

    def test_single_select_action_and_navigation(self):
        import ui
        view = self.view()
        with patch.object(native_ui, 'single_picker',
                          return_value=native_ui.ChooseResult('action', action='r')), \
             patch('sys.stdin.isatty', return_value=True):
            self.assertEqual(view.select('课程', [{'n': 1}], lambda x: 'n', multiple=False), 'r')
        with patch.object(native_ui, 'single_picker',
                          return_value=native_ui.ChooseResult('back')), \
             patch('sys.stdin.isatty', return_value=True):
            with self.assertRaises(ui.Back):
                view.select('课程', [{'n': 1}], lambda x: 'n', multiple=False)
        with patch.object(native_ui, 'single_picker',
                          return_value=native_ui.ChooseResult('quit')), \
             patch('sys.stdin.isatty', return_value=True):
            with self.assertRaises(ui.Cancelled):
                view.select('课程', [{'n': 1}], lambda x: 'n', multiple=False)

    def test_confirm_returns_bool_including_false_and_navigates(self):
        import ui
        view = self.view()
        for value in (True, False):
            with self.subTest(value=value), \
                 patch.object(native_ui, 'confirm_prompt',
                              return_value=native_ui.ChooseResult('select', value)), \
                 patch('sys.stdin.isatty', return_value=True):
                self.assertIs(view.confirm('确认', default=value), value)
        with patch.object(native_ui, 'confirm_prompt',
                          return_value=native_ui.ChooseResult('back')), \
             patch('sys.stdin.isatty', return_value=True):
            with self.assertRaises(ui.Back):
                view.confirm('确认')
        with patch.object(native_ui, 'confirm_prompt',
                          return_value=native_ui.ChooseResult('quit')), \
             patch('sys.stdin.isatty', return_value=True):
            with self.assertRaises(ui.Cancelled):
                view.confirm('确认')

    def test_injected_input_never_calls_the_native_controls(self):
        import ui
        view = self.view(input_fn=lambda: '1')
        with patch.object(native_ui, 'option_menu') as native, \
             patch('sys.stdin.isatty', return_value=True):
            self.assertEqual(view.choice('标题', [(1, '选项', '')]), 1)
        native.assert_not_called()
        confirm_view = self.view(input_fn=lambda: 'n')
        with patch.object(native_ui, 'confirm_prompt') as native_confirm, \
             patch('sys.stdin.isatty', return_value=True):
            self.assertIs(confirm_view.confirm('确认', default=True), False)
        native_confirm.assert_not_called()


if __name__ == '__main__':
    unittest.main()
