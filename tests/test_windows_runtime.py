"""Win32 input-mode boundary tests, runnable without a Windows host."""
import unittest
from unittest.mock import patch
import terminal_runtime


class Console:
    def __init__(self):
        self.value = 0x207
        self.queued = []
    def mode(self):
        return self.value
    def set_mode(self, value):
        self.value = value
    def flush(self):
        self.queued.clear()


class Stream:
    def fileno(self):
        return 7


class WindowsRuntimeTests(unittest.TestCase):
    def test_runtime_and_manual_handoff_restore_exact_console_mode(self):
        console = Console()
        with patch.object(terminal_runtime, 'termios', None), \
             patch.object(terminal_runtime.os, 'isatty', return_value=True), \
             patch.object(terminal_runtime, '_windows_console', return_value=console, create=True):
            guard = terminal_runtime.RuntimeInput(Stream())
            with guard:
                self.assertTrue(guard.active)
                self.assertEqual(console.value & 6, 0)
                self.assertEqual(console.value & 1, 1)
                console.queued.append('\r')
                with guard.suspend():
                    self.assertEqual(console.value, 0x207)
                    self.assertFalse(console.queued)
                self.assertEqual(console.value & 6, 0)
                console.queued.append('\r')
            self.assertEqual(console.value, 0x207)
            self.assertFalse(console.queued)

    def test_keyboard_interrupt_restores_windows_input_mode(self):
        console = Console()
        with patch.object(terminal_runtime, 'termios', None), \
             patch.object(terminal_runtime.os, 'isatty', return_value=True), \
             patch.object(terminal_runtime, '_windows_console', return_value=console, create=True):
            with self.assertRaises(KeyboardInterrupt):
                with terminal_runtime.RuntimeInput(Stream()) as guard:
                    self.assertTrue(guard.active)
                    raise KeyboardInterrupt()
        self.assertEqual(console.value, 0x207)
