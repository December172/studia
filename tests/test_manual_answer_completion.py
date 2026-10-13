"""Manual completion: reuse valid answers, ask the human only for the rest, confirm before posting."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai.client import AIClient, AIConfig, AIServiceError
from ai.questions import answer_record, model_question, prepare_question, question_fingerprint
from ai.runner import AnswerRunner, report_path
from AIMoocMain.exam_api import ExamError, ExamServiceError
from app import Application
from rich.console import Console
from settings import SettingsStore
from tests.test_ai import FakeClock, question
from ui import TerminalUI


class RunnerMixin:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.course = {"id": "ci", "courseId": "c", "courseName": "课程"}
        self.item = {"id": "exam", "name": "测验"}
        self.api = Mock(user_id="user")
        self.api.records.return_value = []
        self.api.verify_submitted.return_value = True
        self.paper = [self.q(i) for i in range(20)]
        self.api.paper.return_value = {"questions": self.paper,
                                       "taskExamRecord": {"id": "task", "status": 0}}
        self.ai = Mock()
        self.ai.config.model = "model"
        self.ai.answer.return_value = {"answer": "A", "confidence": .9, "explanation": "ok"}
        sleep = patch("ai.runner.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    @staticmethod
    def q(i):
        return dict(question(qid=str(i)), title=f"题{i}")

    def run_paper(self, submit=True, **options):
        runner = AnswerRunner(self.api, self.ai, self.temp.name, auto_submit=submit,
                              clock_factory=FakeClock, output=Mock(), **options)
        return runner.run_paper(self.course, self.item, 3)

    def saved_report(self):
        return json.loads(next(Path(self.temp.name).rglob("*.json")).read_text(encoding="utf-8"))

    def write_previous(self, rows, user="user", task_id="task", exam_id="exam", status="needs_review", category=3):
        path = report_path(self.temp.name, user, self.course, exam_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"paper": "测验", "exam_id": exam_id, "category": category, "status": status,
                                    "task_id": task_id, "answers": rows}, ensure_ascii=False), encoding="utf-8")

    def row(self, position, title=None, answer="A", fingerprint=True, source="ai"):
        prepared = prepare_question(dict(question(qid=str(position)), title=title or f"题{position}"), position)
        item = {"title": prepared["title"], "question_number": position + 1,
                "options": model_question(prepared)["options"],
                "answer": answer, "record": answer_record(prepared, answer), "source": source}
        if fingerprint:
            from ai.questions import question_fingerprint
            item["question_fingerprint"] = question_fingerprint(prepared)
        else:
            item["record"]["paperId"] = str(position)
        return item


class ReuseTests(RunnerMixin, unittest.TestCase):
    """Legacy rows carry no fingerprint, so they must be verified against the fresh paper."""

    def test_unchanged_questions_are_reused_and_only_the_failed_one_is_asked_again(self):
        self.write_previous([self.row(i) for i in range(19)] + [
            {"title": "题19", "question_number": 20, "error": "AI 未提供答案"}])
        self.ai.answer.return_value = {"answer": None, "confidence": .9, "explanation": "缺少材料"}
        prompts = []
        report = self.run_paper(manual_answer=lambda q, reason: prompts.append((q, reason)) or "B",
                                confirm_submit=lambda *args: True)
        self.assertEqual(self.ai.answer.call_count, 1)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0][0]["title"], "题19")
        self.assertTrue(prompts[0][1])
        self.assertEqual(prompts[0][1], "AI 未提供答案")
        entries = self.api.submit.call_args.args[0]["taskExamProblemRecordList"]
        self.assertEqual(len(entries), 20)
        self.api.submit.assert_called_once()
        self.assertEqual([row["source"] for row in report["answers"]], ["ai"] * 19 + ["manual"])

    def test_changed_title_or_option_regenerates_only_that_question(self):
        # Fingerprinted rows survive until their own content changes.
        self.paper = [self.q(i) for i in range(3)]
        self.api.paper.return_value["questions"] = self.paper
        self.write_previous([self.row(i) for i in range(3)])
        options = json.loads(self.paper[1]["dataJson"])
        options[0]["Content"] = "改过的选项"
        self.paper[1]["dataJson"] = json.dumps(options)
        self.paper[2]["title"] = "改过的题干"
        report = self.run_paper(False)
        self.assertEqual(self.ai.answer.call_count, 2)
        self.assertEqual(sorted(call.args[0]["title"] for call in self.ai.answer.call_args_list),
                         ["改过的题干", "题1"])
        self.assertEqual(report["answers"][0]["title"], "题0")

    def test_changed_blank_count_regenerates_that_question(self):
        self.paper = [dict(question(4, "fill"), title="填空")]
        self.api.paper.return_value["questions"] = self.paper
        self.ai.answer.return_value = {"answer": ["钢琴", "小提琴"], "confidence": .9, "explanation": ""}
        self.assertEqual(self.run_paper(False)["status"], "preview")
        self.ai.answer.reset_mock()
        self.paper[0]["answerNum"] = 3
        self.ai.answer.return_value = {"answer": ["a", "b", "c"], "confidence": .9, "explanation": ""}
        self.assertEqual(self.run_paper(False)["status"], "preview")
        self.assertEqual(self.ai.answer.call_count, 1)

    def test_legacy_rows_without_a_fingerprint_are_reused_only_when_verifiable(self):
        legacy = self.row(0, fingerprint=False)
        self.write_previous([legacy])
        self.paper = [self.q(0)]
        self.api.paper.return_value["questions"] = self.paper
        report = self.run_paper(False)
        self.assertEqual(self.ai.answer.call_count, 0)
        self.assertEqual(report["answers"][0]["source"], "ai")
        self.assertEqual(report["answers"][0]["answer"], "A")
        # A changed title or option list is not the same question.
        for change in ("title", "options"):
            with self.subTest(change=change):
                self.paper[0]["title"] = "改过的题干" if change == "title" else self.paper[0]["title"]
                if change == "options":
                    options = json.loads(self.paper[0]["dataJson"])
                    options[0]["Content"] = "另一个选项"
                    self.paper[0]["dataJson"] = json.dumps(options)
                self.ai.answer.reset_mock()
                self.run_paper(False)
                self.assertEqual(self.ai.answer.call_count, 1)

    def test_legacy_row_with_another_paper_id_or_invalid_answer_is_not_reused(self):
        self.paper = [self.q(0)]
        self.api.paper.return_value["questions"] = self.paper
        legacy = self.row(0, fingerprint=False)
        legacy["record"]["paperId"] = "another-paper"
        self.write_previous([legacy])
        self.run_paper(False)
        self.assertEqual(self.ai.answer.call_count, 1)
        self.ai.answer.reset_mock()
        legacy = self.row(0, fingerprint=False)
        legacy["record"]["answer"] = "999"
        legacy["answer"] = "Z"
        self.write_previous([legacy])
        self.run_paper(False)
        self.assertEqual(self.ai.answer.call_count, 1)

    def test_a_legacy_row_is_never_reused_for_another_category(self):
        self.paper = [self.q(0)]
        self.api.paper.return_value["questions"] = self.paper
        self.write_previous([self.row(0, fingerprint=False)], category=1)
        self.run_paper(False)
        self.assertEqual(self.ai.answer.call_count, 1)

    def test_a_legacy_row_accepts_reformatted_option_sort_whitespace(self):
        self.paper = [self.q(0)]
        self.api.paper.return_value["questions"] = self.paper
        legacy = self.row(0, fingerprint=False)
        legacy["record"]["optionSort"] = json.dumps(json.loads(legacy["record"]["optionSort"]), indent=2)
        self.write_previous([legacy])
        report = self.run_paper(False)
        self.assertEqual(self.ai.answer.call_count, 0)
        self.assertEqual(report["answers"][0]["answer"], "A")

    def test_a_legacy_row_without_proof_of_identity_is_not_reused(self):
        self.paper = [self.q(0)]
        self.api.paper.return_value["questions"] = self.paper

        def other_options(row):
            raw = json.loads(row["record"]["optionSort"])
            raw[0] = {**raw[0], "Content": "另一个选项"}
            row["record"]["optionSort"] = json.dumps(raw, separators=(",", ":"))

        cases = {
            "no sort": lambda row: row["record"].pop("optionSort"),
            "malformed sort": lambda row: row["record"].update(optionSort="not json"),
            "sort of other options": other_options,
            "other question number": lambda row: row["record"].update(questionNo=99),
            "child question": lambda row: row["record"].update(paperSubId="child"),
            "list answer for single choice": lambda row: row.update(answer=["A"]),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                legacy = self.row(0, fingerprint=False)
                mutate(legacy)
                self.write_previous([legacy])
                self.ai.answer.reset_mock()
                self.run_paper(False)
                self.assertEqual(self.ai.answer.call_count, 1)

    def test_a_legacy_row_is_only_reused_for_simple_choice_questions(self):
        # A non-choice legacy row can carry a self-consistent record; it is still not reusable.
        self.paper = [dict(question(3), title="判断")]
        self.api.paper.return_value["questions"] = self.paper
        q = prepare_question(self.paper[0], 0)
        legacy = {"title": q["title"], "question_number": 1, "options": [],
                  "answer": True, "record": {"questionNo": 0, "paperId": q["paper_id"], "answer": "1"}}
        self.write_previous([legacy])
        self.run_paper(False)
        self.assertEqual(self.ai.answer.call_count, 1)

        self.addCleanup(lambda: [p.unlink() for p in Path(self.temp.name).rglob("*.json")])
        rows = [self.row(i, fingerprint=False) for i in range(3)]
        self.paper = [self.q(i) for i in range(3)]
        self.api.paper.return_value["questions"] = self.paper
        for kwargs in ({"task_id": "older-task"}, {"user": "other-user"}, {"exam_id": "other-exam"}):
            with self.subTest(**kwargs):
                for existing in Path(self.temp.name).rglob("*.json"):
                    existing.unlink()
                self.write_previous(rows, **kwargs)
                self.ai.answer.reset_mock()
                self.run_paper(False)
                self.assertEqual(self.ai.answer.call_count, 3)

    def test_a_legacy_report_without_source_is_labelled_ai_not_manual(self):
        self.paper = [self.q(0)]
        self.api.paper.return_value["questions"] = self.paper
        row = self.row(0, fingerprint=False)
        row.pop("source")
        self.write_previous([row], status="preview")
        report = self.run_paper(False)
        self.assertEqual(report["answers"][0]["source"], "ai")
        markdown = next(Path(self.temp.name).rglob("*.md")).read_text(encoding="utf-8")
        self.assertIn("AI 答案：", markdown)
        self.assertNotIn("人工答案：", markdown)


class ManualCompletionTests(RunnerMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.paper = [self.q(i) for i in range(3)]
        self.api.paper.return_value["questions"] = self.paper

    def fail_second(self):
        self.ai.answer.side_effect = [{"answer": "A", "confidence": .9, "explanation": ""},
                                      {"answer": None, "confidence": .9, "explanation": "缺少材料"},
                                      {"answer": "B", "confidence": .9, "explanation": ""}]

    def test_human_answer_completes_the_paper_and_posts_every_record_after_confirmation(self):
        self.fail_second()
        confirmations = []
        prompts = []
        report = self.run_paper(manual_answer=lambda q, reason: prompts.append((q, reason)) or "B",
                                confirm_submit=lambda paper, done, total: confirmations.append(
                                    (paper, done, total)) or True)
        self.assertEqual(report["status"], "submitted")
        self.assertEqual(confirmations, [("测验", 3, 3)])
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0][0]["options"][0]["label"], "A")
        self.assertEqual([row["source"] for row in report["answers"]], ["ai", "manual", "ai"])
        self.assertEqual(report["answers"][1]["answer"], "B")
        entries = self.api.submit.call_args.args[0]["taskExamProblemRecordList"]
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[1]["answer"], "0")
        self.api.submit.assert_called_once()
        self.assertNotIn("pending_manual", self.saved_report())
        markdown = next(Path(self.temp.name).rglob("*.md")).read_text(encoding="utf-8")
        self.assertIn("人工答案：", markdown)
        self.assertIn("AI 答案：", markdown)
        self.assertIn("人工补答", markdown)

    def test_a_callback_exception_still_leaves_the_complete_preview_on_disk(self):
        self.fail_second()

        def confirm(*args):
            saved = self.saved_report()
            self.assertEqual(saved["status"], "preview")
            self.assertNotIn("pending_manual", saved)
            self.assertEqual([row["source"] for row in saved["answers"]], ["ai", "manual", "ai"])
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_paper(manual_answer=lambda q, reason: "B", confirm_submit=confirm)
        self.api.submit.assert_not_called()
        saved = self.saved_report()
        self.assertEqual(saved["status"], "preview")
        self.assertNotIn("pending_manual", saved)
        self.assertEqual(saved["answers"][1]["source"], "manual")
        self.assertEqual(saved["answers"][1]["answer"], "B")

    def test_declining_confirmation_saves_answers_without_posting(self):
        self.fail_second()
        report = self.run_paper(manual_answer=lambda q, reason: "B",
                                confirm_submit=lambda *args: False)
        self.assertEqual(report["status"], "preview")
        self.api.submit.assert_not_called()
        saved = self.saved_report()
        self.assertEqual(saved["answers"][1]["source"], "manual")
        self.assertEqual(saved["answers"][1]["answer"], "B")

    def test_declining_then_resuming_posts_once_reusing_both_ai_and_manual_answers(self):
        self.fail_second()
        self.run_paper(manual_answer=lambda q, reason: "B", confirm_submit=lambda *args: False)
        self.ai.answer.reset_mock()
        self.ai.answer.side_effect = None
        self.ai.answer.return_value = {"answer": "A", "confidence": .9, "explanation": ""}
        prompts = []
        report = self.run_paper(manual_answer=lambda q, reason: prompts.append(reason) or "B",
                                confirm_submit=lambda *args: True)
        self.assertEqual(report["status"], "submitted")
        self.assertEqual(self.ai.answer.call_count, 0)
        self.assertEqual(prompts, [])
        entries = self.api.submit.call_args.args[0]["taskExamProblemRecordList"]
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[1]["answer"], "0")
        self.api.submit.assert_called_once()

    def test_leaving_the_prompt_pauses_and_keeps_the_draft_answerable(self):
        self.fail_second()
        events = []
        report = self.run_paper(manual_answer=lambda q, reason: None,
                                confirm_submit=lambda *args: events.append("confirm") or True)
        self.assertEqual(report["status"], "paused")
        self.assertEqual(events, [])
        self.api.submit.assert_not_called()
        saved = self.saved_report()
        self.assertEqual(saved["status"], "paused")
        self.assertEqual([row["question_number"] for row in saved["pending_manual"]], [2])
        self.assertEqual(saved["pending_manual"][0]["title"], "题1")
        self.assertEqual(saved["pending_manual"][0]["reason"], "AI 未提供答案")
        self.assertEqual(saved["answers"][0]["source"], "ai")
        self.assertNotIn("source", saved["answers"][1])
        self.assertEqual(saved["answers"][2]["source"], "ai")

    def test_eof_and_keyboard_interrupt_checkpoint_without_submitting(self):
        for stop in (EOFError(), KeyboardInterrupt()):
            with self.subTest(stop=type(stop).__name__):
                self.fail_second()
                with self.assertRaises(type(stop)):
                    self.run_paper(manual_answer=Mock(side_effect=stop), confirm_submit=lambda *args: True)
                self.api.submit.assert_not_called()
                self.assertEqual(self.saved_report()["status"], "paused")
                self.assertIn("pending_manual", self.saved_report())
                self.api.paper.return_value["taskExamRecord"]["id"] += "-next"

    def test_a_service_failure_never_prompts_and_stops_the_paper(self):
        self.ai.answer.side_effect = AIServiceError("HTTP 401")
        manual = Mock()
        report = self.run_paper(manual_answer=manual, confirm_submit=lambda *args: True)
        self.assertEqual(report["status"], "ai_error")
        manual.assert_not_called()
        self.api.submit.assert_not_called()

    def test_unsupported_questions_are_never_prompted_and_block_submission(self):
        self.paper.append(question(12, "file"))
        manual = Mock()
        report = self.run_paper(manual_answer=manual, confirm_submit=lambda *args: True)
        self.assertEqual(report["status"], "needs_review")
        manual.assert_not_called()
        self.api.submit.assert_not_called()

    def test_an_invalid_human_answer_is_reported_and_never_submitted(self):
        self.fail_second()
        report = self.run_paper(manual_answer=lambda q, reason: "Z", confirm_submit=lambda *args: True)
        self.assertEqual(report["status"], "needs_review")
        self.assertIn("未知选项", report["answers"][1]["error"])
        self.api.submit.assert_not_called()

    def test_a_single_unanswered_question_pauses_instead_of_posting_a_partial_paper(self):
        self.paper = [self.q(0), self.q(1)]
        self.api.paper.return_value["questions"] = self.paper
        self.ai.answer.side_effect = [{"answer": "A", "confidence": .9, "explanation": ""},
                                      {"answer": None, "confidence": .9, "explanation": ""}]
        report = self.run_paper(manual_answer=lambda q, reason: None, confirm_submit=lambda *args: True)
        self.assertEqual(report["status"], "paused")
        self.api.submit.assert_not_called()
        self.assertEqual(len(self.saved_report()["answers"]), 2)

    def test_a_deadline_reached_while_the_human_waits_blocks_submission(self):
        self.fail_second()
        clock = Mock(elapsed=30)
        clock.start.return_value = clock

        def expire(*_):
            raise ExamError("试卷剩余时间已用尽，停止交卷；请在网页核对状态")
        clock.check.side_effect = expire
        runner = AnswerRunner(self.api, self.ai, self.temp.name, auto_submit=True, output=Mock(),
                              clock_factory=lambda *args: clock, manual_answer=lambda q, reason: "B",
                              confirm_submit=lambda *args: True)
        report = runner.run_paper(self.course, self.item, 3)
        self.assertEqual(report["status"], "error")
        self.api.submit.assert_not_called()

    def test_a_pure_ai_paper_never_asks_for_confirmation(self):
        confirm = Mock()
        report = self.run_paper(confirm_submit=confirm)
        self.assertEqual(report["status"], "submitted")
        confirm.assert_not_called()
        self.api.submit.assert_called_once()


class RunnerCheckpointTests(RunnerMixin, unittest.TestCase):
    """A setup failure must not replace the previous complete report on disk."""

    def prepare_previous(self, size=19, status="preview"):
        self.rows = [self.row(i) for i in range(size)]
        self.write_previous(self.rows, status=status)
        self.path = report_path(self.temp.name, "user", self.course, "exam")
        self.before = self.path.read_bytes()

    def assert_previous_preserved(self, run):
        run()
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertFalse(self.api.submit.called)

    def test_a_failed_platform_read_never_erases_the_previous_report(self):
        for label, source, error in (
                ("records service error", "records", ExamServiceError("平台登录已失效")),
                ("paper error", "paper", ExamError("平台拒绝请求"))):
            with self.subTest(failure=label):
                self.setUp()
                self.prepare_previous()
                getattr(self.api, source).side_effect = error
                self.assert_previous_preserved(lambda: self.run_paper(False))

    def test_exhausted_or_invalid_questions_never_erase_the_previous_report(self):
        setup_failures = {
            "no remaining time": lambda: self.api.paper.return_value.update(surplusSecond=0),
            "malformed child list": lambda: self.api.paper.return_value["questions"][0].update(questionSubList=5),
        }
        for label, arrange in setup_failures.items():
            with self.subTest(failure=label):
                self.setUp()
                self.prepare_previous()
                arrange()
                self.assert_previous_preserved(lambda: self.run_paper(False))

    def test_a_later_healthy_retry_reuses_the_complete_report_without_ai(self):
        self.prepare_previous()
        # A healthy retry of the same paper: every question already has a valid answer.
        self.api.paper.return_value["questions"] = [self.q(i) for i in range(19)]
        report = self.run_paper(False)
        self.assertEqual(report["status"], "preview")
        self.ai.answer.assert_not_called()
        self.assertNotEqual(self.path.read_bytes(), self.before)
        self.assertEqual([row["source"] for row in report["answers"]], ["ai"] * 19)

    def test_a_new_paper_archives_the_previous_complete_report_before_replacing_it(self):
        self.prepare_previous()
        self.api.paper.return_value["taskExamRecord"]["id"] = "next-task"
        report = self.run_paper(False)
        self.assertEqual(report["status"], "preview")
        self.assertEqual(report["task_id"], "next-task")
        archived = list((Path(self.temp.name) / self.course["courseName"] / "history").rglob("*.json"))
        self.assertEqual(len(archived), 1)
        kept = json.loads(archived[0].read_text())
        self.assertEqual((kept["task_id"], len(kept["answers"])), ("task", 19))
        self.assertNotEqual(self.path.read_bytes(), self.before)


class ConfirmCancelTests(unittest.TestCase):
    """A cancelled confirmation must not silently start the next queued paper."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.course = {"id": "ci", "courseId": "c", "courseName": "课程"}
        self.selected = [(self.course, {"id": exam_id, "name": exam_id}, 3) for exam_id in ("first", "second")]
        self.api = Mock(user_id="user")
        self.ai = Mock()
        self.ai.config.model = "model"
        sleep = patch("ai.runner.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def reset_paper(self):
        self.api.reset_mock()
        self.api.records.return_value = []
        self.api.verify_submitted.return_value = True
        self.api.paper.side_effect = lambda exam_id: {
            "questions": [dict(question(qid=exam_id), title=exam_id)],
            "taskExamRecord": {"id": "task-" + exam_id, "status": 0}}
        self.ai.answer.side_effect = [{"answer": None, "confidence": .9, "explanation": "缺少材料"},
                                      {"answer": "A", "confidence": .9, "explanation": "ok"}]

    def make_view(self, confirm_inputs):
        inputs = iter(confirm_inputs)

        def feed():
            value = next(inputs)
            if isinstance(value, BaseException):
                raise value
            return value
        view = TerminalUI(console=Console(file=io.StringIO(), width=100, highlight=False), input_fn=feed)
        view.begin_operation()
        return view

    def execute(self, view):
        app = Application(view, SettingsStore(Path(self.temp.name) / "settings"))

        def factory(*args, **kwargs):
            return AnswerRunner(*args, **kwargs, output_dir=self.temp.name, clock_factory=FakeClock, output=Mock())
        with patch.object(app, "verify_ai", return_value=self.ai), patch("app.AnswerRunner", side_effect=factory):
            return app.execute_ai(Mock(), self.selected, Mock(), True, api=self.api)

    def saved_first(self):
        return json.loads(report_path(self.temp.name, "user", self.course, "first").read_text(encoding="utf-8"))

    def test_cancelling_the_confirmation_stops_the_queue_without_posting(self):
        for cancel in (KeyboardInterrupt(), EOFError(), "q", "\x1b"):
            with self.subTest(cancel=type(cancel).__name__ if isinstance(cancel, BaseException) else cancel):
                self.reset_paper()
                view = self.make_view(["B", cancel])
                reports = self.execute(view)
                self.assertEqual([report["status"] for report in reports], ["preview"])
                self.api.paper.assert_called_once_with("first")
                self.api.submit.assert_not_called()
                saved = self.saved_first()
                self.assertEqual(saved["answers"][0]["source"], "manual")
                self.assertEqual(saved["answers"][0]["answer"], "B")
                self.assertNotIn("pending_manual", saved)

    def test_a_plain_decline_only_skips_that_paper_and_the_next_one_runs(self):
        self.reset_paper()
        self.ai.answer.side_effect = None
        self.ai.answer.return_value = {"answer": None, "confidence": .9, "explanation": "缺少材料"}
        reports = self.execute(self.make_view(["B", "n", "B", "n"]))
        self.assertEqual([report["status"] for report in reports], ["preview", "preview"])
        self.assertEqual(self.api.paper.call_count, 2)
        self.api.submit.assert_not_called()


class RecordingClock(FakeClock):
    """FakeClock that remembers whether the attempt was stopped and can expire on demand."""

    def __init__(self, *args):
        super().__init__(*args)
        self.stop_called = False
        self.expired = False

    def check(self):
        if self.expired:
            raise ExamError("试卷剩余时间已用尽，停止交卷；请在网页核对状态")

    def stop(self):
        self.stop_called = True


class ClockLifetimeTests(RunnerMixin, unittest.TestCase):
    """The clock runs through the manual prompt and the confirmation; only the POST ends it."""

    def setUp(self):
        super().setUp()
        self.paper = [self.q(i) for i in range(3)]
        self.api.paper.return_value["questions"] = self.paper
        self.clock = RecordingClock()
        self.ai.answer.side_effect = [{"answer": "A", "confidence": .9, "explanation": ""},
                                      {"answer": None, "confidence": .9, "explanation": "缺少材料"},
                                      {"answer": "B", "confidence": .9, "explanation": ""}]

    def run_with_clock(self, **options):
        runner = AnswerRunner(self.api, self.ai, self.temp.name, auto_submit=True, output=Mock(),
                              clock_factory=lambda *args: self.clock, **options)
        return runner.run_paper(self.course, self.item, 3)

    def test_the_clock_keeps_running_until_the_submission_request(self):
        seen = {}

        def manual(question, reason):
            seen["manual"] = self.clock.stop_called
            return "B"

        def confirm(*args):
            seen["confirm"] = self.clock.stop_called
            return True

        def submit(payload):
            seen["submit"] = self.clock.stop_called

        self.api.submit.side_effect = submit
        report = self.run_with_clock(manual_answer=manual, confirm_submit=confirm)
        self.assertEqual(report["status"], "submitted")
        self.assertEqual(seen, {"manual": False, "confirm": False, "submit": True})
        self.assertTrue(self.clock.stop_called)

    def test_an_expiry_after_the_human_answered_still_keeps_the_typed_answer(self):
        def manual(question, reason):
            self.clock.expired = True
            return "B"

        report = self.run_with_clock(manual_answer=manual, confirm_submit=lambda *args: True)
        self.assertEqual(report["status"], "error")
        self.api.submit.assert_not_called()
        saved = self.saved_report()
        self.assertEqual(saved["answers"][1]["source"], "manual")
        self.assertEqual(saved["answers"][1]["answer"], "B")
        self.assertNotIn(2, [row["question_number"] for row in saved.get("pending_manual", [])])


class ExplodingClock(FakeClock):
    """A clock that fails before any answer could be generated."""

    def start(self):
        raise ExamError("计时启动失败")


def unanswered(position):
    """A previous row that never got an answer."""
    return {"title": "题%s" % position, "question_number": position + 1, "error": "AI 未提供答案"}


class PreseedTests(RunnerMixin, unittest.TestCase):
    """Cached rows of the same task are in the report before any clock or AI call."""

    def ai_client(self):
        return AIClient(AIConfig("https://example.org/v1", "model", "fake-key",
                                 api_format="chat_completions"), session=Mock())

    def run_parallel(self, workers, rows, **options):
        self.paper = [self.q(i) for i in range(len(rows))]
        self.api.paper.return_value["questions"] = self.paper
        self.write_previous(rows)
        runner = AnswerRunner(self.api, self.ai_client(), self.temp.name, clock_factory=FakeClock,
                              output=Mock(), max_workers=workers, **options)
        # Any dispatch of a cached question reaches this patched worker client.
        with patch("ai.concurrency.AIClient") as client:
            report = runner.run_paper(self.course, self.item, 3)
        return report, client

    def test_complete_cache_is_reported_without_any_ai_call(self):
        rows = [self.row(i, source="manual" if i == 2 else "ai") for i in range(4)]
        for workers in (2, 8):
            with self.subTest(workers=workers):
                self.setUp()
                report, client = self.run_parallel(workers, rows, auto_submit=False)
                client.assert_not_called()
                self.assertEqual(report["status"], "preview")
                self.assertEqual([row["question_number"] for row in report["answers"]], [1, 2, 3, 4])
                self.assertEqual([row["source"] for row in report["answers"]], ["ai", "ai", "manual", "ai"])
                self.assertEqual([row["answer"] for row in report["answers"]], ["A"] * 4)
                self.assertEqual(report["answers"][3]["question_fingerprint"],
                                 question_fingerprint(prepare_question(self.paper[3], 3)))
                saved = json.loads(report_path(self.temp.name, "user", self.course, "exam").read_text(encoding="utf-8"))
                self.assertEqual(len(saved["answers"]), 4)
                for existing in Path(self.temp.name).rglob("*.json"):
                    existing.unlink()

    def test_seeded_rows_are_already_written_when_the_clock_fails(self):
        rows = [self.row(i) for i in range(3)]
        self.paper = [self.q(i) for i in range(3)]
        self.api.paper.return_value["questions"] = self.paper
        self.write_previous(rows)
        runner = AnswerRunner(self.api, self.ai_client(), self.temp.name, auto_submit=False,
                              clock_factory=lambda *args: ExplodingClock(), output=Mock())
        report = runner.run_paper(self.course, self.item, 3)
        self.assertEqual(report["status"], "error")
        saved = self.saved_report()
        self.assertEqual([row["question_number"] for row in saved["answers"]], [1, 2, 3])
        self.assertEqual([row["source"] for row in saved["answers"]], ["ai", "ai", "ai"])

    def test_only_the_third_cached_row_is_kept_and_a_fatal_error_does_not_lose_it(self):
        rows = [unanswered(0), unanswered(1), self.row(2, source="manual")]
        self.paper = [self.q(i) for i in range(4)]
        self.api.paper.return_value["questions"] = self.paper
        self.write_previous(rows)
        asked = []
        self.ai.answer.side_effect = lambda q: asked.append(q["title"]) or (
            (_ for _ in ()).throw(AIServiceError("HTTP 401")) if q["title"] == "题0"
            else {"answer": "A", "confidence": .9, "explanation": ""})
        report = self.run_paper(False)
        self.assertEqual(report["status"], "ai_error")
        self.assertEqual(asked, ["题0"])
        saved = self.saved_report()
        # The failed question is kept too, and the cached row is intact.
        self.assertEqual([row["question_number"] for row in saved["answers"]], [1, 3])
        self.assertEqual((saved["answers"][1]["source"], saved["answers"][1]["answer"]), ("manual", "A"))
        # A healthy retry generates the three AI questions and keeps the cached one.
        asked.clear()
        self.ai.answer.side_effect = lambda q: asked.append(q["title"]) or {
            "answer": "A", "confidence": .9, "explanation": ""}
        report = self.run_paper(False)
        self.assertEqual(report["status"], "preview")
        self.assertEqual(asked, ["题0", "题1", "题3"])
        self.assertEqual([row["question_number"] for row in report["answers"]], [1, 2, 3, 4])
        self.assertEqual((report["answers"][2]["source"], report["answers"][2]["answer"]), ("manual", "A"))

    def test_a_parallel_run_asks_only_the_questions_without_a_cached_row(self):
        from tests.test_ai_retry import response
        from tests.test_ai_concurrency import Transport
        self.paper = [self.q(i) for i in range(4)]
        self.api.paper.return_value["questions"] = self.paper
        self.write_previous([unanswered(0), self.row(1, source="manual"), unanswered(2), unanswered(3)])
        transport = Transport(lambda q: response())
        runner = AnswerRunner(self.api, self.ai_client(), self.temp.name, auto_submit=False,
                              clock_factory=FakeClock, output=Mock(), max_workers=2)
        with patch("ai.client.requests.Session", side_effect=transport.session):
            report = runner.run_paper(self.course, self.item, 3)
        self.assertEqual(sorted(transport.calls), ["题0", "题2", "题3"])
        self.assertEqual(report["status"], "preview")
        self.assertEqual((report["answers"][1]["source"], report["answers"][1]["answer"]), ("manual", "A"))
        self.api.submit.assert_not_called()

    def test_positions_come_from_the_row_number_and_legacy_rows_fall_back_to_the_index(self):
        legacy = self.row(0)
        legacy.pop("question_number")
        duplicate = dict(self.row(0), question_number=1, answer="B",
                         record=answer_record(prepare_question(self.q(0), 0), "B"))
        beyond = dict(self.row(1), question_number=99)
        self.paper = [self.q(0), self.q(1)]
        self.api.paper.return_value["questions"] = self.paper
        self.write_previous([legacy, duplicate, beyond])
        report = self.run_paper(False)
        # The index fallback wins; the duplicate and the out-of-range number are ignored.
        self.assertEqual(report["answers"][0]["answer"], "A")
        self.assertEqual(self.ai.answer.call_count, 1)
        self.assertEqual(self.ai.answer.call_args.args[0]["title"], "题1")

    def test_a_parallel_fatal_service_error_still_keeps_the_cached_row_on_disk(self):
        self.paper = [self.q(i) for i in range(3)]
        self.api.paper.return_value["questions"] = self.paper
        self.write_previous([unanswered(0), unanswered(1), self.row(2, source="manual")])
        worker = Mock()
        worker.return_value.answer.side_effect = AIServiceError("HTTP 401")
        runner = AnswerRunner(self.api, self.ai_client(), self.temp.name, auto_submit=False,
                              clock_factory=FakeClock, output=Mock(), max_workers=2)
        with patch("ai.concurrency.AIClient", return_value=worker.return_value):
            report = runner.run_paper(self.course, self.item, 3)
        self.assertEqual(report["status"], "ai_error")
        saved = self.saved_report()
        self.assertEqual([row["question_number"] for row in saved["answers"]], [1, 3])
        self.assertEqual((saved["answers"][1]["source"], saved["answers"][1]["answer"]), ("manual", "A"))


class ParallelManualTests(RunnerMixin, unittest.TestCase):
    """Every already-computed parallel answer is in the report before the human is asked."""

    def run_parallel(self, manual_answer):
        from tests.test_ai_retry import response
        from tests.test_ai_concurrency import Transport
        paper = [self.q(i) for i in range(4)]
        self.api.paper.return_value["questions"] = paper

        def handle(payload):
            if payload["title"] == "题1":
                return response(content='{"answer":null,"confidence":0.9,"explanation":"缺少材料"}')
            return response(content='{"answer":"A","confidence":0.9,"explanation":"ok"}')
        transport = Transport(handle)
        ai = AIClient(AIConfig("https://example.org/v1", "model", "fake-key", api_format="chat_completions"),
                      session=Mock())
        runner = AnswerRunner(self.api, ai, self.temp.name, auto_submit=True, clock_factory=FakeClock,
                              output=Mock(), max_workers=2, manual_answer=manual_answer,
                              confirm_submit=lambda *args: True)
        with patch("ai.client.requests.Session", side_effect=transport.session):
            return runner.run_paper(self.course, self.item, 3)

    def test_all_parallel_answers_are_saved_before_the_manual_prompt(self):
        seen = []

        def manual(question, reason):
            saved = self.saved_report()
            seen.append([row.get("source") for row in saved["answers"]])
            return "B"
        report = self.run_parallel(manual)
        self.assertEqual(report["status"], "submitted")
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), 4)
        self.assertEqual(seen[0], ["ai", None, "ai", "ai"], seen[0])
        self.assertEqual([row["source"] for row in report["answers"]], ["ai", "manual", "ai", "ai"])
        self.assertEqual(len(self.api.submit.call_args.args[0]["taskExamProblemRecordList"]), 4)
        self.api.submit.assert_called_once()


