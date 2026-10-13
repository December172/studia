"""Offline resource scheduling and HTTP ownership contracts."""
import copy
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import requests

from AIMoocMain.main import AIMoocHandler
from ZYKMoocMain.main import ZYKMoocHandler
from base.api_client import BaseAPIClient
from base.auth import AuthenticationExpired
from base.util import get_mp3_duration


class StudyTransport:
    def __init__(self, workers=1, count=12, failure=None, *, cancel_event=None):
        self.workers = workers
        self.count = count
        self.failure = failure
        self.cancel_event = cancel_event
        self.cancel_observed = None
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(workers) if workers > 1 else None
        self.active = 0
        self.maximum = 0
        self.writes = []
        self.clones = []
        self.clone_threads = []
        self.closed = []
        self.completed = {}
        self.discovered = []
        self.duplicate = False

    def write(self, client, course, source):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            index = len(self.writes)
            self.writes.append((course, source, id(client)))
        try:
            if self.barrier and index < self.workers:
                self.barrier.wait(timeout=3)
            if source == '0' and self.failure:
                if isinstance(self.failure, BaseException):
                    raise self.failure
                return self.failure
            if self.cancel_event is not None and self.failure:
                # Keep the successful peer in flight until the fatal worker has
                # been cancelled, so the outcome does not depend on which of the
                # simultaneously released writes wins the race.
                if self.cancel_event.wait(timeout=3):
                    self.cancel_observed = True
                else:
                    self.cancel_observed = False
            return {'code': 200, 'msg': 'ok'}
        finally:
            with self.lock:
                self.active -= 1


class StudyAPI:
    def __init__(self, transport, mooc):
        self.transport = transport
        self.mooc = mooc

    def clone(self):
        client = copy.copy(self)
        self.transport.clones.append(client)
        self.transport.clone_threads.append(threading.get_ident())
        return client

    def close(self):
        self.transport.closed.append(self)

    def study_design_list(self, course, *args):
        self.transport.discovered.append((course, self.transport.active))
        return [{'id': 'parent'}] if self.mooc else self.nodes(course)

    def nodes(self, course):
        result = [{'id': str(i), 'courseInfoId': course, 'courseId': 'c',
                   'parentId': 'parent', 'fileType': 'ppt' if self.mooc else 'zip',
                   'name': str(i)} for i in range(self.transport.count)]
        if self.transport.duplicate:
            result += [result[0].copy()]
        return result

    def get_cell_list(self, course, *args):
        return self.nodes(course)

    def study_record_list(self, course, *args):
        return self.transport.completed.get(course, [])

    def course_content(self, source):
        return {}

    def study_record(self, *args):
        course, source = (args[1], args[4]) if self.mooc else (args[0], args[3])
        return self.transport.write(self, course, source)


