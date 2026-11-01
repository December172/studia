"""本地答案记忆：指纹、存取、并发与答题流程复用。"""
import json
import os
import random
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from ZHSMain.answer import ZHSAnswerRunner, prepare, prepare_shared
from ZHSMain.memory import AnswerMemory, Scoped


class FixedRng(random.Random):
    def uniform(self, a, b):
        return a


def raw_question(qid, kind=1, content="1+1=?", options=None):
    if options is None:
        options = [{"id": 101, "content": "1"}, {"id": 102, "content": "2"}]
    return {"id": qid, "content": content, "questionType": kind, "optionVos": options}


def prepared(qid=1, content="1+1=?", options=None):
    return prepare(raw_question(qid, content=content, options=options))


class FakeClient:
    def __init__(self, questions, correct=None):
        self.questions = dict(questions)
        self.correct = correct or {}
        self.calls = []

    def _record(self, name, payload=None):
        self.calls.append((name, payload))

    def exam_open(self, payload):
        self._record("exam_open", payload)

    def exam_sheet(self, test_id, paper_id, *, all_parts):
        return [{"questionId": qid, "version": 1} for qid in self.questions]

    def exam_question(self, test_id, paper_id, question_id, version):
        self._record("exam_question", question_id)
        question = dict(self.questions[question_id])
        if self.calls.count(("exam_question", question_id)) > 1:
            passed = self.correct.get(question_id, True)
            question["userAnswerVo"] = [{"isCorrect": 1 if passed else 0, "answer": ""}]
        return question

    def exam_save_answer(self, payload):
        self._record("exam_save_answer", payload)

    def exam_submit(self, payload):
        self._record("exam_submit", payload)

    def names(self):
        return [name for name, _ in self.calls]

    def call(self, name):
        return [payload for called, payload in self.calls if called == name]


class FakeAI:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def answer(self, question):
        self.asked.append(question)
        result = self.answers.pop(0)
        if isinstance(result, Exception):
            raise result
        return {"answer": result, "explanation": ""}


HOME = {"name": "作业", "zhs": {"kind": "ai_homework", "courseId": 1, "classId": 2,
                               "knowledgeId": 3, "examTestId": 11, "paperId": 22}}
COURSE = {"courseName": "课程", "zhs": {"kind": "ai", "courseId": 1, "classId": 2}}


class MemoryStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "zhs-answers.json"

    def test_key_ignores_option_order_and_matches_by_text(self):
        forward = prepare(raw_question(1, options=[
            {"id": 1, "content": "甲"}, {"id": 2, "content": "乙"}]))
        reverse = prepare(raw_question(9, options=[
            {"id": 8, "content": "乙"}, {"id": 7, "content": "甲"}]))
        other = prepare(raw_question(1, options=[
            {"id": 1, "content": "甲"}, {"id": 2, "content": "丙"}]))
        self.assertEqual(AnswerMemory.key(forward), AnswerMemory.key(reverse))
        self.assertNotEqual(AnswerMemory.key(forward), AnswerMemory.key(other))

    def test_round_trip_and_permissions(self):
        memory = AnswerMemory(self.path)
        question = prepared()
        memory.remember_correct(question, ["2"])
        memory.remember(question, ["1"], False)
        memory.save()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        again = AnswerMemory(self.path).lookup(question)
        self.assertEqual(again["correct"], ["2"])
        self.assertEqual(again["wrong"], [["1"]])

    def test_no_save_without_changes(self):
        memory = AnswerMemory(self.path)
        memory.save()
        self.assertFalse(self.path.exists())

    def test_broken_file_is_empty_and_silent(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("ZHSMain.memory", "WARNING"):
            self.assertEqual(AnswerMemory(self.path).lookup(prepared()),
                             {"correct": None, "wrong": []})

    def test_disabled_memory_is_noop(self):
        memory = AnswerMemory(None)
        memory.remember_correct(prepared(), ["2"])
        memory.save()
        self.assertEqual(memory.lookup(prepared()), {"correct": None, "wrong": []})
        self.assertFalse(self.path.exists())

    def test_correct_judged_wrong_is_not_returned(self):
        memory = AnswerMemory(self.path)
        question = prepared()
        memory.remember_correct(question, ["2"])
        memory.remember(question, ["2"], False)
        self.assertIsNone(memory.lookup(question)["correct"])
        self.assertEqual(memory.lookup(question)["wrong"], [["2"]])

    def test_wrong_deduplicated_and_capped(self):
        memory = AnswerMemory(self.path)
        question = prepared()
        memory.remember(question, ["1"], False)
        memory.remember(question, ["1"], False)
        self.assertEqual(memory.lookup(question)["wrong"], [["1"]])
        for index in range(25):
            memory.remember(question, ["x%d" % index], False)
        wrong = memory.lookup(question)["wrong"]
        self.assertEqual(len(wrong), 20)
        self.assertNotIn(["1"], wrong)

    def test_concurrent_remember_keeps_every_question(self):
        memory = AnswerMemory(self.path)

        def worker(index):
            question = prepare(raw_question(index, content="题目 %d" % index))
            memory.remember_correct(question, ["2"])
            memory.save()

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["items"]), 20)


