"""Platform list fields are scores/remaining attempts, not history counts."""
import io
import unittest
from unittest.mock import Mock, patch
from rich.console import Console
import theme
from ui import Back, TerminalUI


class TaskMetricsTests(unittest.TestCase):
    def render(self, item, native=False, category=1, status='submitted'):
        row = ({'id': 'c', 'courseName': '课程'}, dict(id='a', name='作业一', _completion_status=status, **item), category)
        out = io.StringIO()
        answers = iter(['1', '\x1b'])  # pick the paper; Esc if it cannot be picked (no attempts left)
        ui = TerminalUI(console=Console(file=out, width=120, height=30, theme=theme.THEME,
                                        force_terminal=native), input_fn=None if native else lambda: next(answers))
        if native:
            from checklist import Selection
            with patch('sys.stdin.isatty', return_value=True), \
                 patch('checklist.choose_many', return_value=Selection([0], 1)) as choose:
                ui.papers([row])
            return choose.call_args.args[1][0][3:]
        try:
            ui.papers([row])
        except Back:
            pass
        return out.getvalue()

    def metrics(self, text):
        return next(line for line in text.splitlines() if '作业一' in line).split()[4:6]

    def test_score_zero_is_a_score_and_answer_count_is_already_remaining(self):
        text = self.render({'score': 0, 'frequency': 5, 'answerCount': 2})
        self.assertEqual(self.metrics(text), ['0', '2'])

    def test_ungraded_unlimited_and_exhausted_are_distinct(self):
        text = self.render({'score': None, 'frequency': '0', 'answerCount': 0})
        self.assertEqual(self.metrics(text), ['--', '不限'])
        text = self.render({'score': '92.5', 'frequency': '3', 'answerCount': '0'})
        self.assertEqual(self.metrics(text), ['92.5', 'x'])

    def test_missing_and_invalid_fields_are_unknown_not_zero(self):
        for item in ({'score': 'NaN', 'frequency': 3, 'answerCount': -2},
                     {'score': False, 'frequency': 3, 'answerCount': 7}):
            text = self.render(item)
            self.assertEqual(self.metrics(text), ['?', '?'])

    def test_real_homework_negative_one_means_no_retry(self):
        for native in (False, True):
            text = self.render({'score': 100, 'frequency': '4', 'answerCount': -1}, native=native)
            self.assertEqual(tuple(text if native else self.metrics(text)), ('100', 'x'))

    def test_unavailable_data_for_other_tasks_remains_unknown(self):
        for category, status in ((1, 'submitted'), (1, 'unknown'), (1, 'not_started'), (2, 'submitted'), (3, 'submitted')):
            text = self.render({}, category=category, status=status, native=True)
            self.assertEqual(text, ('?', '?'))

    def test_keyboard_selector_receives_metrics_too(self):
        text = self.render({'score': 85, 'frequency': 4, 'answerCount': 1}, native=True)
        self.assertEqual(text, ('85', '1'))


    def test_a_graded_mooc_paper_counts_as_submitted_whatever_its_record_code(self):
        from task_metrics import completion_with_score
        # 第二次测试: 91 points on the list, but its answer record code was neither 2 nor 0.
        self.assertEqual(completion_with_score({'score': 91}, 'unknown'), 'submitted')
        self.assertEqual(completion_with_score({'score': None, 'status': 1}, 'not_started'), 'submitted')  # 待批
        self.assertEqual(completion_with_score({'score': None}, 'not_started'), 'not_started')
        self.assertEqual(completion_with_score({'score': None}, 'unknown'), 'unknown')
        self.assertEqual(completion_with_score({'score': 60}, 'draft'), 'draft')


if __name__ == '__main__':
    unittest.main()
