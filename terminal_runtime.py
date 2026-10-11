"""Scoped ownership of the terminal screen and of raw runtime input.

Two small, reversible context managers live here:

* :class:`TerminalScreen` owns the alternate screen buffer for one page and can
  hand out a prompt_toolkit ``Output`` adapter that suppresses only the nested
  alternate-screen enter/quit codes, so a full-screen child form shares the
  parent buffer instead of leaving/restoring it underneath us.
* :class:`RuntimeInput` owns the POSIX terminal input mode (no echo, no
  canonical line buffering, ``ISIG`` preserved so Ctrl+C stays a real signal)
  while a runtime activity is drawing, and can be suspended so a manual prompt
  gets a normal echoing line editor with no leftover runtime keystrokes.

Both are no-ops on non-terminals and plain file objects. Windows uses console
input modes; POSIX uses termios. Both preserve system Ctrl+C handling.
"""
import os
import sys
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar

try:  # POSIX only; keep the module importable on Windows.
    import termios
except ImportError:  # pragma: no cover - Windows
    termios = None

from prompt_toolkit.output import Output


_screen_state = ContextVar("terminal_screen_state", default=None)


def _windows_console(fd):
    """Wrap the console handle without truncating it on 64-bit Windows."""
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
    kernel.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.FlushConsoleInputBuffer.argtypes = (wintypes.HANDLE,)

    class ConsoleInput:
        def mode(self):
            value = wintypes.DWORD()
            if not kernel.GetConsoleMode(handle, ctypes.byref(value)):
                raise ctypes.WinError(ctypes.get_last_error())
            return value.value

        def set_mode(self, value):
            if not kernel.SetConsoleMode(handle, value):
                raise ctypes.WinError(ctypes.get_last_error())

        def flush(self):
            if not kernel.FlushConsoleInputBuffer(handle):
                raise ctypes.WinError(ctypes.get_last_error())
    return ConsoleInput()


def _console_is_terminal(console):
    """Whether ``console`` writes to a real, ANSI-capable terminal."""
    if not getattr(console, "is_terminal", True):
        return False
    return not getattr(console, "legacy_windows", False)


class _ScopedOutput:
    """Delegates every ``Output`` method, hiding alt-screen calls while active.

    A fresh object is returned per request; suppression is evaluated when the
    method is called, so the same adapter keeps working after the owning screen
    exits (the real calls are then passed through).
    """

    def __init__(self, output, screen):
        self.output = output
        self._screen = screen

    def _suppresses(self):
        return self._screen.active

    def enter_alternate_screen(self):
        if not self._suppresses():
            self.output.enter_alternate_screen()

    def quit_alternate_screen(self):
        if not self._suppresses():
            self.output.quit_alternate_screen()

    def __getattr__(self, name):
        return getattr(self.output, name)


# prompt_toolkit only checks ``isinstance(output, Output)`` in convenience
# helpers; registering keeps the delegating adapter compatible there too.
Output.register(_ScopedOutput)


class TerminalScreen:
    """Own the alternate screen buffer for the duration of a ``with`` block."""

    def __init__(self, console, enabled=True):
        self.console = console
        self.enabled = enabled
        self.active = False
        self._owns = False
        self._state = None

    def __enter__(self):
        if not self.enabled:
            self.active = False
            return self
        state = _screen_state.get()
        if state is not None:
            # Nested screen: inherit the parent's buffer, never re-enter or
            # exit the alternate screen (the terminal cannot nest them).
            self._state = state
            self.active = bool(state["active"])
            return self
        entered = False
        if _console_is_terminal(self.console):
            try:
                entered = bool(self.console.set_alt_screen(True))
            except Exception:
                entered = False
        state = {"entered": entered, "active": entered}
        _screen_state.set(state)
        self._owns = True
        self._state = state
        self.active = entered
        return self

    def __exit__(self, exc_type, exc, tb):
        self.active = False
        if not self._owns:
            return False
        self._owns = False
        state, self._state = self._state, None
        _screen_state.set(None)
        if state and state.get("entered"):
            state["entered"] = False
            state["active"] = False
            try:
                self._show_cursor()
                self.console.set_alt_screen(False)
            except Exception:
                pass
        return False

    def page_start(self):
        """Home the cursor and clear the page without scrolling the buffer."""
        if not self.active:
            return
        try:
            if hasattr(self.console, "control"):
                from rich.control import Control
                self.console.control(Control.home(), Control.clear())
            else:
                self.console.clear(home=True)
            self._show_cursor()
        except Exception:
            pass

    def _show_cursor(self):
        if hasattr(self.console, "show_cursor"):
            self.console.show_cursor(True)

    def wrap_output(self, output):
        """Return an ``Output`` for a child full-screen form.

        While this screen is active the adapter drops only the alternate-screen
        enter/quit codes; all other calls (and, outside the active block, those
        two as well) are delegated to ``output`` unchanged.
        """
        return _ScopedOutput(output, self)

    # Explicit alias so callers may read ``screen.output(out)``.
    def output(self, output):
        return self.wrap_output(output)


