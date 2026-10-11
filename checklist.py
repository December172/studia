"""Keyboard multiselect. Presentation only; never opens or executes a task."""
from dataclasses import dataclass
from typing import Optional

from name_animation import NameAnimation, terminal_color_depth

from prompt_toolkit.application import Application
from prompt_toolkit.filters import has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from terminal_widgets import STYLE_DICT, cycle_focus, field, frame, responsive_actions


@dataclass
class Selection:
    indexes: list
    cursor: int
    action: Optional[str] = None


def fit(text, width):
    """One physical row, accounting for Chinese full-width characters."""
    text = str(text).replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
    text = ''.join(c for c in text if ord(c) >= 32 and ord(c) != 127)
    if get_cwidth(text) <= width:
        return text + ' ' * (width - get_cwidth(text))
    value = ''
    for char in text:
        if get_cwidth(value + char) > max(0, width - 1):
            break
        value += char
    return value + '…' + ' ' * max(0, width - get_cwidth(value) - 1)


def choose_many(title, rows, *, selected=(), cursor=1, refresh=False, heading='',
                extra_headers=(), account_name='', disabled=(), row_hints=(), input=None, output=None):
    """Rows are (label, status, detail, *extras); indexes refer to original rows."""
    disabled = set(disabled)
    available = set(range(len(rows))) - disabled
    checked = set(selected) & available
    cursor = min(max(0, cursor), len(rows))
    message = ''
    animation = NameAnimation()
    keys = KeyBindings()

    def submit(action=None):
        from prompt_toolkit.application import get_app
        get_app().exit(result=Selection(sorted(checked), cursor, action))

    def finish(event, action=None):
        event.app.exit(result=Selection(sorted(checked), cursor, action))

    def confirm_action():
        nonlocal message
        if checked:
            submit()
        else:
            message = '请先用空格选择任务。'

    buttons = [field('确认', confirm_action, width=10)]
    if refresh:
        buttons.append(field('刷新', lambda: submit('r'), width=10))
    buttons.append(field('返回', lambda: submit('back'), width=10))
    body = Window(FormattedTextControl(lambda: content(), focusable=True), wrap_lines=False,
                  always_hide_cursor=True, dont_extend_height=True)

    @keys.add('escape')
    @keys.add('b')
    def back(event):
        finish(event, 'back')

    @keys.add('q')
    @keys.add('c-c')
    @keys.add('c-d')
    def quit(event):
        finish(event, 'quit')

    if refresh:
        @keys.add('r')
        def reload(event):
            finish(event, 'r')

    @keys.add('tab')
    def next_focus(event):
        cycle_focus(event.app, [body, *buttons], 1)

    @keys.add('s-tab')
    def previous_focus(event):
        cycle_focus(event.app, [body, *buttons], -1)

    @keys.add('up', filter=has_focus(body))
    def up(event):
        nonlocal cursor
        cursor = max(0, cursor - 1)

    @keys.add('down', filter=has_focus(body))
    def down(event):
        nonlocal cursor
        cursor = min(len(rows), cursor + 1)

    @keys.add(' ', filter=has_focus(body))
    def toggle(event):
        nonlocal message
        message = ''
        if cursor == 0:
            if checked == available:
                checked.clear()
            else:
                checked.update(available)
        elif cursor - 1 in disabled:
            message = row_hints[cursor - 1] if cursor - 1 < len(row_hints) else '该任务暂不可选，请在平台核对。'
        elif cursor - 1 in checked:
            checked.remove(cursor - 1)
        else:
            checked.add(cursor - 1)

    @keys.add('enter', filter=has_focus(body))
    def confirm(event):
        confirm_action()

    def heading_text():
        unavailable = f' · {len(rows) - len(available)} 项暂不可选' if disabled else ''
        return f'{title}  ·  已选 {len(checked)}/{len(available)} 项' + unavailable

    def columns():
        """Use one set of terminal-cell widths for the entire list and its header."""
        terminal_width = app.output.get_size().columns
        width = max(1, terminal_width - 2)
        task_rows = any(row[1] for row in rows) or bool(extra_headers)
        # Under 52 columns the Type/detail column is dropped so name, status,
        # score and remaining keep useful widths instead of showing '状… 成…'.
        show_detail = terminal_width >= 52
        fields = []
        if task_rows:
            fields.append(('状态', 1, 10))
        for index, name in enumerate(extra_headers, 3):
            if name == '成绩':
                name = '成绩(分)' if terminal_width >= 80 else '成绩'
            elif name in ('剩余次数', '剩余'):
                name = '剩余(次)' if terminal_width >= 80 else '剩余'
            fields.append((name, index, 10))
        if show_detail and (task_rows or any(row[2] for row in rows)):
            fields.append(('类型' if task_rows else '详情', 2, min(24, width // 4)))
        widths = [max(get_cwidth(name), min(limit, max(
            (get_cwidth(str(row[index])) for row in rows if index < len(row)), default=0)))
            for name, index, limit in fields]
        # Preserve useful name space even with long metadata in a narrow terminal.
        metadata_budget = max(0, width - 6 - 12 - 2 * len(fields))
        while sum(widths) > metadata_budget and any(size > 2 for size in widths):
            largest = max(range(len(widths)), key=widths.__getitem__)
            widths[largest] -= 1
        name_width = max(1, width - 6 - sum(widths) - 2 * len(fields))
        return width, [('名称', 0, name_width)] + [
            (name, index, size) for (name, index, _), size in zip(fields, widths)]

    def column_header():
        width, fields = columns()
        line = ' ' * 6 + '  '.join(fit(name, size) for name, _, size in fields)
        return [('class:columns', fit(line, width))]

    def content():
        width, fields = columns()
        all_mark = 'x' if available and checked == available else '-' if checked else ' '
        all_label = 'All 取消全选' if available and checked == available else 'All 全选'
        display = [(all_mark, all_label, '', '')]
        display += [('-' if i in disabled else 'x' if i in checked else ' ', *row) for i, row in enumerate(rows)]
        fragments = []
        # Only the body owning focus may claim the cursor background; after Tab
        # moves focus to a button the highlighted row keeps its > marker and a
        # plain selected style, so exactly one control ever looks focused.
        body_focused = app.layout.has_focus(body)
        for index, (mark, *values) in enumerate(display):
            highlighted = index == cursor
            focused = highlighted and body_focused
            disabled_row = index - 1 in disabled
            style = 'class:cursor' if focused else 'class:row.disabled' if disabled_row \
                else 'class:row.selected' if highlighted or mark == 'x' else ''
            if focused:
                fragments.append(('[SetCursorPosition]', ''))
            prefix = ('> ' if highlighted else '  ') + f'[{mark}] '
            line = prefix + '  '.join(
                fit(values[column] if column < len(values) else '', size)
                for _, column, size in fields)
            fragments.append((style, fit(line, width)))
            if index < len(display) - 1:
                fragments.append(('', '\n'))
        return fragments

    def footer():
        hint = '↑↓ 移动 · 空格 勾选/取消 · Enter 确认'
        width = app.output.get_size().columns
        available = width - get_cwidth(hint) - 2
        fragments = []
        if 0 < cursor <= len(row_hints) and row_hints[cursor - 1]:
            fragments.append(('class:warning', '\n' + row_hints[cursor - 1] + '\n'))
        fragments.append(('', '\n' + hint))
        if account_name and available >= 2:
            name = fit(account_name, available).rstrip()
            fragments.append(('', ' ' * (width - get_cwidth(hint) - get_cwidth(name))))
            fragments.extend((f'fg:{color} bold', char) for color, char in animation.colors(name))
        hint = '\nEsc 返回 · q 退出'
        if refresh:
            hint += ' · r 刷新状态'
        return fragments + [('', hint), ('class:warning', '\n' + message if message else '')]

    layout = frame(
        heading_text,
        HSplit([Window(FormattedTextControl(column_header), height=1, wrap_lines=False), body]),
        heading=heading or None, actions=responsive_actions(buttons),
        footer=Window(FormattedTextControl(footer), dont_extend_height=True, wrap_lines=True))
    app = Application(layout=Layout(layout, focused_element=body), key_bindings=keys, full_screen=True,
        style=Style.from_dict(dict(STYLE_DICT, warning='ansiyellow')),
        input=input, output=output, color_depth=terminal_color_depth(), refresh_interval=1 / 30 if account_name else None)
    try:
        return app.run()
    except EOFError:
        return Selection(sorted(checked), cursor, 'quit')
