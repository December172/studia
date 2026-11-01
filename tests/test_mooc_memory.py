"""MOOC answer memory: learn the standard answers revealed after submission, reuse them per course and paper."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai.memory import MoocAnswerMemory, MoocDatabaseAnswerMemory, learn, remembered_value, standard_texts
from ai.questions import prepare_question
from ai.runner import AnswerRunner
from tests.test_ai import FakeClock

COURSE = {"id": "ci", "courseId": "c"}


def info_question(answer="3", flags=(3,), kind="1", order=((3, "钢琴"), (0, "桌子"), (1, "椅子"))):
    """One question of getInfo, shaped like the platform's (2026-11-01)."""
    return {"paperId": "p-" + answer, "typeId": kind, "title": "哪项是乐器？", "answer": answer,
            "stuAnswer": "0", "isRight": False, "questionSubList": None,
            "dataJson": json.dumps([{"IsAnswer": "true" if value in flags else "false", "Content": text, "SortOrder": value}
                                    for value, text in order], ensure_ascii=False)}


def paper_question(order=((0, "桌子"), (1, "椅子"), (3, "钢琴")), qid="q1"):
    return {"id": qid, "typeId": 1, "title": "哪项是乐器？",
            "dataJson": json.dumps([{"SortOrder": value, "Content": text} for value, text in order], ensure_ascii=False)}


class StandardAnswerTests(unittest.TestCase):
    def test_standard_answer_must_agree_with_the_flagged_option(self):
        q = prepare_question({**info_question(), "id": "p"}, 0)
        self.assertEqual(standard_texts(q, info_question()), ["钢琴"])
        self.assertIsNone(standard_texts(q, info_question(answer="0")))          # flag says 3
        self.assertIsNone(standard_texts(q, info_question(answer="9", flags=())))  # unknown option
        judge = prepare_question({"id": "j", "typeId": 3, "title": "钢琴是乐器", "dataJson": "[]"}, 0)
        self.assertEqual(standard_texts(judge, {"answer": "1"}), ["正确"])
        self.assertTrue(remembered_value(judge, ["正确"]))

    def test_texts_map_back_to_this_papers_labels_even_when_options_are_shuffled(self):
        q = prepare_question(paper_question(), 1)   # 钢琴 is C here
        self.assertEqual(remembered_value(q, ["钢琴"]), "C")
        self.assertIsNone(remembered_value(q, ["小提琴"]))


class LearnAndReuseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.api = Mock(user_id="user")
        self.api.request.return_value = {"code": 200, "data": {"questions": [info_question()]}}

    def test_learned_answers_stay_inside_their_course_and_paper(self):
        memory = MoocAnswerMemory(self.dir / "mooc-answers.json")
        self.assertEqual(learn(self.api, memory, COURSE, "exam", "t1"), 1)
        params = self.api.request.call_args.kwargs["params"]
        self.assertEqual((params["taskId"], params["examId"], params["courseInfoId"]), ("t1", "exam", "ci"))
        q = prepare_question(paper_question(), 1)
        reloaded = MoocAnswerMemory(self.dir / "mooc-answers.json")
        self.assertEqual(reloaded.lookup(dict(q, scope="ci/exam"))["correct"], ["钢琴"])
        self.assertIsNone(reloaded.lookup(dict(q, scope="ci/other-exam"))["correct"])
        self.assertIsNone(reloaded.lookup(dict(q, scope="other-course/exam"))["correct"])

    def test_redo_uses_answers_revealed_by_the_last_attempt_without_asking_ai(self):
        self.api.records.return_value = [{"id": "t1", "status": 2}]
        self.api.paper.return_value = {"questions": [paper_question()], "taskExamRecord": {"id": "t2", "status": 0}}
        ai = Mock()
        ai.config.model = "m"
        memory = MoocDatabaseAnswerMemory(self.dir / "studia.db")
        runner = AnswerRunner(self.api, ai, str(self.dir / "reports"), clock_factory=FakeClock, memory=memory)
        with patch("ai.runner.time.sleep"):
            report = runner.run_paper(COURSE, {"id": "exam"}, 1)
        ai.answer.assert_not_called()
        self.assertEqual(report["status"], "preview")
        self.assertEqual([(row["answer"], row["source"]) for row in report["answers"]], [("C", "memory")])
        self.assertEqual(report["answers"][0]["record"]["answer"], "3")   # SortOrder of 钢琴
        # The same submitted attempt is read once per runner, not once per paper.
        runner.learn(COURSE, "exam", ["t1"])
        self.assertEqual(self.api.request.call_count, 1)

    def test_a_failing_record_read_never_stops_the_paper(self):
        self.api.records.return_value = [{"id": "t1", "status": 2}]
        self.api.request.side_effect = RuntimeError("boom")
        self.api.paper.return_value = {"questions": [paper_question()], "taskExamRecord": {"id": "t2", "status": 0}}
        ai = Mock()
        ai.config.model = "m"
        ai.answer.return_value = {"answer": "C", "confidence": .9, "explanation": ""}
        runner = AnswerRunner(self.api, ai, str(self.dir / "reports"), clock_factory=FakeClock,
                              memory=MoocAnswerMemory(self.dir / "m.json"))
        with patch("ai.runner.time.sleep"):
            self.assertEqual(runner.run_paper(COURSE, {"id": "exam"}, 1)["status"], "preview")
        ai.answer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
