"""Catalog time windows are checked without opening an attempt."""
import io
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from functools import partial
from pathlib import Path
from unittest.mock import Mock, patch

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
import checklist
import task_capabilities as capability
import theme
from ai.runner import AnswerRunner, report_path, write_report
from app import Application
from AIMoocMain.exam_api import ExamError
from AIMoocMain.export_questions import QuizExportHandler
from settings import SettingsStore
from ui import TerminalUI
from tests.test_ai import FakeClock, question

NOW=datetime(2026,10,10,15,tzinfo=timezone(timedelta(hours=8)))
COURSE={'id':'course-info','courseId':'course','courseName':'课程'}
FUTURE={'id':'future','name':'拓展测试1','typeId':'1','status':-1,
        'startTime':'2026-12-28 00:00:00','endTime':'2026-12-31 23:59:59'}
CLOSED={'id':'closed','name':'已截止考试','status':'-2'}
OPEN={'id':'open','name':'可答测验','typeId':1,'status':0}


class AvailabilityTests(unittest.TestCase):
    def check(self,item,now=NOW):
        self.assertTrue(hasattr(capability,'task_availability'), 'catalog availability is not implemented')
        return capability.task_availability(item,2,now=now)

    def test_server_status_is_authoritative_even_without_dates(self):
        for value,label in ((-1,'未开始'),('-1','未开始'),(-2,'已截止'),('-2','已截止')):
            state=self.check({'status':value})
            self.assertFalse(state.available)
            self.assertEqual(state.label,label)

    def test_future_window_has_exact_opening_time_and_timezone(self):
        state=self.check(dict(FUTURE,status=0))
        self.assertFalse(state.available)
        self.assertEqual(state.label,'未开始')
        self.assertIn('2026-12-28 00:00:00',state.reason)
        self.assertIn('北京时间',state.reason)

    def test_dates_enforce_start_inclusive_end_inclusive_independent_of_host_timezone(self):
        item={'startTime':'2026-10-10 15:00:00','endTime':'2026-10-10 16:00:00'}
        for offset,allowed in ((-1,False),(0,True),(3600,True),(3601,False)):
            with self.subTest(offset=offset):
                instant=(NOW+timedelta(seconds=offset)).astimezone(timezone.utc)
                self.assertEqual(self.check(item,instant).available,allowed)

    def test_explicit_offsets_are_respected(self):
        self.assertTrue(self.check({'startTime':'2026-10-10T07:00:00Z'}).available)
        self.assertFalse(self.check({'startTime':'2026-10-10T08:00:00+00:00'}).available)

    def test_missing_dates_keep_existing_behavior_but_malformed_window_is_not_opened(self):
        for item in ({},{'startTime':None,'endTime':''},{'status':3}):
            self.assertTrue(self.check(item).available)
        for item in ({'startTime':'not-a-date'},{'endTime':True},
                     {'startTime':'2026-12-31','endTime':'2026-01-01'}):
            self.assertFalse(self.check(item).available)
            self.assertEqual(self.check(item).label,'时间未知')

    def test_attachment_precedes_time_restriction_and_unknown_status_does_not_guess(self):
        self.assertEqual(self.check(dict(FUTURE,typeId=3)).label,'暂不支持')
        self.assertTrue(self.check({'status':205}).available)


