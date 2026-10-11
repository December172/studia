"""Shared native prompt_toolkit widgets: one style, responsive actions, page frame.

The focused-button contrast bug lives here: prompt_toolkit renders a focused
``Button`` as ``class:button.focused class:button.text`` (or ``.arrow``). A flat
``button.focused`` rule sets the window background, but the later ``button.text``
fragment reintroduces ``class:button``'s foreground, ending at fg == bg. The
combined selectors below win because the fragment's own class set is a superset
of the window's, so focused labels and arrows stay readable.
"""
from prompt_toolkit.application import get_app
from prompt_toolkit.layout import Container, DynamicContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, Label

# One palette for every native page (model picker/editor, concurrency, checklist).
STYLE_DICT = {
    'title': 'ansicyan bold',
    'hint': 'ansiyellow',
    'error': 'ansiyellow',
    'status': 'ansiyellow',
    'help': '',
    'row': 'ansicyan',
    'row.focused': 'ansicyan bold',      # highlighted row while focus is elsewhere
    'row.selected': 'ansicyan bold',
    'row.disabled': 'ansibrightblack',
    'cursor': 'bg:ansicyan ansiblack bold',  # the control that owns focus
    'checked': 'ansicyan bold',
    'columns': 'bold',
    'button': 'ansicyan',
    'button.focused': 'bg:ansicyan ansiblack bold',
    'button.focused button.text': 'ansiblack bold',
    'button.focused button.arrow': 'ansiblack bold',
}
STYLE = Style.from_dict(STYLE_DICT)

# Class names for a focused list row, reused across pages.
CURSOR = 'class:cursor'
ROW = 'class:row'
ROW_FOCUSED = 'class:row.focused'
ROW_SELECTED = 'class:row.selected'
ROW_DISABLED = 'class:row.disabled'
ERROR = 'class:error'


def text_width(value):
    return get_cwidth(str(value))


def field(text, handler=None, *, width=None):
    """A Button sized for CJK labels instead of a fixed cell count."""
    return Button(text, handler=handler, width=width or max(8, text_width(text) + 4))


def responsive_actions(buttons, *, padding=2):
    """Build one horizontal/vertical pair; pick between them by terminal width.

    ``buttons`` must be stable objects: they are the focus targets, so a render
    pass may never construct replacements. Callers that need different action
    sets build one ``responsive_actions`` per set and choose between those.
    """
    buttons = list(buttons)
    vertical = HSplit(buttons)
    horizontal = VSplit(buttons, padding=padding)
    needed = sum(getattr(button, 'width', text_width(button.text) + 4)
                 for button in buttons) + padding * (len(buttons) - 1)
    return DynamicContainer(lambda: vertical
                            if get_app().output.get_size().columns < needed + 2 else horizontal)


def action_rows(buttons, columns):
    """Row count ``responsive_actions`` will occupy at this terminal width."""
    buttons = list(buttons)
    needed = sum(getattr(button, 'width', text_width(button.text) + 4)
                 for button in buttons) + 2 * max(0, len(buttons) - 1)
    return 1 if columns >= needed + 2 else max(1, len(buttons))


def status_window(source, *, height=2, style='class:status'):
    """Inline feedback line(s) that never push the layout around."""
    return Window(FormattedTextControl(lambda: [(style, source() if callable(source) else source)]),
                  height=height, wrap_lines=True, always_hide_cursor=True)


def wrap_help(text, width):
    """Wrap help on whole `` · `` segments so no token or phrase is split."""
    if not text:
        return []
    width = max(1, width)
    lines, current = [], ''
    for segment in str(text).split(' · '):
        piece = segment if not current else ' · ' + segment
        if current and get_cwidth(current + piece) > width:
            lines.append(current)
            current = segment
        else:
            current += piece
    if current or not lines:
        lines.append(current)
    return lines


def help_window(text):
    """Dynamic-height help that wraps by complete shortcut phrases.

    ``text`` may be a callable so a page can show context-specific shortcuts
    (for example a row-dependent action set) without rebuilding the window.
    """
    def current():
        return text() if callable(text) else text

    def lines():
        width = max(1, get_app().output.get_size().columns - 2)
        return [fit_help(line, width) for line in wrap_help(current(), width)]

    return Window(FormattedTextControl(lambda: [('class:help', '\n'.join(lines()))]),
                  height=lambda: max(1, len(lines())), wrap_lines=False,
                  always_hide_cursor=True)


def fit_help(line, width):
    return line if get_cwidth(line) <= width else line[:max(0, width - 1)] + '…'


def name_fragments(account_name, width, animation):
    """Right-aligned, per-character animated identity for one footer row.

    ``animation`` is a ``NameAnimation``; every render recolors the name, so a
    control with a nonzero refresh interval shows a live identity without
    changing any other cell. Returns an empty list when there is no name, so
    callers can append the row unconditionally.
    """
    text = str(account_name or '')
    if not text or width < 4:
        return []
    limit = max(1, width - 1)
    value = ''
    for char in text:
        if get_cwidth(value + char) > limit:
            break
        value += char
    if not value:
        return []
    return [('', ' ' * max(0, width - get_cwidth(value)))] + [
        (f'fg:{color} bold', char) for color, char in animation.colors(value)]


def focus_targets(elements):
    """Focusable Windows for a mix of Windows and Buttons, in tab order.

    ``app.layout.current_window`` is a ``Window``, never a ``Button``, so a
    Button must be unwrapped to its ``.window`` before comparison; otherwise it
    never matches the focused control and every Tab snaps back to the first one.
    """
    targets = []
    for element in elements:
        window = getattr(element, 'window', element)
        if window is not None:
            targets.append(window)
    return targets


def cycle_focus(application, elements, step=1):
    """Move focus forward (``step=1``) or backward across body/search/buttons."""
    targets = focus_targets(elements)
    if not targets:
        return
    current = application.layout.current_window
    position = targets.index(current) if current in targets else 0
    application.layout.focus(targets[(position + step) % len(targets)])


def frame(title, content, actions=None, *, heading=None, search=None, status=None,
          status_height=1, help_text='', footer=None):
    """Shared heading / title / content / search / actions / status / help frame.

    ``content`` is one container (usually a header+body ``HSplit``); the frame
    only arranges the surrounding chrome so every native page shares one shape.
    """
    rows = []
    if heading:
        rows.append(Label(str(heading), style='class:title'))
    rows.append(Label(title, style='class:title'))
    rows.append(content)
    if search is not None:
        rows.append(search)
    if actions is not None:
        rows.append(actions)
    if status is not None:
        rows.append(status_window(status, height=status_height)
                    if isinstance(status, (str, bytes)) or callable(status)
                    else status)
    if help_text:
        rows.append(help_window(help_text))
    if footer is not None:
        rows.append(footer)
    return HSplit(rows)
