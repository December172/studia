"""Offline wire-format contracts for Chat Completions and Responses."""
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from ai.client import AIClient, AIConfig, AIResponseError, AIServiceError, AITransientServiceError
from ai.runner import AnswerRunner
from tests.test_ai import FakeClock, question


ANSWER = '{"answer":"A","confidence":0.95,"explanation":"ok"}'


def response_payload(text=ANSWER):
    return {'id': 'resp_test', 'object': 'response', 'status': 'completed', 'error': None,
            'output': [{'id': 'msg_test', 'type': 'message', 'role': 'assistant',
                        'status': 'completed', 'content': [
                            {'type': 'output_text', 'text': text, 'annotations': []}]}]}


class ProtocolTests(unittest.TestCase):
    def client(self, payload, api_format='responses', status=200):
        session = Mock()
        session.post.return_value.status_code = status
        session.post.return_value.json.return_value = payload
        return AIClient(AIConfig('https://example.org/v1', 'model', 'synthetic-key',
                                 api_format=api_format), session), session

    def test_endpoint_accepts_base_or_matching_full_path_and_rejects_mismatch(self):
        for api_format, path in [('chat_completions', '/chat/completions'), ('responses', '/responses')]:
            for url in ['https://example.org/v1/', 'https://example.org/v1' + path]:
                config = AIConfig(url, 'm', 'k', api_format=api_format)
                self.assertEqual(config.endpoint, 'https://example.org/v1' + path)
        for url, api_format in [('https://example.org/v1/responses', 'chat_completions'),
                                ('https://example.org/v1/chat/completions', 'responses'),
                                ('https://example.org/v1', 'unsupported')]:
            with self.assertRaises(ValueError):
                AIConfig(url, 'm', 'k', api_format=api_format)

    def test_responses_sends_only_question_material_and_parses_answer(self):
        client, session = self.client(response_payload())
        result = client.answer({'type': '单选', 'title': 'test', 'options': [],
                                'platform_token': 'private', 'std_answer': 'old answer'})
        self.assertEqual(result, {'answer': 'A', 'confidence': .95, 'explanation': 'ok'})
        self.assertEqual(sorted(result), ['answer', 'confidence', 'explanation'])
        call = session.post.call_args
        self.assertEqual(call.args[0], 'https://example.org/v1/responses')
        body = call.kwargs['json']
        self.assertEqual(body['model'], 'model')
        self.assertTrue(body['instructions'])
        self.assertEqual(json.loads(body['input']), {'type': '单选', 'title': 'test', 'options': []})
        self.assertFalse(body['stream'])
        self.assertFalse(body['store'])
        self.assertNotIn('messages', body)
        self.assertFalse(call.kwargs['allow_redirects'])

    def test_chat_completions_keeps_existing_wire_format(self):
        payload = {'choices': [{'finish_reason': 'stop', 'message': {'content': ANSWER}}]}
        client, session = self.client(payload, 'chat_completions')
        client.check_connection()
        call = session.post.call_args
        self.assertEqual(call.args[0], 'https://example.org/v1/chat/completions')
        self.assertEqual([m['role'] for m in call.kwargs['json']['messages']], ['system', 'user'])
        self.assertNotIn('input', call.kwargs['json'])

    def test_responses_skips_reasoning_and_joins_text_blocks(self):
        payload = response_payload()
        payload['output'].insert(0, {'id': 'reason_test', 'type': 'reasoning', 'summary': []})
        payload['output'][1]['content'] = [
            {'type': 'output_text', 'text': ANSWER[:20], 'annotations': []},
            {'type': 'output_text', 'text': ANSWER[20:], 'annotations': []}]
        client, _ = self.client(payload)
        self.assertEqual(client.answer({'title': 'test'})['answer'], 'A')

    def test_responses_rejects_incomplete_failed_refusal_or_malformed_results(self):
        variants = [dict(response_payload(), status=status) for status in
                    ['incomplete', 'failed', 'in_progress', 'queued', 'cancelled', None]]
        variants += [dict(response_payload(), error={'message': 'private provider data'}),
                     dict(response_payload(), output=[]), {'output_text': ANSWER},
                     response_payload('bad JSON'), response_payload('{"answer":"A"}'),
                     response_payload('{"answer":"A","explanation":null}')]
        for content in [[{'type': 'refusal', 'refusal': 'no'}],
                        [{'type': 'output_text', 'text': None}], []]:
            payload = response_payload()
            payload['output'][0]['content'] = content
            variants.append(payload)
        payload = response_payload()
        payload['output'][0]['status'] = 'incomplete'
        variants.append(payload)
        for payload in variants:
            with self.subTest(payload=payload):
                client, _ = self.client(payload)
                with self.assertRaises(AIResponseError) as caught:
                    client.answer({'title': 'test'})
                self.assertNotIn('private provider data', str(caught.exception))

    def test_responses_accepts_a_valid_answer_without_usable_confidence_metadata(self):
        for text in ('{"answer":"A","explanation":"ok"}', '{"answer":"A","confidence":null,"explanation":"ok"}',
                     '{"answer":"A","confidence":"high","explanation":"ok"}',
                     '{"answer":"A","confidence":-1,"explanation":"ok"}',
                     '{"answer":"A","confidence":%s,"explanation":"ok"}' % 10 ** 400,
                     '{"answer":"A","confidence":-%s,"explanation":"ok"}' % 10 ** 400):
            with self.subTest(text=text[:40]):
                client, _ = self.client(response_payload(text))
                result = client.answer({'title': 'test'})
                self.assertEqual(result['answer'], 'A')
                self.assertEqual(result['explanation'], 'ok')
                self.assertNotIn('confidence', result)

    def test_chat_completions_accepts_a_valid_answer_without_usable_confidence_metadata(self):
        for text in ('{"answer":"A","explanation":"ok"}', '{"answer":"A","confidence":true,"explanation":"ok"}',
                     '{"answer":"A","confidence":NaN,"explanation":"ok"}',
                     '{"answer":"A","confidence":%s,"explanation":"ok"}' % 10 ** 400,
                     '{"answer":"A","confidence":-%s,"explanation":"ok"}' % 10 ** 400):
            with self.subTest(text=text[:40]):
                payload = {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]}
                client, _ = self.client(payload, 'chat_completions')
                result = client.answer({'title': 'test'})
                self.assertEqual(result['answer'], 'A')
                self.assertNotIn('confidence', result)

    def test_connection_test_accepts_answer_without_confidence_in_either_format(self):
        payload = response_payload('{"answer":"A","explanation":"ok"}')
        client, session = self.client(payload)
        client.check_connection()
        self.assertEqual(session.post.call_count, 1)
        chat = {'choices': [{'finish_reason': 'stop',
                             'message': {'content': '{"answer":"A","explanation":"ok"}'}}]}
        client, session = self.client(chat, 'chat_completions')
        client.check_connection()
        self.assertEqual(session.post.call_count, 1)
        prompt = session.post.call_args.kwargs['json']['messages'][0]['content']
        self.assertNotIn('confidence', prompt)
        self.assertIn('"answer"', prompt)

    def test_responses_preserves_service_error_and_network_retry_classification(self):
        for status, error in [(401, AIServiceError), (429, AITransientServiceError),
                              (503, AITransientServiceError)]:
            client, _ = self.client({}, status=status)
            with self.assertRaises(error):
                client.check_connection()
        client, session = self.client({})
        session.post.side_effect = requests.Timeout('secret')
        with self.assertRaises(AITransientServiceError) as caught:
            client.check_connection()
        self.assertNotIn('secret', str(caught.exception))

    def test_responses_retries_current_question_and_submits_valid_platform_answer(self):
        client, session = self.client(response_payload())
        session.post.return_value.json.side_effect = [response_payload('broken JSON'), response_payload()]
        api = Mock(user_id='user')
        api.records.return_value = []
        api.paper.return_value = {'questions': [question()],
                                  'taskExamRecord': {'id': 'task', 'status': 0}}
        api.verify_submitted.return_value = True
        with tempfile.TemporaryDirectory() as directory, patch('ai.runner.time.sleep'):
            runner = AnswerRunner(api, client, directory, auto_submit=True,
                                  output=Mock(), clock_factory=FakeClock)
            result = runner.run_paper({'id': 'ci', 'courseId': 'c'}, {'id': 'exam'}, 3)
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(session.post.call_count, 2)
        self.assertTrue(all(call.args[0].endswith('/responses') for call in session.post.call_args_list))
        api.submit.assert_called_once()
        self.assertEqual(api.submit.call_args.args[0]['taskExamProblemRecordList'][0]['answer'], '3')
