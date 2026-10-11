"""Focused tests for scoped terminal ownership (screen + input mode).

POSIX-only tests drive a real ``pty.openpty`` pair so termios flags and pending
input are observed for real; they are skipped on platforms without pty support.
No child processes are spawned, so the pty master never blocks.
"""
import io
import os
import select
import time
import unittest
from unittest.mock import patch

try:
    import termios
except ImportError:  # pragma: no cover - Windows
    termios = None

if os.name == "posix":
    import pty
else:  # pragma: no cover - Windows
    pty = None

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from rich.console import Console

from terminal_runtime import RuntimeInput, TerminalScreen, wrap_output


POSIX_PTY = os.name == "posix" and pty is not None and termios is not None


class FakeConsole:
    """Records the control operations a real rich Console would perform."""

    def __init__(self, terminal=True):
        self.is_terminal = terminal
        self.legacy_windows = False
        self.is_alt_screen = False
        self.ops = []

    def set_alt_screen(self, enable=True):
        if not self.is_terminal or self.legacy_windows:
            return False
        self.ops.append("alt:on" if enable else "alt:off")
        self.is_alt_screen = enable
        return True

    def control(self, *controls):
        for control in controls:
            self.ops.append(control.segment.text)

    def clear(self, home=True):
        self.ops.append("clear:home" if home else "clear")

    def show_cursor(self, show=True):
        self.ops.append("cursor:%s" % show)


class FdStream:
    """Minimal stream exposing only a file descriptor."""

    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd


def vt100_output():
    stream = io.StringIO()
    output = Vt100_Output(stream, lambda: Size(rows=30, columns=120), term="xterm-256color",
                          default_color_depth=ColorDepth.DEPTH_8_BIT, enable_cpr=False)
    return output, stream


def run_form(output):
    """Run a full-screen prompt_toolkit form once, exiting on ``q``."""
    bindings = KeyBindings()

    @bindings.add("q")
    def _quit(event):
        event.app.exit(result="done")

    with create_pipe_input() as pipe:
        pipe.send_text("q")
        app = Application(layout=Layout(Window(FormattedTextControl("terminal-runtime"))),
                          key_bindings=bindings, full_screen=True, input=pipe, output=output)
        return app.run()


def readable(fd, timeout=0.2):
    try:
        return bool(select.select([fd], [], [], timeout)[0])
    except (OSError, ValueError):
        return False


