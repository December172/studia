"""Native keyboard option/item/action controls for real terminals.

Three reusable controls share one style, one focus model and one scroll-aware
renderer:

* :func:`option_menu` - a keyed option list returning a scalar value.
* :func:`single_picker` - a searchable single-item picker with visible actions.
* :func:`confirm_prompt` - a yes/no control built on ``option_menu``.

Focus is explicit: ``Tab``/``Shift+Tab`` move between the search field, the list
and the action buttons. Typed shortcut letters only fire while the list owns
focus, so the same letters stay ordinary text inside search. Movement and
confirmation work on *visible* list positions, so a filtered list with original
indexes ``[0, 2, 5]`` moves 0 -> 2 -> 5. The list, column header and help wrap
are laid out from the actual terminal size, so a narrow or short terminal
scrolls rather than clipping controls.

Business values (including ``0`` and ``False``) are returned as-is; the
``Back``/``Cancelled`` navigation is only translated to exceptions by the
``TerminalUI`` adapter, never here.
"""
from dataclasses import dataclass
from typing import Optional

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.filters import has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

from checklist import fit
from name_animation import NameAnimation, terminal_color_depth
from terminal_widgets import (CURSOR, ROW, ROW_DISABLED, ROW_SELECTED, STYLE_DICT,
                              action_rows, cycle_focus, field, frame, frame_chrome,
                              name_fragments, responsive_actions, status_window,
                              wrap_help)

STYLE = Style.from_dict(dict(STYLE_DICT, **{
    'text-area': 'bg:#202b33 #ffffff', 'text-area focused': 'bg:#263b45 #ffffff',
}))

HELP_MENU = '↑↓ 选择 · Enter 确认 · Tab 按钮 · Esc 返回 · q 退出'
HELP_PICKER = '↑↓ 选择 · / 搜索 · Enter 选择 · Tab 按钮 · Esc 返回 · q 退出'
UNMATCHED = '没有匹配的项目；可继续搜索、清除搜索或返回。'
EMPTY_CONFIRM = '没有匹配的项目；请调整搜索或返回。'


@dataclass
class ChooseResult:
    """Keyboard outcome; ``kind`` is select/action/back/quit.

    ``cursor`` is ``None`` when the path cannot know it (a closed pipe), so a
    caller keeping its own list state never has a real position overwritten.
    """
    kind: str
    value: object = None
    index: Optional[int] = None
    action: Optional[str] = None
    indexes: tuple = ()
    cursor: Optional[int] = None


def _same(left, right):
    """Equality that keeps ``0`` and ``False`` from being conflated."""
    return left is right or (type(left) is type(right) and left == right)


def _wrap_help(text, width):
    """Wrap help on whole `` · `` segments (delegates to the shared renderer)."""
    return wrap_help(text, width)


