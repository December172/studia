"""Concurrency form tests. Draft-only editing: nothing persists or mutates the input."""
import io
import threading
import time
import unittest

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth, DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

KEYS = {'up': '\x1b[A', 'down': '\x1b[B', 'left': '\x1b[D', 'right': '\x1b[C',
        'enter': '\r', 'tab': '\t', 'shift-tab': '\x1b[Z', 'escape': '\x1b'}


class ConcurrencyUITests(unittest.TestCase):
    def form(self, keys, current=None):
        from concurrency_ui import edit_concurrency
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            return edit_concurrency(current, input=pipe, output=DummyOutput())

    def terminal_form(self, keys, current=None, columns=80, rows=24):
        from concurrency_ui import edit_concurrency
        stream = io.StringIO()
        output = Vt100_Output(stream, lambda: Size(rows=rows, columns=columns),
                              term='xterm-256color', default_color_depth=ColorDepth.DEPTH_8_BIT,
                              enable_cpr=False)
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            result = edit_concurrency(current, input=pipe, output=output)
        return result, stream.getvalue()

    def test_defaults_are_eight_and_arrows_adjust_the_selected_row(self):
        self.assertEqual(self.form(KEYS['tab'] + KEYS['enter']), {'ai': 8, 'study': 8})
        self.assertEqual(self.form(KEYS['down'] + KEYS['left'] + KEYS['tab'] + KEYS['enter']),
                         {'ai': 8, 'study': 7})
        self.assertEqual(self.form(KEYS['left'] * 3 + KEYS['tab'] + KEYS['enter']),
                         {'ai': 5, 'study': 8})

    def test_missing_or_invalid_current_falls_back_to_eight_and_saved_values_are_kept(self):
        self.assertEqual(self.form(KEYS['tab'] + KEYS['enter']), {'ai': 8, 'study': 8})
        for current in ({'ai': 99, 'study': '3'}, {'ai': None}, {'study': 0}, {'ai': True, 'study': 9}):
            with self.subTest(current=current):
                self.assertEqual(self.form(KEYS['tab'] + KEYS['enter'], current),
                                 {'ai': 8, 'study': 8})
        for current in ({'ai': 1, 'study': 8}, {'ai': 8, 'study': 2}):
            with self.subTest(current=current):
                self.assertEqual(self.form(KEYS['tab'] + KEYS['enter'], current), current)

    def test_digits_replace_only_the_selected_row_and_save_returns_the_draft(self):
        self.assertEqual(self.form('3' + KEYS['down'] + '5' + KEYS['tab'] + KEYS['enter']),
                         {'ai': 3, 'study': 5})
        # Each digit replaces the whole count, so a run keeps the last valid one.
        self.assertEqual(self.form('17' + KEYS['tab'] + KEYS['enter']), {'ai': 7, 'study': 8})

    def test_invalid_digits_show_a_hint_and_never_produce_invalid_counts(self):
        self.assertEqual(self.form('9' + KEYS['down'] + '0' + KEYS['tab'] + KEYS['enter']),
                         {'ai': 8, 'study': 8})
        self.assertEqual(self.form('93' + KEYS['tab'] + KEYS['enter']), {'ai': 3, 'study': 8})
        result, rendered = self.terminal_form('0' + KEYS['escape'])
        self.assertIsNone(result)
        self.assertIn('并发数只能为 1–8', rendered)
        self.assertNotIn('Window too small', rendered)

    def test_reset_restores_eight_in_the_draft_and_only_saves_when_asked(self):
        current = {'ai': 1, 'study': 2}
        cancel = self.form('4' + KEYS['down'] + '3' + KEYS['tab'] * 3 + KEYS['enter'] + KEYS['escape'],
                           current)
        self.assertIsNone(cancel)
        self.assertEqual(current, {'ai': 1, 'study': 2})
        saved = self.form('4' + KEYS['down'] + '3' + KEYS['tab'] * 3 + KEYS['enter']
                          + KEYS['tab'] * 2 + KEYS['enter'], current)
        self.assertEqual(saved, {'ai': 8, 'study': 8})
        self.assertEqual(current, {'ai': 1, 'study': 2})

    def test_cancel_keys_return_none_and_leave_current_untouched(self):
        current = {'ai': 6, 'study': 7}
        for keys in (KEYS['escape'], '\x03', '\x04'):
            with self.subTest(keys=keys):
                self.assertIsNone(self.form(keys, current))
                self.assertEqual(current, {'ai': 6, 'study': 7})

    def test_enter_on_a_field_moves_focus_and_only_a_button_activates_its_handler(self):
        # Enter leaves the row list to Save without saving; Escape still cancels.
        self.assertIsNone(self.form(KEYS['enter'] + KEYS['escape']))
        self.assertEqual(self.form(KEYS['enter'] + KEYS['enter']), {'ai': 8, 'study': 8})

    def test_tab_and_shift_tab_reach_visible_buttons_in_order(self):
        current = {'ai': 1, 'study': 2}
        self.assertEqual(self.form(KEYS['tab'] + KEYS['enter'], current), current)
        self.assertIsNone(self.form(KEYS['tab'] * 2 + KEYS['enter'], current))
        self.assertEqual(self.form(KEYS['tab'] * 2 + KEYS['shift-tab'] + KEYS['enter'], current),
                         current)

    def test_left_right_are_clamped_and_up_down_never_change_a_count(self):
        self.assertEqual(self.form(KEYS['left'] * 12 + KEYS['tab'] + KEYS['enter']),
                         {'ai': 1, 'study': 8})
        self.assertEqual(self.form(KEYS['down'] + KEYS['right'] * 12 + KEYS['tab'] + KEYS['enter']),
                         {'ai': 8, 'study': 8})
        self.assertEqual(self.form(KEYS['up'] * 3 + KEYS['down'] * 5 + KEYS['tab'] + KEYS['enter']),
                         {'ai': 8, 'study': 8})

    def test_invalid_digit_frame_keeps_the_form_open_at_the_draft_value(self):
        from concurrency_ui import edit_concurrency
        stream = io.StringIO()
        output = Vt100_Output(stream, lambda: Size(rows=24, columns=80),
                              term='xterm-256color', default_color_depth=ColorDepth.DEPTH_8_BIT,
                              enable_cpr=False)
        pipe_context = create_pipe_input()
        pipe = pipe_context.__enter__()
        self.addCleanup(pipe_context.__exit__, None, None, None)
        state = {}

        def run():
            try:
                state['value'] = edit_concurrency({'ai': 5, 'study': 6}, input=pipe, output=output)
            finally:
                state['done'] = True

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        pipe.send_text('0')
        self.wait_until(lambda: '并发数只能为 1–8' in stream.getvalue(), 'invalid digit hint')
        self.assertFalse(state.get('done'), 'invalid input must keep the form open')
        pipe.send_text(KEYS['escape'])
        self.wait_until(lambda: state.get('done'), 'form exits after cancel')
        self.assertIsNone(state['value'])

    def test_save_returns_an_independent_valid_dictionary(self):
        # A returned draft is always persisted-shape input, never a live reference.
        self.assertEqual(sorted(self.form(KEYS['tab'] + KEYS['enter'])), ['ai', 'study'])
        for value in self.form('5' + KEYS['down'] + '2' + KEYS['tab'] + KEYS['enter']).values():
            self.assertIs(type(value), int)
            self.assertTrue(1 <= value <= 8)

    def test_small_terminal_renders_labels_buttons_and_next_run_footer(self):
        result, rendered = self.terminal_form(KEYS['tab'] + KEYS['enter'], columns=60, rows=16)
        self.assertEqual(result, {'ai': 8, 'study': 8})
        for text in ('AI答题', '自动学习', '保存', '取消', '恢复默认', '保存后下次运行生效'):
            self.assertIn(text, rendered)
        self.assertNotIn('Window too small', rendered)

    def wait_until(self, predicate, description):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail('timed out waiting for ' + description)


if __name__ == '__main__':
    unittest.main()
