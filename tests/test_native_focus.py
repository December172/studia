"""Focused-button contrast through the real prompt_toolkit renderer.

The original defect: a focused button's label/arrow fragments carry
``class:button.focused class:button.text`` (and ``.arrow``). The flat
``button.focused`` rule sets the window background, but the later fragment
inherits ``class:button``'s *foreground*, producing fg == bg == #faf9f5 and
invisible text. These tests read the actual rendered cells (character, style
string and resolved attributes), not just the presence of a label in output.
"""
import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import theme

DOWN = '\x1b[B'
FOCUS_BG = theme.TEXT.lstrip('#')  # prompt_toolkit reports bgcolor without '#'


class SizedOutput(DummyOutput):
    def __init__(self, columns=120, rows=30):
        self.columns, self.rows = columns, rows

    def get_size(self):
        return Size(rows=self.rows, columns=self.columns)


def run_frames(module, func, chunks, *args, columns=120, rows=30, timeout=6, output=None,
               **kwargs):
    """Run a native form with real rendering, capturing resolved cell attrs.

    ``chunks`` is sent progressively so intermediate (focused) frames render; a
    callable chunk runs in the feeder with the running application instead of
    being typed, which lets a test resize the terminal the way a real SIGWINCH
    would (change the size, then invalidate). A watchdog background task exits
    the app with an exception if the final chunk fails to close it, so a
    capture can never hang the suite.
    """
    frames = []
    real_application = module.Application

    def application(*a, **kw):
        def capture(app):
            screen = app.renderer.last_rendered_screen
            if not screen:
                return
            cache = getattr(app.renderer, '_attrs_for_style', None)
            lines = []
            for y in sorted(screen.data_buffer):
                line = []
                for x in sorted(screen.data_buffer[y]):
                    cell = screen.data_buffer[y][x]
                    attrs = cache[cell.style] if cache is not None else None
                    line.append((cell.char, cell.style, attrs))
                lines.append(line)
            frames.append(lines)
        app = real_application(*a, **kw, after_render=capture)

        async def watchdog():
            await asyncio.sleep(timeout)
            app.exit(exception=RuntimeError('native form did not close in time'))

        def schedule():
            app.create_background_task(watchdog())

            def feed():
                for text in chunks:
                    time.sleep(0.2)
                    if callable(text):
                        # Resize-style steps get the running app so they can
                        # invalidate it exactly like a real SIGWINCH would.
                        text(app)
                        continue
                    try:
                        pipe.send_text(text)
                    except Exception:
                        return
            threading.Thread(target=feed, daemon=True).start()
        app.pre_run_callables.append(schedule)
        return app

    with create_pipe_input() as pipe:
        with patch.object(module, 'Application', side_effect=application):
            result = func(*args, input=pipe, output=output or SizedOutput(columns, rows), **kwargs)
    return result, frames


def _is_focused_button_cell(style, attrs):
    return 'button.focused' in style and attrs is not None and attrs.bgcolor == FOCUS_BG


def visible_cells(line):
    return [(char, style, attrs) for char, style, attrs in line if char.strip()]


def button_focused_frame(frames, label=None):
    """The last frame in which a button owns the only focused background."""
    found = None
    for lines in frames:
        for line in lines:
            if any(_is_focused_button_cell(style, attrs) and char.strip()
                   for char, style, attrs in line) and (label is None or label in
                                                        ''.join(c for c, _, _ in line)):
                found = lines
                break
    return found


def focused_frame(frames, label):
    """Lines of the frame whose focused button row carries ``label``."""
    for lines in frames:
        for line in lines:
            text = ''.join(c for c, _, _ in line)
            if label not in text or not any(c in ('<', '>') for c, _, _ in line):
                continue
            if any(_is_focused_button_cell(style, attrs) and char.strip()
                   for char, style, attrs in line):
                return lines
    return None


def focused_row(frames, label):
    frame = focused_frame(frames, label)
    if frame is None:
        return None
    candidates = [line for line in frame if label in ''.join(c for c, _, _ in line)]
    return next((line for line in candidates if any(c in ('<', '>') for c, _, _ in line)),
                candidates[0] if candidates else None)