class StudyConcurrencyTests(unittest.TestCase):
    def run_study(self, cls, transport, workers=None, courses=('one',), **kwargs):
        events = []
        main_thread = threading.get_ident()
        callback_threads = []
        def progress(event):
            callback_threads.append(threading.get_ident())
            events.append(event)
        with ExitStack() as stack:
            stack.enter_context(patch('AIMoocMain.main.time.sleep'))
            stack.enter_context(patch('ZYKMoocMain.main.time.sleep'))
            if workers is not None:
                kwargs['max_workers'] = workers
            handler = cls(client=StudyAPI(transport, cls is AIMoocHandler),
                          course_rows=[{'id': c, 'courseInfoId': c, 'courseId': 'c',
                                        'courseName': c} for c in courses],
                          on_progress=progress, **kwargs)
        self.assertEqual(set(callback_threads) - {main_thread}, set())
        return handler, events

    def test_same_course_overlaps_and_respects_two_and_eight_worker_caps(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            for workers in (2, 8):
                with self.subTest(cls=cls, workers=workers):
                    transport = StudyTransport(workers)
                    _, events = self.run_study(cls, transport, workers)
                    self.assertEqual(transport.maximum, workers)
                    self.assertEqual(len(transport.writes), 12)
                    self.assertEqual([e.status for e in events if e.kind == 'result'], ['success'])
                    self.assertCountEqual(transport.closed, transport.clones)
                    self.assertTrue(transport.clones)
                    self.assertEqual(set(transport.clone_threads), {threading.get_ident()})

    def test_ninth_resource_starts_while_first_is_slow_with_eight_workers(self):
        # Completion-driven refill: with eight slots already busy, the 9th
        # resource must enter while the blocked first one is still running.
        # The barrier admits only the first eight writes (the index is claimed
        # under the lock before waiting), so the 9th is never gated by it.
        workers = 8
        for cls in (AIMoocHandler, ZYKMoocHandler):
            with self.subTest(cls=cls):
                release = threading.Event()

                class RollingTransport(StudyTransport):
                    def __init__(self):
                        super().__init__(workers=workers, count=9)
                        self.order = []
                        self.active_at_ninth = []

                    def write(self, client, course, source):
                        with self.lock:
                            self.active += 1
                            self.maximum = max(self.maximum, self.active)
                            index = len(self.writes)
                            self.writes.append((course, source, id(client)))
                            self.order.append(('start', source))
                        try:
                            if index < self.workers:
                                self.barrier.wait(timeout=5)
                            if source == '0':
                                release.wait(timeout=5)
                            if source == '8':
                                self.active_at_ninth.append(self.active)
                            return {'code': 200, 'msg': 'ok'}
                        finally:
                            with self.lock:
                                self.active -= 1
                                self.order.append(('end', source))
                            if source == '8':
                                release.set()

                transport = RollingTransport()
                _, events = self.run_study(cls, transport, workers)
                self.assertEqual(transport.maximum, workers)
                self.assertEqual(len(transport.writes), 9)
                self.assertCountEqual([w[1] for w in transport.writes], [str(i) for i in range(9)])
                self.assertLess(transport.order.index(('start', '8')),
                                transport.order.index(('end', '0')))
                # The blocked first resource still holds a slot when the 9th
                # starts, and the pool never exceeds its eight-worker bound.
                self.assertEqual(len(transport.active_at_ninth), 1)
                self.assertTrue(2 <= transport.active_at_ninth[0] <= workers,
                                f'expected 2..{workers} active, got {transport.active_at_ninth}')
                self.assertEqual([e.status for e in events if e.kind == 'result'], ['success'])
                progress = [e.completed for e in events if e.kind == 'resources']
                self.assertEqual(progress, sorted(progress))
                self.assertEqual(progress[-1], 9)
                self.assertCountEqual(transport.closed, transport.clones)

    def test_default_worker_preserves_resource_order(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            transport = StudyTransport(count=4)
            self.run_study(cls, transport)
            self.assertEqual([w[1] for w in transport.writes], ['0', '1', '2', '3'])
            self.assertEqual(transport.maximum, 1)

    def test_duplicates_write_once_and_courses_finish_before_next_discovery(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            transport = StudyTransport(2, count=4)
            transport.duplicate = True
            self.run_study(cls, transport, 2, courses=('one', 'two'))
            self.assertEqual(len(transport.writes), 8)
            self.assertEqual(transport.discovered, [('one', 0), ('two', 0)])

    def test_completed_record_snapshot_does_not_leak_between_courses(self):
        transport = StudyTransport(2, count=4)
        transport.completed['one'] = ['0', '1']
        self.run_study(AIMoocHandler, transport, 2, courses=('one', 'two'))
        self.assertEqual({(w[0], w[1]) for w in transport.writes},
                         {('one', '2'), ('one', '3'), ('two', '0'), ('two', '1'), ('two', '2'), ('two', '3')})

    def test_business_error_and_partial_failure_are_not_success(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            for failure in ({'code': 500, 'msg': 'failed'}, RuntimeError('failed'), None):
                # None is a successful transport response in this fixture.
                transport = StudyTransport(2, count=4, failure=failure)
                _, events = self.run_study(cls, transport, 2)
                expected = 'success' if failure is None else 'error'
                self.assertEqual([e.status for e in events if e.kind == 'result'], [expected])
                self.assertEqual(len(transport.writes), 4)

    def test_fatal_auth_stops_pending_resources_and_next_course(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            cancel = threading.Event()
            transport = StudyTransport(2, failure=AuthenticationExpired('expired'),
                                       cancel_event=cancel)
            _, events = self.run_study(cls, transport, 2, courses=('one', 'two'),
                                       cancel_event=cancel)
            self.assertTrue(transport.cancel_observed)
            self.assertLessEqual(len(transport.writes), 2)
            self.assertEqual(transport.discovered, [('one', 0)])
            self.assertNotIn('success', [e.status for e in events])
            self.assertCountEqual(transport.closed, transport.clones)

    def test_pre_cancelled_run_does_not_write(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            event = threading.Event()
            event.set()
            transport = StudyTransport(count=4)
            self.run_study(cls, transport, 2, cancel_event=event)
            self.assertEqual(transport.writes, [])


    def test_rate_limit_stops_pending_work_without_retrying(self):
        from base.api_client import RequestRateLimited
        for cls in (AIMoocHandler, ZYKMoocHandler):
            cancel = threading.Event()
            transport = StudyTransport(2, failure=RequestRateLimited('rate limited'),
                                       cancel_event=cancel)
            _, events = self.run_study(cls, transport, 2, courses=('one', 'two'),
                                       cancel_event=cancel)
            self.assertTrue(transport.cancel_observed)
            self.assertLessEqual(len(transport.writes), 2)
            self.assertEqual(transport.discovered, [('one', 0)])
            self.assertNotIn('success', [e.status for e in events])

    def test_cancellation_during_preparation_prevents_writes(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            event = threading.Event()
            transport = StudyTransport(count=4)
            with patch('AIMoocMain.main.time.sleep', side_effect=lambda _: event.set()):
                # run_study patches sleep, so construct directly for this boundary.
                events = []
                cls(client=StudyAPI(transport, cls is AIMoocHandler), max_workers=2,
                    cancel_event=event, on_progress=events.append,
                    course_rows=[{'id': 'one', 'courseInfoId': 'one', 'courseName': 'one'}])
            self.assertEqual(transport.writes, [])
            self.assertNotIn('success', [e.status for e in events])

    def test_zyk_accepts_unwrapped_saved_record_but_mooc_requires_acknowledgement(self):
        for cls, expected in ((ZYKMoocHandler, 'success'), (AIMoocHandler, 'error')):
            transport = StudyTransport(count=1, failure={'id': 'saved-record'})
            _, events = self.run_study(cls, transport)
            self.assertEqual([e.status for e in events if e.kind == 'result'], [expected])

    def test_failed_completed_record_read_starts_no_workers(self):
        transport = StudyTransport(count=4)
        transport.completed['one'] = None
        _, events = self.run_study(AIMoocHandler, transport, 2)
        self.assertEqual(transport.writes, [])
        self.assertEqual(transport.clones, [])
        self.assertEqual([e.status for e in events if e.kind == 'result'], ['error'])

    def test_owned_client_is_closed_but_injected_client_is_retained(self):
        for cls, module, api_name in ((AIMoocHandler, 'AIMoocMain.main', 'AIMoocApi'),
                                      (ZYKMoocHandler, 'ZYKMoocMain.main', 'ZYKMoocApi')):
            transport = StudyTransport(count=0)
            client = StudyAPI(transport, cls is AIMoocHandler)
            with patch(module + '.' + api_name, return_value=client):
                cls(course_rows=[])
            self.assertEqual(transport.closed, [client])
            transport.closed.clear()
            cls(client=client, course_rows=[])
            self.assertEqual(transport.closed, [])

    def test_worker_interruption_stops_dispatch_and_closes_all_clones(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            cancel = threading.Event()
            transport = StudyTransport(2, failure=KeyboardInterrupt(),
                                       cancel_event=cancel)
            with self.assertRaises(KeyboardInterrupt):
                self.run_study(cls, transport, 2, cancel_event=cancel)
            self.assertTrue(transport.cancel_observed)
            self.assertLessEqual(len(transport.writes), 2)
            self.assertCountEqual(transport.closed, transport.clones)

    def test_legacy_ack_and_raw_failure_are_classified_in_single_worker_path(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            for body, expected in (({'msg': 'ok'}, 'success'), ({'code': 500, 'msg': 'failed'}, 'error')):
                transport = StudyTransport(count=1, failure=body)
                _, events = self.run_study(cls, transport)
                self.assertEqual([e.status for e in events if e.kind == 'result'], [expected])


class StudyHTTPTests(unittest.TestCase):
    def test_client_clone_copies_auth_without_login_and_owns_session(self):
        client = BaseAPIClient('https://example.invalid')
        client.set_auth_token('synthetic')
        client.session.cookies.set('session', 'synthetic')
        try:
            clone = client.clone()
            try:
                self.assertIsNot(clone.session, client.session)
                self.assertEqual(clone.session.headers['Authorization'], 'Bearer synthetic')
                self.assertEqual(clone.session.cookies.get('session'), 'synthetic')
                clone.session.headers['Authorization'] = 'changed'
                clone.session.cookies.set('session', 'changed')
                self.assertEqual(client.session.headers['Authorization'], 'Bearer synthetic')
                self.assertEqual(client.session.cookies.get('session'), 'synthetic')
            finally:
                clone.close()
        finally:
            client.session.close()

    def test_all_http_methods_bound_wait_and_close_response(self):
        for method in ('get', 'post', 'put'):
            client = BaseAPIClient('https://example.invalid')
            response = Mock(status_code=200)
            response.json.return_value = {'code': 200, 'data': {'saved': True}}
            with patch.object(client.session, method, return_value=response) as request:
                result = getattr(client, method)('/resource')
                self.assertEqual(result, {'saved': True})
                self.assertEqual(request.call_args.kwargs['timeout'], (10, 30))
                response.close.assert_called_once()
            client.session.close()

    def test_auth_http_and_business_failures_escape_instead_of_becoming_none(self):
        for status, body in ((401, {}), (200, {'code': 401, 'msg': 'expired'})):
            client = BaseAPIClient('https://example.invalid')
            response = Mock(status_code=status)
            response.json.return_value = body
            with patch.object(client.session, 'post', return_value=response):
                with self.assertRaises(AuthenticationExpired):
                    client.post('/resource', origin_response=True)
                response.close.assert_called_once()
            client.session.close()


    def test_rate_limit_http_and_business_failures_raise_without_replay(self):
        from base.api_client import RequestRateLimited
        for status, body in ((429, {}), (200, {'code': 429, 'msg': 'rate limited'})):
            client = BaseAPIClient('https://example.invalid')
            response = Mock(status_code=status)
            response.json.return_value = body
            with patch.object(client.session, 'put', return_value=response) as request:
                with self.assertRaises(RequestRateLimited):
                    client.put('/resource')
                self.assertEqual(request.call_count, 1)
                response.close.assert_called_once()
            client.session.close()

    def test_write_timeout_is_not_retried(self):
        client = BaseAPIClient('https://example.invalid')
        with patch.object(client.session, 'post', side_effect=requests.Timeout()) as request:
            self.assertIsNone(client.post('/resource'))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.kwargs['timeout'], (10, 30))
        client.session.close()

    def test_mp3_stream_has_timeout_and_closes_on_read_error(self):
        response = Mock(status_code=200)
        response.raw.read.side_effect = requests.Timeout()
        with patch('base.util.requests.get', return_value=response) as request:
            with self.assertRaises(requests.Timeout):
                get_mp3_duration('https://example.invalid/audio.mp3')
            self.assertEqual(request.call_args.kwargs['timeout'], (10, 30))
            response.close.assert_called_once()