class PendingResumeTests(RunnerMixin, unittest.TestCase):
    """A legacy pending row is carried into the report and completed by hand, never regenerated."""

    def test_legacy_pending_row_is_resumed_without_any_ai_call(self):
        for workers in (1, 2, 8):
            with self.subTest(workers=workers):
                self.setUp()
                for existing in Path(self.temp.name).rglob("*.json"):
                    existing.unlink()
                self.ai.config = AIConfig("https://example.org/v1", "model", "fake-key",
                                          api_format="chat_completions")
                self.ai.answer.side_effect = AssertionError("the pending row must not be regenerated")
                pending = {"title": "题19", "question_number": 20,
                           "options": model_question(prepare_question(self.q(19), 19))["options"],
                           "answer": None, "error": "AI 未提供答案"}
                self.write_previous([self.row(i) for i in range(19)] + [pending])
                prompts = []
                runner = AnswerRunner(self.api, self.ai, self.temp.name, auto_submit=True,
                                      clock_factory=FakeClock, output=Mock(), max_workers=workers,
                                      manual_answer=lambda q, reason: prompts.append((q, reason)) or "B",
                                      confirm_submit=Mock(return_value=True))
                with patch("ai.concurrency.AIClient") as client:
                    client.return_value.answer.side_effect = AssertionError(
                        "the pending row must not be regenerated")
                    report = runner.run_paper(self.course, self.item, 3)
                self.assertEqual(report["status"], "submitted")
                self.assertEqual(len(self.api.submit.call_args.args[0]["taskExamProblemRecordList"]), 20)
                client.assert_not_called()
                self.assertEqual(len(prompts), 1)
                self.assertEqual(prompts[0][0]["title"], "题19")
                self.assertEqual(prompts[0][1], "AI 未提供答案")
                self.assertEqual([row["source"] for row in report["answers"]],
                                 ["ai"] * 19 + ["manual"])
                runner.confirm_submit.assert_called_once_with("测验", 20, 20)

    def test_an_established_human_question_survives_another_questions_service_failure(self):
        # Q0 was already handed to the human last time; Q1 is a valid cache and Q2
        # was never answered. A real service failure on Q2 must neither regenerate
        # the established human question nor let Q2 pose as one.
        self.paper = [self.q(i) for i in range(3)]
        self.api.paper.return_value["questions"] = self.paper
        failed = self.row(0)
        failed.update(answer=None, error="AI 未提供答案")
        failed.pop("record")
        self.write_previous([failed, self.row(1)], status="ai_error")
        fingerprint = failed["question_fingerprint"]
        path = report_path(self.temp.name, "user", self.course, "exam")
        restore = json.loads(path.read_text(encoding="utf-8"))
        restore["pending_manual"] = [{"question_number": 1, "title": "题0", "reason": "AI 未提供答案",
                                      "question_fingerprint": fingerprint}]
        path.write_text(json.dumps(restore, ensure_ascii=False), encoding="utf-8")
        self.ai.answer.side_effect = AIServiceError("HTTP 401")
        manual = Mock(return_value="B")
        confirm = Mock(return_value=True)
        report = self.run_paper(manual_answer=manual, confirm_submit=confirm)
        self.assertEqual(report["status"], "ai_error")
        manual.assert_not_called()
        self.assertEqual([call.args[0]["title"] for call in self.ai.answer.call_args_list], ["题2"])
        self.assertEqual([(row["question_number"], row["question_fingerprint"])
                          for row in report["pending_manual"]], [(1, fingerprint)])
        self.assertNotIn("题2", [row["title"] for row in report["pending_manual"]])
        # The next healthy retry regenerates only the question the service failed on.
        self.ai.answer.reset_mock()
        self.ai.answer.side_effect = None
        prompts = []
        report = self.run_paper(manual_answer=lambda q, reason: prompts.append(q["title"]) or "B",
                                confirm_submit=confirm)
        self.assertEqual([call.args[0]["title"] for call in self.ai.answer.call_args_list], ["题2"])
        self.assertEqual(self.ai.answer.call_count, 1)
        self.assertEqual(prompts, ["题0"])
        self.assertEqual(report["status"], "submitted")
        self.assertEqual([row["source"] for row in report["answers"]], ["manual", "ai", "ai"])
        self.assertEqual(len(self.api.submit.call_args.args[0]["taskExamProblemRecordList"]), 3)
        self.api.submit.assert_called_once()
        confirm.assert_called_once_with("测验", 3, 3)


if __name__ == "__main__":
    unittest.main()
