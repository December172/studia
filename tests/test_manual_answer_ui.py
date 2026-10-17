"""The terminal prompt shows the unresolved question and reads B as a choice, not as Back."""
import io
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from ai.questions import prepare_question
from ai.runner import AnswerRunner
from rich.console import Console
import theme
from tests.test_ai import FakeClock, question
from ui import Back, TerminalUI


class ManualPromptTests(unittest.TestCase):
    def make_view(self, answers):
        output = io.StringIO()
        inputs = iter(answers)

        def feed():
            try:
                return next(inputs)
            except StopIteration:
                raise EOFError()
        view = TerminalUI(console=Console(file=output, width=100, highlight=False, theme=theme.THEME), input_fn=feed)
        view.live = Mock()
        return view, output

    def prepared(self, kind=1, title="哪项是乐器？"):
        return prepare_question(dict(question(kind), title=title), 0)

    def test_prompt_shows_number_type_reason_context_title_and_options(self):
        view, output = self.make_view(["B"])
        prepared = prepare_question(dict(question(1), title="哪项是乐器？"), 2,
                                    {"id": "parent", "title": "材料一"}, 0)
        value = view.manual_answer(prepared, "AI 未提供答案")
        self.assertEqual(value, "B")
        shown = output.getvalue()
        for expected in ("3.", "单选题", "材料一", "哪项是乐器？", "A. 钢琴", "B. 桌子",
                         "AI 未提供答案", "第 1 小题", "B 是选项"):
            with self.subTest(expected=expected):
                self.assertIn(expected, shown)

    def test_an_invalid_line_reprompts_and_the_valid_one_is_returned(self):
        view, output = self.make_view(["Z", ":s"])
        self.assertIsNone(view.manual_answer(self.prepared(), "原因"))
        self.assertIn("输入无效", output.getvalue())
        self.assertEqual(output.getvalue().count("请输入答案"), 2)

    def test_a_b_letter_is_an_answer_not_navigation(self):
        view, _ = self.make_view(["b"])
        self.assertEqual(view.manual_answer(self.prepared(), "原因"), "B")

    def test_save_and_escape_leave_the_prompt_without_an_answer(self):
        for answer in (":s", ":q"):
            with self.subTest(answer=answer):
                view, _ = self.make_view([answer])
                self.assertIsNone(view.manual_answer(self.prepared(), "原因"))
        view, _ = self.make_view(["\x1b"])
        self.assertIsNone(view.manual_answer(self.prepared(), "原因"))

    def test_eof_propagates_so_the_runner_can_checkpoint(self):
        for stop in (EOFError(), KeyboardInterrupt()):
            with self.subTest(stop=type(stop).__name__):
                view, _ = self.make_view([], )
                view.input_fn = Mock(side_effect=stop)
                with self.assertRaises(type(stop)):
                    view.manual_answer(self.prepared(), "原因")

    def test_the_live_view_and_raw_input_are_suspended_while_reading(self):
        view, _ = self.make_view(["A"])
        view.runtime_input = Mock()
        view.runtime_input.suspend.return_value = contextmanager(lambda: (yield))()
        view.manual_answer(self.prepared(), "原因")
        view.live.stop.assert_called()
        view.live.start.assert_called()
        view.runtime_input.suspend.assert_called_once()

    def test_judgement_and_blank_questions_are_converted_to_typed_values(self):
        for kind, text, expected in ((3, "n", False), (4, "钢琴 | 小提琴", ["钢琴", "小提琴"])):
            with self.subTest(kind=kind):
                view, _ = self.make_view([text])
                self.assertEqual(view.manual_answer(self.prepared(kind), "原因"), expected)


