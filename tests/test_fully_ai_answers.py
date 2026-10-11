"""Every supported paper is answered by AI; no confidence gate and no manual input."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai.client import AIResponseError, AIServiceError, AITransientServiceError
from ai.runner import AnswerRunner
from AIMoocMain.exam_api import ExamError
from app import Application
from settings import SettingsStore
from tests.test_ai import FakeClock, question


class FullyAIAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.course = {"id": "ci", "courseId": "c"}
        self.item = {"id": "exam"}
        self.api = Mock(user_id="user")
        self.api.records.return_value = []
        self.api.verify_submitted.return_value = True
        self.api.paper.return_value = {"questions": [dict(question(qid=str(i)), title=f"题{i}") for i in range(3)],
                                       "taskExamRecord": {"id": "task", "status": 0}}
        self.good = {"answer": "A", "confidence": .95, "explanation": "ok"}
        self.ai = Mock()
        self.ai.config.model = "model"
        self.ai.answer.return_value = self.good
        sleep = patch("ai.runner.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def run_paper(self, submit=True, clock_factory=FakeClock):
        runner = AnswerRunner(self.api, self.ai, self.temp.name, auto_submit=submit,
                              clock_factory=clock_factory, output=Mock())
        return runner.run_paper(self.course, self.item, 3)

    def saved_report(self):
        return json.loads(next(Path(self.temp.name).rglob("*.json")).read_text(encoding="utf-8"))

    def test_low_confidence_answer_is_trusted_and_submitted_without_extra_requests(self):
        self.ai.answer.side_effect = [dict(self.good, confidence=.2), self.good, self.good]
        report = self.run_paper()
        self.assertEqual(report["status"], "submitted")
        self.assertEqual(self.ai.answer.call_count, 3)
        self.assertEqual(report["answers"][0]["answer"], "A")
        self.assertEqual(report["answers"][0]["confidence"], .2)
        self.assertNotIn("source", report["answers"][0])
        self.assertNotIn("error", report["answers"][0])
        entries = self.api.submit.call_args.args[0]["taskExamProblemRecordList"]
        self.assertEqual([entry["answer"] for entry in entries], ["3", "3", "3"])
        self.api.submit.assert_called_once()

    def test_zero_and_missing_confidence_metadata_never_block_a_valid_answer(self):
        for answer in (dict(self.good, confidence=0), {"answer": "A", "explanation": "ok"},
                       dict(self.good, confidence=1)):
            with self.subTest(answer=answer):
                self.api.submit.reset_mock()
                self.api.paper.return_value["taskExamRecord"]["id"] += "-next"
                self.ai.answer.reset_mock()
                self.ai.answer.side_effect = None
                self.ai.answer.return_value = answer
                self.assertEqual(self.run_paper()["status"], "submitted")
                self.assertEqual(self.ai.answer.call_count, 3)
                self.api.submit.assert_called_once()

    def test_legacy_confidence_preference_is_inert_and_report_files_stay_ai_only(self):
        self.ai.answer.return_value = dict(self.good, confidence=.1)
        self.assertEqual(self.run_paper(False)["status"], "preview")
        self.assertEqual(self.ai.answer.call_count, 3)
        saved = self.saved_report()
        self.assertNotIn("pending_manual", json.dumps(saved, ensure_ascii=False))
        self.assertTrue(all(row.get("source") != "manual" for row in saved["answers"]))
        markdown = next(Path(self.temp.name).rglob("*.md")).read_text(encoding="utf-8")
        self.assertIn("AI 答案", markdown)
        self.assertNotIn("人工答案", markdown)

    def test_invalid_answers_still_block_submission_from_a_local_report(self):
        for answer in ({"answer": None, "confidence": .99, "explanation": "缺少材料"},
                       {"answer": "Z", "confidence": .99, "explanation": "未知选项"}):
            with self.subTest(answer=answer):
                self.api.submit.reset_mock()
                self.ai.answer.return_value = answer
                report = self.run_paper()
                self.assertEqual(report["status"], "needs_review")
                self.assertIn("error", report["answers"][0])
                self.api.submit.assert_not_called()
                self.api.paper.return_value["taskExamRecord"]["id"] += "-next"

    def test_retry_exhaustion_records_failure_automatically_without_manual_input(self):
        self.ai.answer.side_effect = [self.good] + [AIResponseError("bad JSON") for _ in range(3)] + [self.good]
        report = self.run_paper()
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(self.ai.answer.call_count, 5)
        self.assertIn("重试 2 次", report["answers"][1]["error"])
        self.assertNotIn("record", report["answers"][1])
        self.assertEqual(len(report["answers"]), 3)
        self.assertTrue(all("record" in row for row in (report["answers"][0], report["answers"][2])))
        self.assertEqual(self.saved_report()["status"], "needs_review")
        self.api.submit.assert_not_called()

    def test_exhausted_transient_service_error_stops_the_queue_without_submitting(self):
        self.api.paper.return_value["questions"] = [question(qid=str(i)) for i in range(2)]
        self.ai.answer.side_effect = [AITransientServiceError("timeout") for _ in range(3)] + [self.good]
        report = self.run_paper(False)
        self.assertEqual(report["status"], "ai_error")
        self.assertEqual(self.ai.answer.call_count, 3)
        self.assertEqual(len(report["answers"]), 1)
        self.assertIn("timeout", report["answers"][0]["error"])
        self.api.submit.assert_not_called()

    def test_fatal_service_error_stops_without_submitting_incomplete_work(self):
        self.ai.answer.side_effect = AIServiceError("HTTP 401")
        self.assertEqual(self.run_paper()["status"], "ai_error")
        self.api.submit.assert_not_called()
        self.ai.answer.reset_mock(side_effect=True)
        self.ai.answer.side_effect = None
        self.ai.answer.return_value = self.good
        self.api.paper.return_value["taskExamRecord"]["id"] = "next-task"
        self.assertEqual(self.run_paper()["status"], "submitted")
        self.assertEqual(self.ai.answer.call_count, 3)

    def test_deadline_expiry_during_retry_wait_blocks_submission(self):
        clock = Mock(elapsed=30)
        clock.start.return_value = clock
        self.ai.answer.side_effect = [AIResponseError("bad JSON")]

        def expire(_):
            clock.check.side_effect = ExamError("时间已用尽")
        with patch("ai.runner.time.sleep", side_effect=expire):
            report = self.run_paper(clock_factory=lambda *args: clock)
        self.assertEqual(report["status"], "error")
        self.assertEqual(self.ai.answer.call_count, 1)
        self.assertLessEqual(len(report["answers"]), 1)
        self.api.submit.assert_not_called()

    def test_restart_regenerates_all_answers_after_a_failed_question(self):
        self.ai.answer.side_effect = [self.good] + [AIResponseError("bad JSON") for _ in range(3)] + [self.good]
        self.assertEqual(self.run_paper()["status"], "needs_review")
        self.api.submit.assert_not_called()
        self.ai.answer.reset_mock(side_effect=True)
        self.ai.answer.return_value = self.good
        self.api.paper.return_value["taskExamRecord"]["id"] = "next-task"
        report = self.run_paper()
        self.assertEqual(report["status"], "submitted")
        self.assertEqual(self.ai.answer.call_count, 3)
        self.assertEqual([call.args[0]["title"] for call in self.ai.answer.call_args_list], ["题0", "题1", "题2"])
        self.api.submit.assert_called_once()

    def test_changed_options_and_questions_still_regenerate_every_answer(self):
        self.run_paper(False)
        self.ai.answer.reset_mock()
        options = json.loads(self.api.paper.return_value["questions"][0]["dataJson"])
        options[0]["Content"] = "新选项"
        self.api.paper.return_value["questions"][0]["dataJson"] = json.dumps(options)
        self.api.paper.return_value["questions"][1]["title"] = "改过的题干"
        self.assertEqual(self.run_paper()["status"], "submitted")
        self.assertEqual(self.ai.answer.call_count, 3)
        self.assertEqual(self.ai.answer.call_args_list[1].args[0]["title"], "改过的题干")

    def test_new_attempt_does_not_reuse_previous_local_answers(self):
        self.assertEqual(self.run_paper(False)["status"], "preview")
        self.api.paper.return_value["taskExamRecord"]["id"] = "new-task"
        self.ai.answer.reset_mock()
        self.assertEqual(self.run_paper()["status"], "submitted")
        self.assertEqual(self.ai.answer.call_count, 3)

    def test_legacy_preview_or_paused_report_cannot_supply_answers(self):
        for status in ("preview", "paused"):
            with self.subTest(status=status):
                self.assertEqual(self.run_paper(False)["status"], "preview")
                path = next(Path(self.temp.name).rglob("*.json"))
                previous = json.loads(path.read_text(encoding="utf-8"))
                previous["status"] = status
                for answer in previous["answers"]:
                    answer.pop("question_fingerprint", None)
                path.write_text(json.dumps(previous), encoding="utf-8")
                self.ai.answer.reset_mock()
                self.assertEqual(self.run_paper()["status"], "submitted")
                self.assertEqual(self.ai.answer.call_count, 3)
                self.api.paper.return_value["taskExamRecord"]["id"] += "-next"

    def test_fresh_ai_answers_after_history_read_failure(self):
        self.assertEqual(self.run_paper(False)["status"], "preview")
        self.api.records.side_effect = ExamError("读取历史失败")
        self.assertEqual(self.run_paper()["status"], "error")
        self.api.records.side_effect = None
        self.api.paper.return_value["taskExamRecord"]["id"] = "new-task"
        self.ai.answer.reset_mock()
        self.assertEqual(self.run_paper()["status"], "submitted")
        self.assertEqual(self.ai.answer.call_count, 3)

    def test_new_subquestion_answer_uses_current_parent_context(self):
        self.api.paper.return_value["questions"] = [{"id": "parent", "title": "旧材料",
                                                   "questionSubList": [question(qid="child")]}]
        self.run_paper(False)
        self.api.paper.return_value["questions"][0]["title"] = "新材料"
        self.ai.answer.reset_mock()
        self.assertEqual(self.run_paper()["status"], "submitted")
        self.ai.answer.assert_called_once()

    def test_preview_then_submit_uses_fresh_answers_even_for_unchanged_questions(self):
        self.assertEqual(self.run_paper(False)["status"], "preview")
        self.ai.answer.reset_mock()
        self.ai.answer.return_value = dict(self.good, answer="B")
        self.assertEqual(self.run_paper()["status"], "submitted")
        self.assertEqual(self.ai.answer.call_count, 3)
        entries = self.api.submit.call_args.args[0]["taskExamProblemRecordList"]
        self.assertEqual([entry["answer"] for entry in entries], ["0", "0", "0"])

    def test_runner_exposes_no_manual_or_confidence_configuration(self):
        runner = AnswerRunner(self.api, self.ai, self.temp.name)
        for removed in ("manual_answer", "min_confidence"):
            self.assertFalse(hasattr(runner, removed), removed)


class FullyAIApplicationTests(unittest.TestCase):
    """The production entry points never request a confidence threshold."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SettingsStore(Path(self.temp.name))
        self.store.set("confidence", .99)
        from tests.test_course_flow import make_view
        self.view, self.output = make_view([])
        self.app = Application(self.view, self.store)
        self.course = {"id": "ci", "courseId": "c", "courseName": "课程"}
        self.selected = [(self.course, {"id": "exam", "name": "测验"}, 3)]
        self.api = Mock(user_id="user")
        self.api.records.return_value = []
        self.api.verify_submitted.return_value = True
        self.api.paper.return_value = {"questions": [question(qid="one")],
                                       "taskExamRecord": {"id": "task", "status": 0}}
        self.ai = Mock()
        self.ai.config.model = "test-model"
        self.ai.answer.return_value = {"answer": "A", "confidence": .05, "explanation": "unsure"}

    def execute(self, auto_submit=True):
        self.view.begin_operation()
        def factory(*args, **kwargs):
            return AnswerRunner(*args, **kwargs, output_dir=self.temp.name, clock_factory=FakeClock,
                                on_log=lambda message: None)
        with patch.object(self.app, "verify_ai", return_value=self.ai), \
                patch("app.AnswerRunner", side_effect=factory):
            return self.app.execute_ai(Mock(), self.selected, Mock(), auto_submit, api=self.api)

    def test_legacy_saved_confidence_cannot_block_a_low_confidence_valid_answer(self):
        reports = self.execute()
        self.assertEqual([report["status"] for report in reports], ["submitted"])
        self.api.submit.assert_called_once()
        self.assertEqual(self.store.get("confidence"), .99)
        self.assertNotIn("置信度", self.output.getvalue())

    def test_report_only_mode_still_skips_platform_submission(self):
        self.assertEqual([r["status"] for r in self.execute(False)], ["preview"])
        self.api.submit.assert_not_called()

    def test_configured_workers_still_bound_real_generation(self):
        self.store.save_concurrency(1, 8)
        for workers in (1, 8):
            with self.subTest(workers=workers):
                self.store.save_concurrency(workers, 8)
                runner = Mock(reused=False)
                runner.run_paper.return_value = {"status": "preview", "paper": "测验"}
                self.view.begin_operation()
                with patch.object(self.app, "verify_ai", return_value=self.ai), \
                        patch("app.AnswerRunner", return_value=runner) as factory:
                    self.app.execute_ai(Mock(), self.selected, Mock(), False, api=self.api)
                self.assertEqual(factory.call_args.kwargs["max_workers"], workers)


if __name__ == "__main__":
    unittest.main()
