"""Actual runner results stay aligned when one paper fails in a batch."""
import io
import tempfile
import unittest
from unittest.mock import Mock, patch

from rich.console import Console
from AIMoocMain import exam_api
from AIMoocMain.exam_api import ExamAPI, ExamError
from ai.runner import AnswerRunner, AttemptClock, report_path
from app import Application
from reporting import ProgressEvent
from settings import SettingsStore
from tests.test_ai import FakeClock, question
from ui import TerminalUI
import theme


class QueueResultTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output=io.StringIO()
        self.view=TerminalUI(console=Console(file=self.output,width=160,theme=theme.THEME),input_fn=lambda:'1')
        self.app=Application(self.view,SettingsStore(self.temp.name))
        self.course={'id':'ci','courseId':'c','courseName':'课程'}
        self.selected=[(self.course,{'id':str(i),'name':f'试卷{i}'},3) for i in range(4)]
        self.api=Mock(user_id='user')
        self.api.records.return_value=[]
        self.api.verify_submitted.return_value=True
        self.ai=Mock()
        self.ai.config.model='test-model'
        self.ai.answer.return_value={'answer':'A','confidence':.95,'explanation':'ok'}

    def execute(self,selected=None):
        self.view.begin_operation()
        def factory(*args,**kwargs):
            return AnswerRunner(*args,**kwargs,output_dir=self.temp.name,clock_factory=FakeClock,
                                on_log=lambda message:None)
        with patch.object(self.app,'verify_ai',return_value=self.ai),patch('app.AnswerRunner',side_effect=factory):
            return self.app.execute_ai(Mock(),selected or self.selected,Mock(),True,api=self.api)

    def paper(self,exam_id):
        return {'questions':[question(qid=exam_id)],'taskExamRecord':{'id':'task-'+exam_id,'status':0}}

    def test_independent_failures_do_not_stop_batch_and_summary_counts_papers(self):
        def paper(exam_id):
            if exam_id=='0':raise ExamError('平台拒绝请求（code=205）')
            row=self.paper(exam_id)
            if exam_id=='2':row['questions']=[question(kind=12)]
            return row
        self.api.paper.side_effect=paper
        reports=self.execute()
        self.assertEqual([r['status'] for r in reports],['error','submitted','needs_review','submitted'])
        self.assertEqual([r['paper'] for r in reports],[f'试卷{i}' for i in range(4)])
        self.assertEqual(self.api.submit.call_count,2)
        self.assertEqual(self.view.progress.tasks[0].completed,4)
        self.view.operation_result()
        text=self.output.getvalue()
        self.assertIn('试卷汇总',text)
        self.assertIn('成功 2 · 失败 2',text)

    def test_pre_report_lock_error_has_result_and_next_paper_runs(self):
        path=report_path(self.temp.name,'user',self.course,'0')
        path.parent.mkdir(parents=True)
        lock=path.with_suffix('.lock')
        lock.write_text('other-process')
        self.api.paper.side_effect=self.paper
        reports=self.execute(self.selected[:2])
        self.assertEqual([r['status'] for r in reports],['error','submitted'])
        self.assertIn('锁定',reports[0]['message'])
        self.assertEqual([r['paper'] for r in reports],['试卷0','试卷1'])
        self.assertEqual(lock.read_text(),'other-process')
        self.api.paper.assert_called_once_with('1')

    def test_global_fault_has_one_failed_paper_and_remaining_not_executed(self):
        self.assertTrue(hasattr(exam_api,'ExamServiceError'),'global errors need a distinct type')
        self.api.paper.side_effect=exam_api.ExamServiceError('平台登录已失效')
        reports=self.execute()
        self.assertEqual(len(reports),1)
        self.assertEqual(reports[0]['status'],'error')
        self.assertTrue(reports[0]['stop_queue'])
        self.api.paper.assert_called_once()
        self.view.operation_result()
        self.assertIn('成功 0 · 失败 1',self.output.getvalue())
        self.assertIn('未执行 3',self.output.getvalue())

    def test_a_runner_level_service_error_stops_the_queue_but_a_plain_error_does_not(self):
        # A global error escaping run_paper must reach the shared stop_queue check
        # instead of being counted as one local paper failure and continuing.
        for error, expected in ((exam_api.ExamServiceError('登录已失效'), 1), (ExamError('试卷不可用'), 4)):
            with self.subTest(error=type(error).__name__):
                self.view.begin_operation()
                runner = Mock()
                runner.run_paper.side_effect = [error] + [
                    {'status': 'preview', 'paper': '试卷%s' % i} for i in range(4)]
                with patch.object(self.app, 'verify_ai', return_value=self.ai), \
                        patch('app.AnswerRunner', return_value=runner):
                    reports = self.app.execute_ai(Mock(), self.selected, Mock(), True, api=self.api)
                self.assertEqual(runner.run_paper.call_count, expected)
                self.assertEqual(len(reports), expected)
                self.assertEqual(reports[0]['stop_queue'], isinstance(error, exam_api.ExamServiceError))

    def test_unknown_submission_stops_without_replay_or_counting_pending_as_failures(self):
        self.api.paper.side_effect=self.paper
        self.api.verify_submitted.return_value=False
        reports=self.execute()
        self.assertEqual([r['status'] for r in reports],['submission_unknown'])
        self.api.submit.assert_called_once()
        self.view.operation_result()
        self.assertIn('失败 1',self.output.getvalue())
        self.assertIn('未执行 3',self.output.getvalue())

    def test_summary_reports_skipped_and_never_counts_unstarted_papers_as_failures(self):
        self.view.begin_operation()
        for event in (ProgressEvent('begin','AI 答题',total=5,unit='份试卷'),
                      ProgressEvent('result','A',status='submitted'),
                      ProgressEvent('result','B',status='skipped')):
            self.view.on_progress(event)
        self.view.cancelled=True
        self.view.finish()
        text=self.output.getvalue()
        for value in ('试卷汇总','成功 1 · 失败 0','跳过 1','未执行 3','已取消'):
            self.assertIn(value,text)

    def test_preflight_error_is_visible_without_inventing_failed_paper(self):
        self.view.begin_operation()
        self.view.on_progress(ProgressEvent('begin','AI 答题',total=4,unit='份试卷'))
        self.view.pending_error='AI 连接测试失败'
        with patch.object(self.view.console,'print',wraps=self.view.console.print) as output:
            self.view.finish()
        self.assertIn('成功 0 · 失败 0',self.output.getvalue())
        self.assertIn('未执行 4',self.output.getvalue())
        self.assertIn('AI 连接测试失败',self.output.getvalue())
        self.assertEqual(output.call_args.args[0].style,'error')


