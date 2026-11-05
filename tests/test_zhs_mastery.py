"""Synthetic mastery protocol and queue checks; no platform login or network calls."""
import copy
import json
import tempfile
import time
import unittest
from unittest.mock import Mock

from ai.client import AIServiceError
from base.auth import PlatformUnavailable
from settings import SettingsStore
from web.server import HttpError, Studia, drain
from ZHSMain.answer import MASTERY_ROUNDS, ZHSAnswerRunner
from ZHSMain.api import CaptchaRequired, KEYS, MASTERY, MASTERY_PAGE, ZHSApi, ZHSApiError, decrypt
from ZHSMain.memory import AnswerMemory

COURSE = {"courseName": "离线练习课", "zhs": {"kind": "shared", "courseId": 101, "recruitId": 202}}
NODE = {"idHash": "node-a", "nodeName": "加法", "freeExam": 0}
QUESTION = {"questionId": "q1", "questionType": 1, "questionName": "1+1=?", "optionVos": [
    {"id": "o1", "name": "1", "sort": 0, "isCorrect": 0},
    {"id": "o2", "name": "2", "sort": 1, "isCorrect": 1}]}


class MasteryApiTest(unittest.TestCase):
    def setUp(self):
        self.api = ZHSApi()
        self.addCleanup(self.api.close)

    def test_reads_encrypt_the_site_parameters_and_submit_is_plain_json(self):
        self.api._request = Mock(return_value={"code": 200, "data": {"examId": "attempt", "questions": []}})
        self.api.mastery_start(101, 202, "node-a")
        args, kw = self.api._request.call_args
        self.assertEqual(args, ("GET", MASTERY + "/exam/start"))
        payload = json.loads(decrypt(kw["params"]["secretStr"], KEYS["video"]))
        self.assertEqual((payload["courseId"], payload["recruitId"], payload["knowledeId"]), (101, 202, "node-a"))
        self.assertEqual(kw["origin"], MASTERY_PAGE)
        answers = [{"questionId": "q1", "answer": "o2"}]
        self.api.mastery_submit(202, "node-a", "attempt", answers)
        args, kw = self.api._request.call_args
        self.assertEqual(args, ("POST", MASTERY + "/exam/submit"))
        self.assertEqual(kw["json_body"]["answerVos"], answers)
        self.assertEqual(kw["json_body"]["knowledgeId"], "node-a")
        self.assertNotIn("secretStr", kw["json_body"])

    def test_map_includes_nested_and_unattached_nodes_without_duplicates(self):
        self.api._mastery_get = Mock(return_value={"mapHashStr": "map", "mapStr": "public-fixture"})
        self.api._request = Mock(return_value={"code": 200, "data": {
            "nodeThemeRespVos": [{"children": [{"children": [NODE]}]}],
            "randomNodes": [NODE, dict(NODE, idHash="extra")]}})
        self.assertEqual([n["idHash"] for n in self.api.mastery_nodes(101, 202)], ["node-a", "extra"])

    def test_refused_or_unconfirmed_submit_is_not_success(self):
        for result, error in (({"code": -12}, CaptchaRequired),
                              ({"code": 421, "message": "无权限"}, ZHSApiError),
                              ({"code": 200, "data": False}, PlatformUnavailable)):
            with self.subTest(result=result), self.assertRaises(error):
                self.api._request = Mock(return_value=result)
                self.api.mastery_submit(202, "node-a", "attempt", [])

    def test_course_without_mastery_has_null_lists_not_a_broken_response(self):
        self.api._mastery_get = Mock(return_value={"mapHashStr": "map", "mapStr": "public-fixture"})
        self.api._request = Mock(return_value={"code": 200, "data": {
            "nodeThemeRespVos": None, "randomNodes": None}})
        self.assertEqual(self.api.mastery_nodes(101, 202), [])


class FakeMastery:
    def __init__(self):
        self.nodes = [copy.deepcopy(NODE)]
        self.progress = []
        self.started, self.submitted = [], []
        self.rate = None

    def mastery_nodes(self, *args):
        return self.nodes

    def mastery_progress(self, *args):
        return self.progress

    def mastery_start(self, course, recruit, knowledge):
        self.started.append(knowledge)
        return {"examId": "attempt-%d" % len(self.started), "questions": [copy.deepcopy(QUESTION)]}

    def mastery_submit(self, recruit, knowledge, exam, answers):
        self.submitted.append(answers)
        return True

    def mastery_result(self, *args):
        right = self.submitted[-1][0]["answer"] == "o2"
        return {"rate": self.rate if self.rate is not None else 100 if right else 0}

    def mastery_answers(self, *args):
        raw = copy.deepcopy(QUESTION)
        raw["userAnswerVo"] = {"isCorrect": 1 if self.submitted[-1][0]["answer"] == "o2" else 2}
        return {"questions": [raw]}


class MasteryRunnerTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeMastery()
        self.ai = Mock()
        self.ai.answer.return_value = {"answer": "B"}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runner = ZHSAnswerRunner(self.client, self.ai, auto_submit=True, sleep=lambda _: None, max_workers=1,
                                      memory=AnswerMemory(temp.name + "/answers.json"))

    def test_wrong_answer_feedback_is_reused_on_a_bounded_redo(self):
        self.ai.answer.return_value = {"answer": "A"}
        self.client.nodes += [dict(NODE, idHash="full"), dict(NODE, idHash="free", freeExam=1)]
        self.client.progress = [{"knowledgeId": "full", "rate": 100}]
        result = self.runner.run_mastery(COURSE)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(self.client.started, ["node-a", "node-a"])
        self.assertEqual([s[0]["answer"] for s in self.client.submitted], ["o1", "o2"])
        self.assertEqual(self.ai.answer.call_count, 1)
        self.assertIn("满分 2 个", result["message"])

    def test_all_mastered_and_exempt_nodes_do_not_open_tests_or_call_ai(self):
        self.client.progress = [{"knowledgeId": "node-a", "rate": 100}]
        self.client.nodes.append(dict(NODE, idHash="free", freeExam=1))
        self.runner.run_mastery(COURSE)
        self.assertEqual(self.client.started, [])
        self.ai.answer.assert_not_called()

    def test_checkbox_and_judgement_encode_platform_option_ids(self):
        self.client.mastery_start = Mock(return_value={"examId": "attempt", "questions": [
            dict(QUESTION, questionType=2), dict(QUESTION, questionId="judge", questionType=14)]})
        self.ai.answer.side_effect = lambda q, **kw: {"answer": ["A", "B"] if q["type"] == "多选题" else "B"}
        self.client.rate = 100
        self.runner.run_mastery(COURSE)
        self.assertEqual(self.client.submitted, [[{"questionId": "q1", "answer": "o1,o2"},
                                                   {"questionId": "judge", "answer": "o2"}]])

    def test_empty_map_and_no_question_code_are_explicit_skips(self):
        self.client.nodes = []
        self.assertEqual(self.runner.run_mastery(COURSE)["status"], "skipped")
        self.client.nodes = [NODE]
        self.client.mastery_start = Mock(side_effect=ZHSApiError(2004, "无题"))
        result = self.runner.run_mastery(COURSE)
        self.assertIn("免考/无题 1 个", result["message"])
        self.assertEqual(self.client.submitted, [])

    def test_partial_mastery_never_runs_forever_or_claims_success(self):
        self.client.rate = 50
        result = self.runner.run_mastery(COURSE)
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(self.client.submitted), MASTERY_ROUNDS)
        self.assertIn("未达标 1 个", result["message"])

    def test_unknown_submit_or_grade_stops_before_redo_or_next_node(self):
        for method, effect in (("mastery_submit", PlatformUnavailable("timeout")),
                               ("mastery_result", {})):
            with self.subTest(method=method):
                self.setUp()
                self.client.nodes.append(dict(NODE, idHash="next"))
                setattr(self.client, method, Mock(side_effect=effect) if isinstance(effect, Exception)
                        else Mock(return_value=effect))
                result = self.runner.run_mastery(COURSE)
                self.assertEqual(result["status"], "submission_unknown")
                self.assertTrue(result["stop_queue"])
                self.assertEqual(len(self.client.started), 1)

    def test_cancel_during_ai_does_not_submit(self):
        def answer(*args, **kwargs):
            self.runner.cancel_event.set()
            return {"answer": "B"}
        self.ai.answer.side_effect = answer
        with self.assertRaises(KeyboardInterrupt):
            self.runner.run_mastery(COURSE)
        self.assertEqual(self.client.submitted, [])

    def test_unsupported_questions_and_failed_ai_do_not_submit(self):
        paper = {"examId": "x", "questions": [dict(QUESTION, questionType=3)]}
        self.client.mastery_start = Mock(return_value=paper)
        self.assertEqual(self.runner.run_mastery(COURSE)["status"], "error")
        self.ai.answer.assert_not_called()
        self.client.mastery_start = Mock(return_value={"examId": "x", "questions": [QUESTION]})
        self.ai.answer.side_effect = AIServiceError("offline provider failure")
        self.assertEqual(self.runner.run_mastery(COURSE)["status"], "ai_error")
        self.assertEqual(self.client.submitted, [])

    def test_preview_and_captcha_do_not_trigger_automatic_submissions(self):
        self.runner.auto_submit = False
        self.assertEqual(self.runner.run_mastery(COURSE)["status"], "preview")
        self.assertEqual(self.client.submitted, [])
        self.runner.auto_submit = True
        self.client.mastery_start = Mock(side_effect=CaptchaRequired("验证"))
        with self.assertRaises(CaptchaRequired):
            self.runner.run_mastery(COURSE)


class MasteryWebTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = Studia(SettingsStore(temp.name))
        self.app.platforms["zhs"].rows = [COURSE]
        self.app.platforms["zhs"].client = FakeMastery()
        self.app._ai = Mock(return_value=Mock(answer=Mock(return_value={"answer": "B"})))

    def test_real_job_thread_runs_for_a_course_with_no_mastery_grade_share(self):
        self.app.start_job({"platform": "zhs", "type": "mastery", "course": "shared-101"})
        drain(5)
        self.assertFalse(self.app.jobs)
        self.assertEqual(self.app.history[0]["state"], "done")
        self.assertIn("满分 1 个", self.app.history[0]["summary"])
        self.app._ai.return_value.session.close.assert_called_once()

    def test_mastery_conflicts_with_answering_and_never_auto_resumes(self):
        self.app.jobs["busy"] = {"platform": "zhs", "course": "shared-101", "kind": "answer", "title": "AI 答题"}
        with self.assertRaises(HttpError):
            self.app.start_job({"platform": "zhs", "type": "mastery", "course": "shared-101"})
        self.app.jobs.clear()
        self.app._saved = Mock(return_value={"saved_at": time.time(), "active": [
            {"platform": "zhs", "type": "mastery", "course": "shared-101", "course_name": "练习"}]})
        self.app.start_job = Mock()
        self.app.resume()
        self.app.start_job.assert_not_called()
        self.assertEqual(self.app.history[0]["state"], "stopped")

    def test_ai_courses_are_not_misclassified_as_shared_mastery(self):
        self.app.platforms["zhs"].rows = [{"zhs": {"kind": "ai", "courseId": 101}}]
        with self.assertRaises(HttpError):
            self.app.start_job({"platform": "zhs", "type": "mastery", "course": "ai-101"})


if __name__ == "__main__":
    unittest.main()