def _fields(columns, rows, width):
    """Fixed terminal-cell widths shared by the header and every data row.

    The flexible name column claims the leftover space, but never more than half
    the terminal, so limited metadata columns keep enough room for their own
    values instead of being truncated to a bare header.
    """
    fields = []
    for name, index, limit in columns:
        if limit is None:
            fields.append([name, index, None])
            continue
        content = max((get_cwidth(str(row[index])) for row in rows if index < len(row)), default=0)
        fields.append([name, index, max(get_cwidth(name), min(limit, content))])
    fixed = sum(size for _, _, size in fields if size is not None)
    flexible = [field for field in fields if field[2] is None]
    gaps = 2 * max(0, len(fields) - 1)
    remaining = max(4, width - 4 - fixed - gaps)
    for entry in flexible:
        entry[2] = max(4, remaining // len(flexible))
    # Redistribute any oversized flexible allocation to metadata that is still
    # truncated, so details stay readable on a wide terminal.
    cap = max(8, width // 2)
    for entry in flexible:
        spare = max(0, entry[2] - cap)
        if not spare:
            continue
        entry[2] -= spare
        for other in fields:
            if other[2] is None or other in flexible:
                continue
            desired = max((get_cwidth(str(row[other[1]])) for row in rows
                           if other[1] < len(row)), default=0)
            grow = min(spare, max(0, desired - other[2]))
            other[2] += grow
            spare -= grow
            if not spare:
                break
    return fields


def _row_line(marker, number, numwidth, values, fields, width):
    prefix = f'{marker}{number.rjust(numwidth)} '
    cells = [prefix]
    for _, index, size in fields:
        value = values[index] if index < len(values) else ''
        cells.append(fit(value, size))
    # Strip the trailing cell padding before fitting, so a row whose words all
    # fit never grows a spurious ellipsis just because the last column is wide.
    return fit('  '.join(cells).rstrip(), width)


def _action_rows(buttons, columns):
    """Row count of ``responsive_actions`` for the same width and spacing."""
    needed = sum(button.width for button in buttons) + 2 * (len(buttons) - 1)
    return 1 if columns >= needed + 2 else len(buttons)


class _List:
    """Shared list behaviour: filtering, cursor, scrolling and rendering.

    ``visible(query)`` is the single source of truth for which original indexes
    survive a filter. Movement maps the current cursor to its visible position,
    steps there and maps back to an original index, so filtered navigation is
    contiguous instead of jumping over filtered-out rows.
    """

    def __init__(self, rows, numbers, columns, *, disabled=(), checked=()):
        self.rows = rows          # tuples of display values
        self.numbers = numbers    # left-column label per row (key or index)
        self.columns = columns    # (header, row_index, cell_limit|None)
        self.disabled = set(disabled)
        self.checked = set(checked)
        self.visible_indexes = []
        self.fields = []
        self.width = 1
        # Reserve two cells per leading gap so `` › `` never clips the marker.
        self.numwidth = max((get_cwidth(str(number)) for number in numbers), default=1)

    def visible(self, query):
        needle = str(query).casefold()
        if not needle:
            return list(range(len(self.rows)))
        return [index for index, row in enumerate(self.rows)
                if needle in ' '.join(str(part) for part in row).casefold()]

    def refresh(self, state):
        """Recompute visible indexes and column widths for the current query."""
        size = get_app().output.get_size()
        self.width = max(1, size.columns - 2)
        self.visible_indexes = self.visible(state.get('query', ''))
        self.fields = _fields(self.columns, [self.rows[i] for i in self.visible_indexes], self.width)
        return self.visible_indexes

    def clamp(self, state):
        """Keep the cursor on a visible row (first match when filtered out)."""
        if self.visible_indexes and state['cursor'] not in self.visible_indexes:
            state['cursor'] = self.visible_indexes[0]

    def clamp_index(self, index):
        if not self.visible_indexes:
            return 0
        return index if index in self.visible_indexes else self.visible_indexes[0]

    def move(self, state, step):
        """Step ``step`` positions through the visible list, returning the index."""
        indexes = self.refresh(state)
        if not indexes:
            return state['cursor']
        position = indexes.index(state['cursor']) if state['cursor'] in indexes else 0
        position = max(0, min(len(indexes) - 1, position + step))
        return indexes[position]

    def header(self):
        # Mirror ``_row_line``'s prefix and drop cell padding before fitting, so
        # a header whose words all fit shows no spurious trailing ellipsis.
        line = '  ' + ' ' * self.numwidth + ' ' + '  '.join(
            fit(name, size) for name, _, size in self.fields)
        return fit(line.rstrip(), self.width)

    def header_control(self, state):
        def fragments():
            self.refresh(state)
            return [('class:columns', self.header())]
        return FormattedTextControl(fragments)

    def body(self, state, available):
        self.refresh(state)
        self.clamp(state)
        fragments = []
        if not self.visible_indexes:
            fragments.append(('class:error', fit(UNMATCHED, self.width)))
            return fragments
        list_focused = get_app().layout.has_focus(state['body'])
        offset = self._offset(state, available)
        state['offset'] = offset
        last = min(len(self.visible_indexes), offset + available)
        for position in range(offset, last):
            original = self.visible_indexes[position]
            row = self.rows[original]
            highlighted = original == state['cursor']
            active = highlighted and list_focused
            retained = original in self.checked and not highlighted
            if active:
                fragments.append(('[SetCursorPosition]', ''))
            style = (CURSOR if active else ROW_DISABLED if original in self.disabled else
                     ROW_SELECTED if retained else ROW if highlighted else '')
            marker = '› ' if highlighted else '  '
            fragments.append((style, _row_line(marker, self.numbers[original], self.numwidth,
                                               row, self.fields, self.width)))
            if position < last - 1:
                fragments.append(('', '\n'))
        return fragments

    def _offset(self, state, available):
        total = len(self.visible_indexes)
        if not total:
            return 0
        offset = min(max(0, state['offset']), max(0, total - available))
        position = (self.visible_indexes.index(state['cursor'])
                    if state['cursor'] in self.visible_indexes else 0)
        if position < offset:
            offset = position
        elif position >= offset + available:
            offset = position - available + 1
        return max(0, min(offset, max(0, total - available)))


def _controls(title, state, *, buttons, listctl, search, heading, account_name, help_text,
              input, output):
    """Assemble the shared layout, key bindings and focus order.

    Fixed chrome (heading, title, column header, action row, search, status and
    wrapped help) is measured exactly, and the list window takes the leftover
    rows. No manual viewport count drifts from the real layout.
    """
    def message():
        source = state['message']
        return source() if callable(source) else str(source)

    search_field = (TextArea(text=state.get('query', ''), multiline=False, prompt='/ ')
                    if search else None)
    if search_field is not None:
        # Open with the caret after the retained query so Backspace edits it.
        search_field.buffer.cursor_position = len(search_field.text)
    state['search_field'] = search_field

    def help_count():
        if not help_text:
            return 0
        return len(_wrap_help(help_text, max(1, get_app().output.get_size().columns - 2)))

    def chrome_rows():
        columns = get_app().output.get_size().columns
        rows = 2 + (1 if heading else 0)  # title + column header (+ heading)
        rows += _action_rows(buttons, columns)
        rows += 1 if search_field is not None else 0
        rows += 1  # status line
        rows += help_count()
        rows += 1 if account_name else 0
        return rows + frame_chrome(heading=bool(heading), search=search_field is not None,
                                   actions=True, help_text=bool(help_text))

    def available_rows():
        return max(1, get_app().output.get_size().rows - chrome_rows())

    body = Window(FormattedTextControl(lambda: listctl.body(state, available_rows()),
                                       focusable=True),
                  always_hide_cursor=True, wrap_lines=False, dont_extend_height=True)
    state['body'] = body
    order = ([search_field] if search_field is not None else []) + [body, *buttons]

    keys = KeyBindings()

    def commit_search(focus=True):
        state['query'] = search_field.text
        listctl.visible_indexes = listctl.visible(state['query'])
        listctl.clamp(state)
        state['offset'] = 0
        if focus:
            get_app().layout.focus(body)

    @keys.add('tab')
    def next_focus(event):
        cycle_focus(event.app, order, 1)

    @keys.add('s-tab')
    def previous_focus(event):
        cycle_focus(event.app, order, -1)

    @keys.add('c-c')
    @keys.add('c-d')
    def interrupt(event):
        event.app.exit(result=ChooseResult('quit'))

    # ``q`` is advertised on every page, so it must work from the buttons too;
    # only an editable search field may swallow the keystroke as text.
    if search_field is not None:
        @keys.add('q', filter=~has_focus(search_field))
        def quit_(event):
            event.app.exit(result=ChooseResult('quit'))
    else:
        @keys.add('q')
        def quit_(event):
            event.app.exit(result=ChooseResult('quit'))

    if search_field is not None:
        @keys.add('/', filter=has_focus(body))
        def start_search(event):
            get_app().layout.focus(search_field)

        @keys.add('enter', filter=has_focus(search_field))
        def apply_search(event):
            commit_search()

        @keys.add('escape', filter=has_focus(search_field))
        def leave_search(event):
            commit_search()

    # The list Escape is filtered out while search owns focus, so its behaviour
    # never depends on key-binding registration order.
    if search_field is not None:
        @keys.add('escape', filter=~has_focus(search_field))
        def back(event):
            event.app.exit(result=ChooseResult('back', cursor=state['cursor']))
    else:
        @keys.add('escape')
        def back(event):
            event.app.exit(result=ChooseResult('back', cursor=state['cursor']))

    @keys.add('up', filter=has_focus(body))
    @keys.add('down', filter=has_focus(body))
    @keys.add('left', filter=has_focus(body))
    @keys.add('right', filter=has_focus(body))
    def move(event):
        step = {'up': -1, 'left': -1, 'down': 1, 'right': 1}[event.key_sequence[-1].key]
        state['cursor'] = listctl.move(state, step)
        event.app.invalidate()

    @keys.add('pageup', filter=has_focus(body))
    @keys.add('pagedown', filter=has_focus(body))
    def page(event):
        step = -5 if event.key_sequence[-1].key == 'pageup' else 5
        state['cursor'] = listctl.move(state, step)
        event.app.invalidate()

    header = Window(listctl.header_control(state), height=1, wrap_lines=False,
                    always_hide_cursor=True)
    content = HSplit([header, body])
    footer = None
    if account_name:
        animation = NameAnimation()
        footer = Window(
            FormattedTextControl(
                lambda: name_fragments(account_name, get_app().output.get_size().columns,
                                       animation)),
            height=1, wrap_lines=False, always_hide_cursor=True)

    layout = frame(title, content, responsive_actions(buttons), heading=heading,
                   search=search_field, status=message, help_text=help_text or '',
                   footer=footer)
    app = Application(layout=Layout(layout, focused_element=body),
                      key_bindings=keys, style=STYLE, full_screen=True,
                      input=input, output=output, color_depth=terminal_color_depth(),
                      refresh_interval=1 / 30 if account_name else None)
    return app, body, search_field, keys, commit_search


def _run(app, input, output):
    """Run one control view; a closed pipe is navigation, not a crash."""
    try:
        return app.run()
    except EOFError:
        return ChooseResult('quit')


def option_menu(title, choices, default=None, help_text=HELP_MENU, *, message='', heading='',
                account_name='', input=None, output=None, quick=None):
    """Keyed option list; Enter returns the highlighted key (0/False included)."""
    choices = [(key, str(name), str(desc)) for key, name, desc in choices]
    cursor = next((i for i, (key, _, _) in enumerate(choices)
                   if default is not None and _same(key, default)), 0)
    state = {'cursor': cursor, 'query': '', 'offset': 0, 'message': message or ''}
    listctl = _List(choices, [str(key) for key, _, _ in choices],
                    [('选项', 1, None), ('说明', 2, None)])

    def choose():
        if not choices:
            return
        key = choices[state['cursor']][0]
        app.exit(result=ChooseResult('select', key, state['cursor'], cursor=state['cursor']))

    buttons = [field('选择', choose, width=10),
               field('返回', lambda: app.exit(result=ChooseResult('back', cursor=state['cursor'])),
                     width=10)]
    app, body, search, keys, _ = _controls(
        title, state, buttons=buttons, listctl=listctl, search=False, heading=heading,
        account_name=account_name, help_text=help_text, input=input, output=output)

    @keys.add('enter', filter=has_focus(body))
    def enter(event):
        choose()

    for char, value in (quick or {}).items():
        def make(value=value):
            def handler(event):
                app.exit(result=ChooseResult('select', value, cursor=state['cursor']))
            return handler

        keys.add(char, filter=has_focus(body))(make())

    def choose_key(value):
        """Move the cursor to an explicitly keyed row without selecting it."""
        def handler(event):
            for index, (key, _, _) in enumerate(choices):
                if str(key) == value:
                    state['cursor'] = index
                    return
        return handler

    # Explicitly supplied keys (digits or letters such as r/s/t) move the
    # highlight only; Enter still confirms, so a letter never exits the page.
    # Page-level control keys win: escape/back, q/quit, movement and Tab are
    # registered above and below, and reserved names are skipped here.
    reserved = {'q', 'b', 'escape', 'tab', 's-tab', 'enter', 'up', 'down', 'left', 'right',
                'pageup', 'pagedown', 'c-c', 'c-d', ' '}
    for key in {str(key) for key, _, _ in choices}:
        if len(key) == 1 and key not in reserved and key not in (quick or {}):
            keys.add(key, filter=has_focus(body))(choose_key(key))

    listctl.visible_indexes = listctl.visible('')
    return _run(app, input, output)


def single_picker(title, items, label, details=None, *, actions=None, state=None, selected=None,
                  status=None, extra_columns=(), selectable=None, item_hint=None,
                  heading='', account_name='', input=None, output=None):
    """Searchable single-item picker returning the chosen item list or an action."""
    actions = actions or {}
    state = state if state is not None else {}
    items = list(items)
    disabled = {i for i, item in enumerate(items) if selectable and not selectable(item)}
    selected = list(selected or ())
    state.setdefault('query', '')
    initial = next((i for i, item in enumerate(items) if item in selected), 0)
    state['cursor'] = max(0, min(len(items) - 1, state.get('cursor', initial))) if items else 0
    state['offset'] = state.get('offset', 0)
    checked = {i for i, item in enumerate(items) if item in selected and i not in disabled}
    messages = ['']

    rows, numbers = [], []
    for index, item in enumerate(items):
        extras = [str(value(item)) for _, value in extra_columns]
        rows.append((str(label(item)), str(status(item)) if status else '',
                     str(details(item)) if details else '', *extras))
        numbers.append(str(index + 1))
    columns = [('名称', 0, None)]
    if status:
        columns.append(('状态', 1, 10))
    columns += [(name, 3 + offset, 10) for offset, (name, _) in enumerate(extra_columns)]
    if details or any(row[2] for row in rows):
        columns.append(('详情', 2, 18))
    listctl = _List(rows, numbers, columns, disabled=disabled, checked=checked)

    def confirm():
        # Recompute visibility here: a queued Enter can arrive before a redraw,
        # and the typed query must still decide what is confirmable.
        listctl.refresh(state)
        index = state['cursor']
        if not listctl.visible_indexes or index not in listctl.visible_indexes:
            messages[0] = EMPTY_CONFIRM
            get_app().invalidate()
            return
        if index in disabled:
            messages[0] = (str(item_hint(items[index])) if item_hint and index < len(items)
                           else '该项目暂不可选，请选择其他项目。')
            get_app().invalidate()
            return
        state['selected'] = [items[index]]
        app.exit(result=ChooseResult('select', [items[index]], index, indexes=(index,),
                                     cursor=state['cursor']))

    buttons = [field('选择', confirm, width=10)]
    for key, name in actions.items():
        def make_action(key=key):
            def handler():
                get_app().exit(result=ChooseResult('action', action=key, cursor=state['cursor']))
            return handler

        buttons.append(field(str(name), make_action(), width=max(8, get_cwidth(str(name)) + 4)))
    buttons.append(field('返回', lambda: get_app().exit(result=ChooseResult('back', cursor=state['cursor'])),
                         width=10))

    def message():
        cursor = state['cursor']
        if messages[0]:
            return messages[0]
        if item_hint and cursor < len(items) and cursor in disabled:
            return str(item_hint(items[cursor]))
        return ''

    state['message'] = message
    app, body, search, keys, commit_search = _controls(
        title, state, buttons=buttons, listctl=listctl, search=True, heading=heading,
        account_name=account_name, help_text=HELP_PICKER, input=input, output=output)
    commit_search(focus=False)  # seed query/indexes; the field is already populated

    @keys.add('enter', filter=has_focus(body))
    def enter(event):
        confirm()

    for key in actions:
        def make_action(key=key):
            def handler(event):
                # Keep the typed query visible to a following re-entry.
                state['query'] = search.text if search is not None else state.get('query', '')
                event.app.exit(result=ChooseResult('action', action=key, cursor=state['cursor']))
            return handler

        keys.add(key, filter=has_focus(body))(make_action())

    result = _run(app, input, output)
    if search is not None:
        state['query'] = search.text
    if result.cursor is not None:
        state['cursor'] = result.cursor
    if result.kind == 'select':
        state['selected'] = list(result.value or [])
    return result


@dataclass
class PageResult:
    """Outcome of an action page; ``kind`` is action/back/quit."""
    kind: str
    action: Optional[str] = None


def action_page(title, lines, actions, *, default='start', status='', help_text='',
                heading='', account_name='', input=None, output=None):
    """Read-only summary page whose buttons/keys return the chosen action.

    ``lines`` is a callable so the body always reflects the caller's live state
    (connection status, options, results). ``actions`` maps a stable key to a
    label; ``default`` is the key Enter returns, and the first action is the
    primary (Enter-to-start) button.
    """
    source = lines
    get_lines = source if callable(source) else (lambda: list(source))
    actions = dict(actions)
    if not actions:
        actions = {'back': '返回'}
    keys_of = list(actions)
    if default not in actions:
        default = keys_of[0]
    state = {'offset': 0}

    def message():
        return status() if callable(status) else status

    buttons = []
    for key in keys_of:
        def make(key=key):
            return lambda: get_app().exit(result=PageResult('action', key))
        label = str(actions[key])
        buttons.append(field(label, make(), width=max(8, get_cwidth(label) + 4)))

    def submit(key):
        get_app().exit(result=PageResult('action', key))

    def viewport():
        """Body rows left after the fixed chrome, so long pages scroll."""
        columns = get_app().output.get_size().columns
        total = get_app().output.get_size().rows
        chrome = 1 + (1 if heading else 0)  # title (+ heading)
        chrome += action_rows(buttons, columns)
        chrome += 1  # status line
        if help_text:
            chrome += len(wrap_help(help_text, max(1, columns - 2)))
        if account_name:
            chrome += 1
        return max(1, total - chrome - frame_chrome(
            heading=bool(heading), actions=True, help_text=bool(help_text)))

    def content():
        rows = get_lines()
        limit = viewport()
        offset = min(max(0, state['offset']), max(0, len(rows) - limit))
        state['offset'] = offset
        width = max(1, get_app().output.get_size().columns - 2)
        visible = [fit(str(line), width) for line in rows[offset:offset + limit]]
        return [('', '\n'.join(visible))] if visible else [('class:error', fit('（无内容）', width))]

    body = Window(FormattedTextControl(content, focusable=True), wrap_lines=False,
                  always_hide_cursor=True, dont_extend_height=True)
    order = [body, *buttons]
    keys = KeyBindings()

    @keys.add('enter', filter=has_focus(body))
    def enter(event):
        submit(default)

    # Only the explicitly supplied single-character action keys are bound (for
    # example a result page's own ``1``/``2``). There are no hidden ordinal
    # shortcuts, so an unrelated digit never starts or switches anything.
    for key in keys_of:
        if len(key) == 1 and key != 'q':
            def make(key=key):
                return lambda event: submit(key)
            keys.add(key, filter=has_focus(body))(make())

    def step(event, delta):
        state['offset'] = max(0, state['offset'] + delta)
        event.app.invalidate()

    @keys.add('up', filter=has_focus(body))
    def up(event):
        step(event, -1)

    @keys.add('down', filter=has_focus(body))
    def down(event):
        step(event, 1)

    @keys.add('pageup', filter=has_focus(body))
    def pageup(event):
        step(event, -viewport())

    @keys.add('pagedown', filter=has_focus(body))
    def pagedown(event):
        step(event, viewport())

    @keys.add('escape')
    def back(event):
        event.app.exit(result=PageResult('back'))

    # ``q`` is advertised on a summary page, so it works from a button too; no
    # editable field exists here to swallow it as text.
    @keys.add('q')
    def quit_(event):
        event.app.exit(result=PageResult('quit'))

    @keys.add('tab')
    def next_focus(event):
        cycle_focus(event.app, order, 1)

    @keys.add('s-tab')
    def previous_focus(event):
        cycle_focus(event.app, order, -1)

    @keys.add('c-c')
    @keys.add('c-d')
    def interrupt(event):
        event.app.exit(result=PageResult('quit'))

    footer = None
    if account_name:
        animation = NameAnimation()
        footer = Window(
            FormattedTextControl(
                lambda: name_fragments(account_name, get_app().output.get_size().columns,
                                       animation)),
            height=1, wrap_lines=False, always_hide_cursor=True)
    layout = frame(title, HSplit([body]), responsive_actions(buttons), heading=heading,
                   status=status_window(message, height=1),
                   help_text=help_text or '', footer=footer)
    app = Application(layout=Layout(layout, focused_element=body),
                      key_bindings=keys, style=STYLE, full_screen=True,
                      input=input, output=output, color_depth=terminal_color_depth(),
                      refresh_interval=1 / 30 if account_name else None)
    return _run(app, input, output)


def confirm_prompt(prompt, default=False, *, input=None, output=None):
    """Yes/no control; Enter accepts the default, y/n answer immediately."""
    return option_menu(prompt, [(True, '是', ''), (False, '否', '')], default=default,
                       help_text='←→ 或 Tab 切换 · Enter 确认 · y/n 直接选择 · Esc 返回 · q 退出',
                       input=input, output=output, quick={'y': True, 'n': False})
