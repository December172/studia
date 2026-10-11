import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import unquote

import requests

from ai.client import AIClient, AIConfig, AIError, prompt_config
from ai.questions import answer_record, prepare_question
from ai.runner import AnswerRunner, AttemptClock, report_path
from AIMoocMain.exam_api import ExamAPI, ExamError, checked_list


def question(kind=1, qid="paper-question-1"):
    return {"id": qid, "typeId": kind, "title": "哪项是乐器？",
            "dataJson": json.dumps([{"SortOrder": 3, "Content": "钢琴"}, {"SortOrder": 0, "Content": "桌子"}])}


class ClientTests(unittest.TestCase):
    def test_custom_menu_hidden_key_and_normalized_url(self):
        config = prompt_config(Mock(side_effect=["1", "https://example.org/v1/", "model-x"]), Mock(return_value="secret-key"), Mock())
        self.assertEqual(config.endpoint, "https://example.org/v1/chat/completions")
        self.assertNotIn("secret-key", repr(config))

    def test_deepseek_model_menu(self):
        config = prompt_config(Mock(side_effect=["2", "2"]), Mock(return_value="secret"), Mock())
        self.assertEqual(config.model, "deepseek-reasoner")
        self.assertEqual(config.base_url, "https://api.deepseek.com")

    def test_url_validation(self):
        for url in ("http://remote.example/v1", "https://u:p@example.org", "https://example.org?key=x"):
            with self.assertRaises(ValueError):
                AIConfig(url, "m", "k")
        self.assertEqual(AIConfig("http://localhost:8080/v1", "m", "k").endpoint, "http://localhost:8080/v1/chat/completions")

    def client(self, content, finish="stop"):
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {"choices": [{"finish_reason": finish, "message": {"content": content}}]}
        return AIClient(AIConfig("https://example.org/v1", "m", "secret"), session), session

    def test_only_question_material_sent(self):
        client, session = self.client('{"answer":"A","confidence":0.9,"explanation":"理由"}')
        result = client.answer({"title": "题干", "type": "单选", "options": [], "platform_token": "private", "std_answer": "B"})
        self.assertEqual(result["answer"], "A")
        body = session.post.call_args.kwargs["json"]
        self.assertNotIn("private", json.dumps(body))
        self.assertNotIn("std_answer", json.dumps(body))
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    def test_malformed_truncated_or_incomplete_results_are_rejected(self):
        for content, finish in [("oops", "stop"), ('{"answer":"A","explanation":""}', "length"),
                                ('{"answer":"A","confidence":0.9,"explanation":7}', "stop"),
                                ('{"confidence":0.9,"explanation":"理由"}', "stop"),
                                ('{"answer":"A"}', "stop"), ('[]', "stop")]:
            with self.subTest(content=content, finish=finish):
                client, _ = self.client(content, finish)
                with self.assertRaises(AIError):
                    client.answer({"title": "题干"})

    def test_valid_answer_ignores_any_legacy_confidence_field(self):
        for field in ('"confidence":0.1,', '"confidence":null,', '"confidence":"high",',
                      '"confidence":true,', '"confidence":NaN,', '"confidence":-1,',
                      '"confidence":2,', ''):
            with self.subTest(field=field):
                client, _ = self.client('{"answer":"A",%s"explanation":"理由"}' % field)
                self.assertEqual(client.answer({"title": "题干"})["answer"], "A")

    def test_only_a_usable_confidence_metadata_value_is_kept(self):
        for field in ('"confidence":"high",', '"confidence":null,', '"confidence":true,',
                      '"confidence":NaN,', '"confidence":-1,', '"confidence":2,', ''):
            with self.subTest(field=field):
                client, _ = self.client('{"answer":"A",%s"explanation":""}' % field)
                self.assertNotIn("confidence", client.answer({"title": "题干"}))
        for value in (0, 0.2, 1):
            with self.subTest(value=value):
                client, _ = self.client('{"answer":"A","confidence":%s,"explanation":""}' % value)
                self.assertEqual(client.answer({"title": "题干"})["confidence"], value)



    def test_network_errors_do_not_echo_credentials(self):
        client, session = self.client("")
        session.post.side_effect = requests.ConnectionError("secret")
        with self.assertRaises(AIError) as error:
            client.answer({"title": "题干"})
        self.assertNotIn("secret", str(error.exception))