def wrap_output(output, screen):
    """Module-level convenience wrapper around :meth:`TerminalScreen.wrap_output`."""
    return screen.wrap_output(output)


class RuntimeInput:
    """Own the terminal input mode while a runtime activity is drawing.

    ``suspend()`` is a nested context manager that temporarily restores the
    original terminal settings, flushes any runtime keystrokes that queued up,
    and re-applies the runtime mode on exit -- so a manual prompt gets ordinary
    echoing input without inheriting stray keys or a non-canonical line editor.
    """

    def __init__(self, input_stream=None, enabled=True):
        self.stream = input_stream
        self.enabled = enabled
        self._depth = 0
        self._suspends = 0
        self._owning = False
        self._mode = "off"  # off | raw | cooked
        self._saved = None
        self._raw = None
        self._windows = None

    # -- state -----------------------------------------------------------
    @property
    def active(self):
        """True while the runtime owns the terminal and is not suspended."""
        return self._owning and self._mode == "raw"

    # -- context manager -------------------------------------------------
    def __enter__(self):
        if self._depth == 0:
            self._owning = False
            self._mode = "off"
            self._saved = None
            self._raw = None
            self._windows = None
            if self.enabled:
                self._owning = self._setup()
        self._depth += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self._depth = max(0, self._depth - 1)
        if self._depth == 0:
            if self._owning:
                self._teardown()
            self._owning = False
            self._mode = "off"
            self._saved = None
            self._raw = None
        return False

    @contextmanager
    def suspend(self):
        """Temporarily hand the terminal back for a normal manual prompt."""
        if not self._owning:
            yield self
            return
        self._suspends += 1
        if self._suspends == 1:
            self._apply_cooked()
        try:
            yield self
        finally:
            self._suspends = max(0, self._suspends - 1)
            if self._suspends == 0 and self._owning and self._depth > 0:
                self._apply_raw()

    # -- terminal plumbing ----------------------------------------------
    def _fileno(self):
        stream = self.stream if self.stream is not None else getattr(sys, "stdin", None)
        if stream is None:
            return None
        try:
            return stream.fileno()
        except Exception:
            return None

    def _is_tty(self, fd):
        try:
            return bool(os.isatty(fd))
        except (OSError, ValueError):
            return False

    def _setup(self):
        fd = self._fileno()
        if fd is None or not self._is_tty(fd):
            return False
        if termios is None:
            try:
                self._windows = _windows_console(fd)
                self._saved = self._windows.mode()
                # Disable line input and echo; keep processed input (Ctrl+C).
                self._raw = (self._saved & ~0x6) | 0x1
                self._windows.set_mode(self._raw)
                self._mode = 'raw'
                return True
            except (ImportError, OSError, AttributeError):
                self._windows = None
                return False
        try:
            saved = termios.tcgetattr(fd)
        except (termios.error, OSError, ValueError):
            return False
        raw = deepcopy(saved)
        lflag = raw[3]
        lflag &= ~termios.ECHO
        lflag &= ~termios.ECHONL
        lflag &= ~termios.ICANON
        lflag |= termios.ISIG  # keep Ctrl+C a real signal
        raw[3] = lflag
        try:
            raw[6][termios.VMIN] = 1
            raw[6][termios.VTIME] = 0
        except (IndexError, KeyError, TypeError):
            pass
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, raw)
        except (termios.error, OSError, ValueError):
            return False
        self._saved = saved
        self._raw = raw
        self._mode = "raw"
        return True

    def _apply_raw(self):
        if self._raw is None:
            return
        if self._windows is not None:
            self._windows.set_mode(self._raw)
            self._mode = 'raw'
            return
        fd = self._fileno()
        if fd is None:
            return
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, self._raw)
            self._mode = "raw"
        except (termios.error, OSError, ValueError):
            pass

    def _apply_cooked(self):
        if self._windows is not None:
            try:
                self._windows.flush()
            finally:
                self._windows.set_mode(self._saved)
                self._mode = 'cooked'
            return
        fd = self._fileno()
        if self._saved is not None and fd is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, self._saved)
                self._mode = "cooked"
            except (termios.error, OSError, ValueError):
                pass
        self._flush()

    def _teardown(self):
        if self._mode != "off":
            self._apply_cooked()
        self._mode = "off"

    def _flush(self):
        if termios is None or self._saved is None:
            # Nothing is owned; flushing a borrowed stdin would be wrong.
            return
        fd = self._fileno()
        if fd is None:
            return
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except (termios.error, OSError, ValueError):
            pass
