"""Resource completion observations use real handlers and offline platform responses."""
import threading
import unittest
from unittest.mock import patch

from AIMoocMain.main import AIMoocHandler
from ZYKMoocMain.main import ZYKMoocHandler
from base.auth import AuthenticationExpired
from tests import test_study_concurrency as fixtures
from tests.test_study_concurrency import StudyAPI, StudyTransport


class StudyProgressTests(unittest.TestCase):
    run_study = fixtures.StudyConcurrencyTests.run_study

    def test_keyboard_interrupt_before_write_does_not_complete_resource(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            for workers in (1, 2, 8):
                with self.subTest(platform=cls.__name__, workers=workers):
                    transport = StudyTransport(count=1)
                    events = []
                    with patch('AIMoocMain.main.time.sleep', side_effect=KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt):
                            cls(client=StudyAPI(transport, cls is AIMoocHandler), max_workers=workers,
                                on_progress=events.append,
                                course_rows=[{'id': 'one', 'courseInfoId': 'one', 'courseName': 'one'}])
                    self.assertEqual(transport.writes, [])
                    self.assertEqual(max(e.completed for e in events if e.kind == 'resources'), 0)

    def test_all_worker_counts_report_completed_resources_on_coordinator(self):
        # Missing per-resource emissions or worker callbacks must fail this contract.
        for cls in (AIMoocHandler, ZYKMoocHandler):
            for workers in (1, 2, 8):
                with self.subTest(platform=cls.__name__, workers=workers):
                    transport = StudyTransport(workers, count=8)
                    _, events = self.run_study(cls, transport, workers)
                    progress = [e for e in events if e.kind == 'resources']
                    self.assertTrue(progress)
                    self.assertEqual((progress[0].completed, progress[0].total), (0, None))
                    known = [e for e in progress if e.total is not None]
                    self.assertEqual([e.completed for e in known], list(range(9)))
                    self.assertEqual({e.total for e in known}, {8})
                    self.assertEqual({e.unit for e in progress}, {'项资源'})
                    self.assertEqual(transport.maximum, workers)

    def test_deduped_skips_and_business_failures_are_terminal_but_not_success(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            for workers in (1, 2, 8):
                transport = StudyTransport(count=4, failure={'code': 500, 'msg': 'failed'})
                transport.duplicate = True
                original = StudyAPI.nodes
                def nodes(api, course):
                    result = original(api, course)
                    result[1]['fileType'] = '作业'
                    result[2]['studentStudyRecord'] = {'speed': 100}
                    return result
                transport.completed['one'] = ['2']
                with patch.object(StudyAPI, 'nodes', nodes):
                    _, events = self.run_study(cls, transport, workers)
                progress = [e for e in events if e.kind == 'resources' and e.total is not None]
                self.assertEqual([e.completed for e in progress], [0, 1, 2, 3, 4])
                self.assertEqual({e.total for e in progress}, {4})
                for label in ('成功 1', '跳过 2', '失败 1'):
                    self.assertIn(label, progress[-1].name)
                self.assertEqual([e.status for e in events if e.kind == 'result'], ['error'])

    def test_course_discovery_resets_progress_before_platform_reads(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            transport = StudyTransport(count=2)
            events = []
            at_discovery = []
            original = StudyAPI.study_design_list
            def discover(api, *args):
                at_discovery.append([(e.completed, e.total) for e in events if e.kind == 'resources'][-1:])
                return original(api, *args)
            with patch('AIMoocMain.main.time.sleep'), patch.object(StudyAPI, 'study_design_list', discover):
                cls(client=StudyAPI(transport, cls is AIMoocHandler), max_workers=2,
                    on_progress=events.append,
                    course_rows=[{'id': name, 'courseInfoId': name, 'courseName': name}
                                 for name in ('one', 'two')])
            self.assertEqual(at_discovery, [[(0, None)], [(0, None)]])
            progress = [e for e in events if e.kind == 'resources']
            self.assertEqual([(e.completed, e.total) for e in progress],
                             [(0, None), (0, 2), (1, 2), (2, 2)] * 2)
            self.assertIn('one', progress[0].name)
            self.assertIn('two', progress[4].name)

    def test_preparation_cancel_does_not_count_unfinished_resource(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            for workers in (1, 2, 8):
                cancel = threading.Event()
                events = []
                transport = StudyTransport(count=4)
                with patch('AIMoocMain.main.time.sleep', side_effect=lambda _: cancel.set()):
                    cls(client=StudyAPI(transport, cls is AIMoocHandler), max_workers=workers,
                        cancel_event=cancel, on_progress=events.append,
                        course_rows=[{'id': 'one', 'courseInfoId': 'one', 'courseName': 'one'}])
                progress = [e for e in events if e.kind == 'resources' and e.total is not None]
                self.assertTrue(progress)
                self.assertEqual(progress[-1].total, 4)
                self.assertEqual(progress[-1].completed, 0)
                self.assertEqual(transport.writes, [])

    def test_completion_is_emitted_after_response_and_drains_inflight_on_cancel(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            cancel = threading.Event()
            events = []
            transport = StudyTransport(2, count=4)
            entered = threading.Barrier(2)
            first_finished = threading.Event()
            def write(client, course, source):
                # At request time neither request has completed.
                self.assertEqual(max(e.completed for e in events if e.kind == 'resources'), 0)
                entered.wait(timeout=3)
                if source == '0':
                    cancel.set()
                    first_finished.set()
                else:
                    self.assertTrue(first_finished.wait(timeout=3))
                return {'code': 200, 'msg': 'ok'}
            transport.write = write
            with patch('AIMoocMain.main.time.sleep'):
                cls(client=StudyAPI(transport, cls is AIMoocHandler), max_workers=2,
                    cancel_event=cancel, on_progress=events.append,
                    course_rows=[{'id': 'one', 'courseInfoId': 'one', 'courseName': 'one'}])
            progress = [e for e in events if e.kind == 'resources' and e.total is not None]
            self.assertEqual([e.completed for e in progress], [0, 1, 2])
            self.assertEqual(progress[-1].total, 4)
            self.assertIn('成功 2', progress[-1].name)
            self.assertIn('失败 0', progress[-1].name)
            self.assertCountEqual(transport.closed, transport.clones)

    def test_fatal_failure_keeps_partial_progress_and_closes_clients(self):
        for cls in (AIMoocHandler, ZYKMoocHandler):
            cancel = threading.Event()
            transport = StudyTransport(2, count=8, failure=AuthenticationExpired('expired'),
                                       cancel_event=cancel)
            _, events = self.run_study(cls, transport, 2, cancel_event=cancel)
            self.assertTrue(transport.cancel_observed)
            progress = [e for e in events if e.kind == 'resources' and e.total is not None]
            self.assertTrue(progress)
            self.assertEqual(progress[-1].completed, 2)
            self.assertEqual(progress[-1].total, 8)
            self.assertIn('失败 1', progress[-1].name)
            self.assertCountEqual(transport.closed, transport.clones)

    def test_fatal_inflight_error_survives_shutdown_and_cleanup_errors(self):
        from concurrent.futures import ThreadPoolExecutor
        import logging
        from base.study import run_resources
        from reporting import Reporter

        transport = StudyTransport(count=4)
        handler = AIMoocHandler.__new__(AIMoocHandler)
        handler.client = StudyAPI(transport, True)
        handler.cancel_event = threading.Event()
        handler.max_workers = 2
        handler.study_record_list = ()
        handler._course_failed = False
        handler._course_processed = False
        handler.logging = logging.getLogger('study-progress-test')
        events = []
        handler.progress = Reporter(events.append)
        entered = threading.Barrier(2)
        shutting_down = threading.Event()
        cancel_requested = threading.Event()
        failure = AuthenticationExpired('original expired session')
        def write(client, course, source):
            entered.wait(timeout=3)
            if source == '1':
                handler.cancel_event.set()
                cancel_requested.set()
                if not shutting_down.wait(timeout=3):
                    raise AssertionError('coordinator failed to drain requests')
                raise failure
            if not cancel_requested.wait(timeout=3):
                raise AssertionError('in-flight cancellation was not signalled')
            return {'code': 200, 'msg': 'ok'}
        transport.write = write
        original_shutdown = ThreadPoolExecutor.shutdown
        def shutdown(pool, *args, **kwargs):
            shutting_down.set()
            return original_shutdown(pool, *args, **kwargs)
        original_close = StudyAPI.close
        def close(client):
            original_close(client)
            raise RuntimeError('cleanup error must not replace authentication failure')
        with self.assertLogs('study-progress-test', level='ERROR'), patch('AIMoocMain.main.time.sleep'), patch.object(ThreadPoolExecutor, 'shutdown', shutdown), patch.object(StudyAPI, 'close', close):
            with self.assertRaises(AuthenticationExpired) as raised:
                run_resources(handler, ((node, 'parent', 0) for node in handler.client.nodes('one')), 'one')
        self.assertIs(raised.exception, failure)
        self.assertEqual(events[-1].completed, 2)
        self.assertCountEqual(transport.closed, transport.clones)