class ExamFailureClassificationTests(unittest.TestCase):
    def test_auth_rate_and_service_errors_are_global_but_permission_and_205_are_local(self):
        self.assertTrue(hasattr(exam_api,'ExamServiceError'),'global errors need a distinct type')
        session=Mock()
        session.headers={}
        api=ExamAPI(Mock(user_id='user',default_base_url='https://example.org',access_token='synthetic'),session)
        for http,code,global_error in ((401,None,True),(429,None,True),(503,None,True),
                                       (200,401,True),(200,429,True),(403,None,False),(200,205,False)):
            with self.subTest(http=http,code=code):
                session.request.return_value.status_code=http
                session.request.return_value.json.return_value={'code':code}
                with self.assertRaises(ExamError) as caught:api.paper('paper')
                self.assertEqual(isinstance(caught.exception,exam_api.ExamServiceError),global_error)

    def test_heartbeat_preserves_global_failure_type(self):
        self.assertTrue(hasattr(exam_api,'ExamServiceError'),'global errors need a distinct type')
        api=Mock()
        api.heartbeat.side_effect=exam_api.ExamServiceError('登录已失效')
        clock=AttemptClock(api,{'courseId':'c'},'exam','task',None,False)
        clock.stop_event=Mock()
        clock.stop_event.wait.return_value=False
        clock._loop()
        with self.assertRaises(exam_api.ExamServiceError):clock.check()