class QuestionTests(unittest.TestCase):
    def test_choice_uses_original_sort_order_not_display_index(self):
        q = prepare_question(question(), 0)
        result = answer_record(q, "A")
        self.assertEqual(result["answer"], "3")
        self.assertEqual(json.loads(result["optionSort"])[0]["SortOrder"], 3)
        with self.assertRaises(ValueError):
            answer_record(q, "C")

    def test_multiple_judgement_blanks_and_text(self):
        self.assertEqual(answer_record(prepare_question(question(2), 0), ["B", "A"])["answer"], "0,3")
        self.assertEqual(answer_record(prepare_question(question(3), 0), False)["answer"], "0")
        self.assertEqual(json.loads(answer_record(prepare_question(question(4), 0), ["钢琴", "小提琴"])["answer"]), ["钢琴", "小提琴"])
        self.assertEqual(answer_record(prepare_question(question(6), 0), "说明")["answer"], "说明")
        for kind, answer in [(2, ["A", "A"]), (3, "0"), (4, ["一个空"]), (6, "")]:
            with self.assertRaises(ValueError):
                answer_record(prepare_question(question(kind), 0), answer)

    def test_subquestion_preserves_parent_and_child_identity(self):
        q = prepare_question(question(1, "child"), 2, {"id": "parent", "title": "材料"}, 1)
        result = answer_record(q, "B")
        self.assertEqual((result["paperId"], result["paperSubId"]), ("parent", "child"))
        self.assertEqual(json.loads(result["questionNo"]), [2, 1])

    def test_media_and_unknown_types_never_guessed(self):
        for raw in [question(12), dict(question(), title='<img src="x">辨认乐谱'), dict(question(), audioUrl="https://example.org/a")]:
            with self.assertRaises(ValueError):
                prepare_question(raw, 0)


class APITests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.headers = {}
        client = SimpleNamespace(user_id="self", default_base_url="https://example.org/prod-api", access_token="platform-secret")
        self.api = ExamAPI(client, self.session)
        self.session.request.return_value.status_code = 200
        self.session.request.return_value.json.return_value = {"code": 200, "data": {}}

    def test_submission_wire_encoding_matches_web_client(self):
        payload = {"examName": "音乐 '()!", "isLast": True, "taskExamProblemRecordList": [{"answer": "0,3"}]}
        self.api.submit(payload)
        args = self.session.request.call_args
        self.assertEqual(args.args[:2], ("POST", "https://example.org/prod-api/course/exam/record"))
        self.assertEqual(json.loads(unquote(base64.b64decode(args.kwargs["data"]).decode())), payload)
        self.assertFalse(args.kwargs["allow_redirects"])

    def test_ambiguous_network_failure_is_not_retried(self):
        self.session.request.side_effect = requests.Timeout("platform-secret")
        with self.assertRaises(ExamError) as error:
            self.api.submit({})
        self.assertEqual(self.session.request.call_count, 1)
        self.assertNotIn("platform-secret", str(error.exception))

    def test_business_failure_is_not_success(self):
        for value in ({"code": 500}, {"data": "ok"}, {"code": 205}, []):
            self.session.request.return_value.json.return_value = value
            with self.assertRaises(ExamError):
                self.api.submit({})

    def test_unknown_record_response_does_not_mean_empty(self):
        self.assertEqual(checked_list({"code": 200, "data": []}), [])
        self.assertEqual(checked_list({"rows": [{"status": 0}]}), [{"status": 0}])
        for payload in ({"code": 200, "data": {}}, {"code": 200}, {"data": [None]}):
            with self.assertRaises(ExamError):
                checked_list(payload)

    def test_paper_endpoint_accepts_current_platform_bare_paper(self):
        # Captured response: HTTP 200, questions + taskExamRecord, no code field.
        paper = {"name": "测试作业", "questions": [question()],
                 "taskExamRecord": {"id": "task", "status": "0"}}
        self.session.request.return_value.json.return_value = paper
        self.assertEqual(self.api.paper("exam"), paper)

    def test_bare_paper_exception_does_not_accept_error_or_write(self):
        paper = {"questions": [question()], "taskExamRecord": {"id": "task"}}
        for payload in ({**paper, "code": 403}, {"questions": []}, {"questions": [], "taskExamRecord": {}}):
            self.session.request.return_value.json.return_value = payload
            with self.assertRaises(ExamError):
                self.api.paper("exam")
        self.session.request.return_value.json.return_value = paper
        with self.assertRaises(ExamError):
            self.api.submit({})