class FocusContrastTests(unittest.TestCase):
    def assert_readable(self, cells, label):
        """Every visible glyph of the focused control must contrast its own bg."""
        self.assertIsNotNone(cells, f'{label}: no focused frame captured')
        checked = 0
        for char, style, attrs in cells:
            if not char.strip() or attrs is None:
                continue
            if 'button.focused' not in style:
                continue
            self.assertNotEqual(attrs.color or '', attrs.bgcolor or '',
                                f'{label}: {char!r} has fg==bg ({attrs.color})')
            checked += 1
        self.assertGreater(checked, 0, f'{label}: focused button had no visible glyphs')

    def assert_row_has_no_focus_background(self, cells, label):
        """A list row must never carry the focused control's background.

        The row is checked through its *resolved* background, not through the
        ``button.focused`` class name: a row style that wrongly painted the
        cursor background would still be a plain row fragment.
        """
        checked = 0
        for char, style, attrs in visible_cells(cells):
            if attrs is None:
                continue
            self.assertNotEqual(attrs.bgcolor or '', FOCUS_BG,
                                f'{label}: {char!r} ({style}) claims the focus background')
            checked += 1
        self.assertGreater(checked, 0, f'{label}: no rendered row cells')

    def test_model_picker_focused_button_cells_have_contrast(self):
        import model_ui
        result, frames = run_frames(model_ui, model_ui.choose_model,
                                    ['\t', '\x1b'], [('a', '供应商一', 'same', '?')], 'a')
        self.assertEqual(result.action, 'back')
        cells = focused_row(frames, '使用此模型')
        self.assert_readable(cells, '模型使用按钮')
        self.assertTrue(any('class:button.focused' in style and 'class:button.text' in style
                            for char, style, attrs in cells if char.strip()),
                        'focused label glyphs must carry class:button.focused class:button.text')
        self.assertTrue(any('class:button.focused' in style and 'class:button.arrow' in style
                            for char, style, attrs in cells if char.strip()),
                        'focused arrows must carry class:button.focused class:button.arrow')

    def test_model_picker_add_row_only_offers_add_and_back(self):
        import model_ui
        rows = [('a', '供应商一', 'same', '?'), ('b', '供应商二', 'same', '?')]
        result, frames = run_frames(model_ui, model_ui.choose_model,
                                    ['\x1b[B\x1b[B\t', '\x1b'], rows, 'a')
        self.assertEqual(result.action, 'back')
        self.assert_readable(focused_row(frames, '添加模型'), '模型添加按钮')
        frame = focused_frame(frames, '添加模型')
        add_row = next(line for line in frame if '添加模型' in ''.join(c for c, _, _ in line)
                       and '返回' in ''.join(c for c, _, _ in line))
        text = ''.join(c for c, _, _ in add_row)
        self.assertNotIn('使用此模型', text)
        self.assertNotIn('测试连接', text)
        self.assertNotIn('编辑', text)

    def test_model_picker_builtin_hides_edit_and_keeps_use_and_test(self):
        import model_ui
        result, frames = run_frames(model_ui, model_ui.choose_model,
                                    ['\t', '\x1b'], [('a', '默认供应商', 'model', '?')], 'a',
                                    locked_ids={'a'})
        self.assert_readable(focused_row(frames, '使用此模型'), '内置使用按钮')
        frame = focused_frame(frames, '使用此模型')
        action_row = next(line for line in frame if '使用此模型' in ''.join(c for c, _, _ in line))
        self.assertIn('测试连接', ''.join(c for c, _, _ in action_row))
        self.assertNotIn('编辑', ''.join(c for c, _, _ in action_row))

    def test_model_picker_custom_row_offers_edit(self):
        import model_ui
        rows = [('a', '供应商一', 'same', '?'), ('b', '供应商二', 'same', '?')]
        _, frames = run_frames(model_ui, model_ui.choose_model, ['\t', '\x1b'], rows, 'a')
        self.assert_readable(focused_row(frames, '使用此模型'), '自定义使用按钮')
        frame = focused_frame(frames, '使用此模型')
        action_row = next(line for line in frame if '编辑' in ''.join(c for c, _, _ in line))
        self.assertIn('编辑', ''.join(c for c, _, _ in action_row))

    def test_model_picker_highlight_is_not_a_focused_control(self):
        import model_ui
        _, frames = run_frames(model_ui, model_ui.choose_model, ['\t', '\x1b'],
                               [('a', '供应商一', 'same', '?')], 'a')
        frame = button_focused_frame(frames, '使用此模型')
        self.assertIsNotNone(frame, 'no button-focused frame captured')
        list_row = next(line for line in frame if '供应商一' in ''.join(c for c, _, _ in line))
        # The retained row keeps its primary foreground but must not claim the
        # focused button background while focus is on the actions.
        self.assert_row_has_no_focus_background(list_row, '模型行')

    def test_model_editor_focused_save_button_has_contrast(self):
        import model_ui
        result, frames = run_frames(model_ui, model_ui.edit_model, ['\t\t\t\t', '\x1b'])
        self.assertEqual(result.action, 'back')
        self.assert_readable(focused_row(frames, '保存并测试'), '模型保存按钮')

    def test_concurrency_focused_button_has_contrast(self):
        import concurrency_ui
        result, frames = run_frames(concurrency_ui, concurrency_ui.edit_concurrency,
                                    ['\t\t\t', '\x1b'], {'ai': 5, 'study': 6})
        self.assertIsNone(result)
        self.assert_readable(focused_row(frames, '恢复默认'), '并发恢复默认按钮')

    def test_concurrency_default_actions_have_contrast(self):
        import concurrency_ui
        result, frames = run_frames(concurrency_ui, concurrency_ui.edit_concurrency,
                                    ['\t', '\x1b'], {'ai': 5, 'study': 6})
        self.assertIsNone(result)
        self.assert_readable(focused_row(frames, '保存'), '并发保存按钮')

    def test_concurrency_row_is_not_focused_while_a_button_owns_focus(self):
        import concurrency_ui
        _, frames = run_frames(concurrency_ui, concurrency_ui.edit_concurrency,
                               ['\t', '\x1b'], {'ai': 5, 'study': 6})
        frame = button_focused_frame(frames, '保存')
        self.assertIsNotNone(frame, 'no button-focused frame captured')
        row = next(line for line in frame if 'AI答题' in ''.join(c for c, _, _ in line))
        self.assert_row_has_no_focus_background(row, '并发行')

    def test_checklist_cursor_row_uses_focused_style(self):
        import checklist
        result, frames = run_frames(checklist, checklist.choose_many, ['\x1b'],
                                    '任务', [('任务0', '', '作业')])
        self.assertEqual(result.action, 'back')
        rows = [line for lines in frames for line in lines
                if any('class:cursor' in style and attrs is not None
                       and attrs.bgcolor == FOCUS_BG and char.strip()
                       for char, style, attrs in line)]
        self.assertTrue(rows, '任务行: no focused cursor row captured')

    def test_checklist_row_drops_the_cursor_background_when_a_button_is_focused(self):
        import checklist
        _, frames = run_frames(checklist, checklist.choose_many,
                               [' ', '\t', '\x1b'],
                               '任务', [('任务0', '未作答', '作业'), ('任务1', '未作答', '作业')])
        # The frame where a button owns focus must not also paint a row cursor.
        button_frames = [lines for lines in frames
                         if any(_is_focused_button_cell(s, a) and c.strip()
                                for line in lines for c, s, a in line)]
        self.assertTrue(button_frames, 'no focused-button frame captured')
        for lines in button_frames:
            for line in lines:
                self.assertFalse(
                    any('class:cursor' in s and a is not None and a.bgcolor == FOCUS_BG
                        and c.strip() for c, s, a in line),
                    'a checklist row still claims the cursor background while a button is focused')
        # The highlighted row keeps its > marker and the checked task stays selected.
        frame = button_frames[-1]
        self.assertTrue(any('>' in ''.join(c for c, _, _ in line) and '任务0' in
                            ''.join(c for c, _, _ in line) for line in frame))
        self.assertTrue(any('[x]' in ''.join(c for c, _, _ in line) for line in frame))


