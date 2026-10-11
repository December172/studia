"""End-to-end terminal ownership without accounts, network calls or pyte."""
import json
import os
import select
import subprocess
import sys
import time
import unittest

if os.name == 'posix':
    import fcntl
    import pty
    import struct
    import termios


@unittest.skipUnless(os.name == 'posix', 'requires a POSIX controlling terminal')
class TerminalLifecycleTests(unittest.TestCase):
    def launch(self, body, rows=30, columns=120):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
        report, writer = os.pipe()
        gate, release = os.pipe()
        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        code = ('import os,json,time\nfrom ui import TerminalUI\n'
                f'def report(value): os.write({writer}, (json.dumps(value)+"\\n").encode())\n'
                f'def wait(): os.read({gate},1)\n'
                'u=TerminalUI()\n' + body + '\nimport termios\nreport({"restored_echo": bool(termios.tcgetattr(0)[3] & termios.ECHO)})\n')
        child = subprocess.Popen([sys.executable, '-c', code], stdin=slave, stdout=slave, stderr=slave,
                                 pass_fds=(writer, gate), preexec_fn=controlling_terminal,
                                 env={**os.environ, 'TERM': 'xterm-256color'})
        os.close(writer)
        os.close(gate)
        self.master, self.slave, self.report, self.release, self.child = master, slave, report, release, child
        self.raw, self.messages, self.pending = bytearray(), [], b''
        def close():
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            for fd in (master, slave, report, release):
                os.close(fd)
        self.addCleanup(close)

    def drain(self, duration=.05):
        until = time.monotonic() + duration
        while time.monotonic() < until:
            for fd in select.select([self.master, self.report], [], [], .01)[0]:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    continue
                if fd == self.master:
                    self.raw.extend(data)
                    if b'\x1b[6n' in data:
                        os.write(self.master, b'\x1b[1;1R')
                else:
                    self.pending += data
                    while b'\n' in self.pending:
                        line, self.pending = self.pending.split(b'\n', 1)
                        self.messages.append(json.loads(line))

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.drain()
            if predicate():
                return
        self.fail('terminal timeout: ' + repr(self.messages) + repr(bytes(self.raw[-300:])))

    def test_nested_forms_enter_spam_resize_and_later_confirmation(self):
        self.launch('''with u.session():
 report('model')
 u.model_picker([('a','Synthetic','model','?')], 'a')
 with u.activity('synthetic'):
  report('running')
  wait()
 u.table('After', ['State'], [['Waiting']])
 report('confirm')
 value=u.ask('CONFIRM', default='yes')
 report(value)
report('done')
''')
        self.until(lambda: b'model' in self.raw)
        os.write(self.master, b'\x1b')
        self.until(lambda: 'running' in self.messages)
        self.assertFalse(termios.tcgetattr(self.slave)[3] & termios.ECHO)
        self.assertTrue(termios.tcgetattr(self.slave)[3] & termios.ISIG)
        os.write(self.master, b'\r' * 12)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', 16, 60, 0, 0))
        self.drain(.1)
        os.write(self.release, b'x')
        self.until(lambda: b'CONFIRM' in self.raw)
        self.drain(.15)
        self.assertNotIn('yes', self.messages)
        self.assertNotIn('done', self.messages)
        os.write(self.master, b'no\r')
        self.until(lambda: 'done' in self.messages)
        self.child.wait(timeout=5)
        self.drain()
        self.assertIn('no', self.messages)
        self.until(lambda: {'restored_echo': True} in self.messages)
        self.assertEqual(self.raw.count(b'\x1b[?1049h'), 1)
        self.assertEqual(self.raw.count(b'\x1b[?1049l'), 1)

    def test_native_workflow_menu_to_results(self):
        """The real adapter path: menu → course → confirm → study → AI → export → results."""
        self.launch('''from ai.client import AIConfig
rows=[{'id':'a','courseName':'bqt演示课程'}, {'id':'b','courseName':'另一课程'}]
with u.session():
 report('menu')
 report({'platform':u.choose_platform(0)})
 report('course')
 report({'course':u.library(rows,0,{})[0]['id']})
 report('confirm')
 report({'confirm':u.confirm('确认操作',False)})
 report('study')
 u.review_study([rows[0]])
 report('ai')
 selected=[(rows[0],{'id':'p','name':'演示试卷'},1)]
 config=AIConfig('https://example.org/v1','synthetic-model','synthetic-key')
 report({'submit':u.review_ai(selected,config,{})})
 report('export')
 report({'format':u.review_export(selected,{})['export_format']})
 report('results')
 report({'navigation':u.operation_result()})
report('done')
''')
        self.until(lambda: 'menu' in self.messages and '切换平台'.encode() in self.raw)
        os.write(self.master, b'\r')
        self.until(lambda: 'course' in self.messages and '我的课程'.encode() in self.raw)
        self.assertIn({'platform': 0}, self.messages)
        # A narrow terminal still keeps every action reachable.
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', 20, 52, 0, 0))
        os.write(self.master, b'/bqt\r')
        self.drain(.25)
        os.write(self.master, b'\r')
        self.until(lambda: 'confirm' in self.messages and '确认操作'.encode() in self.raw)
        self.assertIn({'course': 'a'}, self.messages)
        os.write(self.master, b'\r')
        self.until(lambda: 'study' in self.messages and '自动学习'.encode() in self.raw)
        self.assertIn({'confirm': False}, self.messages)
        os.write(self.master, b'\r')
        self.until(lambda: 'ai' in self.messages and 'synthetic-model'.encode() in self.raw)
        os.write(self.master, b'\r')
        self.until(lambda: 'export' in self.messages and '导出'.encode() in self.raw)
        self.assertIn({'submit': True}, self.messages)
        os.write(self.master, b'\r')
        self.until(lambda: 'results' in self.messages and '本次没有处理任务'.encode() in self.raw)
        self.assertIn({'format': 'md'}, self.messages)
        os.write(self.master, b'\x1b')
        self.until(lambda: 'done' in self.messages)
        self.assertIn({'navigation': 1}, self.messages)
        self.until(lambda: {'restored_echo': True} in self.messages)
        self.assertEqual(self.raw.count(b'\x1b[?1049h'), 1)
        self.assertEqual(self.raw.count(b'\x1b[?1049l'), 1)
        self.assertNotIn(b'Window too small', self.raw)

    def test_ctrl_c_still_interrupts_and_restores_terminal(self):
        self.launch('''try:
 with u.session():
  with u.activity('synthetic'):
   report('running')
   wait()
except KeyboardInterrupt:
 report('interrupted')
report('done')
''', rows=16, columns=60)
        self.until(lambda: 'running' in self.messages)
        os.write(self.master, b'\x03')
        self.until(lambda: 'done' in self.messages)
        self.assertIn('interrupted', self.messages)
        self.until(lambda: {'restored_echo': True} in self.messages)

    def test_runtime_input_handoff_returns_to_runtime_mode(self):
        self.launch('''from ui import legacy_input
with u.session():
 with u.activity('synthetic'):
  report('running')
  wait()
  answer=legacy_input('输入选项 ')
  report(answer)
  report('resumed')
  wait()
report('done')
''')
        self.until(lambda: 'running' in self.messages)
        os.write(self.master, b'\r' * 5)
        os.write(self.release, b'x')
        self.until(lambda: '输入选项'.encode() in self.raw)
        # The suspended prompt accepts ordinary echoing input.
        self.assertTrue(termios.tcgetattr(self.slave)[3] & termios.ECHO)
        os.write(self.master, b'B\r')
        self.until(lambda: 'resumed' in self.messages)
        self.assertIn('B', self.messages)
        self.assertFalse(termios.tcgetattr(self.slave)[3] & termios.ECHO)
        os.write(self.release, b'x')
        self.until(lambda: 'done' in self.messages)
        self.until(lambda: {'restored_echo': True} in self.messages)
