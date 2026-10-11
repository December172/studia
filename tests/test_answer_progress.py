"""Question progress observes real work without changing paper result counts."""
import io
import tempfile
import unittest
from unittest.mock import Mock, patch

from rich.console import Console
import theme

from ai.client import AIResponseError
from ai.runner import AnswerRunner
from reporting import ProgressEvent
from tests.test_ai import FakeClock, question
from ui import TerminalUI


class AnswerProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = Mock(user_id='user')
        self.api.records.return_value = []
        self.api.verify_submitted.return_value = True
        self.api.paper.return_value = {
            'questions': [question(qid='one'), question(qid='two')],
            'taskExamRecord': {'id': 'task', 'status': 0}}
        self.good = dict(answer='A', confidence=.95, explanation='ok')
        self.ai = Mock()
        self.ai.config.model = 'test'
        self.ai.answer.return_value = self.good
        self.events = []

    def run_paper(self):
        runner = AnswerRunner(self.api, self.ai, self.temp.name, auto_submit=True,
                              clock_factory=FakeClock, output=Mock(),
                              on_progress=self.events.append)
        with patch('ai.runner.time.sleep'):
            return runner.run_paper({'id': 'ci', 'courseId': 'c'}, {'id': 'exam'}, 3)

    def test_questions_advance_after_answers_and_submission_has_separate_stages(self):
        snapshots = []
        def answer(_):
            snapshots.append(self.events[-1])
            return self.good
        self.ai.answer.side_effect = answer
        self.assertEqual(self.run_paper()['status'], 'submitted')
        self.assertEqual([(e.kind, e.completed, e.total) for e in snapshots],
                         [('questions', 0, 2), ('questions', 1, 2)])
        stages = [e.name for e in self.events if e.kind == 'questions']
        self.assertIn('正在交卷', stages)
        self.assertIn('确认交卷结果', stages)
        self.assertEqual(stages[-1], '已交卷')
        self.assertEqual(self.events[-1].completed, 2)

    def test_retry_exhaustion_does_not_advance_unfinished_question(self):
        self.ai.answer.side_effect = [self.good] + [AIResponseError('bad')] * 3
        self.assertEqual(self.run_paper()['status'], 'needs_review')
        events = [e for e in self.events if e.kind == 'questions']
        self.assertTrue(any('重试 1/2' in e.name and e.completed == 1 for e in events))
        self.assertEqual(events[-1].completed, 1)
        self.assertIn('答案未完成', events[-1].name)

    def test_compound_questions_count_each_answerable_child(self):
        self.api.paper.return_value['questions'] = [
            {'id': 'parent', 'typeId': 6, 'title': '材料',
             'questionSubList': [question(qid='a'), question(qid='b')]}, question(qid='c')]
        self.assertEqual(self.run_paper()['status'], 'submitted')
        events = [e for e in self.events if e.kind == 'questions' and e.total is not None]
        self.assertTrue(events)
        self.assertTrue(all(e.total == 3 for e in events))
        self.assertEqual(events[-1].completed, 3)

    def test_question_bar_is_independent_and_cleared_for_next_operation(self):
        output = io.StringIO()
        view = TerminalUI(console=Console(file=output, width=100, theme=theme.THEME))
        view.on_progress(ProgressEvent('begin', 'AI 答题', total=2))
        view.on_progress(ProgressEvent('questions', '第 2 题 · AI 生成', total=4, completed=1))
        view.console.print(view.render())
        self.assertIn('25%', output.getvalue())
        self.assertIn('1/4', output.getvalue())
        self.assertEqual(view.progress.tasks[0].completed, 0)
        self.assertEqual(view.counts, {})
        output.seek(0)
        output.truncate()
        view.begin_operation()
        view.console.print(view.render())
        self.assertNotIn('1/4', output.getvalue())

    def test_next_paper_loading_does_not_retain_previous_question_total(self):
        output = io.StringIO()
        view = TerminalUI(console=Console(file=output, width=100, theme=theme.THEME))
        view.on_progress(ProgressEvent('questions', '已交卷', total=10, completed=10))
        view.on_progress(ProgressEvent('questions', '读取试卷', completed=0))
        view.console.print(view.render())
        self.assertIn('0/?', output.getvalue())
        self.assertNotIn('0/10', output.getvalue())
