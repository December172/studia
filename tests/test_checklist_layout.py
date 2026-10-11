"""Check the real terminal screen, including CJK cell widths and scrolling."""
import unittest
from unittest.mock import patch

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth

import checklist


class SizedOutput(DummyOutput):
    def __init__(self, columns, rows=18):
        self.columns, self.rows = columns, rows

    def get_size(self):
        return Size(rows=self.rows, columns=self.columns)


def render(rows, columns, *, extra_headers=(), cursor=1, keys='q', account_name='', row_hints=(),
           rows_count=18, **kwargs):
    screens = []

    def application(*args, **kwargs2):
        def capture(app):
            screen = app.renderer.last_rendered_screen
            if screen:
                screens.append([
                    ''.join(screen.data_buffer[y][x].char for x in range(columns)).rstrip()
                    for y in range(rows_count)
                ])
        return Application(*args, **kwargs2, after_render=capture)

    with create_pipe_input() as pipe, patch('checklist.Application', side_effect=application):
        pipe.send_text(keys)
        result = checklist.choose_many('选择任务', rows, cursor=cursor,
                                       extra_headers=extra_headers,
                                       account_name=account_name,
                                       row_hints=row_hints,
                                       input=pipe, output=SizedOutput(columns, rows_count), **kwargs)
    return screens[0], result


def cell_position(line, value):
    return get_cwidth(line[:line.index(value)])


class ChecklistLayoutTests(unittest.TestCase):
    def test_focused_task_displays_full_opening_window_in_narrow_and_wide_terminal(self):
        for width in (52,80,120):
            with self.subTest(width=width):
                screen,_=render([('拓展测试1','未开始','考试')],width,
                    row_hints=['未开始；北京时间：2026-12-28 00:00:00 至 2026-12-31 23:59:59'])
                text='\n'.join(screen)
                for value in ('未开始','2026-12-28','00:00:00','2026-12-31','23:59:59'):
                    self.assertIn(value,text)

    def test_account_name_appears_once_on_first_footer_line(self):
        screen, _ = render([('任务', '未作答', '作业')], 80, account_name='张三')
        matches = [line for line in screen if '张三' in line]
        self.assertEqual(len(matches), 1)
        self.assertIn('Enter 确认', matches[0])
        self.assertTrue(matches[0].endswith('张三'))

    def test_headers_and_mixed_length_values_share_fixed_columns(self):
        rows = [
            ('中文标题很长' * 10, '已提交', '作业', '99.5', '9999'),
            ('短标题', '未作答', '考试', '?', '?'),
            ('其他任务', '已提交', '作业', '--', '不限'),
        ]
        for columns in (52, 80, 120):
            with self.subTest(columns=columns):
                screen, _ = render(rows, columns, extra_headers=('成绩', '剩余次数'))
                headers = [line for line in screen if '名称' in line and '状态' in line]
                self.assertEqual(len(headers), 1, screen)
                header = headers[0]
                data = [line for line in screen if '已提交' in line or '未作答' in line]
                self.assertEqual(len(data), 3)
                for line, row in zip(data, rows):
                    for name, value in zip(('状态', '成绩', '剩余', '类型'),
                                           (row[1], row[3], row[4], row[2])):
                        # Two unknown values occupy separate metric cells.
                        start = cell_position(header, name)
                        positions = [get_cwidth(line[:i]) for i in range(len(line))
                                     if line.startswith(value, i)]
                        self.assertIn(start, positions, line)
                    self.assertLessEqual(get_cwidth(line), columns)
                self.assertGreaterEqual(cell_position(header, '状态') - 6, 12)
                self.assertTrue(any('All 全选' in line for line in screen))

    def test_header_remains_visible_when_list_scrolls_and_cursor_keeps_row_index(self):
        rows = [(f'任务{i}', '未作答', '作业', '?', '?') for i in range(40)]
        screen, result = render(rows, 52, extra_headers=('成绩', '剩余次数'),
                                cursor=40, keys=' \r')
        self.assertTrue(any('名称' in line and '成绩' in line for line in screen), screen)
        self.assertTrue(any('任务39' in line for line in screen), screen)
        self.assertEqual(result.indexes, [39])
        self.assertEqual(result.cursor, 40)

    def test_course_list_omits_empty_task_status_and_metric_columns(self):
        screen, result = render([('中文课程', '', '课程介绍'), ('第二门课', '', '另一段介绍')],
                                52, cursor=0, keys=' \r')
        headers = [line for line in screen if '名称' in line]
        self.assertEqual(len(headers), 1, screen)
        self.assertIn('详情', headers[0])
        self.assertNotIn('状态', headers[0])
        self.assertNotIn('成绩', headers[0])
        self.assertEqual(result.indexes, [0, 1])

    def test_narrow_width_hides_type_column_and_keeps_metrics_useful(self):
        rows = [('第一章 单元作业', '未提交', '作业', '0', '2'),
                ('期中测验', '已提交', '测验', '92.5', '不限'),
                ('附件作业 · 实践报告', '未作答', '附件作业', '--', '1')]
        screen, _ = render(rows, 40, extra_headers=('成绩', '剩余次数'),
                           cursor=1, keys='q', row_hints=['附件作业暂不支持'])
        header = next(line for line in screen if '名称' in line)
        # No squeezed placeholder column; the three metadata columns fit 38 cells.
        self.assertNotIn('类', header)
        for name in ('状态', '成绩', '剩余'):
            self.assertIn(name, header)
        self.assertNotIn('…', header)
        joined = '\n'.join(screen)
        # A real zero and a real remaining count stay visible at their columns.
        first = next(line for line in screen if '第一章' in line)
        metrics = first.split('未提交', 1)[1].split()
        self.assertEqual(metrics[:2], ['0', '2'])
        self.assertIn('附件作业暂不支持', joined)

    def test_wide_width_keeps_the_type_column(self):
        rows = [('第一章 单元作业', '未提交', '作业', '0', '2')]
        screen, _ = render(rows, 80, extra_headers=('成绩', '剩余次数'))
        header = next(line for line in screen if '名称' in line)
        self.assertIn('类型', header)


if __name__ == '__main__':
    unittest.main()