def wait_for_state(fd, pending, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if readable(fd, 0.02) == pending:
            return True
        time.sleep(0.01)
    return False


class TerminalScreenTests(unittest.TestCase):
    def test_enter_and_restore_alternate_screen_exactly_once(self):
        console = FakeConsole()
        with TerminalScreen(console) as screen:
            self.assertTrue(screen.active)
            screen.page_start()
        self.assertFalse(screen.active)
        self.assertFalse(console.is_alt_screen)
        self.assertEqual(console.ops.count("alt:on"), 1)
        self.assertEqual(console.ops.count("alt:off"), 1)
        self.assertEqual(console.ops[0], "alt:on")
        self.assertIn("\x1b[H", console.ops)
        self.assertIn("\x1b[2J", console.ops)
        self.assertIn("cursor:True", console.ops)

    def test_page_start_does_not_print_a_trailing_newline(self):
        stream = io.StringIO()
        console = Console(file=stream, force_terminal=True, width=80, color_system=None,
                          legacy_windows=False)
        console._environ = {}
        with TerminalScreen(console) as screen:
            self.assertTrue(screen.active)
            screen.page_start()
        stream.flush()
        text = stream.getvalue()
        self.assertNotIn("\n", text)
        self.assertEqual(text.count("\x1b[?1049h"), 1)
        self.assertEqual(text.count("\x1b[?1049l"), 1)
        self.assertIn("\x1b[2J", text)

    def test_nested_screen_never_exits_the_parent_buffer(self):
        console = FakeConsole()
        with TerminalScreen(console) as outer:
            with TerminalScreen(console) as inner:
                self.assertTrue(inner.active)
                self.assertEqual(console.ops.count("alt:on"), 1)
                self.assertEqual(console.ops.count("alt:off"), 0)
            self.assertTrue(console.is_alt_screen)
            self.assertTrue(outer.active)
            self.assertEqual(console.ops.count("alt:off"), 0)
        self.assertFalse(console.is_alt_screen)
        self.assertEqual(console.ops.count("alt:on"), 1)
        self.assertEqual(console.ops.count("alt:off"), 1)

    def test_non_terminal_console_is_a_no_op(self):
        console = FakeConsole(terminal=False)
        with TerminalScreen(console) as screen:
            self.assertFalse(screen.active)
            screen.page_start()
        self.assertEqual(console.ops, [])

    def test_disabled_screen_leaves_the_terminal_untouched(self):
        console = FakeConsole()
        with TerminalScreen(console, enabled=False) as screen:
            self.assertFalse(screen.active)
            screen.page_start()
        self.assertEqual(console.ops, [])
        self.assertFalse(console.is_alt_screen)

    def test_non_tty_rich_console_writes_nothing(self):
        stream = io.StringIO()
        console = Console(file=stream, force_terminal=False)
        with TerminalScreen(console) as screen:
            self.assertFalse(screen.active)
            screen.page_start()
        stream.flush()
        self.assertEqual(stream.getvalue(), "")

    def test_exception_still_restores_the_alternate_screen(self):
        console = FakeConsole()
        with self.assertRaises(ValueError):
            with TerminalScreen(console):
                raise ValueError("boom")
        self.assertFalse(console.is_alt_screen)
        self.assertEqual(console.ops.count("alt:off"), 1)


class RuntimeOutputAdapterTests(unittest.TestCase):
    def test_nested_full_screen_forms_keep_the_parent_buffer(self):
        output, stream = vt100_output()
        screen = TerminalScreen(FakeConsole())
        with screen:
            adapter = screen.wrap_output(output)
            self.assertEqual(run_form(adapter), "done")
            self.assertEqual(run_form(adapter), "done")
            self.assertEqual(stream.getvalue().count("\x1b[?1049h"), 0)
            self.assertEqual(stream.getvalue().count("\x1b[?1049l"), 0)
        # Outside the active screen the same adapter delegates the real calls.
        self.assertEqual(run_form(adapter), "done")
        self.assertEqual(stream.getvalue().count("\x1b[?1049h"), 1)
        self.assertEqual(stream.getvalue().count("\x1b[?1049l"), 1)

    def test_adapter_delegates_every_other_method(self):
        output, _ = vt100_output()
        screen = TerminalScreen(FakeConsole())
        with screen:
            adapter = screen.wrap_output(output)
            self.assertEqual(adapter.get_size(), output.get_size())
            self.assertIs(adapter.stdout, output.stdout)
            self.assertEqual(adapter.encoding, output.encoding)
            adapter.set_title("runtime")
            adapter.hide_cursor()
            adapter.flush()

    def test_module_wrap_output_binds_the_screen(self):
        output, stream = vt100_output()
        screen = TerminalScreen(FakeConsole())
        with screen:
            adapter = wrap_output(output, screen)
            adapter.enter_alternate_screen()
            adapter.quit_alternate_screen()
            adapter.flush()
            self.assertNotIn("\x1b[?1049", stream.getvalue())
        adapter.enter_alternate_screen()
        adapter.flush()
        self.assertIn("\x1b[?1049h", stream.getvalue())


@unittest.skipUnless(POSIX_PTY, "requires POSIX pty support")
class RuntimeInputPTYTests(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.saved = termios.tcgetattr(self.slave)
        self.addCleanup(self._close)

    def _close(self):
        try:
            termios.tcsetattr(self.slave, termios.TCSADRAIN, self.saved)
        except Exception:
            pass
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def stream(self):
        return FdStream(self.slave)

    def attrs(self):
        return termios.tcgetattr(self.slave)

    def drain_master(self):
        while readable(self.master, 0.02):
            try:
                os.read(self.master, 4096)
            except OSError:
                return

    def read_master(self, timeout=0.3):
        if not readable(self.master, timeout):
            return None
        return os.read(self.master, 4096)

    def test_echo_harness_detects_echo_when_the_terminal_enables_it(self):
        self.drain_master()
        os.write(self.master, b"ab")
        self.assertEqual(self.read_master(0.5), b"ab")
        termios.tcflush(self.slave, termios.TCIFLUSH)

    def test_enter_disables_echo_and_canonical_but_keeps_isig(self):
        before = self.attrs()
        self.assertTrue(before[3] & termios.ECHO)
        self.assertTrue(before[3] & termios.ICANON)
        with RuntimeInput(self.stream()) as runtime:
            self.assertTrue(runtime.active)
            inside = self.attrs()
            self.assertFalse(inside[3] & termios.ECHO)
            self.assertFalse(inside[3] & termios.ICANON)
            self.assertTrue(inside[3] & termios.ISIG)
        self.assertEqual(self.attrs(), before)
        self.assertFalse(runtime.active)

    def test_runtime_keystrokes_are_not_echoed(self):
        runtime = RuntimeInput(self.stream())
        with runtime:
            self.drain_master()
            os.write(self.master, b"ab")
            self.assertIsNone(self.read_master(0.2))
            self.assertTrue(wait_for_state(self.slave, True))
            termios.tcflush(self.slave, termios.TCIFLUSH)

    def test_buffered_runtime_key_is_discarded_on_suspend_and_exit(self):
        runtime = RuntimeInput(self.stream())
        with runtime:
            os.write(self.master, b"\r")
            self.assertTrue(wait_for_state(self.slave, True))
            with runtime.suspend():
                self.assertTrue(wait_for_state(self.slave, False))
                restored = self.attrs()
                self.assertTrue(restored[3] & termios.ECHO)
                self.assertTrue(restored[3] & termios.ICANON)
                self.assertTrue(restored[3] & termios.ISIG)
            resumed = self.attrs()
            self.assertFalse(resumed[3] & termios.ECHO)
            self.assertFalse(resumed[3] & termios.ICANON)
            os.write(self.master, b"\r")
            self.assertTrue(wait_for_state(self.slave, True))
        self.assertEqual(self.attrs(), self.saved)
        self.assertTrue(wait_for_state(self.slave, False))

    def test_nested_suspend_restores_after_an_exception(self):
        runtime = RuntimeInput(self.stream())
        with runtime:
            with self.assertRaises(ValueError):
                with runtime.suspend():
                    with runtime.suspend():
                        raise ValueError("boom")
            self.assertFalse(self.attrs()[3] & termios.ECHO)
            self.assertFalse(self.attrs()[3] & termios.ICANON)
        self.assertEqual(self.attrs(), self.saved)

    def test_nested_runtime_contexts_restore_once(self):
        runtime = RuntimeInput(self.stream())
        with runtime:
            with runtime:
                self.assertFalse(self.attrs()[3] & termios.ECHO)
            self.assertFalse(self.attrs()[3] & termios.ECHO)
        self.assertEqual(self.attrs(), self.saved)

    def test_runtime_exception_still_restores_the_terminal(self):
        runtime = RuntimeInput(self.stream())
        with self.assertRaises(KeyboardInterrupt):
            with runtime:
                raise KeyboardInterrupt
        self.assertEqual(self.attrs(), self.saved)

    def test_disabled_runtime_changes_nothing(self):
        runtime = RuntimeInput(self.stream(), enabled=False)
        with runtime:
            self.assertFalse(runtime.active)
            self.assertEqual(self.attrs(), self.saved)
        self.assertEqual(self.attrs(), self.saved)

    def test_setup_failure_does_not_leave_the_terminal_modified(self):
        import terminal_runtime
        with patch.object(terminal_runtime.termios, "tcsetattr", side_effect=termios.error("nope")):
            runtime = RuntimeInput(self.stream())
            with runtime:
                self.assertFalse(runtime.active)
                self.assertEqual(self.attrs(), self.saved)
        self.assertEqual(self.attrs(), self.saved)


class RuntimeInputFallbackTests(unittest.TestCase):
    def test_stringio_is_a_no_op(self):
        runtime = RuntimeInput(io.StringIO())
        with runtime:
            self.assertFalse(runtime.active)

    def test_object_without_fileno_is_a_no_op(self):
        runtime = RuntimeInput(object())
        with runtime:
            self.assertFalse(runtime.active)

    def test_failing_fileno_is_a_no_op(self):
        class Exploding:
            def fileno(self):
                raise OSError("no descriptor")

        runtime = RuntimeInput(Exploding())
        with runtime:
            self.assertFalse(runtime.active)

    def test_pipe_descriptor_is_a_no_op(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        self.addCleanup(os.close, read_fd)
        runtime = RuntimeInput(FdStream(read_fd))
        with runtime:
            self.assertFalse(runtime.active)

    def test_suspend_without_an_active_runtime_is_a_no_op(self):
        runtime = RuntimeInput(io.StringIO())
        with runtime.suspend():
            pass
        with runtime:
            with runtime.suspend():
                self.assertFalse(runtime.active)

    def test_default_stream_falls_back_when_stdin_is_unusable(self):
        runtime = RuntimeInput(enabled=False)
        with runtime:
            self.assertFalse(runtime.active)


if __name__ == "__main__":
    unittest.main()
