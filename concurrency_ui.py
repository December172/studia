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
from prompt_toolkit.layout import DynamicContainer, HSplit, VSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Button, Label

from base.concurrency import MAX_WORKERS, validate_workers

DEFAULT_COUNT = MAX_WORKERS
MIN_COUNT = 1
FIELDS = (('ai', 'AI答题'), ('study', '自动学习'))
INVALID_HINT = f'并发数只能为 {MIN_COUNT}–{MAX_WORKERS} 的整数，请重新输入'

STYLE = Style.from_dict({
    'title': 'ansicyan bold', 'hint': 'ansiyellow', 'row': 'ansicyan',
    'row.focused': 'bg:ansicyan ansiblack bold', 'button': 'ansicyan',
    'button.focused': 'bg:ansicyan ansiblack bold',
})


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
        fragments = []
        for index, (key, label) in enumerate(FIELDS):
            focused = index == selected[0]
            if focused:
                fragments.append(('[SetCursorPosition]', ''))
            fragments.append(('class:row.focused' if focused else 'class:row',
                              f'{"› " if focused else "  "}{label}  ‹ {draft.value(key)} ›'))
            fragments.append(('', '\n' if index + 1 < len(FIELDS) else ''))
        return fragments

    body = Window(FormattedTextControl(rows, focusable=True), always_hide_cursor=True,
                  dont_extend_height=True, wrap_lines=False)

    @keys.add('up')
    @keys.add('down')
    def move(event):
        step = -1 if event.key_sequence[-1].key == 'up' else 1
        selected[0] = (selected[0] + step) % len(FIELDS)

    @keys.add('left')
    @keys.add('right')
    def adjust(event):
        step = -1 if event.key_sequence[-1].key == 'left' else 1
        draft.hint = ''
        key = FIELDS[selected[0]][0]
        setattr(draft, key, min(MAX_WORKERS, max(MIN_COUNT, draft.value(key) + step)))

    @keys.add('1')   # PTK routes "\r" to the enter binding, never to digits.
    @keys.add('2')
    @keys.add('3')
    @keys.add('4')
    @keys.add('5')
    @keys.add('6')
    @keys.add('7')
    @keys.add('8')
    @keys.add('9')
    @keys.add('0')
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

    buttons = [Button('保存', handler=save, width=10),
               Button('取消', handler=lambda: app.exit(result=None), width=10),
               Button('恢复默认', handler=reset, width=12)]
    vertical, horizontal = HSplit(buttons), VSplit(buttons, padding=2)
    responsive_buttons = DynamicContainer(lambda: vertical
        if get_app().output.get_size().columns < 46 else horizontal)

    @keys.add('enter', filter=has_focus(body))
    def finish_fields(event):
        event.app.layout.focus(buttons[0])

    @keys.add('escape')
    @keys.add('c-c')
    @keys.add('c-d')
    def cancel(event):
        event.app.exit(result=None)

    app = Application(layout=Layout(HSplit([
        Label('并发设置', style='class:title'),
        Label('←/→ 调整 · ↑/↓ 切换 · 数字 1–8 直接输入 · Tab 切换控件'),
        body,
        responsive_buttons,
        Window(height=1),
        Label('保存后下次运行生效'),
        Window(FormattedTextControl(lambda: [('class:hint', draft.hint)]),
               height=1, wrap_lines=False, always_hide_cursor=True),
        Label(message, style='class:hint'),
    ]), focused_element=body), key_bindings=keys, style=STYLE, full_screen=True,
        input=input, output=output)
    try:
        return app.run()
    except EOFError:
        return None