class LongListAnchorTests(unittest.TestCase):
    """A Tab-focused action must not throw the list back to its first row.

    The list window scrolls to its content's cursor position. Dropping that
    anchor when the body loses focus moved the highlight off screen while the
    button still acted on the retained row.
    """

    def screen_text(self, lines):
        return '\n'.join(''.join(c for c, _, _ in line) for line in lines)

    def test_model_picker_keeps_the_highlighted_row_visible_after_tab(self):
        import model_ui
        rows = [(f'm{i:02d}', f'供应商{i:02d}', f'model-{i:02d}', '?') for i in range(30)]
        result, frames = run_frames(model_ui, model_ui.choose_model,
                                    [DOWN * 24, '\t', '\x1b'], rows, 'm00',
                                    columns=40, rows=16, timeout=10)
        self.assertEqual(result.action, 'back')
        frame = button_focused_frame(frames, '使用此模型')
        self.assertIsNotNone(frame, 'no button-focused frame captured')
        text = self.screen_text(frame)
        self.assertIn('供应商24', text.split('使用此模型')[0])
        self.assertNotIn('供应商00', text.split('使用此模型')[0])

    def test_checklist_keeps_the_highlighted_row_visible_after_tab(self):
        import checklist
        rows = [(f'任务{i:02d}', '未作答', '作业') for i in range(30)]
        _, frames = run_frames(checklist, checklist.choose_many,
                               [DOWN * 24, '\t', '\x1b'], '选择任务', rows,
                               columns=40, rows=16, timeout=10)
        frame = button_focused_frame(frames, '确认')
        self.assertIsNotNone(frame, 'no button-focused frame captured')
        text = self.screen_text(frame)
        self.assertIn('任务24', text.split('确认')[0])
        self.assertNotIn('任务00', text.split('确认')[0])


