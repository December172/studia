"""All terminal rendering and interaction, including legacy CLI adapters."""
from collections import Counter, deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
import getpass
import io
import logging
import re
import sys
import threading
import time

from rich.console import Console, Group
from rich.cells import cell_len
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, ProgressColumn, SpinnerColumn, TimeElapsedColumn
from rich.table import Table, Column
from rich.text import Text

import theme
from name_animation import NameAnimation, terminal_color_depth
from reporting import ProgressEvent
from ai.questions import manual_answer_value

_active_ui = ContextVar("active_terminal_ui", default=None)


class Cancelled(Exception):
    """User cancelled configuration; no queued work should begin."""


class Back(Exception):
    """Return to the preceding screen without ending the application."""


def read_terminal_line(prompt, *, secret=False, input=None, output=None, above_prompt=None):
    """Read a native terminal field with immediate Esc navigation."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.formatted_text import ANSI, to_formatted_text
    from prompt_toolkit.styles import Style
    keys = KeyBindings()

    @keys.add('escape')
    def back(event):
        event.app.exit(exception=Back())

    session = PromptSession(input=input, output=output, key_bindings=keys,
                            color_depth=terminal_color_depth(),
                            is_password=secret, style=Style.from_dict({'input-prompt': f'{theme.BRAND} bold'}))
    def message():
        fragments = []
        if above_prompt:
            width = session.app.output.get_size().columns
            stream = io.StringIO()
            console = Console(file=stream, width=width, theme=theme.THEME,
                              force_terminal=True, color_system="truecolor", no_color=False, highlight=False)
            for row in above_prompt(width):
                console.print(row)
            fragments.extend(to_formatted_text(ANSI(stream.getvalue())))
        fragments.append(('class:input-prompt', prompt))
        return fragments

    return session.prompt(message, refresh_interval=1 / 30 if above_prompt else None)


def safe_text(value):
    # Treat course names, model text and exceptions as literal, untrusted text.
    value = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", str(value))
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    return "".join(c for c in value if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))


class CountColumn(ProgressColumn):
    def render(self, task):
        total = "?" if task.total is None else str(int(task.total))
        count = f"{int(task.completed)}/{total}"
        if (task.fields.get("questions") or task.fields.get("resources")) and task.total:
            count += f" · {task.percentage:.0f}%"
        return Text(count, style="muted", no_wrap=True)


class TaskColumn(ProgressColumn):
    def __init__(self):
        super().__init__(table_column=Column(ratio=1, no_wrap=True, overflow="ellipsis"))

    def render(self, task):
        if task.fields.get("resources"):
            return Text("本课资源", style="accent", no_wrap=True)
        return Text(" ".join(task.description.split()), style="accent", no_wrap=True, overflow="ellipsis")


class CompatibleBar:
    def __init__(self, bar):
        self.bar = bar

    def __rich_console__(self, console, options):
        # Use Rich's single-cell bar glyphs only for this column. Keep CJK text,
        # colors and the rest of the UI untouched.
        if theme.PROGRESS_ASCII:
            options = replace(options, legacy_windows=True)
        yield from console.render(self.bar, options)


class CompatibleBarColumn(BarColumn):
    def render(self, task):
        return CompatibleBar(super().render(task))


class UILogHandler(logging.Handler):
    def __init__(self, ui, level):
        super().__init__(level)
        self.ui = ui

    def emit(self, record):
        level = "error" if record.levelno >= logging.ERROR else "warning" if record.levelno >= logging.WARNING else "info"
        # File handlers retain traceback/details; the panel only needs the message.
        self.ui.log(record.getMessage(), level)


class TerminalUI:
    def __init__(self, console=None, input_fn=None, secret_fn=None, dev=False, clock=time.monotonic):
        self.console = console or Console(theme=theme.THEME, highlight=False)
        self.input_fn = input_fn
        self.secret_fn = secret_fn
        self.dev = dev
        self.clock = clock
        self.lines = deque(maxlen=theme.LOG_LINES)
        self.lock = threading.RLock()
        self.live = None
        self.depth = 0
        self.progress = Progress(
            SpinnerColumn(theme.SPINNER, style="accent"), TaskColumn(),
            CompatibleBarColumn(bar_width=None, style=theme.BAR_STYLE, complete_style=theme.BAR_COMPLETE,
                      finished_style=theme.BAR_FINISHED, table_column=Column(ratio=1)),
            CountColumn(), TimeElapsedColumn(),
            console=self.console, expand=True, auto_refresh=False,
        )
        self.task = self.progress.add_task("准备中", total=None)
        self.question_task = self.progress.add_task("本卷题目", total=None, visible=False, questions=True)
        self.resource_task = self.progress.add_task("本课资源", total=None, visible=False, resources=True)
        self.counts = Counter()
        self.unit = "项"
        self.started = clock()
        self.finished = False
        self.handler = None
        self.pending_error = ""
        self.cancelled = False
        self.heading = "ICVE AI Assistant"
        self.account_name = ""
        self.name_animation = NameAnimation()
        self._prompt_rows = []
        self.results = []
        self.errors = []
        self.workspace = False
        self.operation_active = False
        from terminal_runtime import TerminalScreen, RuntimeInput
        self.screen = TerminalScreen(self.console, enabled=False)
        self.runtime_input = RuntimeInput(enabled=self.input_fn is None)

    def terminal_output(self):
        if self.screen.active:
            from prompt_toolkit.output.defaults import create_output
            return self.screen.wrap_output(create_output(stdout=self.console.file))
        return None

    @contextmanager
    def session(self):
        self.started = self.clock()
        self.handler = UILogHandler(self, logging.DEBUG if self.dev else logging.INFO)
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.DEBUG if self.dev else logging.INFO)
        root.addHandler(self.handler)
        token = _active_ui.set(self)
        try:
            self.screen.enabled = self.input_fn is None and self.console.is_terminal and sys.stdin.isatty()
            with self.screen:
                yield self
        finally:
            _active_ui.reset(token)
            root.removeHandler(self.handler)
            root.setLevel(previous_level)
            self.handler.close()
            self.handler = None

    def log(self, message, level="info"):
        from MoocMain.log import redact
        with self.lock:
            for line in safe_text(redact(message)).splitlines():
                if line.strip():
                    self.lines.append((line.expandtabs(4), level))
                    if level in ("error", "warning") and line not in self.errors:
                        self.errors.append(line)
        # Never write outside the dynamic region while a task is running.

    def on_progress(self, event: ProgressEvent):
        with self.lock:
            if event.kind == "begin":
                self.unit = event.unit
                self.progress.update(self.question_task, visible=False)
                self.progress.update(self.resource_task, visible=False)
                self.progress.reset(self.task, total=event.total, description=safe_text(event.name))
            elif event.kind == "questions":
                if event.total is None:
                    # Rich reset(total=None) retains the previous total.
                    self.progress.remove_task(self.question_task)
                    self.question_task = self.progress.add_task(safe_text(event.name), total=None,
                                                                visible=True, questions=True)
                else:
                    self.progress.update(self.question_task, total=event.total, completed=event.completed or 0,
                                         visible=True, description=safe_text(event.name))
            elif event.kind == "resources":
                if event.total is None:
                    self.progress.remove_task(self.resource_task)
                    self.resource_task = self.progress.add_task(safe_text(event.name), total=None,
                                                                visible=True, resources=True)
                else:
                    self.progress.update(self.resource_task, total=event.total, completed=event.completed or 0,
                                         visible=True, description=safe_text(event.name))
            elif event.kind == "add_total":
                task = self.progress.tasks[0]
                self.progress.update(self.task, total=(task.total or 0) + (event.total or 0))
            elif event.kind == "detail":
                self.progress.update(self.task, description=safe_text(event.name))
            elif event.kind == "result":
                self.counts[event.status] += 1
                self.results.append((event.name, event.status))
                self.progress.advance(self.task)
                label = {"success": "成功", "submitted": "已交卷", "preview": "答案已生成 · 未交卷",
                         "skipped": "跳过", "needs_review": "答案未完成", "paused": "待补答",
                         "submission_unknown": "交卷结果未知",
                         "submitting": "交卷结果未知", "error": "失败", "ai_error": "AI 服务失败", "cancelled": "中断"}.get(event.status, event.status)
                level = "success" if event.status in ("success", "submitted", "preview") else "warning" if event.status == "skipped" else "error"
                self.log(f"{event.name} · {label}", level)

    def render(self):
        with self.lock:
            resource_details = []
            for task in self.progress.tasks:
                if task.visible and task.fields.get("resources"):
                    summary = re.search(r'(?:成功|跳过|失败) \d+.*$', task.description)
                    parts = ([task.description[:summary.start()].rstrip(' ·'), summary.group()]
                             if summary else [task.description])
                    resource_details.extend(Text(part, style="muted", no_wrap=True, overflow="ellipsis")
                                            for part in parts if part)
            visible_tasks = sum(task.visible for task in self.progress.tasks)
            log_rows = min(theme.LOG_LINES, max(0, self.console.height - visible_tasks - len(resource_details) - 5))
            rows = list(self.lines)[-log_rows:] if log_rows else []
            # Keep the entire live frame shorter than the physical viewport.
            body = Text(no_wrap=True, overflow="ellipsis")
            for index in range(log_rows):
                if index < len(rows):
                    line, level = rows[index]
                    body.append(theme.ICONS.get(level, theme.ICONS["info"]) + " ", style=level)
                    body.append(line, style=level)
                if index < log_rows - 1:
                    body.append("\n")
            panel = Panel(body, title=Text("运行日志 · 最近 8 行", style="muted"),
                          box=theme.BOX, border_style="border", height=log_rows + 2, padding=(0, 1))
            if self.screen.active:
                return Group(Text(self.heading, style="title"), self.progress, *resource_details, panel)
            return Group(self.progress, *resource_details, panel)

    @contextmanager
    def activity(self, name):
        outer = self.depth == 0
        self.depth += 1
        from contextlib import ExitStack
        try:
            with ExitStack() as stack:
                if outer:
                    stack.enter_context(self.runtime_input)
                    if self.screen.active:
                        self.screen.page_start()
                    self.progress.update(self.question_task, visible=False)
                    self.progress.update(self.resource_task, visible=False)
                    self.progress.reset(self.task, description=safe_text(name), total=None)
                    self.live = Live(console=self.console, get_renderable=self.render, transient=True,
                                     refresh_per_second=theme.REFRESH_PER_SECOND, vertical_overflow='crop',
                                     auto_refresh=self.console.is_terminal, redirect_stdout=False, redirect_stderr=False)
                    stack.callback(self.live.stop)
                    self.live.start()
                yield
        finally:
            self.depth -= 1
            if outer:
                self.live = None

    @contextmanager
    def prompt_region(self):
        """Hand the terminal to a blocking manual prompt, then restore the live view."""
        live = self.live
        if live:
            live.stop()
            self.live = None
        try:
            with self.runtime_input.suspend():
                yield
        finally:
            self.live = live
            if live:
                try:
                    live.start()
                except Exception:
                    pass

    def header(self, version):
        self.heading = f"ICVE AI Assistant · {version}"
        self.console.print(Panel(Text.assemble(("ICVE AI Assistant", "title"),
                                              (f"  {version}\n智慧职教 AI 助手", "muted")),
                                 box=theme.BOX, border_style="border"))
        self.note("Esc 返回 · q 退出")

    def note(self, message, level="muted"):
        if self._prompt_rows and not self.live:
            row = Text(safe_text(message), style=level)
            self._prompt_rows.append(lambda width: [row])
        elif self.live:
            self.log(message, "info" if level == "muted" else level)
        else:
            self.console.print(Text(safe_text(message), style=level))

    def ask(self, prompt, default=None, secret=False, *, navigation=True):
        if self.live:
            raise RuntimeError("输入必须在动态任务区域之外进行")
        suffix = f" [默认 {default}]" if default is not None else ""
        text = f"{prompt}{suffix} {theme.ICONS['prompt']} "
        if self.input_fn is None and self.console.is_terminal and sys.stdin.isatty() and (not secret or self.secret_fn is None):
            rows, self._prompt_rows = self._prompt_rows, []
            above = (lambda width: [line for render in rows for line in render(width)]) if rows else None
            value = read_terminal_line(text, secret=secret, above_prompt=above, output=self.terminal_output())
        elif secret:
            value = (self.secret_fn or getpass.getpass)(text)
        else:
            self.console.print(Text(text, style="accent"), end="")
            value = (self.input_fn or input)()
        if value == "\x1b":
            raise Back()
        if navigation and not secret and value.strip().lower() == "q":
            raise Cancelled()
        if navigation and not secret and value.strip().lower() == "b":
            raise Back()
        return value.strip() or (str(default) if default is not None else "")

    def table(self, title, columns, rows, help_text=""):
        self._prompt_rows.clear()
        if self.console.is_terminal:
            if self.screen.active:
                self.screen.page_start()
            else:
                self.console.clear()
            self.console.print(Text(self.heading, style="title"))
        table = Table(box=theme.TABLE_BORDER, expand=True, show_edge=False, header_style="table_header")
        for i, column in enumerate(columns):
            table.add_column(column, style="accent" if i == 0 else "info",
                             no_wrap=True, overflow="ellipsis", ratio=3 if i == 1 else None,
                             width={"状态": 8, "成绩(分)": 8, "剩余(次)": 8}.get(column))
        for row in rows:
            table.add_row(*(Text(safe_text(value), no_wrap=True, overflow="ellipsis") for value in row))
        help_text = safe_text(help_text)
        self.console.print(Panel(table, title=Text(safe_text(title), style="title"),
                                 box=theme.BOX, border_style="border", padding=(0, 1)))
        if help_text:
            self.shortcuts(help_text)

    def set_user(self, user=None):
        """Display names only; never substitute login IDs or phone numbers."""
        self._prompt_rows.clear()
        self.account_name = ""
        if isinstance(user, dict):
            for key in ("displayName", "realName", "nickName"):
                value = user.get(key)
                if isinstance(value, str) and value.strip():
                    self.account_name = " ".join(safe_text(value).split())
                    if self.account_name:
                        break

    def shortcuts(self, message, *, show_name=True):
        """Let the input renderer own animated rows, so it also owns the cursor."""
        if self.account_name and self.input_fn is None and self.console.is_terminal and sys.stdin.isatty():
            self._prompt_rows.append(lambda width: self._shortcut_lines(message, width, show_name, animated=True))
        else:
            for line in self._shortcut_lines(message, self.console.width, show_name):
                self.console.print(line)

    def _shortcut_lines(self, message, width, show_name, animated=False):
        lines = []
        line = Text(style="info")
        name_space = min(cell_len(self.account_name), 12) + 2 if show_name and self.account_name else 0
        first_width = max(width // 2, width - name_space) if cell_len(safe_text(message)) > width else width
        for part in safe_text(message).split(" · "):
            if line and cell_len(line.plain) + 3 + cell_len(part) > (width if lines else first_width):
                lines.append(line)
                line = Text(style="info")
            if line:
                line.append(" · ")
            key, separator, label = ("/", "", part[1:]) if part.startswith("/") else part.partition(" ")
            line.append(key, style="shortcut")
            line.append(separator + label)
        lines.append(line)
        line = lines[0]
        available = width - cell_len(line.plain) - 2
        if show_name and self.account_name and available >= 2:
            name = Text(self.account_name, style="info", no_wrap=True)
            name.truncate(available, overflow="ellipsis")
            if animated:
                for index, (color, _) in enumerate(self.name_animation.colors(name.plain)):
                    name.stylize(f"bold {color}", index, index + 1)
            line.append(" " * (width - cell_len(line.plain) - cell_len(name.plain)))
            line.append_text(name)
        return lines

    def native_enabled(self):
        """True only for a real interactive terminal with no injected input."""
        return self.input_fn is None and self.console.is_terminal and sys.stdin.isatty()

    def choice(self, title, choices, default=None, help_text="", *, message=""):
        values = {str(key): key for key, _, _ in choices}
        if self.native_enabled():
            from native_ui import option_menu
            result = option_menu(title, [(key, name, description) for key, name, description in choices],
                                 default, help_text=help_text, message=message,
                                 heading=self.heading, account_name=self.account_name,
                                 output=self.terminal_output())
            if result.kind == "back":
                raise Back()
            if result.kind == "quit":
                raise Cancelled()
            if result.kind == "select":
                return result.value
        warning = ""
        while True:
            self.table(title, ["编号", "选项", "说明"],
                       [(str(key), name, description) for key, name, description in choices], help_text)
            if message:
                self.note(message)
            if warning:
                self.note(warning, "warning")
            value = self.ask("选择", default)
            if value in values:
                return values[value]
            warning = "请输入编号；Esc 返回，q 退出。"

    def select(self, title, items, label, details=None, default=None, *,
               multiple=True, actions=None, filters=None, state=None, selected=None, status=None,
               extra_columns=(), selectable=None, item_hint=None):
        actions = actions or {}
        disabled = {i for i, item in enumerate(items) if selectable and not selectable(item)}
        if not items and not actions:
            self.note("没有可选择的项目。", "warning")
            return []
        if multiple and self.input_fn is None and self.console.is_terminal and sys.stdin.isatty():
            from checklist import choose_many
            state = state if state is not None else {}
            rows = [(safe_text(label(item)), safe_text(status(item)) if status else "",
                     safe_text(details(item)) if details else "",
                     *(safe_text(value(item)) for _, value in extra_columns)) for item in items]
            result = choose_many(safe_text(title), rows, heading=self.heading,
                                 account_name=self.account_name,
                                 selected=[i for i, item in enumerate(items) if selected and item in selected],
                                 cursor=state.get("cursor", 1), refresh="r" in actions,
                                 disabled=disabled,
                                 row_hints=[safe_text(item_hint(item)) for item in items] if item_hint else (),
                                 extra_headers=[name for name, _ in extra_columns], output=self.terminal_output())
            state["cursor"] = result.cursor
            state["selected"] = [items[i] for i in result.indexes if i not in disabled]
            if result.action == "back":
                raise Back()
            if result.action == "quit":
                raise Cancelled()
            return result.action or state["selected"]
        if not multiple and self.native_enabled():
            from native_ui import single_picker
            state = state if state is not None else {}
            result = single_picker(
                safe_text(title), items, lambda item: safe_text(label(item)),
                (lambda item: safe_text(details(item))) if details else None,
                actions=actions,
                state=state,
                selected=[item for item in items if selected and item in selected],
                status=(lambda item: safe_text(status(item))) if status else None,
                extra_columns=tuple((name, (lambda item, value=value: safe_text(value(item))))
                                    for name, value in extra_columns),
                selectable=selectable,
                item_hint=(lambda item: safe_text(item_hint(item))) if item_hint else None,
                heading=self.heading, account_name=self.account_name,
                output=self.terminal_output())
            if result.kind == "back":
                raise Back()
            if result.kind == "quit":
                raise Cancelled()
            if result.kind == "action":
                return result.action
            return result.value
        # Piped input and scripted clients retain a line-based fallback.
        from AIMoocMain.export_questions import parse_index_input
        state = state if state is not None else {}
        rows = [(i, label(x), details(x) if details else "") for i, x in enumerate(items, 1)]
        while True:
            query, category = state.get("query", ""), state.get("filter", "f0")
            predicate = filters.get(category, ("全部", lambda x: True))[1] if filters else lambda x: True
            filtered = [row for row in rows if predicate(items[row[0] - 1])
                        and query in (str(row[1]) + " " + str(row[2])).casefold()]
            pages = max(1, (len(filtered) + theme.PAGE_SIZE - 1) // theme.PAGE_SIZE)
            page = min(state.get("page", 0), pages - 1)
            available_rows = [row for row in filtered if row[0] - 1 not in disabled]
            help_parts = (["1,3 / 1-3", f"all 全选 {len(available_rows)} 项"] if multiple else ["编号进入"])
            if pages > 1:
                help_parts.append("n/p 翻页")
            help_parts += ["/ 关键词搜索"] + [f"{key} {name}" for key, name in actions.items()]
            page_rows = filtered[page * theme.PAGE_SIZE:(page + 1) * theme.PAGE_SIZE]
            columns = ["编号", "名称", "详情"]
            if status:
                columns = ["编号", "名称", "状态", "详情"]
                page_rows = [(i, name, status(items[i - 1]), detail) for i, name, detail in page_rows]
            if extra_columns:
                columns = columns[:-1] + [name for name, _ in extra_columns] + ["详情"]
                columns = [{"成绩": "成绩(分)", "剩余次数": "剩余(次)"}.get(name, name) for name in columns]
                page_rows = [(*row[:-1], *(value(items[row[0] - 1]) for _, value in extra_columns), row[-1])
                             for row in page_rows]
                if self.console.width < 80:
                    columns = columns[:-1]
                    page_rows = [row[:-1] for row in page_rows]
            self.table(f"{title} · {len(filtered)} 项 · {page + 1}/{pages}", columns,
                       page_rows,
                       " · ".join(help_parts))
            if item_hint:
                for row in page_rows:
                    if row[0] - 1 in disabled:
                        self.note(f"{row[0]}. {label(items[row[0] - 1])} · {item_hint(items[row[0] - 1])}", "warning")
            if filters:
                self.shortcuts(" · ".join(f"{key} {name}" + (" ✓" if key == category else "")
                                     for key, (name, _) in filters.items()), show_name=False)
            if query:
                self.note(f"搜索：{query} · / 清除搜索")
            visible = {row[0] for row in filtered}
            retained = [i for i, item in enumerate(items, 1) if selected and item in selected and i in visible]
            retained = [i for i in retained if i - 1 not in disabled]
            effective_default = "保留" if retained else default
            if retained:
                self.note(f"已选 {len(retained)} 项 · 回车保留")
            self.shortcuts("Esc 返回 · q 退出", show_name=False)
            raw = self.ask("选择编号", effective_default)
            if raw.lower() in actions:
                return raw.lower()
            if raw.lower() in ("n", "p"):
                state["page"] = min(pages - 1, page + 1) if raw.lower() == "n" else max(0, page - 1)
                continue
            if filters and raw.lower() in filters:
                state.update(filter=raw.lower(), page=0)
                continue
            if raw.startswith("/"):
                state.update(query=raw[1:].strip().casefold(), page=0)
                continue
            if not raw:
                self.note("请输入编号。", "warning")
                continue
            if raw == "保留" and retained:
                indexes = retained
            else:
                indexes = [row[0] for row in available_rows] if raw.lower() == "all" else parse_index_input(raw, len(items))
            if any(i - 1 in disabled for i in (indexes or ())) or (raw.lower() == "all" and not available_rows):
                self.note("所选任务暂不可选，请核对开放时间或任务类型；请选择其他任务。", "warning")
                continue
            if indexes and set(indexes) <= visible and (multiple or len(indexes) == 1):
                self.note(f"已选择 {len(indexes)} 项。")
                return [items[index - 1] for index in indexes]
            self.note("请输入当前列表的编号。" if multiple else "请输入一个课程编号。", "warning")

    def library(self, rows, platform, state):
        platform_name = "资源库" if platform == 1 else "MOOC / AI 优课"
        if not rows:
            return self.empty_library(platform_name)
        return self.select(f"我的课程 · {platform_name}", rows,
                           lambda x: x.get("courseName", "未知课程"),
                           self.course_details,
                           multiple=False, state=state,
                           actions={"s": "设置", "t": "切换平台", "r": "刷新"})

    def empty_library(self, platform_name):
        """A readable homepage when the signed-in account has no courses."""
        # Keep every line short enough to survive 40-column tables unclipped.
        lines = ["当前平台暂无可显示的课程",
                 "可先到平台网页核对或加入课程。",
                 "加入课程后可刷新。",
                 "已有课程？请核对账号与平台，",
                 "并确认课程是否开放。"]
        actions = {"r": "刷新课程", "t": "切换平台", "s": "设置"}
        help_text = "Enter/r 刷新 · t 切换平台 · s 设置 · Esc 返回 · q 退出"
        if self.native_enabled():
            from native_ui import action_page
            result = action_page(
                f"我的课程 · {platform_name}", lambda: list(lines), actions, default="r",
                help_text=help_text, heading=self.heading,
                status="已登录 · 课程列表已读取",
                account_name=self.account_name, output=self.terminal_output())
            if result.kind == "back":
                raise Back()
            if result.kind == "quit":
                raise Cancelled()
            return result.action
        warning = ""
        while True:
            self.table(f"我的课程 · {platform_name}", ["说明"], [(line,) for line in lines], help_text)
            if warning:
                self.note(warning, "warning")
            raw = self.ask("操作", "r").strip().lower()
            if raw in actions:
                return raw
            warning = "请输入 r 刷新、t 切换平台或 s 设置；q 退出。"

    def course_menu(self, courses, platform):
        title = courses[0].get("courseName", "未知课程") if len(courses) == 1 else f"已选 {len(courses)} 门课程"
        choices = [(1, "自动学习", "完成课程学习任务")]
        if platform == 0:
            choices += [(2, "AI 答题", "AI 作答并自动交卷"), (3, "导出题目", "保存题目和已有答案")]
        return self.choice(title, choices, help_text="Esc 返回课程列表 · q 退出")

    def choose_platform(self, current):
        return self.choice("切换平台", [(0, "MOOC / AI 优课", "ai.icve.com.cn"),
                                        (1, "资源库", "zyk.icve.com.cn")], default=current,
                           help_text="Esc 返回 · q 退出")

    def connection_problem(self, error):
        from MoocMain.log import redact
        return self.choice("登录或课程加载失败", [("r", "重试登录 / 读取课程", ""),
                                            ("s", "本机设置", "修改配置或退出已保存的登录"),
                                            ("t", "切换平台", "")],
                           help_text=redact(str(error)) or "Esc 返回 · q 退出")

    def empty_tasks(self):
        if self.native_enabled():
            from native_ui import action_page
            result = action_page(
                "课程任务", lambda: ["当前课程 · 暂无作业、考试或测验",
                                     "可在平台或稍后刷新查看。"],
                {"b": "返回课程"}, default="b",
                help_text="Enter 返回 · q 退出",
                heading=self.heading, status="没有可执行的任务。",
                account_name=self.account_name, output=self.terminal_output())
            if result.kind == "quit":
                raise Cancelled()
            return None
        self.table("课程任务", ["任务", "状态"], [("当前课程", "暂无作业、考试或测验")])
        self.ask("回车返回课程，q 退出")

    def review_study(self, courses, max_workers=8):
        if self.native_enabled():
            from native_ui import action_page
            names = [safe_text(c.get("courseName", "未知课程")) for c in courses]
            lines = lambda: [f"共 {len(names)} 门课程"] + names
            while True:
                result = action_page(
                    "自动学习", lines, {"start": "开始学习", "b": "返回"}, default="start",
                    help_text="Enter 开始 · Esc 返回 · q 退出",
                    heading=self.heading, account_name=self.account_name,
                    output=self.terminal_output())
                if result.kind == "back" or result.action == "b":
                    raise Back()
                if result.kind == "quit":
                    raise Cancelled()
                return None
        self.table("自动学习", ["课程", "操作"], [(c.get("courseName", "未知课程"), "完成课程学习任务") for c in courses],
                   "回车开始 · Esc 返回 · q 退出")
        while self.ask("操作", "开始") != "开始":
            self.note("回车开始；Esc 返回，q 退出。")

    def courses(self, rows):
        return self.select("选择课程", rows, lambda x: x.get("courseName", "未知课程"), self.course_details)

    def _manual_question_lines(self, question, reason, warning=""):
        rows = []
        title = f"{question.get('number', 0) + 1}. {question.get('type', '')}"
        if question.get("child_number") is not None:
            title += " · 第 %s 小题" % (question["child_number"] + 1)
        rows.append(Text(title, style="title"))
        if question.get("context"):
            rows.append(Text("材料：" + question["context"], style="muted"))
        rows.append(Text("题干：" + question.get("title", ""), style="info"))
        for option in question.get("options", []):
            rows.append(Text("%s. %s" % (option["label"], option["text"]), style="info"))
        if question.get("type_id") in (4, 5):
            rows.append(Text("共 %s 个空，用 | 分隔" % question.get("blank_count"), style="muted"))
        rows.append(Text("AI 未完成原因：" + safe_text(reason or "AI 未提供答案"), style="warning"))
        rows.append(Text("B 是选项，不是返回；输入 :s 保存退出，Esc 返回", style="muted"))
        if warning:
            rows.append(Text(warning, style="warning"))
        return rows

    def _read_manual_line(self, rows, prompt="请输入答案"):
        """Print the question rows and read one line with the live view suspended."""
        with self.prompt_region():
            for row in rows:
                self.console.print(row)
            return self.ask(prompt, navigation=False)

    def manual_answer(self, question, reason):
        """Read one human answer with the question, options and reason visible."""
        warning = ""
        while True:
            try:
                answer = self._read_manual_line(self._manual_question_lines(question, reason, warning)).strip()
            except Back:
                return None
            if answer in (":s", ":save", ":q", ":quit"):
                return None
            try:
                return manual_answer_value(question, answer)
            except ValueError as exc:
                warning = "输入无效：%s" % exc

    def confirm_submit(self, paper, completed, total):
        """Whole-paper confirmation: True submits, False declines, None cancels the run."""
        try:
            with self.prompt_region():
                return self.confirm("%s %s/%s 题已完成，交卷提交整份试卷？" % (paper, completed, total), default=False)
        except (Back, Cancelled, EOFError, KeyboardInterrupt):
            return None

    @staticmethod
    def course_details(course):
        parts = [str(course.get("courseInfoName") or "")]
        if course.get("studySpeed") is not None:
            parts.append(f"进度 {course['studySpeed']}%")
        return " · ".join(x for x in parts if x)

    def confirm(self, prompt, default=False):
        if self.native_enabled():
            from native_ui import confirm_prompt
            result = confirm_prompt(prompt, default, output=self.terminal_output())
            if result.kind == "back":
                raise Back()
            if result.kind == "quit":
                raise Cancelled()
            if result.kind == "select":
                return result.value
        while True:
            answer = self.ask(prompt + " [y/n]", "y" if default else "n").lower()
            if answer in ("y", "n"):
                return answer == "y"
            self.note("请输入 y 或 n。", "warning")

    def ai_config(self, current=None):
        from model_ui import draft_config
        provider = 2 if current and current.base_url == "https://api.deepseek.com" else 1
        url, model = (current.base_url, current.model) if current else (None, None)
        step = "provider"
        while True:
            try:
                if step == "provider":
                    provider = self.choice("AI 服务", [(1, "自定义服务", "OpenAI 兼容"),
                                                        (2, "DeepSeek", "填写 Key 并选择模型")], default=provider,
                                           help_text="Esc 返回 · q 退出")
                    if provider == 2 and url != "https://api.deepseek.com":
                        url, model = "https://api.deepseek.com", None
                    step = "url" if provider != 2 else "model"
                elif step == "url":
                    url = self.ask("API Base URL（含 /v1 等服务路径）", url)
                    step = "model"
                elif step == "model":
                    if provider != 2:
                        model = self.ask("Model ID", model)
                        step = "key"
                    else:
                        value = self.choice("DeepSeek 模型", [(1, "deepseek-chat", ""), (2, "deepseek-reasoner", ""),
                                                               (3, "自定义模型", "填写 Model ID")],
                                            default={"deepseek-chat": 1, "deepseek-reasoner": 2}.get(model, 3 if model else 1))
                        if value == 3:
                            step = "custom_model"
                        else:
                            model = {1: "deepseek-chat", 2: "deepseek-reasoner"}[value]
                            step = "key"
                elif step == "custom_model":
                    model = self.ask("DeepSeek Model ID", model)
                    step = "key"
                else:
                    reuse_key = current and url.strip().rstrip("/") == current.base_url
                    key = self.ask("API Key（隐藏输入，回车保留原 Key）" if reuse_key else "API Key（隐藏输入，将保存到本机）", secret=True)
                    return draft_config(url, model, key, current,
                                        provider_name=current.provider_name if current else "")
            except Back:
                if step == "provider":
                    raise
                step = {"url": "provider", "model": "url" if provider != 2 else "provider",
                        "custom_model": "model", "key": "model"}[step]
            except ValueError as exc:
                self.note(str(exc), "warning")
                step = "url" if provider != 2 else "model"

    def model_picker(self, rows, active_id=None, *, cursor_id=None, message="", locked_ids=(),
                     test_connection=None):
        from model_ui import choose_model, ModelChoice
        rows = [(row[0], *(safe_text(value) for value in row[1:])) for row in rows]
        if self.input_fn is None and self.console.is_terminal and sys.stdin.isatty():
            return choose_model(rows, active_id, cursor_id=cursor_id, message=safe_text(message),
                                locked_ids=locked_ids, test_connection=test_connection, output=self.terminal_output())
        self.table("选择模型", ["编号", "供应商", "模型", "状态"],
                   [(str(i), ("● " if row[0] == active_id else "○ ") + row[1], row[2], row[3])
                    for i, row in enumerate(rows, 1)],
                   "编号 使用 · t 编号 测试连接 · a 添加 · e 编号 编辑自定义模型 · Esc 返回")
        if message:
            self.note(message)
        while True:
            try:
                value = self.ask("模型")
            except Back:
                return ModelChoice("back")
            if value == "a":
                return ModelChoice("add")
            if value.startswith("t"):
                index = value[1:].strip()
                if index.isdigit() and 1 <= int(index) <= len(rows) and test_connection:
                    self.note(f"测试中 · {rows[int(index) - 1][1]}")
                    self.note(test_connection(rows[int(index) - 1][0]))
                    continue
                self.note("请输入要测试的模型编号，如 t 1。", "warning")
                continue
            action, index = ("edit", value[1:].strip()) if value.startswith("e") else ("select", value)
            if index.isdigit() and 1 <= int(index) <= len(rows):
                profile_id = rows[int(index) - 1][0]
                if action == "edit" and profile_id in locked_ids:
                    self.note("默认供应商不可修改或删除。", "warning")
                    continue
                return ModelChoice(action, profile_id)
            self.note("请输入模型编号，或 t 编号测试、a 添加、e 编号编辑。", "warning")

    def model_editor(self, current=None):
        from model_ui import edit_model, draft_config, ModelEdit
        if self.input_fn is None and self.console.is_terminal and sys.stdin.isatty():
            return edit_model(current, output=self.terminal_output())
        provider_name = current.provider_name if current else ""
        url, model = (current.base_url, current.model) if current else ("", "")
        try:
            if current:
                action = self.choice("编辑模型", [(1, "编辑", ""), (2, "删除", "")], default=1)
                if action == 2:
                    return ModelEdit("delete")
            while True:
                provider_name = self.ask("供应商名称（可留空）", provider_name)
                url = self.ask("接口地址（OpenAI 兼容）", url)
                model = self.ask("模型名称", model)
                key = self.ask("API Key（隐藏输入，留空保留原 Key）" if current else "API Key", secret=True)
                try:
                    config = draft_config(url, model, key, current, provider_name=provider_name)
                except ValueError as exc:
                    self.note(str(exc), "warning")
                    continue
                if self.confirm("保存并测试", default=True):
                    return ModelEdit("save", config)
                return ModelEdit("back")
        except Back:
            return ModelEdit("back")

    def finish_text(self):
        """One-line summary and its style, or ``None`` when nothing is shown."""
        from MoocMain.log import redact
        if self.finished:
            return None
        self.finished = True
        if self.workspace and not self.operation_active and not self.pending_error:
            return ("已退出。", "muted") if self.cancelled else None
        success = sum(self.counts[s] for s in ("success", "submitted", "preview"))
        failures = sum(self.counts[s] for s in ("error", "needs_review", "submission_unknown", "submitting",
                                                "ai_error", "paused"))
        # Fatal errors before any task result must still be visible in the one-line summary.
        if self.pending_error and self.unit != "份试卷":
            failures += 1
        elapsed = max(0, int(self.clock() - self.started))
        text = ("试卷汇总 · " if self.unit == "份试卷" else "") + f"成功 {success} · 失败 {failures}"
        if self.counts["skipped"]:
            text += f" · 跳过 {self.counts['skipped']}"
        if self.counts["needs_review"]:
            text += f" · 未完成 {self.counts['needs_review']}"
        if self.counts["paused"]:
            text += f" · 待补答 {self.counts['paused']}"
        if self.unit == "份试卷":
            total = self.progress.tasks[0].total
            pending = max(0, int(total) - len(self.results)) if total is not None else 0
            if pending:
                text += f" · 未执行 {pending}"
        text += f" · 总用时 {elapsed // 60:02d}:{elapsed % 60:02d}"
        unknown = self.counts["submission_unknown"] + self.counts["submitting"]
        if unknown:
            text += f" · 结果未知 {unknown}（先核对平台）"
        if self.cancelled:
            text += " · 已取消"
        if self.pending_error:
            text += " · " + " ".join(safe_text(redact(self.pending_error)).split())
        style = "error" if failures or self.pending_error else "warning" if self.cancelled else "success"
        return text, style

    def finish(self):
        result = self.finish_text()
        if result:
            text, style = result
            self.console.print(Text(text, style=style, no_wrap=True, overflow="ellipsis"))

    def begin_operation(self):
        self.unit = "项"
        self.progress.update(self.question_task, visible=False)
        self.progress.update(self.resource_task, visible=False)
        self.counts.clear()
        self.lines.clear()
        self.results.clear()
        self.errors.clear()
        self.started = self.clock()
        self.finished = False
        self.pending_error = ""
        self.cancelled = False
        self.operation_active = True

    def operation_result(self, paths=()):
        from MoocMain.log import redact
        statuses = {"success": "完成", "submitted": "已交卷", "preview": "答案已生成 · 未交卷",
                    "skipped": "已跳过", "needs_review": "未完成 · 未交卷", "error": "失败", "ai_error": "AI 服务失败",
                    "paused": "待补答 · 未交卷",
                    "submission_unknown": "交卷结果未知", "submitting": "交卷结果未知"}
        if self.native_enabled():
            from native_ui import action_page
            summary = self.finish_text()

            def lines():
                rows = [f"{safe_text(name)} · {statuses.get(status, status)}"
                        for name, status in self.results]
                if not rows and not self.pending_error:
                    rows.append("本次没有处理任务。")
                if self.counts["preview"]:
                    rows.append("本地答案未提交到平台，不会产生成绩。")
                if self.counts["needs_review"]:
                    rows.append("存在未完成或不支持的题目；仅保存本地报告，未交卷。")
                if self.counts["paused"]:
                    rows.append("有待补答题目，已在本地保存；再次选择同一试卷可继续补答。")
                rows += [safe_text(error) for error in self.errors]
                if self.pending_error:
                    rows.append("错误 · " + safe_text(redact(self.pending_error)))
                for path in dict.fromkeys(str(p) for p in paths):
                    rows.append(f"文件 · {safe_text(path)}")
                return rows

            status = summary[0] if summary else ""
            result = action_page(
                "执行结果", lines, {"1": "返回本课程", "2": "切换课程"}, default="1",
                status=status, help_text="Enter 返回 · 2 切换课程 · Esc 返回本课程 · q 退出",
                heading=self.heading, account_name=self.account_name,
                output=self.terminal_output())
            if result.kind == "quit":
                raise Cancelled()
            if result.kind == "action" and result.action == "2":
                return 2
            if result.kind == "action" and result.action == "1":
                return 1
            return 1  # Esc / back returns to the current course
        self.table("执行结果", ["任务", "结果"],
                   [(name, statuses.get(status, status)) for name, status in self.results])
        self.finish()
        if self.counts["preview"]:
            self.note("本地答案未提交到平台，不会产生成绩。需要交卷时，在执行摘要按 s → 执行方式 → 答题并自动交卷。", "warning")
        if self.counts["needs_review"]:
            self.note("存在未完成或不支持的题目；仅保存本地报告，未交卷。")
        if self.counts["paused"]:
            self.note("有待补答题目，已在本地保存；再次选择同一试卷可继续补答。", "warning")
        for error in self.errors:
            self.note(error, "warning")
        if self.pending_error:
            self.note(redact(self.pending_error), "error")
        if not self.results and not self.pending_error:
            self.note("本次没有处理任务。")
        for path in dict.fromkeys(str(p) for p in paths):
            self.note(f"文件：{path}")
        self.note("回车返回本课程 · 2 切换课程 · Esc 返回本课程 · q 退出")
        while True:
            try:
                action = self.ask("继续", "1")
            except Back:
                return 1
            if action in ("1", "2"):
                return int(action)
            self.note("请输入 1 或 2。", "warning")

    def selected_task_note(self, selected):
        from AIMoocMain.export_questions import paper_display_name
        for course, item, _ in selected[:5]:
            self.note(f"  {course.get('courseName', '')} / {paper_display_name(item)}")
        if len(selected) > 5:
            self.note(f"  … 共 {len(selected)} 份，Esc 返回查看或修改选择")

    def review_export(self, selected, options):
        if options.get("export_format") not in ("md", "docx", "both"):
            options["export_format"] = "md"
        if options.get("quiz_mode") not in ("per_chapter", "merged"):
            options["quiz_mode"] = "per_chapter"
        options["include_my_answer"] = options.get("include_my_answer") is True
        if self.native_enabled():
            from native_ui import action_page
            formats = {"md": "Markdown", "docx": "Word", "both": "Markdown + Word"}

            def lines():
                rows = [f"文件格式 · {formats[options['export_format']]}",
                        f"文件组织 · {'按课程合并' if options['quiz_mode'] == 'merged' else '每份一个文件'}",
                        f"历史答案 · {'包含' if options['include_my_answer'] else '不包含'}",
                        "保存位置 · tiku/<课程名>/"]
                for course, item, _ in selected[:5]:
                    from AIMoocMain.export_questions import paper_display_name
                    rows.append(f"{safe_text(course.get('courseName', ''))} / "
                                f"{safe_text(paper_display_name(item))}")
                if len(selected) > 5:
                    rows.append(f"… 共 {len(selected)} 份")
                return rows

            while True:
                result = action_page(
                    f"导出 {len(selected)} 份题目", lines, {"start": "开始导出", "s": "修改设置", "b": "返回"},
                    default="start", help_text="Enter 开始 · s 修改设置 · Esc 返回 · q 退出",
                    heading=self.heading, account_name=self.account_name,
                    output=self.terminal_output())
                if result.kind == "quit":
                    raise Cancelled()
                if result.kind == "back" or result.action == "b":
                    raise Back()
                if result.action == "start":
                    return options
                if result.action == "s":
                    self.edit_export_options(options)
        while True:
            self.table(f"导出 {len(selected)} 份题目", ["设置", "当前值"], [
                ("文件格式", {"md": "Markdown", "docx": "Word", "both": "Markdown + Word"}[options["export_format"]]),
                ("文件组织", "按课程合并" if options["quiz_mode"] == "merged" else "每份一个文件"),
                ("历史答案", "包含" if options["include_my_answer"] else "不包含"),
                ("保存位置", "tiku/<课程名>/"),
            ])
            self.selected_task_note(selected)
            self.note("回车开始 · s 修改设置 · Esc 返回选择 · q 退出")
            action = self.ask("操作", "开始")
            if action == "开始":
                return options
            if action.lower() == "s":
                self.edit_export_options(options)

    def edit_export_options(self, options):
        while True:
            try:
                action = self.choice("导出设置", [(1, "文件格式", "Markdown / Word"),
                                                  (2, "文件组织", "分开保存或按课程合并"),
                                                  (3, "历史答案", "是否包含我的已有作答")],
                                     help_text="Esc 返回摘要 · q 退出")
            except Back:
                return
            try:
                if action == 1:
                    value = self.choice("文件格式", [(1, "Markdown", ".md"), (2, "Word", ".docx"),
                                                     (3, "两种都导出", ".md + .docx")],
                                        default={"md": 1, "docx": 2, "both": 3}[options["export_format"]])
                    options["export_format"] = {1: "md", 2: "docx", 3: "both"}[value]
                elif action == 2:
                    value = self.choice("文件组织", [(1, "每份一个文件", ""), (2, "按课程合并", "仅合并所选内容")],
                                        default=2 if options["quiz_mode"] == "merged" else 1)
                    options["quiz_mode"] = "merged" if value == 2 else "per_chapter"
                else:
                    options["include_my_answer"] = self.confirm("包含我的历史答案", options["include_my_answer"])
            except Back:
                continue

    def review_ai(self, selected, config, options, check_connection=None, manage_models=None, connection_status=None):
        options["config"] = config
        options.setdefault("auto_submit", True)
        if self.native_enabled():
            from native_ui import action_page
            message = ""

            def connection_text():
                value = str(options.get("connection_status", ""))
                return "连接失败" if "失败" in value else "已通过" if "通过" in value else "未测试"

            def lines():
                rows = [f"供应商 · {(config.provider_name or '自定义供应商') if config else '未选择'}",
                        f"模型 · {config.model if config else '未选择'}",
                        f"连接状态 · {connection_text()}",
                        "执行方式 · " + ("答题并自动交卷" if options["auto_submit"] else "仅生成答案（不交卷，无成绩）")]
                for course, item, _ in selected[:5]:
                    from AIMoocMain.export_questions import paper_display_name
                    rows.append(f"{safe_text(course.get('courseName', ''))} / "
                                f"{safe_text(paper_display_name(item))}")
                if len(selected) > 5:
                    rows.append(f"… 共 {len(selected)} 份")
                return rows

            actions = {"start": "开始答题", "s": "答题设置"}
            if manage_models:
                actions["m"] = "模型配置"
            if check_connection:
                actions["t"] = "测试连接"
            actions["b"] = "返回"
            while True:
                result = action_page(
                    f"AI 答题 · {len(selected)} 份任务", lines, actions, default="start",
                    status=lambda: message,
                    help_text="Enter 开始 · m 模型配置 · s 答题设置 · t 测试连接 · Esc 返回 · q 退出",
                    heading=self.heading, account_name=self.account_name,
                    output=self.terminal_output())
                if result.kind == "quit":
                    raise Cancelled()
                if result.kind == "back" or result.action == "b":
                    raise Back()
                if result.action == "s":
                    self.edit_answer_options(options)
                elif result.action == "t" and check_connection:
                    options["connection_status"] = check_connection()
                elif result.action == "m" and manage_models:
                    config = manage_models()
                    options["config"] = config
                    options["connection_status"] = connection_status(config) if connection_status else "未测试"
                elif result.action == "start":
                    if config is None:
                        message = "请按 m 选择模型。"
                        continue
                    return options["auto_submit"]
        while True:
            connection = str(options.get("connection_status", ""))
            connection = "连接失败" if "失败" in connection else "已通过" if "通过" in connection else "未测试"
            self.table(f"AI 答题 · {len(selected)} 份任务", ["设置", "当前值"], [
                ("供应商", (config.provider_name or "自定义供应商") if config else "未选择"),
                ("模型", config.model if config else "未选择"),
                ("连接状态", connection),
                ("执行方式", "通过校验后自动交卷" if options["auto_submit"] else "仅生成答案（不交卷，无成绩）"),
            ])
            self.selected_task_note(selected)
            self.shortcuts("回车开始 · " + ("m 模型配置 · " if manage_models else "") +
                           "s 答题设置 · " + ("t 测试连接 · " if check_connection else "") + "Esc 返回选择 · q 退出")
            action = self.ask("操作", "开始")
            if action == "开始":
                if config is None:
                    self.note("请按 m 选择模型。", "warning")
                    continue
                return options["auto_submit"]
            if action.lower() == "s":
                self.edit_answer_options(options)
            if action.lower() == "t" and check_connection:
                options["connection_status"] = check_connection()
            if action.lower() == "m" and manage_models:
                config = manage_models()
                options["config"] = config
                options["connection_status"] = connection_status(config) if connection_status else "未测试"

    def edit_answer_options(self, options):
        try:
            mode = self.choice("执行方式", [(1, "仅生成答案", "不交卷，无平台成绩"),
                                            (2, "答题并自动交卷", "全部题目通过校验后提交")],
                               default=2 if options["auto_submit"] else 1)
        except Back:
            return
        options["auto_submit"] = mode == 2

    def home(self, platform_default=0):
        mode = self.choice("1 / 选择功能", [
            (1, "自动学习", "完成课程学习任务"),
            (2, "AI 答题", "AI 作答并自动交卷"),
            (3, "导出题目", "保存题目和已有答案"),
            (4, "本机设置", "修改模型配置、清除 Key、退出登录"),
        ], default=1)
        platform = 0
        if mode == 1:
            platform = self.choice("2 / 选择平台", [
                (0, "常规 MOOC / AI 优课", "ai.icve.com.cn"),
                (1, "资源库", "zyk.icve.com.cn"),
            ], default=platform_default if platform_default in (0, 1) else 0)
        elif mode != 4:
            self.note("平台：常规 MOOC / AI 优课")
        return mode, platform

    def login_hint(self):
        self.note("接下来在浏览器登录，支持扫码或短信；完成后自动返回。")
        self.note("等待登录期间可按 Ctrl+C 退出。")

    def export_settings(self, defaults=None):
        defaults = defaults if isinstance(defaults, dict) else {}
        fmt = self.choice("导出 / 文件格式", [(1, "Markdown", ".md"), (2, "Word", ".docx"),
                                                   (3, "两种都导出", ".md + .docx")],
                          default={"md": 1, "docx": 2, "both": 3}.get(defaults.get("export_format"), 1))
        include = self.confirm("包含我的历史答案", default=defaults.get("include_my_answer") is True)
        saved_categories = defaults.get("category_ids")
        category_default = next((key for key, value in ((0, [1, 2, 3]), (1, [1]), (2, [2]), (3, [3])) if value == saved_categories), 3)
        category = self.choice("导出 / 题目范围", [(0, "全部", "作业、考试、测验"),
                                                        (1, "作业", ""), (2, "考试", ""),
                                                        (3, "章节测验", "")], default=category_default)
        mode = "per_chapter"
        if category in (0, 3):
            selected = self.choice("导出 / 测验组织方式", [(1, "手动选择", "选择具体章节测验"),
                                                              (2, "每章一个文件", "导出全部章节"),
                                                              (3, "合并一个文件", "按课程合并全部章节")],
                                   default={"single": 1, "per_chapter": 2, "merged": 3}.get(defaults.get("quiz_mode"), 2))
            mode = {1: "single", 2: "per_chapter", 3: "merged"}[selected]
        return {"export_format": {1: "md", 2: "docx", 3: "both"}[fmt],
                "include_my_answer": include, "category_ids": [1, 2, 3] if category == 0 else [category],
                "quiz_mode": mode}

    def quizzes(self, catalog):
        return self.select("选择章节测验", catalog, lambda x: x["paper_name"],
                           lambda x: x["course"], default="all")

    def categories(self):
        from AIMoocMain.export_questions import CATEGORY_FALLBACK
        return self.select("AI / 题目范围", [1, 2, 3], lambda x: CATEGORY_FALLBACK[x])

    def papers(self, catalog, selected=None, state=None, refresh=False):
        from AIMoocMain.export_questions import CATEGORY_FALLBACK, paper_display_name
        from task_metrics import score_text, attempts_text
        from task_capabilities import is_attachment_task, task_type_label, task_availability, task_time_hint
        filters = {"f0": ("全部", lambda x: True)}
        filters.update({f"f{category}": (CATEGORY_FALLBACK[category], lambda x, c=category: x[2] == c)
                        for category in (1, 2, 3)})
        statuses = {"submitted": "已提交", "draft": "未提交", "not_started": "未作答"}
        single_course = bool(catalog) and all(x[0] == catalog[0][0] for x in catalog)
        def unsupported(task):
            return is_attachment_task(task[1], task[2])
        unsupported_count = sum(unsupported(task) for task in catalog)
        if unsupported_count:
            self.note(f"附件作业 {unsupported_count} 项暂不支持，请在平台完成；全选会自动排除。", "warning")
        unavailable = Counter(task_availability(x[1], x[2]).label for x in catalog
                              if not unsupported(x) and not task_availability(x[1], x[2]).available)
        if unavailable:
            self.note(" · ".join(f"{label} {count} 项" for label, count in unavailable.items()) +
                      "；暂不可选，全选会排除。刷新可重新检查开放状态。", "warning")
        def availability(task):
            return task_availability(task[1], task[2])
        def hint(task):
            return availability(task).reason or task_time_hint(task[1])
        return self.select("选择任务", catalog,
                           lambda x: ("[附件作业] " if unsupported(x) else "") + paper_display_name(x[1]),
                           lambda x: task_type_label(x[1], x[2]) if single_course else
                           f"{x[0].get('courseName', '')} · {task_type_label(x[1], x[2])}",
                           filters=filters, selected=selected, state=state,
                           status=lambda x: availability(x).label or statuses.get(x[1].get("_completion_status"), "?"),
                           selectable=lambda x: availability(x).available, item_hint=hint,
                           extra_columns=(("成绩", lambda x: score_text(x[1])),
                                          ("剩余次数", lambda x: attempts_text(x[1], x[2]))),
                           actions={"r": "刷新状态"} if refresh else None)

    def answer_settings(self):
        mode = self.choice("AI / 执行方式", [(1, "仅生成答案", "不交卷，无平台成绩"),
                                                  (2, "答题并自动交卷", "全部题目通过校验后提交")], default=2)
        return mode == 2

    def concurrency_editor(self, current, *, message=''):
        from concurrency_ui import edit_concurrency
        if self.input_fn is None and self.console.is_terminal and sys.stdin.isatty():
            return edit_concurrency(current, message=message, output=self.terminal_output())
        draft = dict(current)
        try:
            while True:
                self.table('并发设置', ['项目', '当前值'],
                           [('AI 答题', draft['ai']), ('自动学习', draft['study'])])
                self.note(message or '范围 1–8 · 保存后下次运行生效 · d 恢复默认 · Esc 取消')
                ai = self.ask('AI 答题并发', draft['ai'])
                if ai.lower() == 'd':
                    draft = {'ai': 8, 'study': 8}
                    continue
                study = self.ask('自动学习并发', draft['study'])
                if not ai.isdigit() or not study.isdigit() or not 1 <= int(ai) <= 8 or not 1 <= int(study) <= 8:
                    message = '请输入 1–8 的整数。'
                    continue
                draft = {'ai': int(ai), 'study': int(study)}
                return draft if self.confirm('保存并发设置', default=True) else None
        except Back:
            return None

    def settings_menu(self, path, ai_status="已配置 · 未测试", *, message="", concurrency=None):
        concurrency = concurrency or {'ai': 8, 'study': 8}
        return self.choice("本机设置", [
            (1, "模型配置", "添加、编辑、切换 · " + ai_status),
            (2, "并发设置", f"AI 答题 {concurrency['ai']} · 自动学习 {concurrency['study']} · 可设 1–8"),
            (3, "退出 MOOC 登录", "下次浏览器登录先退出共享 SSO，可切换账号"),
            (4, "退出资源库登录", "下次浏览器登录先退出共享 SSO；MOOC 本机登录保留"),
            (0, "返回", "回到功能选择"),
        ], default=0, help_text=f"配置含凭证，请勿分享：{path}", message=message)

    def plan(self, name, courses, options=None):
        self.note(f"准备开始：{name} · {len(courses)} 门课程", "accent")
        if options:
            self.note("输出位置：tiku/<课程名>/；答案来自平台，不调用 AI。")

    def ai_plan(self, selected, auto_submit):
        self.note(f"准备开始：{len(selected)} 份任务 · " + ("通过校验后自动交卷" if auto_submit else "只生成本地报告"), "accent")
        self.note("题干和选项会发送给所选 AI 服务。打开试卷可能创建作答记录或开始计时。")
        self.note("报告位置：ai_results/<课程名>/；已交卷任务可再次作答，是否开放由平台决定。")


# Compatibility entry points for standalone scripts. All direct input lives here.
def legacy_input(prompt=""):
    ui = _active_ui.get()
    live = ui.live if ui else None
    if live:
        live.stop()
    try:
        from contextlib import nullcontext
        with ui.runtime_input.suspend() if ui else nullcontext():
            return (ui.input_fn if ui and ui.input_fn else input)(prompt)
    finally:
        if live:
            live.start()


def run_console_tool(operation, name, view=None):
    """Attach the shared renderer to older standalone diagnostic entry points."""
    view = view or TerminalUI()
    with view.session():
        try:
            with view.activity(name):
                view.on_progress(ProgressEvent("begin", name, total=1))
                result = operation()
                view.on_progress(ProgressEvent("result", name, status="error" if result is False else "success"))
                return result
        except (Cancelled, KeyboardInterrupt, EOFError):
            view.cancelled = True
        except Exception as exc:
            from MoocMain.log import Logger
            Logger(__name__).get_log().exception("工具运行失败")
            view.pending_error = str(exc)
        finally:
            view.finish()


def prompt_ai_config(input_fn=None, secret_fn=None, output=None):
    """Keep the former injectable configuration API for library callers/tests."""
    from ai.client import AIConfig
    read = input_fn or input
    secret = secret_fn or getpass.getpass
    emit = output or TerminalUI().note
    emit("AI 服务：1. 自定义服务（OpenAI 兼容）  2. DeepSeek")
    while True:
        provider = read("请选择 [1/2]（默认1）: ").strip() or "1"
        if provider in ("1", "2"):
            break
        emit("请输入 1 或 2")
    while True:
        if provider != "2":
            url = read("API Base URL（含服务所需的 /v1 等路径）: ").strip()
            model = read("Model ID: ").strip()
        else:
            url = "https://api.deepseek.com"
            emit("模型：1. deepseek-chat  2. deepseek-reasoner  3. 自定义模型 ID")
            choice = read("请选择 [1/2/3]（默认1）: ").strip() or "1"
            if choice not in ("1", "2", "3"):
                emit("请输入 1、2 或 3")
                continue
            model = {"1": "deepseek-chat", "2": "deepseek-reasoner"}.get(choice)
            if model is None:
                model = read("DeepSeek Model ID: ").strip()
        key = secret("API Key（隐藏输入，仅本次运行使用）: ")
        try:
            return AIConfig(url, model, key)
        except ValueError as exc:
            emit(str(exc))


def display_questions(questions, show_options=True):
    from NewMoocMain.icve_exam_parser import get_question_stats
    ui = TerminalUI()
    stats = get_question_stats(questions)
    ui.note(f"题目总数 {stats['total']} · 总分 {stats['total_score']}")
    for q in questions:
        ui.note(f"{q['number']}. {q['text']}（{q['score']} 分）")
        if q['type'] == '填空题':
            ui.note(f"空格数：{q['blank_count']}")
        elif show_options:
            for opt in q.get('options', []):
                ui.note(f"  {opt['label']}. {opt['text']}")


def parser_help():
    TerminalUI().note("职教云考试解析工具\nquestions = parse_exam_questions(html_content)\ndisplay_questions(questions)")


def oauth_diagnostic(login):
    run_console_tool(lambda: bool(login()), "测试浏览器登录")


def confirm_update():
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    try:
        return messagebox.askyesno("更新可用", "检测到新版本，是否下载？")
    finally:
        root.destroy()


def demo(snapshot=None):
    """Offline UI preview; no platform, browser login or AI requests."""
    from pathlib import Path
    console = Console(theme=theme.THEME, highlight=False, record=bool(snapshot),
                      width=100 if snapshot else None)
    answers = iter(["1", "2", "f3", "all", "", ""])

    def demo_input():
        value = next(answers)
        console.print(value)
        return value

    view = TerminalUI(console=console, input_fn=demo_input)
    with view.session():
        view.header("界面预览 · 模拟数据")
        courses = [{"id": "demo1", "courseName": "中外音乐与民族技艺实践", "studySpeed": 36},
                   {"id": "demo2", "courseName": "大学英语", "studySpeed": 82},
                   {"id": "demo3", "courseName": "计算机应用基础", "studySpeed": 100}]
        selected_courses = view.library(courses, 0, {})
        view.course_menu(selected_courses, 0)
        catalog = [(courses[0], {"id": str(i), "name": f"第 {i} 章测验"}, 3) for i in range(1, 13)]
        selected = view.papers(catalog)
        view.review_export(selected, {"export_format": "docx", "quiz_mode": "merged"})
        view.note("以下演示使用模拟任务，不写入题目文件。")
        view.begin_operation()

        def step(index):
            view.on_progress(ProgressEvent("detail", f"音乐课程 · 模拟任务 {index + 1}"))
            status = "error" if index == 10 else "skipped" if index == 8 else "success"
            view.on_progress(ProgressEvent("result", f"任务 {index + 1:02d}", status=status))

        if snapshot:
            view.on_progress(ProgressEvent("begin", "音乐课程 · 模拟任务", total=12))
            for index in range(9):
                step(index)
            view.log("长行截断演示：" + "课程资源名称与处理详情 " * 12)
            console.print(view.render())
            for index in range(9, 12):
                step(index)
        else:
            with view.activity("模拟运行"):
                view.on_progress(ProgressEvent("begin", "音乐课程 · 模拟任务", total=12))
                for index in range(12):
                    step(index)
                    view.log("处理详情：" + "课程资源名称 " * 20)
                    time.sleep(0.25)
        view.operation_result(["tiku/中外音乐与民族技艺实践/题目_所选题目_中外音乐与民族技艺实践.docx（示例）"])
    if snapshot:
        path = Path(snapshot)
        path.parent.mkdir(parents=True, exist_ok=True)
        console.save_html(str(path))
