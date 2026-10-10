"""Offline concurrency checks use real clients with a controlled HTTP boundary."""
import json
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

from ai.client import AIClient, AIConfig, AITransientServiceError
from ai.concurrency import ParallelAnswers, prepared_questions
from ai.runner import AnswerRunner, write_report
from AIMoocMain.exam_api import ExamError
from tests.test_ai import FakeClock, question
from tests.test_ai_retry import response


class Transport:
    def __init__(self, handler):
        self.handler = handler
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls = []
        self.sessions = []

    def session(self):
        owner = self
        class Session:
            closed = False
            def post(self, url, **kwargs):
                payload = kwargs['json']
                q = json.loads(payload['messages'][1]['content'])
                with owner.lock:
                    owner.active += 1
                    owner.peak = max(owner.peak, owner.active)
                    owner.calls.append(q['title'])
                    owner.sessions.append((self, threading.get_ident(), url, payload['model']))
                try:
                    return owner.handler(q)
                finally:
                    with owner.lock:
                        owner.active -= 1
            def close(self):
                self.closed = True
        return Session()


class AIConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = Mock(user_id='user')
        self.api.records.return_value = []
        self.api.verify_submitted.return_value = True
        self.api.paper.return_value = {'questions': [self.q(i) for i in range(12)],
                                      'taskExamRecord': {'id': 'task', 'status': 0}}
        self.ai = AIClient(AIConfig('https://example.org/v1', 'original-model', 'fake-key',
                                   api_format='chat_completions'), session=Mock())
        self.owner = threading.get_ident()
        self.events = []

    @staticmethod
    def q(i):
        return dict(question(qid=str(i)), title=f'题{i}')

    def run_paper(self, transport, workers=2, manual=None, clock=FakeClock):
        runner = AnswerRunner(self.api, self.ai, self.temp.name, auto_submit=True,
                              clock_factory=clock, max_workers=workers, manual_answer=manual,
                              on_progress=lambda event: self.events.append((threading.get_ident(), event)))
        def save(path, report):
            self.assertEqual(threading.get_ident(), self.owner)
            write_report(path, report)
        with patch('ai.client.requests.Session', side_effect=transport.session), \
                patch('ai.runner.write_report', side_effect=save):
            return runner.run_paper({'id': 'ci', 'courseId': 'c'}, {'id': 'exam'}, 3)

    def test_real_overlap_respects_two_and_eight_worker_caps_and_submits_once(self):
        # Removing the pool or its bound breaks the barrier or peak assertions.
        for workers in (2, 8):
            with self.subTest(workers=workers):
                barrier = threading.Barrier(workers)
                first = set(f'题{i}' for i in range(workers))
                def handle(q):
                    if q['title'] in first:
                        barrier.wait(timeout=3)
                    return response()
                transport = Transport(handle)
                result = self.run_paper(transport, workers)
                self.assertEqual(result['status'], 'submitted')
                self.assertEqual(transport.peak, workers)
                self.assertEqual(Counter(transport.calls), Counter(f'题{i}' for i in range(12)))
                self.assertEqual([a['title'] for a in result['answers']], [f'题{i}' for i in range(12)])
                self.assertEqual(len(self.api.submit.call_args.args[0]['taskExamProblemRecordList']), 12)
                self.assertEqual(self.api.submit.call_count, 1)
                self.api.submit.reset_mock()
                self.assertTrue(all(owner == self.owner for owner, _ in self.events))
                completed = [event.completed for _, event in self.events if event.kind == 'questions' and event.total]
                # Each independent paper starts a fresh progress sequence.
                self.assertEqual(completed, sorted(completed))
                self.events.clear()
                self.api.paper.return_value['taskExamRecord']['id'] += '-next'

    def test_out_of_order_children_keep_correct_identity_and_answer(self):
        self.api.paper.return_value['questions'] = [
            {'id': 'parent', 'title': '材料', 'questionSubList': [self.q(0), self.q(1)]}, self.q(2)]
        second = threading.Event()
        def handle(q):
            if q['title'] == '题0':
                self.assertTrue(second.wait(3))
                return response(content='{"answer":"B","confidence":0.95,"explanation":"first"}')
            second.set()
            return response()
        report = self.run_paper(Transport(handle))
        self.assertEqual(report['status'], 'submitted')
        entries = self.api.submit.call_args.args[0]['taskExamProblemRecordList']
        self.assertEqual([(e['paperId'], e.get('paperSubId'), e['questionNo'], e['answer']) for e in entries],
                         [('parent', '0', '[0, 0]', '0'), ('parent', '1', '[0, 1]', '3'), ('2', None, 1, '3')])

    def test_manual_abort_stops_dispatch_and_late_results_cannot_submit(self):
        second = threading.Event()
        def handle(q):
            if q['title'] == '题0':
                self.assertTrue(second.wait(3))
                return response(content='{"answer":"A","confidence":0.1,"explanation":"unsure"}')
            second.set()
            return response()
        transport = Transport(handle)
        calls_at_manual = []
        def manual(q, error):
            self.assertEqual(threading.get_ident(), self.owner)
            self.assertEqual(transport.active, 0)
            calls_at_manual.extend(transport.calls)
            return None
        report = self.run_paper(transport, manual=manual)
        self.assertEqual(report['status'], 'paused')
        self.assertEqual(transport.calls, calls_at_manual)
        self.assertTrue({'题0', '题1'}.issubset(transport.calls))
        self.assertFalse(self.api.submit.called)
        saved = json.loads(next(Path(self.temp.name).rglob('*.json')).read_text())
        self.assertEqual(saved['status'], 'paused')
        self.assertTrue(all(s.closed for s, *_ in transport.sessions))

    def test_fatal_out_of_order_failure_stops_dispatch(self):
        failed = threading.Event()
        def handle(q):
            if q['title'] == '题0':
                self.assertTrue(failed.wait(3))
                return response()
            failed.set()
            return response(401)
        transport = Transport(handle)
        report = self.run_paper(transport)
        self.assertEqual(report['status'], 'ai_error')
        self.assertEqual(Counter(transport.calls), Counter(['题0', '题1']))
        self.assertFalse(self.api.submit.called)
        failed_answer = next(row for row in report['answers'] if row['title'] == '题1')
        self.assertIn('401', failed_answer['error'])

    def test_expired_attempt_stops_dispatch_and_submission(self):
        expired = threading.Event()
        class Clock(FakeClock):
            def check(self):
                if expired.is_set():
                    raise ExamError('时间已用尽')
        def handle(q):
            expired.set()
            return response()
        transport = Transport(handle)
        self.assertEqual(self.run_paper(transport, clock=Clock)['status'], 'error')
        self.assertLessEqual(len(transport.calls), 2)
        self.assertFalse(self.api.submit.called)

    def test_worker_sessions_are_isolated_and_config_is_snapshotted(self):
        barrier = threading.Barrier(2)
        def handle(q):
            if q['title'] in ('题0', '题1'):
                barrier.wait(3)
                self.ai.config.model = 'changed-during-run'
                self.ai.config.api_format = 'responses'
            return response()
        transport = Transport(handle)
        self.assertEqual(self.run_paper(transport)['status'], 'submitted')
        self.assertTrue(all(model == 'original-model' and url.endswith('/chat/completions')
                            for _, _, url, model in transport.sessions))
        session_threads = {}
        for session, thread, *_ in transport.sessions:
            session_threads.setdefault(id(session), set()).add(thread)
            self.assertTrue(session.closed)
        self.assertTrue(all(len(threads) == 1 for threads in session_threads.values()))
        self.assertFalse(self.ai.session.post.called)

    def test_duplicate_identity_is_not_requested_twice_or_submitted(self):
        self.api.paper.return_value['questions'] = [self.q(0), self.q(0), self.q(1)]
        transport = Transport(lambda _: response())
        report = self.run_paper(transport)
        self.assertEqual(report['status'], 'needs_review')
        self.assertEqual(Counter(transport.calls), Counter(['题0', '题1']))
        self.assertFalse(self.api.submit.called)

    def test_fatal_auth_failure_cancels_other_question_retry_before_manual(self):
        first = threading.Event()
        def handle(q):
            if q['title'] == '题0':
                first.set()
                return response(content='broken')
            self.assertTrue(first.wait(3))
            return response(401)
        transport = Transport(handle)
        manual = Mock(return_value='B')
        report = self.run_paper(transport, manual=manual)
        self.assertEqual(report['status'], 'ai_error')
        self.assertEqual(Counter(transport.calls), Counter(['题0', '题1']))
        self.assertFalse(manual.called)
        self.assertFalse(self.api.submit.called)
        self.assertTrue(all(s.closed for s, *_ in transport.sessions))

    def test_retry_after_is_exposed_without_echoing_provider_body(self):
        rejected = response(429)
        rejected.headers = {'Retry-After': '2'}
        self.ai.session.post.return_value = rejected
        with self.assertRaises(AITransientServiceError) as error:
            self.ai.answer({'title': 'test'})
        self.assertEqual(getattr(error.exception, 'retry_after', None), 2)

    def test_parallel_retries_share_cooldown_and_never_repeat_completed_questions(self):
        self.api.paper.return_value['questions'] = [self.q(i) for i in range(3)]
        counts = Counter()
        barrier = threading.Barrier(2)
        times = {}
        lock = threading.Lock()
        def handle(q):
            title = q['title']
            with lock:
                counts[title] += 1
                times.setdefault(title, []).append(time.monotonic())
                call = counts[title]
            if call == 1 and title in ('题0', '题1'):
                barrier.wait(3)
                if title == '题0':
                    limited = response(429)
                    limited.headers = {'Retry-After': '2'}
                    return limited
                return response(content='broken')
            return response()
        transport = Transport(handle)
        report = self.run_paper(transport)
        self.assertEqual(report['status'], 'submitted')
        self.assertEqual(counts, {'题0': 2, '题1': 2, '题2': 1})
        self.assertGreaterEqual(times['题0'][1] - times['题0'][0], 1.95)
        self.assertGreaterEqual(times['题1'][1] - times['题1'][0], 1.95)
        self.assertLessEqual(transport.peak, 2)
        self.assertEqual(self.api.submit.call_count, 1)

    def test_exhausted_service_retry_keeps_final_cooldown_for_next_question(self):
        # Dropping the final Retry-After lets the next question start at 103,
        # even though the provider prohibited requests until 403.
        for status in (429, 503):
            with self.subTest(status=status):
                now = [100.0]
                started = []
                delays = iter(('1', '2', '300'))
                def handle(q):
                    started.append(now[0])
                    if q['title'] == '题0':
                        limited = response(status)
                        limited.headers = {'Retry-After': next(delays)}
                        return limited
                    return response()
                def advance(delay):
                    now[0] += delay
                    return False
                transport = Transport(handle)
                questions = prepared_questions([self.q(0), self.q(1)])
                pool = ParallelAnswers(self.ai, questions, 2, Mock(), allow_manual=True)
                try:
                    with patch('ai.client.requests.Session', side_effect=transport.session), \
                            patch('ai.concurrency.time.monotonic', side_effect=lambda: now[0]), \
                            patch.object(pool.stopped, 'wait', side_effect=advance):
                        with self.assertRaises(AITransientServiceError) as exhausted:
                            pool._generate(questions[0])
                        # An exhausted worker returns without sleeping again.
                        self.assertEqual(now[0], 103.0)
                        self.assertEqual(pool._generate(questions[1])['answer'], 'A')
                        self.assertEqual(started, [100.0, 101.0, 103.0, 403.0])
                        self.assertEqual(exhausted.exception.retry_after, 300)
                finally:
                    pool.close()

    def test_manual_recovery_preserves_later_answers_and_resumes_dispatch(self):
        self.api.paper.return_value['questions'] = [self.q(i) for i in range(4)]
        def handle(q):
            if q['title'] == '题0':
                return response(content='{"answer":"A","confidence":0.1,"explanation":"unsure"}')
            return response()
        transport = Transport(handle)
        def manual(q, error):
            self.assertEqual(threading.get_ident(), self.owner)
            self.assertEqual(Counter(transport.calls), Counter(['题0', '题1']))
            return 'B'
        report = self.run_paper(transport, manual=manual)
        self.assertEqual(report['status'], 'submitted')
        self.assertEqual(Counter(transport.calls), Counter(['题0', '题1', '题2', '题3']))
        self.assertEqual([row['record']['answer'] for row in report['answers']], ['0', '3', '3', '3'])
        self.assertEqual(self.api.submit.call_count, 1)

    def test_invalid_worker_settings_fail_before_any_platform_request(self):
        for workers in (0, 9, True, 2.0, '2'):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                AnswerRunner(self.api, self.ai, max_workers=workers)
        self.assertFalse(self.api.paper.called)

    def test_fast_answer_is_saved_and_replacement_starts_while_first_is_slow(self):
        self.api.paper.return_value['questions'] = [self.q(i) for i in range(3)]
        replacement_started = threading.Event()
        saved_before_replacement = []
        def handle(q):
            if q['title'] == '题0':
                self.assertTrue(replacement_started.wait(2), 'slow first question blocked the queue')
            if q['title'] == '题2':
                reports = list(Path(self.temp.name).rglob('*.json'))
                saved_before_replacement.extend(json.loads(reports[0].read_text())['answers'])
                replacement_started.set()
            return response()
        transport = Transport(handle)
        report = self.run_paper(transport)
        self.assertEqual(report['status'], 'submitted')
        self.assertEqual([a['title'] for a in saved_before_replacement], ['题1'])
        self.assertEqual(saved_before_replacement[0]['question_number'], 2)
        self.assertEqual([a['title'] for a in report['answers']], ['题0', '题1', '题2'])
        self.assertEqual(transport.peak, 2)
        self.assertIn(1, [event.completed for _, event in self.events if event.kind == 'questions'])