class SharedFrameFillTests(unittest.TestCase):
    """Every native page fills the window: content on top, actions at the bottom.

    The user asked for a filled window with a blank middle (窗口铺满，中间留空):
    headings and content stay at the top, the rule/actions/status/help/footer
    group sits at the bottom, and the surplus rows in between stay blank. The
    flexible filler must give way on short terminals so nothing becomes
    unreachable, and a live resize must reflow naturally.
    """

    def screen(self, lines):
        return [''.join(c for c, _, _ in line).rstrip() for line in lines]

    def test_short_model_page_keeps_actions_at_the_bottom(self):
        import model_ui
        _, frames = run_frames(model_ui, model_ui.choose_model, ['\x1b'],
                               [('a', '供应商一', 'model', '?')], 'a')
        lines = self.screen(frames[-1])
        self.assertEqual(len(lines), 30)
        content = next(i for i, line in enumerate(lines) if '供应商一' in line)
        rule = next(i for i, line in enumerate(lines) if '─' in line)
        action = next(i for i, line in enumerate(lines) if '使用此模型' in line)
        self.assertLess(content, 5, 'content must stay at the top')
        self.assertGreater(action, 24, 'actions must sit at the bottom')
        self.assertEqual(rule + 1, action, 'the rule marks the content/action boundary')
        self.assertTrue(any(not line.strip() for line in lines[content + 1:rule]),
                        'the surplus rows between content and actions must stay blank')
        self.assertIn('Esc 返回', '\n'.join(lines[action:]))

    def test_short_checklist_keeps_its_actions_at_the_bottom(self):
        import checklist
        _, frames = run_frames(checklist, checklist.choose_many, ['\x1b'], '选择任务',
                               [('第一章 测验', '已提交', '测验')])
        lines = self.screen(frames[-1])
        self.assertEqual(len(lines), 30)
        action = next(i for i, line in enumerate(lines) if '确认' in line)
        self.assertGreater(action, 24, 'checklist actions must sit at the bottom')
        self.assertLess(next(i for i, line in enumerate(lines) if '第一章' in line), 5)
        self.assertIn('空格 勾选/取消', '\n'.join(lines[action:]))

    def test_short_page_collapses_the_filler_at_16_rows(self):
        import model_ui
        result, frames = run_frames(model_ui, model_ui.choose_model, ['\x1b'],
                                    [('a', '供应商一', 'model', '?')], 'a',
                                    columns=40, rows=16)
        self.assertEqual(result.action, 'back')
        text = '\n'.join(self.screen(frames[-1]))
        self.assertNotIn('Window too small', text)
        for label in ('供应商一', '使用此模型', '测试连接', '编辑', '返回', 'Esc 返回'):
            self.assertIn(label, text)
        self.assertEqual(len(self.screen(frames[-1])), 16)

    def test_resize_reflows_the_filler_without_hiding_rows(self):
        import model_ui
        output = SizedOutput(120, 30)

        def shrink(app):
            size = output.get_size()
            self.assertEqual((size.rows, size.columns), (30, 120))
            output.columns, output.rows = 40, 16
            # A real SIGWINCH makes the app notice and repaint; this is the same
            # path a terminal resize takes for this application.
            app.invalidate()

        rows = [(f'm{i:02d}', f'供应商{i:02d}', f'model-{i:02d}', '?') for i in range(30)]
        result, frames = run_frames(model_ui, model_ui.choose_model, [shrink, '\x1b'],
                                    rows, 'm00', output=output, timeout=10)
        self.assertEqual(result.action, 'back')
        lines = self.screen(frames[-1])
        self.assertEqual(len(lines), 16, 'the frame must follow the new terminal height')
        text = '\n'.join(lines)
        self.assertNotIn('Window too small', text)
        for label in ('供应商00', '使用此模型', '测试连接', '编辑', '返回'):
            self.assertIn(label, text)


class ScopedBindingTests(unittest.TestCase):
    def form(self, chunks, current=None):
        import concurrency_ui
        result, _ = run_frames(concurrency_ui, concurrency_ui.edit_concurrency, chunks, current)
        return result

    def test_arrows_do_not_edit_a_row_while_a_button_owns_focus(self):
        # ai=4, focus a button, Left must be ignored, then back to fields, Save.
        self.assertEqual(self.form(['4\t', '\x1b[D', '\x1b[Z\t\r']), {'ai': 4, 'study': 8})

    def test_digits_do_not_edit_a_row_while_a_button_owns_focus(self):
        self.assertEqual(self.form(['4\t', '5', '\x1b[Z\t\r']), {'ai': 4, 'study': 8})

    def test_arrows_still_adjust_while_the_field_list_is_focused(self):
        self.assertEqual(self.form(['4\t\x1b[Z', '\x1b[D\t\r']), {'ai': 3, 'study': 8})


if __name__ == '__main__':
    unittest.main()