class ConfirmSubmitTests(unittest.TestCase):
    def make_view(self, answers):
        output = io.StringIO()
        inputs = iter(answers)

        def feed():
            try:
                return next(inputs)
            except StopIteration:
                raise EOFError()
        view = TerminalUI(console=Console(file=output, width=100, highlight=False, theme=theme.THEME), input_fn=feed)
        view.runtime_input = Mock()
        view.runtime_input.suspend.return_value = contextmanager(lambda: (yield))()
        return view, output

    def test_default_answer_keeps_the_paper_unsubmitted(self):
        view, output = self.make_view([""])
        self.assertFalse(view.confirm_submit("测验", 20, 20))
        self.assertIn("20/20 题已完成，交卷提交整份试卷？", output.getvalue())

    def test_explicit_yes_submits_the_whole_paper(self):
        view, _ = self.make_view(["y"])
        self.assertTrue(view.confirm_submit("测验", 20, 20))

    def test_plain_no_returns_false_so_the_queue_continues(self):
        for answer in ("n", ""):
            with self.subTest(answer=answer):
                view, _ = self.make_view([answer] if answer else [])
                self.assertFalse(view.confirm_submit("测验", 20, 20))

    def test_explicit_cancel_returns_none_so_the_queue_stops(self):
        for answer in ("q",):  # q quits, Esc is Back, an empty input raises EOFError
            with self.subTest(answer=answer):
                view, _ = self.make_view([answer])
                self.assertIsNone(view.confirm_submit("测验", 20, 20))
        view, _ = self.make_view([])
        view.input_fn = Mock(return_value="\x1b")
        self.assertIsNone(view.confirm_submit("测验", 20, 20))
        view, _ = self.make_view([])
        view.input_fn = Mock(side_effect=EOFError())
        self.assertIsNone(view.confirm_submit("测验", 20, 20))


class PausedStatusTests(unittest.TestCase):
    def test_a_paused_paper_is_reported_as_waiting_for_a_human_answer(self):
        from reporting import ProgressEvent
        view, output = ConfirmSubmitTests().make_view([])
        view.on_progress(ProgressEvent("begin", "AI 答题", total=1, unit="份试卷"))
        view.on_progress(ProgressEvent("result", "测验", status="paused"))
        self.assertEqual(view.results, [("测验", "paused")])
        self.assertIn("待补答", "".join(line for line, _ in view.lines))
        text, style = view.finish_text()
        self.assertIn("失败 1", text)
        self.assertIn("待补答 1", text)
        self.assertEqual(style, "error")


class LiveHandoffTests(unittest.TestCase):
    """The real view prompts must survive the runner's handoff, not just a Mock lambda."""

    def test_real_terminal_prompt_answers_and_confirms_a_live_paper(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        course = {"id": "ci", "courseId": "c", "courseName": "课程"}
        item = {"id": "exam", "name": "测验"}
        api = Mock(user_id="user")
        api.records.return_value = []
        api.verify_submitted.return_value = True
        api.paper.return_value = {"questions": [dict(question(qid=str(i)), title="题%s" % i) for i in range(3)],
                                  "taskExamRecord": {"id": "task", "status": 0}}
        ai = Mock()
        ai.config.model = "model"
        ai.answer.side_effect = [{"answer": "A", "confidence": .9, "explanation": ""},
                                 {"answer": None, "confidence": .9, "explanation": "缺少材料"},
                                 {"answer": "A", "confidence": .9, "explanation": ""}]
        view, output = ManualPromptTests().make_view(["B", "y"])
        runner = AnswerRunner(api, ai, temp.name, auto_submit=True, clock_factory=FakeClock, output=Mock(),
                              manual_answer=view.manual_answer, confirm_submit=view.confirm_submit)
        with patch("ai.runner.time.sleep"):
            report = None
            try:
                with view.activity("AI 答题"):
                    report = runner.run_paper(course, item, 3)
            except RuntimeError as exc:
                self.fail("the live handoff broke the real prompt: %s" % exc)
        self.assertEqual(report["status"], "submitted")
        self.assertEqual([row["source"] for row in report["answers"]], ["ai", "manual", "ai"])
        self.assertEqual(report["answers"][1]["answer"], "B")
        self.assertIn("题干：题1", output.getvalue())
        self.assertIn("3/3 题已完成，交卷提交整份试卷？", output.getvalue())
        api.submit.assert_called_once()
        entries = api.submit.call_args.args[0]["taskExamProblemRecordList"]
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[1]["answer"], "0")


if __name__ == "__main__":
    unittest.main()