class AvailabilityWorkflowTests(unittest.TestCase):
    def view(self,answers=()):
        out=io.StringIO()
        inputs=iter(answers)
        return TerminalUI(console=Console(file=out,width=120,theme=theme.THEME),
                          input_fn=lambda:next(inputs)),out

    def test_line_all_and_retained_selection_exclude_future_and_closed(self):
        catalog=[(COURSE,FUTURE,2),(COURSE,CLOSED,2),(COURSE,OPEN,3)]
        for keys in (['all'],['1,3','3'],['']):
            view,out=self.view(keys)
            self.assertEqual(view.papers(catalog,selected=catalog),[catalog[-1]])
            self.assertIn('未开始',out.getvalue())
            self.assertIn('已截止',out.getvalue())
            self.assertIn('2026-12-28',out.getvalue())

    def test_native_all_and_space_keep_time_blocked_tasks_unselected(self):
        catalog=[(COURSE,FUTURE,2),(COURSE,CLOSED,2),(COURSE,OPEN,3)]
        for keys in ('\x1b[A \r',' \x1b[B \x1b[B \r'):
            view=TerminalUI(console=Console(file=io.StringIO(),force_terminal=True,theme=theme.THEME))
            with create_pipe_input() as pipe:
                pipe.send_text(keys)
                native=partial(checklist.choose_many,input=pipe,output=DummyOutput())
                with patch('checklist.choose_many',side_effect=native),patch('sys.stdin.isatty',return_value=True):
                    self.assertEqual(view.papers(catalog),[catalog[-1]])

    def test_unavailable_only_does_not_test_ai_or_read_paper(self):
        view,_=self.view()
        with tempfile.TemporaryDirectory() as directory:
            app=Application(view,SettingsStore(directory))
            with patch.object(app,'verify_ai') as verify,patch('app.AnswerRunner') as runner:
                runner.return_value.run_paper.return_value={'status':'preview'}
                reports=app.execute_ai(Mock(),[(COURSE,FUTURE,2),(COURSE,CLOSED,2)],Mock(),True,api=Mock())
        self.assertEqual([r['status'] for r in reports],['skipped','skipped'])
        verify.assert_not_called()
        runner.assert_not_called()

    def test_mixed_queue_continues_after_unavailable_task(self):
        view,_=self.view()
        with tempfile.TemporaryDirectory() as directory:
            app=Application(view,SettingsStore(directory))
            with patch.object(app,'verify_ai'),patch('app.AnswerRunner') as factory:
                factory.return_value.run_paper.return_value={'status':'preview','paper':'可答测验'}
                results=app.execute_ai(Mock(),[(COURSE,FUTURE,2),(COURSE,OPEN,3)],Mock(),False,api=Mock())
        self.assertEqual([r['status'] for r in results],['skipped','preview'])
        factory.return_value.run_paper.assert_called_once_with(COURSE,OPEN,3)

    def test_runner_blocks_before_files_model_or_platform_calls(self):
        for item in (FUTURE,CLOSED):
            with tempfile.TemporaryDirectory() as directory:
                api,ai=Mock(),Mock()
                runner=AnswerRunner(api,ai,directory)
                with patch.object(runner,'_run_paper',return_value=None):
                    with self.assertRaises(ExamError):runner.run_paper(COURSE,item,2)
                self.assertEqual(list(Path(directory).iterdir()),[])
                self.assertEqual(api.mock_calls,[])
                ai.answer.assert_not_called()

    def test_export_skips_future_and_closed_without_getting_paper(self):
        for mode in ('per_chapter','merged'):
            with tempfile.TemporaryDirectory() as directory:
                api,events=Mock(),[]
                handler=QuizExportHandler(client=api,course_rows=[COURSE],category_ids=[2],
                    selected_papers=[(COURSE,FUTURE,2),(COURSE,CLOSED,2)],output_dir=directory,
                    quiz_mode=mode,on_progress=events.append)
                api.get_exam_paper.assert_not_called()
                self.assertEqual(handler.stats['papers'],0)
                self.assertEqual([e.status for e in events if e.kind=='result'],['skipped','skipped'])

    def test_window_expiring_during_connection_test_skips_and_continues(self):
        view,_=self.view()
        now=[NOW]
        first=dict(OPEN,id='first',endTime='2026-10-10 15:00:00')
        api=Mock(user_id='user')
        api.records.return_value=[]
        api.paper.return_value={'questions':[question()], 'taskExamRecord':{'id':'task','status':0}}
        ai=Mock()
        ai.config.model='test'
        ai.answer.return_value={'answer':'A','confidence':.95,'explanation':'ok'}
        def verify(config):
            now[0]+=timedelta(seconds=1)
            return ai
        with tempfile.TemporaryDirectory() as directory,patch('task_capabilities.datetime') as clock:
            clock.fromisoformat.side_effect=datetime.fromisoformat
            clock.now.side_effect=lambda tz:now[0]
            app=Application(view,SettingsStore(directory))
            def runner(*args,**kwargs):
                return AnswerRunner(*args,**kwargs,output_dir=directory,clock_factory=FakeClock)
            with patch.object(app,'verify_ai',side_effect=verify),patch('app.AnswerRunner',side_effect=runner):
                reports=app.execute_ai(Mock(),[(COURSE,first,3),(COURSE,OPEN,3)],Mock(),False,api=api)
        self.assertEqual([r['status'] for r in reports],['skipped','preview'])
        api.paper.assert_called_once_with('open')

    def test_expiring_during_answer_keeps_report_and_never_submits(self):
        now=[NOW]
        item=dict(OPEN,endTime='2026-10-10 15:00:00')
        api=Mock(user_id='user')
        api.records.return_value=[]
        api.paper.return_value={'questions':[question()], 'taskExamRecord':{'id':'task','status':0}}
        ai=Mock()
        ai.config.model='test'
        def answer(q):
            now[0]+=timedelta(seconds=1)
            return {'answer':'A','confidence':.95,'explanation':'ok'}
        ai.answer.side_effect=answer
        with tempfile.TemporaryDirectory() as directory,patch('task_capabilities.datetime') as clock:
            clock.fromisoformat.side_effect=datetime.fromisoformat
            clock.now.side_effect=lambda tz:now[0]
            report=AnswerRunner(api,ai,directory,auto_submit=True,clock_factory=FakeClock,
                                on_log=lambda message:None).run_paper(COURSE,item,3)
            self.assertEqual(report['status'],'skipped')
            self.assertIn('已截止',report['message'])
            self.assertEqual(len(report['answers']),1)
            self.assertTrue(list(Path(directory).rglob('*.json')))
        api.submit.assert_not_called()

    def test_generic_platform_205_remains_an_error_not_a_guessed_time_skip(self):
        api=Mock(user_id='user')
        api.records.return_value=[]
        api.paper.side_effect=ExamError('平台拒绝请求（code=205）')
        ai=Mock()
        ai.config.model='test'
        with tempfile.TemporaryDirectory() as directory:
            report=AnswerRunner(api,ai,directory,on_log=lambda message:None).run_paper(COURSE,OPEN,3)
        self.assertEqual(report['status'],'error')
        ai.answer.assert_not_called()

    def test_expiry_before_opening_preserves_previous_answers_and_report_files(self):
        for status in ('preview','paused','needs_review'):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as directory:
                now=[NOW]
                item=dict(OPEN,endTime='2026-10-10 15:00:00')
                path=report_path(directory,'user',COURSE,item['id'])
                write_report(path,{'paper':'可答测验','status':status,
                                   'answers':[{'title':'已保存题目','answer':'A'}]})
                before={p.name:p.read_bytes() for p in path.parent.iterdir()}
                api=Mock(user_id='user')
                def records(exam_id):
                    now[0]+=timedelta(seconds=1)
                    return []
                api.records.side_effect=records
                ai=Mock()
                ai.config.model='test'
                with patch('task_capabilities.datetime') as clock:
                    clock.fromisoformat.side_effect=datetime.fromisoformat
                    clock.now.side_effect=lambda tz:now[0]
                    report=AnswerRunner(api,ai,directory,on_log=lambda message:None).run_paper(COURSE,item,3)
                self.assertEqual(report['status'],'skipped')
                api.paper.assert_not_called()
                self.assertEqual({p.name:p.read_bytes() for p in path.parent.iterdir()},before)
