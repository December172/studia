"""Transient AI failures retry only the current question, never submission."""
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from ai.client import AIClient, AIConfig
from ai.runner import AnswerRunner
from AIMoocMain.exam_api import ExamError
from tests.test_ai import FakeClock, question


def response(status=200, content=None):
    result = Mock(status_code=status)
    result.json.return_value = {"choices": [{"finish_reason": "stop", "message": {
        "content": content if content is not None else
        '{"answer":"A","confidence":0.95,"explanation":"ok"}'}}]}
    return result


class QuestionRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = Mock(user_id="user")
        self.api.records.return_value = []
        self.api.verify_submitted.return_value = True
        self.api.paper.return_value = {"questions": [dict(question(qid=str(i)), title=f"题{i}")
                                                     for i in range(3)],
                                       "taskExamRecord": {"id": "task", "status": 0}}
        self.session = Mock()
        self.ai = AIClient(AIConfig("https://example.org/v1", "model", "secret"), self.session)
        self.output = Mock()
        sleep_patch = patch("ai.runner.time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def run_paper(self, clock_factory=FakeClock):
        runner = AnswerRunner(self.api, self.ai, self.temp.name, auto_submit=True,
                              output=self.output, clock_factory=clock_factory)
        return runner.run_paper({"id": "ci", "courseId": "c"}, {"id": "exam"}, 3)

    def test_malformed_answer_retries_only_failed_question_and_submits_once(self):
        self.session.post.side_effect = [response(), response(content="broken"), response(), response()]
        report = self.run_paper()
        self.assertEqual(report["status"], "submitted")
        titles = [json.loads(call.kwargs["json"]["messages"][1]["content"])["title"]
                  for call in self.session.post.call_args_list]
        self.assertEqual(titles, ["题0", "题1", "题1", "题2"])
        self.assertEqual(len(report["answers"]), 3)
        self.api.submit.assert_called_once()
        self.assertEqual(len(self.api.submit.call_args.args[0]["taskExamProblemRecordList"]), 3)
        self.assertTrue(any("重试 1/2" in call.args[0] for call in self.output.call_args_list))

    def test_transient_http_and_network_failures_recover(self):
        self.api.paper.return_value["questions"] = [question()]
        for index, failure in enumerate((requests.Timeout("secret"), requests.ConnectionError("secret"),
                        requests.exceptions.ChunkedEncodingError("secret"),
                        response(408), response(429), response(500), response(502), response(503), response(504))):
            with self.subTest(failure=failure):
                self.session.post.reset_mock(side_effect=True)
                self.session.post.side_effect = [failure, response()]
                # Each transport case uses its own paper, without cached answers.
                runner = AnswerRunner(self.api, self.ai, self.temp.name, output=self.output,
                                      clock_factory=FakeClock)
                report = runner.run_paper({"id": "ci", "courseId": "c"}, {"id": f"exam-{index}"}, 3)
                self.assertEqual(report["status"], "preview")
                self.assertEqual(self.session.post.call_count, 2)
                self.assertNotIn("secret", str(self.output.call_args_list))

    def test_persistent_network_failure_stops_after_two_retries(self):
        self.session.post.side_effect = requests.Timeout("secret")
        report = self.run_paper()
        self.assertEqual(report["status"], "ai_error")
        self.assertEqual(self.session.post.call_count, 3)
        self.assertEqual(sum(call.args[0] for call in self.sleep.call_args_list), 3)
        self.assertEqual(len(report["answers"]), 1)
        self.api.submit.assert_not_called()

    def test_persistent_format_failure_keeps_other_answers_and_does_not_submit(self):
        self.session.post.side_effect = [response(content="broken") for _ in range(3)] + [response(), response()]
        report = self.run_paper()
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(self.session.post.call_count, 5)
        self.assertEqual(len(report["answers"]), 3)
        self.assertIn("重试 2 次", report["answers"][0]["error"])
        self.assertTrue(all("record" in row for row in report["answers"][1:]))
        self.api.submit.assert_not_called()

    def test_configuration_errors_are_not_retried(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                self.session.post.reset_mock()
                self.session.post.return_value = response(status)
                self.assertEqual(self.run_paper()["status"], "ai_error")
                self.assertEqual(self.session.post.call_count, 1)
        self.sleep.assert_not_called()
        self.api.submit.assert_not_called()

    def test_low_confidence_is_not_retried_until_a_higher_score_appears(self):
        self.api.paper.return_value["questions"] = [question()]
        self.session.post.return_value = response(content='{"answer":"A","confidence":0.2,"explanation":"uncertain"}')
        self.assertEqual(self.run_paper()["status"], "needs_review")
        self.assertEqual(self.session.post.call_count, 1)
        self.sleep.assert_not_called()
        self.api.submit.assert_not_called()

    def test_expired_attempt_stops_before_retry(self):
        self.api.paper.return_value["questions"] = [question()]
        clock = Mock(elapsed=12)
        clock.start.return_value = clock
        self.session.post.side_effect = [response(content="broken"), response()]
        def expire(_):
            clock.check.side_effect = ExamError("时间已用尽")
        self.sleep.side_effect = expire
        self.assertEqual(self.run_paper(lambda *args: clock)["status"], "error")
        self.assertEqual(self.session.post.call_count, 1)
        self.api.submit.assert_not_called()

    def test_serial_retry_respects_provider_retry_after(self):
        self.api.paper.return_value['questions'] = [question()]
        limited = response(429)
        limited.headers = {'Retry-After': '5'}
        self.session.post.side_effect = [limited, response()]
        self.assertEqual(self.run_paper()['status'], 'submitted')
        self.assertEqual(sum(call.args[0] for call in self.sleep.call_args_list), 5)

    def test_manual_recovery_does_not_bypass_final_service_cooldown(self):
        self.api.paper.return_value['questions'] = [question(qid='first'), question(qid='next')]
        now, started = [100.0], []
        replies = []
        for delay in ('1', '2', '300'):
            limited = response(429)
            limited.headers = {'Retry-After': delay}
            replies.append(limited)
        replies.append(response())
        def request(*args, **kwargs):
            started.append(now[0])
            return replies.pop(0)
        self.session.post.side_effect = request
        self.sleep.side_effect = lambda delay: now.__setitem__(0, now[0] + delay)
        with patch('ai.runner.time.monotonic', side_effect=lambda: now[0]):
            runner = AnswerRunner(self.api, self.ai, self.temp.name, clock_factory=FakeClock,
                                  manual_answer=lambda *_: 'B')
            report = runner.run_paper({'id': 'ci', 'courseId': 'c'}, {'id': 'exam'}, 3)
        self.assertEqual(report['status'], 'preview')
        self.assertEqual(started, [100, 101, 103, 403])

    def test_long_retry_and_carried_cooldown_notice_deadline_promptly(self):
        for carried in (False, True):
            with self.subTest(carried=carried):
                now = [0.0]
                class Clock(FakeClock):
                    def check(self):
                        if now[0] >= 10:
                            raise ExamError('时间已用尽')
                limited = response(429)
                limited.headers = {'Retry-After': '300'}
                self.session.post.reset_mock(side_effect=True)
                self.session.post.return_value = limited
                self.sleep.side_effect = lambda delay: now.__setitem__(0, now[0] + delay)
                runner = AnswerRunner(self.api, self.ai, self.temp.name)
                if carried:
                    runner.service_cooldown_until = 300
                with patch('ai.runner.time.monotonic', side_effect=lambda: now[0]):
                    with self.assertRaises(ExamError):
                        runner.answer_question({'title': 'test'}, Clock())
                self.assertLessEqual(now[0], 10.25)
                self.assertEqual(runner.service_cooldown_until, 300)
                self.assertEqual(self.session.post.call_count, 0 if carried else 1)


if __name__ == "__main__":
    unittest.main()
