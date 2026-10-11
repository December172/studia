"""Configuration presence, live verification and fatal service errors are distinct."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rich.console import Console
import theme
import ai.client as client_module
from ai.client import AIClient, AIConfig, AIError
from app import Application
from settings import SettingsStore
from ui import TerminalUI


class AIHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SettingsStore(Path(self.temp.name))
        self.config = AIConfig('https://example.org/v1', 'model', 'synthetic-key')
        self.store.save_ai(self.config)
        self.output = io.StringIO()
        self.view = TerminalUI(console=Console(file=self.output, theme=theme.THEME, width=120))
        self.app = Application(self.view, self.store)

    def test_401_is_authentication_failure_not_a_question_for_manual_review(self):
        session = Mock()
        session.post.return_value.status_code = 401
        self.assertTrue(hasattr(client_module, 'AIServiceError'))
        with self.assertRaises(client_module.AIServiceError) as caught:
            AIClient(self.config, session).answer({'title': 'example'})
        self.assertIn('鉴权失败', str(caught.exception))
        self.assertIn('401', str(caught.exception))
        self.assertNotIn('synthetic-key', str(caught.exception))

    def test_verification_uses_a_synthetic_question(self):
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {
            'content': '{"answer":"A","confidence":1,"explanation":"ok"}'}}]}
        client = AIClient(self.config, session)
        self.assertTrue(hasattr(client, 'check_connection'))
        client.check_connection()
        body = session.post.call_args.kwargs['json']
        self.assertIn('连接测试', body['messages'][1]['content'])
        self.assertEqual(body['model'], 'model')

    def test_saved_and_verified_states_are_distinct_and_key_change_invalidates(self):
        self.assertTrue(hasattr(self.app, 'ai_status_text'))
        self.assertIn('未测试', self.app.ai_status_text(self.config))
        client = Mock()
        self.app.verify_ai(self.config, client)
        self.assertIn('通过', Application(self.view, SettingsStore(self.store.directory)).ai_status_text(self.config))
        changed = AIConfig(self.config.base_url, self.config.model, 'another-key')
        self.store.save_ai(changed)
        self.assertIn('未测试', self.app.ai_status_text(changed))
        self.assertNotIn('synthetic-key', self.output.getvalue())

    def test_failed_preflight_does_not_open_any_platform_paper(self):
        session = Mock()
        session.post.return_value.status_code = 401
        api = Mock()
        selected = [({'id': 'ci', 'courseId': 'c'}, {'id': 'paper', 'name': '任务'}, 1)]
        with patch('app.AIClient', return_value=AIClient(self.config, session)):
            with self.assertRaises(AIError):
                self.app.execute_ai(Mock(), selected, self.config, False, api=api)
        api.paper.assert_not_called()
        self.assertIn('失败', self.app.ai_status_text(self.config))

    def test_service_failure_mid_paper_stops_without_manual_review_or_submission(self):
        from ai.runner import AnswerRunner
        from tests.test_ai import question, FakeClock
        api = Mock(user_id='user')
        api.records.return_value = []
        api.paper.return_value = {'questions': [question(qid='a'), question(qid='b')],
                                 'taskExamRecord': {'id': 'task', 'status': 0, 'userId': 'user'}}
        ai = Mock(config=self.config)
        ai.answer.side_effect = client_module.AIServiceError('AI 服务 HTTP 401 · 鉴权失败')
        runner = AnswerRunner(api, ai, self.temp.name, auto_submit=True, output=Mock(), clock_factory=FakeClock)
        result = runner.run_paper({'id': 'ci', 'courseId': 'c'}, {'id': 'exam'}, 1)
        self.assertEqual(result['status'], 'ai_error')
        self.assertEqual(ai.answer.call_count, 1)
        api.submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
