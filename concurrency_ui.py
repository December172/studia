"""Concurrency draft editor. Edits two worker counts in memory only.

Both counts default to 8 and are bounded to 1–8. The form never writes to the
settings store, contacts a service or mutates the dictionary it was given; the
caller persists the returned draft, which the next run picks up.
"""
from dataclasses import dataclass

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.filters import has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Label

from base.concurrency import MAX_WORKERS, validate_workers
from terminal_widgets import STYLE_DICT, field, frame, responsive_actions

DEFAULT_COUNT = MAX_WORKERS
MIN_COUNT = 1
FIELDS = (('ai', 'AI答题'), ('study', '自动学习'))
INVALID_HINT = f'并发数只能为 {MIN_COUNT}–{MAX_WORKERS} 的整数，请重新输入'

STYLE = Style.from_dict(dict(STYLE_DICT, hint='ansiyellow'))


@dataclass
class _Draft:
    """Mutable form state seeded by copying, never holding the caller's dict."""
    ai: int = DEFAULT_COUNT
    study: int = DEFAULT_COUNT
    hint: str = ''

    def value(self, key):
        return getattr(self, key)


def _count(value):
    """Persisted values only; anything invalid or missing falls back to the default."""
    try:
        return validate_workers(value)
    except (ValueError, TypeError):
        return DEFAULT_COUNT


def _seed(current):
    draft = _Draft()
    if isinstance(current, dict):
        for key, _ in FIELDS:
            setattr(draft, key, _count(current.get(key)))
    return draft


def edit_concurrency(current=None, *, input=None, output=None, message=''):
    """Return the edited ``{'ai': int, 'study': int}`` draft, or None on cancel.

    Up/down move the row cursor, left/right adjust the selected row within 1–8,
    digits 1–8 replace it, and 0/9 report a hint without changing any count.
    Reset restores both counts to 8 in the draft only; only Save returns a
    value, and it always returns a fresh dictionary.
    """
    draft = _seed(current)
    selected = [0]
    keys = KeyBindings()

    def rows():
        list_focused = get_app().layout.has_focus(body)
        fragments = []
        for index, (key, label) in enumerate(FIELDS):
            highlighted = index == selected[0]
            focused = highlighted and list_focused
            if focused:
                fragments.append(('[SetCursorPosition]', ''))
            fragments.append(('class:cursor' if focused else 'class:row.focused' if highlighted
                              else 'class:row',
                              f'{"› " if highlighted else "  "}{label}  ‹ {draft.value(key)} ›'))
            fragments.append(('', '\n' if index + 1 < len(FIELDS) else ''))
        return fragments

    body = Window(FormattedTextControl(rows, focusable=True), always_hide_cursor=True,
                  dont_extend_height=True, wrap_lines=False)

    @keys.add('up', filter=has_focus(body))
    @keys.add('down', filter=has_focus(body))
    def move(event):
        step = -1 if event.key_sequence[-1].key == 'up' else 1
        selected[0] = (selected[0] + step) % len(FIELDS)

    @keys.add('left', filter=has_focus(body))
    @keys.add('right', filter=has_focus(body))
    def adjust(event):
        step = -1 if event.key_sequence[-1].key == 'left' else 1
        draft.hint = ''
        key = FIELDS[selected[0]][0]
        setattr(draft, key, min(MAX_WORKERS, max(MIN_COUNT, draft.value(key) + step)))

    @keys.add('1', filter=has_focus(body))   # PTK routes "\r" to enter, never to digits.
    @keys.add('2', filter=has_focus(body))
    @keys.add('3', filter=has_focus(body))
    @keys.add('4', filter=has_focus(body))
    @keys.add('5', filter=has_focus(body))
    @keys.add('6', filter=has_focus(body))
    @keys.add('7', filter=has_focus(body))
    @keys.add('8', filter=has_focus(body))
    @keys.add('9', filter=has_focus(body))
    @keys.add('0', filter=has_focus(body))
    def digit(event):
        value = int(event.key_sequence[-1].data)
        if MIN_COUNT <= value <= MAX_WORKERS:
            draft.hint = ''
            setattr(draft, FIELDS[selected[0]][0], value)
        else:
            draft.hint = INVALID_HINT

    keys.add('tab')(focus_next)
    keys.add('s-tab')(focus_previous)

    def save():
        app.exit(result={'ai': draft.ai, 'study': draft.study})

    def reset():
        draft.ai = draft.study = DEFAULT_COUNT
        draft.hint = ''

    buttons = [field('保存', save, width=10),
               field('取消', lambda: app.exit(result=None), width=10),
               field('恢复默认', reset, width=12)]
    responsive_buttons = responsive_actions(buttons)

    @keys.add('enter', filter=has_focus(body))
    def finish_fields(event):
        event.app.layout.focus(buttons[0])

    @keys.add('escape')
    @keys.add('c-c')
    @keys.add('c-d')
    def cancel(event):
        event.app.exit(result=None)

    def help_text():
        # Whole `` · `` segments wrap cleanly on narrow terminals without leaving
        # a single trailing glyph on its own line.
        return '←→ 调整 · ↑↓ 切换 · 数字 1–8 · Tab 控件 · Esc 取消'

    layout = frame(
        '并发设置', body, responsive_buttons,
        status=lambda: draft.hint or message or '保存后下次运行生效',
        help_text=help_text)
    app = Application(layout=Layout(layout, focused_element=body),
                      key_bindings=keys, style=STYLE, full_screen=True,
                      input=input, output=output)
    try:
        return app.run()
    except EOFError:
        return None
