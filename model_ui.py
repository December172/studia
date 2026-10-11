"""Model picker and draft editor. Never writes credentials or contacts a service."""
from dataclasses import dataclass
import threading
import time
from typing import Optional

from prompt_toolkit.application import Application
from prompt_toolkit.filters import has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import DynamicContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea, Label

from ai.client import AIConfig
from checklist import fit
from terminal_widgets import STYLE_DICT, field, frame, responsive_actions, status_window

# Shared palette plus the editor's text-area colors. Focused-button contrast is
# defined once in terminal_widgets and must not be overridden here.
STYLE = Style.from_dict(dict(STYLE_DICT, **{
    'active': 'ansicyan bold',
    'text-area': 'bg:#202b33 #ffffff', 'text-area focused': 'bg:#263b45 #ffffff',
}))

@dataclass
class ModelChoice:
    action: str
    profile_id: Optional[str] = None


@dataclass
class ModelEdit:
    action: str
    config: Optional[AIConfig] = None


def choose_model(rows, active_id=None, *, cursor_id=None, message='', locked_ids=(),
                 input=None, output=None, test_connection=None):
    """Test the highlighted saved profile without activating it or leaving the page."""
    rows = list(rows)
    cursor = next((i for i, row in enumerate(rows) if row[0] == (cursor_id or active_id)), 0)
    keys = KeyBindings()
    state = {'pending': False, 'message': message}
    closed = threading.Event()

    def finish(action):
        if not state['pending']:
            app.exit(result=ModelChoice(action, rows[cursor][0] if cursor < len(rows) else None))

    def use():
        finish('select' if cursor < len(rows) else 'add')

    def edit():
        if cursor < len(rows) and rows[cursor][0] not in locked_ids:
            finish('edit')

    def start_test():
        if state['pending'] or test_connection is None or cursor >= len(rows):
            return
        profile_id = rows[cursor][0]
        started = time.monotonic()
        state.update(pending=True, message='测试中 · ' + rows[cursor][1])
        loop = app.loop
        app.invalidate()

        def record(status, elapsed):
            if closed.is_set():
                return
            state.update(pending=False, message=f'{status} · 用时 {elapsed:.1f} 秒')
            # Only this row changes; the active marker and cursor remain unchanged.
            row = rows[cursor]
            rows[cursor] = (*row[:3], '✓ 上次通过' if status == '连接通过' else '× 失败')
            app.layout.focus(body)
            app.invalidate()

        def run():
            try:
                status = str(test_connection(profile_id))
            except Exception:
                status = '连接测试失败，请检查配置后重试'
            if not closed.is_set():
                try:
                    loop.call_soon_threadsafe(record, status, time.monotonic() - started)
                except RuntimeError:
                    pass  # The user left the page while the request finished.
        threading.Thread(target=run, name='model-connection-test', daemon=True).start()

    def content():
        width = max(1, app.output.get_size().columns - 2)
        name_width = max(4, width // 4)
        model_width = max(1, width - name_width - 20)
        list_focused = app.layout.has_focus(body)
        fragments = []
        for i in range(len(rows) + 1):
            highlighted = i == cursor
            focused = highlighted and list_focused
            retained = not highlighted and i < len(rows) and rows[i][0] == active_id
            style = 'class:cursor' if focused else 'class:row.focused' if highlighted else \
                'class:row.selected' if retained else ''
            if focused:
                fragments.append(('[SetCursorPosition]', ''))
            if i == len(rows):
                line = '  ＋ 添加模型'
            else:
                profile_id, provider, model, status = rows[i]
                line = ('› ' if highlighted else '  ') + ('● ' if profile_id == active_id else '○ ')
                line += fit(provider, name_width) + '  ' + fit(model, model_width) + '  ' + status
            fragments.append((style, fit(line, width)))
            if i < len(rows):
                fragments.append(('', '\n'))
        return fragments

    body = Window(FormattedTextControl(content, focusable=True), always_hide_cursor=True,
                  dont_extend_height=True, wrap_lines=False)

    @keys.add('up', filter=has_focus(body))
    @keys.add('down', filter=has_focus(body))
    def move(event):
        nonlocal cursor
        if not state['pending']:
            cursor = max(0, min(len(rows), cursor + (-1 if event.key_sequence[-1].key == 'up' else 1)))

    @keys.add('t', filter=has_focus(body))
    def test(event):
        start_test()

    @keys.add('e', filter=has_focus(body))
    def edit_key(event):
        edit()

    @keys.add('tab')
    @keys.add('s-tab')
    def focus(event):
        if state['pending']:
            return
        # The visible action set follows the cursor, so navigate to the buttons
        # that belong to the current row instead of the previous render's set.
        targets = context_buttons()
        if event.app.layout.has_focus(body):
            target = targets[-1] if event.key_sequence[-1].key == 's-tab' else targets[0]
            event.app.layout.focus(target.window)
        else:
            (focus_previous if event.key_sequence[-1].key == 's-tab' else focus_next)(event)

    @keys.add('escape')
    @keys.add('b')
    @keys.add('c-c')
    @keys.add('c-d')
    def back(event):
        app.exit(result=ModelChoice('back'))

    @keys.add('enter', filter=has_focus(body))
    def select(event):
        use()

    # Stable buttons: focus targets must never be rebuilt during a render pass.
    use_button = field('使用此模型', use)
    test_button = field('测试连接', start_test)
    edit_button = field('编辑', edit)
    add_button = field('添加模型', use)
    back_button = field('返回', lambda: finish('back'))
    sets = {
        'add': responsive_actions([add_button, back_button]),
        'builtin': responsive_actions([use_button, test_button, back_button]),
        'custom': responsive_actions([use_button, test_button, edit_button, back_button]),
    }
    contexts = {
        'add': [add_button, back_button],
        'builtin': [use_button, test_button, back_button],
        'custom': [use_button, test_button, edit_button, back_button],
    }

    def context_name():
        if cursor >= len(rows):
            return 'add'
        return 'builtin' if rows[cursor][0] in locked_ids else 'custom'

    def context_buttons():
        return contexts[context_name()]

    button_row = DynamicContainer(lambda: sets[context_name()])

    def context_help():
        if cursor >= len(rows):
            return '↑↓ 选择 · Enter 添加 · Tab 按钮 · Esc 返回'
        if rows[cursor][0] in locked_ids:
            return '↑↓ 选择 · Enter 使用 · t 测试 · Tab 按钮 · Esc 返回'
        return '↑↓ 选择 · Enter 使用 · t 测试 · e 编辑 · Tab 按钮 · Esc 返回'

    layout = frame(
        '模型配置 · ● 当前使用',
        HSplit([body, button_row]),
        heading=None, status=lambda: state['message'], status_height=2,
        help_text=context_help, footer=None)
    app = Application(layout=Layout(layout, focused_element=body),
                      key_bindings=keys, style=STYLE, full_screen=True,
                      input=input, output=output)
    try:
        return app.run()
    except EOFError:
        return ModelChoice('back')
    finally:
        closed.set()


def draft_config(url, model, key, current=None, *, provider_name=""):
    """Blank keys reuse only the original normalized service URL."""
    if not key.strip() and current:
        if url.strip().rstrip('/') != current.base_url:
            raise ValueError('接口地址已更改，请填写新 API Key')
        key = current.api_key
    config = AIConfig(url, model, key, provider_name=provider_name)
    if current and (config.base_url, config.model, config.api_key) == (
            current.base_url, current.model, current.api_key):
        config.api_format = current.api_format
    return config


def edit_model(current=None, *, input=None, output=None):
    provider = TextArea(text=current.provider_name if current else '', multiline=False)
    url = TextArea(text=current.base_url if current else '', multiline=False)
    model = TextArea(text=current.model if current else '', multiline=False)
    key = TextArea(text='', multiline=False, password=True)
    error = ''
    keys = KeyBindings()

    def finish(action):
        app.exit(result=ModelEdit(action))

    def save():
        nonlocal error
        try:
            config = draft_config(url.text, model.text, key.text, current, provider_name=provider.text)
        except ValueError as exc:
            error = str(exc)
            return
        app.exit(result=ModelEdit('save', config))

    save_button = field('保存并测试', save, width=16)
    buttons = [save_button, field('取消', lambda: finish('back'))]
    if current:
        buttons.append(field('删除', lambda: finish('delete')))
    responsive_buttons = responsive_actions(buttons)

    keys.add('tab')(focus_next)
    keys.add('s-tab')(focus_previous)

    @keys.add('enter', filter=has_focus(provider) | has_focus(url) | has_focus(model) | has_focus(key))
    def next_field(event):
        event.app.layout.focus_next()

    @keys.add('escape')
    @keys.add('c-c')
    @keys.add('c-d')
    def cancel(event):
        finish('back')

    app = Application(layout=Layout(HSplit([
        Label('编辑模型' if current else '添加模型', style='class:title'),
        Label('供应商名称（可留空）'), provider,
        Label('接口地址（OpenAI 兼容 · 含 /v1 等路径）'), url,
        Label('模型名称（Model ID）'), model,
        Label('API Key · 已设置，留空保留' if current else 'API Key'), key,
        Window(height=1), responsive_buttons,
        Label('Tab 下一项 · Shift+Tab 上一项 · Esc 返回'),
        Window(FormattedTextControl(lambda: [('class:error', error)]), height=2, wrap_lines=True),
    ]), focused_element=provider), key_bindings=keys, style=STYLE, full_screen=True,
        input=input, output=output)
    try:
        return app.run()
    except EOFError:
        return ModelEdit('back')
