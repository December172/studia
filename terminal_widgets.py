"""Shared native prompt_toolkit widgets: one style, responsive actions, page frame.

The focused-button contrast bug lives here: prompt_toolkit renders a focused
``Button`` as ``class:button.focused class:button.text`` (or ``.arrow``). A flat
``button.focused`` rule sets the window background, but the later ``button.text``
fragment reintroduces ``class:button``'s foreground, ending at fg == bg. The
combined selectors below win because the fragment's own class set is a superset
of the window's, so focused labels and arrows stay readable.
"""
from prompt_toolkit.application import get_app
from prompt_toolkit.layout import DynamicContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, Label

import theme

# One palette for every native page (model picker/editor, concurrency, checklist).
STYLE_DICT = {
    'title': f'{theme.BRAND} bold',
    'hint': theme.WARNING,
    'error': theme.WARNING,
    'status': theme.WARNING,
    'help': '',
    'row': '',
    'row.focused': f'{theme.TEXT} bold',  # highlighted row while focus is elsewhere
    'row.selected': f'{theme.TEXT} bold',
    'row.disabled': theme.TERTIARY,
    'cursor': f'bg:{theme.TEXT} {theme.INK} bold',  # the control that owns focus
    'checked': f'{theme.BRAND} bold',
    'columns': f'{theme.SECONDARY} bold',
    'rule': theme.BORDER,
    'button': theme.SECONDARY,
    'button.focused': f'bg:{theme.TEXT} {theme.INK} bold',
    'button.focused button.text': f'{theme.INK} bold',
    'button.focused button.arrow': f'{theme.INK} bold',
    'text-area': f'bg:{theme.SURFACE} {theme.TEXT}',
    'text-area focused': f'bg:{theme.SURFACE_RAISED} {theme.TEXT}',
}
STYLE = Style.from_dict(STYLE_DICT)

# Class names for a focused list row, reused across pages.
CURSOR = 'class:cursor'
ROW = 'class:row'
ROW_FOCUSED = 'class:row.focused'
ROW_SELECTED = 'class:row.selected'
ROW_DISABLED = 'class:row.disabled'
ERROR = 'class:error'

# Secondary chrome is dropped on short terminals before any control is.
RULE_ROWS = 20   # the thin content/action rule needs this many terminal rows
SPACING_ROWS = 24  # extra breathing room between groups needs this many


def text_width(value):
    return get_cwidth(str(value))


def _terminal_rows():
    return get_app().output.get_size().rows


def _terminal_columns():
    return get_app().output.get_size().columns


def spacing():
    """Whether this height can afford one blank separation row per group."""
    return _terminal_rows() >= SPACING_ROWS


def spacer():
    """One-row gap; collapses to nothing when the terminal is short."""
    return Window(height=lambda: 1 if spacing() else 0, always_hide_cursor=True)


def filler():
    """Flexible blank block that keeps the bottom chrome at the bottom.

    It asks for no preferred height, so a list or form first receives its
    natural height and only the *surplus* rows turn into blank space between
    the content and the actions. The user asked for a filled window with a
    blank middle: headings and content stay at the top, the action/status/help
    group stays at the bottom, and the gap grows or shrinks with the terminal.
    On a short terminal it collapses to zero, so nothing reachable is lost.
    """
    return Window(always_hide_cursor=True)


def rule_height():
    """Rows a content/action rule costs here: 1, or 0 when too short."""
    return 1 if _terminal_rows() >= RULE_ROWS else 0


def frame_chrome(*, heading=False, search=False, actions=False, help_text=False):
    """Gap/rule rows ``frame`` spends for this configuration right now.

    ``frame`` is built before its application exists, so it may only create
    containers whose height is decided at render time. Callers that reserve
    rows for the body (native viewports) use this same live predicate so the
    reservation can never drift from what the frame actually renders.
    """
    rows = 0
    if spacing():
        rows += (1 if heading else 0) + 1 + (1 if search else 0) + (1 if help_text else 0)
    if actions:
        rows += rule_height()
    return rows


def rule():
    """Dim separator whose row height and length follow the live terminal."""
    return Window(
        FormattedTextControl(lambda: [('class:rule', '─' * max(1, _terminal_columns() - 2))]),
        height=lambda: rule_height(), wrap_lines=False,
        always_hide_cursor=True, dont_extend_width=True)


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

    Spacing is one blank row between semantic groups (heading/title, content,
    search, actions, help) plus a dim rule between the content and the actions,
    so a page reads as tidy blocks instead of a compressed stack. Both the gaps
    and the rule are dynamic containers: ``frame`` runs before its application
    exists, so deciding here would read a dummy terminal and the rule would
    survive on a 16-row screen. Short terminals drop every spacer and the rule
    before anything else gives way; padding never competes with the list or the
    action row. ``frame_chrome`` mirrors these rows for viewport reservations.
    """
    rows = []
    if heading:
        rows.append(Label(str(heading), style='class:title'))
        rows.append(spacer())
    rows.append(Label(title, style='class:title'))
    rows.append(spacer())
    rows.append(content)
    if search is not None:
        rows.append(spacer())
        rows.append(search)
    rows.append(filler())
    if actions is not None:
        rows.append(rule())
        rows.append(actions)
    if status is not None:
        rows.append(status_window(status, height=status_height)
                    if isinstance(status, (str, bytes)) or callable(status)
                    else status)
    if help_text:
        rows.append(spacer())
        rows.append(help_window(help_text))
    if footer is not None:
        rows.append(footer)
    return HSplit(rows)