class FakeClock:
    elapsed = 12
    def __init__(self, *args):
        pass
    def start(self):
        return self
    def check(self):
        pass
    def stop(self):
        pass


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.course = {"id": "course-info", "courseId": "course", "courseName": "测试音乐课"}
        self.item = {"id": "exam", "name": "测试测验"}
        self.api = Mock()
        self.api.user_id = "user"
        self.api.records.return_value = []
        self.api.paper.return_value = {"name": "测试测验", "questions": [question()], "surplusSecond": 300,
                                       "taskExamRecord": {"id": "task", "status": 0, "userId": "user"}}
        self.api.verify_submitted.return_value = True
        self.ai = Mock()
        self.ai.config.model = "test-model"
        self.ai.answer.return_value = {"answer": "A", "confidence": 0.95, "explanation": "说明"}

    def run_paper(self, submit=True):
        runner = AnswerRunner(self.api, self.ai, self.temp.name, auto_submit=submit, output=Mock(), clock_factory=FakeClock)
        return runner.run_paper(self.course, self.item, 3)

    def test_complete_answers_submit_once_and_verify(self):
        self.assertEqual(self.run_paper()["status"], "submitted")
        payload = self.api.submit.call_args.args[0]
        self.assertEqual(payload["taskExamProblemRecordList"][0]["answer"], "3")
        self.assertEqual(payload["examTime"], 12)
        self.api.submit.assert_called_once()
        self.api.verify_submitted.assert_called_once_with("exam", "task")

    def test_selecting_submitted_paper_starts_a_new_attempt_and_regenerates_answers(self):
        first = self.run_paper()
        self.api.records.return_value = [{"id": "task", "status": 2}]
        self.api.paper.return_value["taskExamRecord"]["id"] = "next-task"
        self.ai.answer.return_value = {"answer": "B", "confidence": .95, "explanation": "新一轮"}
        second = self.run_paper()
        self.assertEqual(second["status"], "submitted")
        self.assertEqual(second["task_id"], "next-task")
        self.assertEqual(self.ai.answer.call_count, 2)
        self.assertEqual([call.args[0]["id"] for call in self.api.submit.call_args_list], ['task', 'next-task'])
        self.assertEqual(second["answers"][0]["answer"], 'B')
        archived = list((Path(self.temp.name) / self.course['courseName'] / 'history').rglob('*.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text()), first)

    def test_preview_does_not_submit(self):
        self.assertEqual(self.run_paper(False)["status"], "preview")
        self.api.submit.assert_not_called()
        self.assertTrue(list(Path(self.temp.name).rglob("*.md")))

    def test_invalid_answers_and_ai_failure_never_submit(self):
        for result in [{"answer": None, "confidence": 0.9, "explanation": ""},
                       {"answer": "Z", "confidence": 0.99, "explanation": ""}]:
            self.ai.answer.return_value = result
            self.assertEqual(self.run_paper()["status"], "needs_review")
            self.api.paper.return_value["taskExamRecord"]["id"] += "-next"
        self.ai.answer.side_effect = AIError("AI failed")
        self.api.paper.return_value["taskExamRecord"]["id"] += "-next"
        self.assertEqual(self.run_paper()["status"], "needs_review")
        self.api.submit.assert_not_called()

    def test_low_confidence_answer_is_valid_and_submitted(self):
        self.ai.answer.return_value = {"answer": "A", "confidence": 0.2, "explanation": ""}
        self.assertEqual(self.run_paper()["status"], "submitted")
        self.api.submit.assert_called_once()

    def test_unsupported_question_blocks_whole_paper(self):
        self.api.paper.return_value["questions"].append(question(12, "file"))
        self.assertEqual(self.run_paper()["status"], "needs_review")
        self.api.submit.assert_not_called()

    def test_duplicate_id_blocks_whole_paper(self):
        self.api.paper.return_value["questions"].append(question())
        self.assertEqual(self.run_paper()["status"], "needs_review")
        self.api.submit.assert_not_called()

    def test_count_mismatch_and_wrong_owner_stop_before_ai(self):
        self.api.paper.return_value["questionCount"] = 2
        self.assertEqual(self.run_paper()["status"], "error")
        self.ai.answer.assert_not_called()
        del self.api.paper.return_value["questionCount"]
        self.api.paper.return_value["taskExamRecord"]["userId"] = "another-user"
        self.assertEqual(self.run_paper()["status"], "error")
        self.api.submit.assert_not_called()

    def test_existing_submission_does_not_block_new_paper(self):
        self.api.records.return_value = [{"id": "previous", "status": 2}]
        self.assertEqual(self.run_paper()["status"], "submitted")
        self.api.paper.assert_called_once_with('exam')
        self.api.submit.assert_called_once()

    def test_stale_platform_response_cannot_resubmit_same_completed_attempt(self):
        self.assertEqual(self.run_paper()['status'], 'submitted')
        # Both remote reads are stale, but the first POST was already verified.
        for _ in range(2):
            report = self.run_paper()
            self.assertEqual(report['status'], 'error')
            self.assertIn('平台未提供新的可作答记录', report['message'])
        self.api.submit.assert_called_once()
        self.api.paper.return_value['taskExamRecord']['id'] = 'new-task'
        self.assertEqual(self.run_paper()['status'], 'submitted')
        self.assertEqual([call.args[0]['id'] for call in self.api.submit.call_args_list], ['task', 'new-task'])

    def test_platform_returning_old_submitted_attempt_reports_reason_without_writing_it(self):
        self.api.records.return_value = [{"id": "task", "status": 2}]
        report = self.run_paper()
        self.assertEqual(report["status"], "error")
        self.assertIn('平台未提供新的可作答记录', report['message'])
        self.ai.answer.assert_not_called()
        self.api.submit.assert_not_called()

    def test_platform_unavailable_attempt_is_error_instead_of_silent_skip(self):
        self.api.paper.return_value['tjzt'] = 1
        report = self.run_paper()
        self.assertEqual(report['status'], 'error')
        self.assertIn('无法再次作答', report['message'])
        self.api.submit.assert_not_called()

    def test_old_history_does_not_hide_current_attempt_submitted_in_browser(self):
        self.api.records.side_effect = [[{'id': 'older', 'status': 2}],
                                       [{'id': 'older', 'status': 2}, {'taskId': 'task', 'status': 2}]]
        self.assertEqual(self.run_paper()['status'], 'error')
        self.api.paper.assert_called_once()
        self.ai.answer.assert_called_once()
        self.api.submit.assert_not_called()

    def test_browser_submission_during_ai_stops_write(self):
        self.api.records.side_effect = [[], [{"status": 2, "id": "task"}]]
        self.assertEqual(self.run_paper()["status"], "error")
        self.api.submit.assert_not_called()

    def test_submission_timeout_persisted_and_not_retried_on_restart(self):
        self.api.submit.side_effect = ExamError("timeout")
        self.assertEqual(self.run_paper()["status"], "submission_unknown")
        self.assertEqual(self.run_paper()["status"], "submission_unknown")
        self.api.submit.assert_called_once()

    def test_unverified_success_stays_unknown(self):
        self.api.verify_submitted.return_value = False
        self.assertEqual(self.run_paper()["status"], "submission_unknown")

    def test_reports_scoped_to_account(self):
        first = report_path(self.temp.name, "one", self.course, "exam")
        second = report_path(self.temp.name, "two", self.course, "exam")
        self.assertNotEqual(first, second)

    def test_real_clock_deadline(self):
        clock = AttemptClock(self.api, self.course, "exam", "task", -1, False)
        with self.assertRaises(ExamError):
            clock.check()

    def test_second_process_cannot_run_same_task(self):
        lock = report_path(self.temp.name, "user", self.course, "exam").with_suffix(".lock")
        lock.parent.mkdir(parents=True)
        lock.write_text("123")
        with self.assertRaises(ExamError):
            self.run_paper()
        self.api.paper.assert_not_called()

    def test_error_reason_is_visible_in_terminal(self):
        self.api.records.side_effect = ExamError("平台拒绝请求（code=None）")
        output = Mock()
        runner = AnswerRunner(self.api, self.ai, self.temp.name, output=output, clock_factory=FakeClock)
        result = runner.run_paper(self.course, self.item, 1)
        self.assertEqual(result["status"], "error")
        self.assertTrue(any("code=None" in str(call) for call in output.call_args_list))


class MainResultTests(unittest.TestCase):
    def test_failed_ai_task_is_not_reported_as_normal_success(self):
        import io
        import StartWork
        import theme
        from rich.console import Console
        from reporting import ProgressEvent
        from ui import TerminalUI
        from unittest.mock import patch
        stream = io.StringIO()
        view = TerminalUI(console=Console(file=stream, theme=theme.THEME, width=120))
        def fail():
            view.on_progress(ProgressEvent("result", "作业", status="error"))
        with patch("app.Application.run", side_effect=fail):
            StartWork.main(view)
        text = stream.getvalue()
        self.assertIn("成功 0 · 失败 1", text)
        self.assertNotIn("正常结束", text)


if __name__ == "__main__":
    unittest.main()
