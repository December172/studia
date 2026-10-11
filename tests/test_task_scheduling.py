"""Serial fallback publishes results promptly without artificial scheduling delays."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from AIMoocMain.exam_api import ExamError
from ai.runner import AnswerRunner
from base.study import run_resources
from reporting import Reporter
from tests.test_ai import FakeClock, question


class TaskSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = Mock(user_id='user')
        self.api.records.return_value = []
        self.api.paper.return_value = {
            'questions': [question(qid=str(i)) for i in range(3)],
            'taskExamRecord': {'id': 'task', 'status': 0},
        }
        self.ai = Mock()
        self.ai.config.model = 'test-model'
        self.ai.answer.return_value = {'answer': 'A', 'confidence': .95, 'explanation': 'ok'}
        self.events = []

    def run_paper(self, **kwargs):
        runner = AnswerRunner(self.api, self.ai, self.temp.name,
                              on_progress=self.events.append, **kwargs)
        return runner.run_paper({'id': 'ci', 'courseId': 'c'}, {'id': 'exam'}, 3)

    def test_each_answer_is_saved_and_reported_before_next_request_without_sleep(self):
        observations = []
        def answer(_):
            report = json.loads(next(Path(self.temp.name).rglob('*.json')).read_text())
            completed = [e.completed for e in self.events if e.kind == 'questions'][-1]
            observations.append((len(report['answers']), completed))
            return self.ai.answer.return_value
        self.ai.answer.side_effect = answer
        with patch('ai.runner.time.sleep') as sleep:
            result = self.run_paper(clock_factory=FakeClock)
        self.assertEqual(result['status'], 'preview')
        self.assertEqual(observations, [(0, 0), (1, 1), (2, 2)])
        sleep.assert_not_called()

    def test_single_question_has_no_inter_task_pause(self):
        self.api.paper.return_value['questions'] = [question()]
        with patch('ai.runner.time.sleep') as sleep:
            self.assertEqual(self.run_paper(clock_factory=FakeClock)['status'], 'preview')
        sleep.assert_not_called()

    def test_low_confidence_answer_is_used_without_artificial_pause(self):
        self.ai.answer.side_effect = [
            {'answer': 'A', 'confidence': .1, 'explanation': 'unsure'},
            self.ai.answer.return_value, self.ai.answer.return_value]
        with patch('ai.runner.time.sleep') as sleep:
            result = self.run_paper(auto_submit=True, clock_factory=FakeClock)
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual([row['answer'] for row in result['answers']], ['A', 'A', 'A'])
        self.assertEqual(self.api.submit.call_count, 1)
        sleep.assert_not_called()

    def test_interrupt_during_next_request_preserves_saved_answer(self):
        self.ai.answer.side_effect = [self.ai.answer.return_value, KeyboardInterrupt]
        with self.assertRaises(KeyboardInterrupt):
            self.run_paper(clock_factory=FakeClock)
        self.assertEqual(self.ai.answer.call_count, 2)
        report = json.loads(next(Path(self.temp.name).rglob('*.json')).read_text())
        self.assertEqual(len(report['answers']), 1)
        self.assertFalse(list(Path(self.temp.name).rglob('*.lock')))
        self.api.submit.assert_not_called()

    def test_deadline_stops_next_request_and_submission(self):
        clock = FakeClock()
        def answer(_):
            clock.check = Mock(side_effect=ExamError('时间已用尽'))
            return self.ai.answer.return_value
        self.ai.answer.side_effect = answer
        result = self.run_paper(clock_factory=lambda *args: clock, auto_submit=True)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.ai.answer.call_count, 1)
        self.api.submit.assert_not_called()

    def study_handler(self, cancel):
        handled = []
        handler = SimpleNamespace(
            max_workers=1, cancel_event=cancel, _course_failed=False,
            _course_processed=False, progress=Reporter(self.events.append),
            handle_resource=lambda node: handled.append(node['id']) or 'success')
        return handler, handled

    def test_resources_report_each_completion_without_artificial_pause(self):
        cancel = threading.Event()
        handler, handled = self.study_handler(cancel)
        with patch.object(cancel, 'wait') as wait:
            run_resources(handler, [({'id': str(i)},) for i in range(3)])
        self.assertEqual(handled, ['0', '1', '2'])
        self.assertEqual([e.completed for e in self.events], [0, 1, 2, 3])
        wait.assert_not_called()

    def test_cancel_after_completion_starts_no_more_resources(self):
        cancel = threading.Event()
        handler, handled = self.study_handler(cancel)
        def observe(event):
            self.events.append(event)
            if event.completed == 1:
                cancel.set()
        handler.progress = Reporter(observe)
        run_resources(handler, [({'id': str(i)},) for i in range(3)])
        self.assertEqual(handled, ['0'])
        self.assertTrue(handler._course_failed)
        self.assertEqual(self.events[-1].completed, 1)

    def test_empty_and_single_resource_have_no_pause(self):
        for items in ([], [({'id': 'only'},)]):
            cancel = threading.Event()
            handler, handled = self.study_handler(cancel)
            with patch.object(cancel, 'wait') as wait:
                run_resources(handler, items)
            self.assertEqual(len(handled), len(items))
            wait.assert_not_called()

    def test_one_resource_courses_have_no_added_pause(self):
        cancel = threading.Event()
        handler, handled = self.study_handler(cancel)
        with patch.object(cancel, 'wait', return_value=False) as wait:
            run_resources(handler, [({'id': 'first'},)], 'first course')
            run_resources(handler, [({'id': 'second'},)], 'second course')
        self.assertEqual(handled, ['first', 'second'])
        wait.assert_not_called()

    def test_one_question_papers_have_no_added_pause(self):
        self.api.paper.return_value['questions'] = [question()]
        runner = AnswerRunner(self.api, self.ai, self.temp.name, clock_factory=FakeClock)
        with patch('ai.runner.time.sleep') as sleep:
            for paper in ('first', 'second'):
                result = runner.run_paper({'id': 'ci', 'courseId': 'c'}, {'id': paper}, 3)
                self.assertEqual(result['status'], 'preview')
        self.assertEqual(self.ai.answer.call_count, 2)
        sleep.assert_not_called()