def in_course(memory, scope="ai/1"):
    """The view run_paper() gives the runner for COURSE (or another course)."""
    return Scoped(memory, scope)


def runner(client, ai, memory, auto_submit=True):
    return ZHSAnswerRunner(client, ai, auto_submit=auto_submit, memory=memory,
                           sleep=lambda seconds: None, rng=FixedRng(),
                           heartbeat_interval=3600)


class CourseScopeTest(unittest.TestCase):
    def test_answers_reach_every_paper_of_their_course_but_no_other_course(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = AnswerMemory(Path(directory) / "m.json")
            in_course(memory, "ai/1").remember_correct(prepared(1), ["2"])
            self.assertEqual(in_course(memory, "ai/1").lookup(prepared(7))["correct"], ["2"])
            self.assertIsNone(in_course(memory, "ai/9").lookup(prepared(1))["correct"])
            client, ai = FakeClient({1: raw_question(1)}), FakeAI(["A"])
            runner(client, ai, memory).run_paper(
                {"courseName": "别的课", "zhs": {"kind": "ai", "courseId": 9, "classId": 2}}, HOME, 1)
            self.assertEqual(len(ai.asked), 1)   # another course asks the AI


class ReuseFlowTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "zhs-answers.json"

    def test_confirmed_answer_skips_ai(self):
        memory = in_course(AnswerMemory(self.path))
        memory.remember_correct(prepared(1), ["2"])
        ai = FakeAI([])
        client = FakeClient({1: raw_question(1)})
        result = runner(client, ai, memory).run_paper(COURSE, HOME, 1)
        self.assertEqual(result["status"], "submitted", result)
        self.assertEqual(ai.asked, [])
        self.assertEqual(client.call("exam_save_answer")[0]["answer"], "102")
        self.assertIn("复用已确认答案 1 题", result["message"])

    def test_single_choice_elimination_skips_ai(self):
        memory = in_course(AnswerMemory(self.path))
        memory.remember(prepared(1), ["1"], False)
        ai = FakeAI([])
        client = FakeClient({1: raw_question(1)})
        result = runner(client, ai, memory).run_paper(COURSE, HOME, 1)
        self.assertEqual(result["status"], "submitted", result)
        self.assertEqual(ai.asked, [])
        self.assertEqual(client.call("exam_save_answer")[0]["answer"], "102")

    def test_wrong_hint_is_sent_as_context(self):
        memory = in_course(AnswerMemory(self.path))
        options = [{"id": 1, "content": "甲"}, {"id": 2, "content": "乙"},
                   {"id": 3, "content": "丙"}]
        memory.remember(prepared(1, options=options), ["甲"], False)
        ai = FakeAI(["C"])
        client = FakeClient({1: raw_question(1, options=options)})
        result = runner(client, ai, memory).run_paper(COURSE, HOME, 1)
        self.assertEqual(result["status"], "submitted", result)
        self.assertIn("以下答案已被平台判定为错误，不要再选：A", ai.asked[0]["context"])

    def test_ai_repeating_known_wrong_answer_stops_paper(self):
        memory = in_course(AnswerMemory(self.path))
        options = [{"id": 1, "content": "甲"}, {"id": 2, "content": "乙"},
                   {"id": 3, "content": "丙"}]
        memory.remember(prepared(1, options=options), ["甲"], False)
        ai = FakeAI(["A", "A", "A"])
        client = FakeClient({1: raw_question(1, options=options)})
        result = runner(client, ai, memory).run_paper(COURSE, HOME, 1)
        self.assertEqual(result["status"], "error")
        self.assertIn("格式不符", result["message"])
        self.assertEqual(len(ai.asked), 3)
        self.assertEqual(client.call("exam_save_answer"), [])
        self.assertNotIn("exam_submit", client.names())

    def test_ai_retried_after_repeating_known_wrong_answer(self):
        memory = in_course(AnswerMemory(self.path))
        options = [{"id": 1, "content": "甲"}, {"id": 2, "content": "乙"},
                   {"id": 3, "content": "丙"}]
        memory.remember(prepared(1, options=options), ["甲"], False)
        ai = FakeAI(["A", "B"])
        client = FakeClient({1: raw_question(1, options=options)})
        runner(client, ai, memory, auto_submit=False).run_paper(COURSE, HOME, 1)
        self.assertEqual(len(ai.asked), 2)
        self.assertIn("2", str(client.call("exam_save_answer")[0]))

    def test_image_only_single_choice_guesses_untried_option(self):
        memory = in_course(AnswerMemory(self.path))
        options = [{"id": i, "content": '<img src="x/%s.png">' % i} for i in (1, 2, 3, 4)]
        raw = raw_question(1, options=options)
        memory.remember(prepare(raw, True), ["[图片:1.png]"], False)
        for seed in range(6):
            client = FakeClient({1: raw})
            ai = FakeAI([None, None, None])
            ZHSAnswerRunner(client, ai, auto_submit=False, memory=memory, heartbeat_interval=3600,
                            sleep=lambda _: None, rng=random.Random(seed)).run_paper(COURSE, HOME, 1)
            self.assertEqual(len(ai.asked), 3)
            self.assertIn(client.call("exam_save_answer")[0]["answer"], {"2", "3", "4"})

    def test_homework_after_submit_updates_memory(self):
        memory = in_course(AnswerMemory(self.path))
        save = Mock(wraps=memory.save)
        memory.save = save
        client = FakeClient(
            {1: raw_question(1, options=[
                 {"id": 101, "content": "1"}, {"id": 102, "content": "2", "isCorrect": 1}]),
             2: raw_question(2, content="2+2=?", options=[
                 {"id": 201, "content": "3"}, {"id": 202, "content": "4", "isCorrect": 1}])},
            correct={1: True, 2: False})
        ai = FakeAI(["B", "A"])
        result = runner(client, ai, memory).run_paper(COURSE, HOME, 1)
        self.assertEqual(result["status"], "submitted", result)
        self.assertEqual(result["correct"], 1)
        self.assertEqual(memory.lookup(prepared(
            1, options=[{"id": 101, "content": "1"},
                        {"id": 102, "content": "2", "isCorrect": 1}]))["correct"], ["2"])
        self.assertEqual(memory.lookup(prepared(
            2, content="2+2=?", options=[{"id": 201, "content": "3"},
                                          {"id": 202, "content": "4", "isCorrect": 1}]))["wrong"], [["3"]])
        self.assertEqual(memory.lookup(prepared(
            2, content="2+2=?", options=[{"id": 201, "content": "3"},
                                          {"id": 202, "content": "4", "isCorrect": 1}]))["correct"], ["4"])
        save.assert_called()

    def test_failed_blank_learns_revealed_standard_answer(self):
        memory = in_course(AnswerMemory(self.path))
        raw = raw_question(1, kind=3, content="波特率指 ___，单位为 ___。", options=[
            {"id": 2, "content": "Baud", "isCorrect": 1, "sort": 2},
            {"id": 1, "content": "<p>码元数</p>", "isCorrect": 1, "sort": 1}])
        client = FakeClient({1: raw}, correct={1: False})
        runner(client, FakeAI([["码元", "波特"]]), memory).run_paper(COURSE, HOME, 1)
        self.assertEqual(memory.lookup(prepare(raw))["correct"], ["码元数", "Baud"])

        again = FakeClient({1: raw})
        ai = FakeAI([])
        runner(again, ai, memory, auto_submit=False).run_paper(COURSE, HOME, 1)
        self.assertEqual(ai.asked, [])
        self.assertEqual(again.call("exam_save_answer")[0]["answer"], "码元数#@#Baud")

    def test_passing_answer_is_remembered_when_options_are_unmarked(self):
        # Live judgement questions mark no option isCorrect; only the verdict proves it.
        memory = in_course(AnswerMemory(self.path))
        options = [{"id": 101, "content": "对"}, {"id": 102, "content": "错"}]
        client = FakeClient({1: raw_question(1, options=options)}, correct={1: True})
        result = runner(client, FakeAI(["A"]), memory).run_paper(COURSE, HOME, 1)
        self.assertEqual(result["status"], "submitted", result)
        self.assertEqual(memory.lookup(prepared(1, options=options))["correct"], ["对"])

    def test_reconfirmed_answer_leaves_the_wrong_list(self):
        memory = in_course(AnswerMemory(self.path))
        item = prepared(1, options=[{"id": 101, "content": "1"}, {"id": 102, "content": "2"}])
        memory.remember(item, ["2"], False)
        memory.remember_correct(item, ["2"])
        self.assertEqual(memory.lookup(item), {"correct": ["2"], "wrong": []})

    def test_shared_homework_after_submit_updates_memory(self):
        from ZHSMain.answer import prepare_shared
        from tests.test_zhs_shared_answer import FakeSharedClient, homework_item
        memory = in_course(AnswerMemory(self.path), "shared/3")

        class SharedClient(FakeSharedClient):
            def shared_look_homework(self, recruit_id, stu_exam_id, exam_id, school_id,
                                     course_id):
                self._record("shared_look_homework")
                return {"examBase": {"workExamParts": [{"questionDtos": [
                    {"id": 501, "eid": "e1", "name": "1+1=?", "questionType": 1,
                     "questionOptions": [{"id": 101, "content": "1"},
                                         {"id": 102, "content": "2"}]},
                    {"id": 502, "eid": "e2", "name": "2+2=?", "questionType": 1,
                     "questionOptions": [{"id": 201, "content": "3"},
                                         {"id": 202, "content": "4"}]},
                ]}]}}

            def shared_answer_info(self, recruit_id, stu_exam_id, exam_id, school_id,
                                   course_id, question_ids):
                self._record("shared_answer_info", question_ids)
                return {"501": {"isCurrent": "1", "answer": "102"},
                        "502": {"isCurrent": "0", "answer": "201"}}

        client = SharedClient([{"eid": "e1", "name": "1+1=?", "questionType": 1,
                                "questionOptions": [{"id": 101, "content": "1"},
                                                    {"id": 102, "content": "2"}]},
                               {"eid": "e2", "name": "2+2=?", "questionType": 1,
                                "questionOptions": [{"id": 201, "content": "3"},
                                                    {"id": 202, "content": "4"}]}])
        result = runner(client, FakeAI(["B", "A"]), memory).run_paper(
            {"courseName": "共享课", "zhs": {"kind": "shared", "recruitId": 9,
                                           "courseId": 3}}, homework_item(), 1)
        self.assertEqual(result["status"], "submitted", result)
        self.assertEqual(client.call("shared_answer_info"), [[501, 502]])
        shared = prepare_shared({"eid": "e1", "name": "1+1=?", "questionType": 1,
                                 "questionOptions": [{"id": 101, "content": "1"},
                                                     {"id": 102, "content": "2"}]})
        self.assertEqual(memory.lookup(shared)["correct"], ["2"])
        self.assertEqual(memory.lookup(prepare_shared({
            "eid": "e2", "name": "2+2=?", "questionType": 1,
            "questionOptions": [{"id": 201, "content": "3"},
                                {"id": 202, "content": "4"}]}))["wrong"], [["3"]])

    def test_shared_lookup_failure_still_submits(self):
        from ZHSMain.answer import prepare_shared
        from tests.test_zhs_shared_answer import FakeSharedClient, homework_item
        memory = in_course(AnswerMemory(self.path), "shared/3")

        class SharedClient(FakeSharedClient):
            def shared_look_homework(self, *args):
                raise RuntimeError("down")

        client = SharedClient([{"eid": "e1", "name": "1+1=?", "questionType": 1,
                                "questionOptions": [{"id": 101, "content": "1"},
                                                    {"id": 102, "content": "2"}]}])
        with self.assertLogs("ZHSMain.answer", "WARNING"):
            result = runner(client, FakeAI(["B"]), memory).run_paper(
                {"courseName": "共享课", "zhs": {"kind": "shared", "recruitId": 9,
                                               "courseId": 3}}, homework_item(), 1)
        self.assertEqual(result["status"], "submitted", result)
        shared = prepare_shared({"eid": "e1", "name": "1+1=?", "questionType": 1,
                                 "questionOptions": [{"id": 101, "content": "1"},
                                                     {"id": 102, "content": "2"}]})
        self.assertEqual(memory.lookup(shared), {"correct": None, "wrong": []})


class MemoryPathTest(unittest.TestCase):
    def test_execute_zhs_ai_uses_settings_directory(self):
        from app import Application

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = Mock()
        store.path = Path(directory.name) / "settings.json"
        store.concurrency.return_value = {"ai": 5, "study": 1}
        view = MagicMock()
        view.on_progress = None
        app = Application(view, store=store)
        app.verify_ai = Mock(return_value=Mock(session=Mock()))
        runner = Mock()
        runner.run_paper.return_value = {"paper": "一", "status": "submitted", "message": "ok"}
        with patch("app.ZHSAnswerRunner", return_value=runner) as factory:
            app.execute_zhs_ai(Mock(), [(COURSE, {"id": "a", "name": "一", "zhs": {}}, 1)],
                               Mock(), True)
        memory = factory.call_args.kwargs["memory"]
        self.assertEqual(memory.path, Path(directory.name) / "zhs-answers.json")


if __name__ == "__main__":
    unittest.main()


class DatabaseMemoryTest(unittest.TestCase):
    def test_one_users_graded_answer_serves_everyone_and_survives_restart(self):
        from ZHSMain.memory import DatabaseAnswerMemory
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "studia.db"
            memory = DatabaseAnswerMemory(db)
            question = prepared()
            memory.remember(question, ["1"], False)        # alice got it wrong
            memory.remember_correct(question, ["2"])       # bob got it right
            memory.save()
            self.assertEqual(memory.lookup(question)["correct"], ["2"])
            restarted = DatabaseAnswerMemory(db)
            self.assertEqual(restarted.lookup(question), {"correct": ["2"], "wrong": [["1"]]})
            self.assertEqual(restarted.stats(), {"questions": 1, "confirmed": 1})

    def test_legacy_user_files_merge_without_losing_either_side(self):
        from ZHSMain.memory import DatabaseAnswerMemory
        with tempfile.TemporaryDirectory() as directory:
            question, other = prepared(), prepared(2, content="2+2=?")
            alice = AnswerMemory(Path(directory) / "alice.json")
            alice.remember(question, ["1"], False)
            alice.remember_correct(other, ["2"])
            alice.save()
            shared = DatabaseAnswerMemory(Path(directory) / "studia.db")
            shared.remember_correct(question, ["2"])
            shared.save()
            self.assertEqual(shared.merge_file(alice.path), 2)
            again = DatabaseAnswerMemory(Path(directory) / "studia.db")
            self.assertEqual(again.lookup(question), {"correct": ["2"], "wrong": [["1"]]})
            self.assertEqual(again.lookup(other)["correct"], ["2"])
