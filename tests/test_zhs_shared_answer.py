import random
import unittest

from base.auth import PlatformUnavailable
from task_capabilities import task_availability
from ZHSMain.answer import ZHSAnswerRunner, catalog, catalog_shared, encode, prepare_shared
from ZHSMain.api import CaptchaRequired


class FixedRng(random.Random):
    def uniform(self, a, b):
        return a


def shared_question(eid, kind=1, name="1+1=?", options=None):
    if options is None:
        options = [{"id": 101, "content": "1"}, {"id": 102, "content": "2"}]
    return {"eid": eid, "name": name, "questionType": kind, "questionOptions": options}


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


class FakeSharedClient:
    def __init__(self, questions, *, homework_rows=None, exam_rows=None, lock=0,
                 school="88", submit_score=None):
        self.questions = list(questions)
        self.homework_rows = homework_rows if homework_rows is not None else [
            {"id": 100, "examId": 200, "examName": "第一次作业", "state": 1,
             "score": 0, "totalScore": 20, "startTime": "a", "endDate": "b", "courseId": 3},
        ]
        self.exam_rows = exam_rows if exam_rows is not None else [
            {"id": 300, "examId": 400, "examName": "期末考试", "state": 2,
             "studentStartTime": "s", "faStudentExamRemainCount": 3, "courseId": 3},
        ]
        self.lock = lock
        self.school = school
        self.submit_score = submit_score
        self.calls = []

    def _record(self, name, payload=None):
        self.calls.append((name, payload))

    def shared_exam_sso(self):
        self._record("shared_exam_sso")

    def shared_school_id(self):
        self._record("shared_school_id")
        return self.school

    def shared_homework_list(self, recruit_id, course_id, flag):
        self._record("shared_homework_list", flag)
        return [dict(row) for row in self.homework_rows] if flag == 1 else []

    def shared_exam_list(self, recruit_id, course_id, flag):
        self._record("shared_exam_list", flag)
        return [dict(row) for row in self.exam_rows] if flag == 1 else []

    def shared_answer_lock(self, exam_id, recruit_id):
        self._record("shared_answer_lock", exam_id)
        return self.lock

    def shared_do_homework(self, recruit_id, exam_id, stu_exam_id, school_id, course_id):
        self._record("shared_do_homework",
                     (recruit_id, exam_id, stu_exam_id, school_id, course_id))
        return {"examBase": {"workExamParts": [{"questionDtos": self.questions}]}}

    def shared_do_exam(self, recruit_id, exam_id, stu_exam_id, school_id, course_id):
        self._record("shared_do_exam",
                     (recruit_id, exam_id, stu_exam_id, school_id, course_id))
        return {"examBase": {"workExamParts": [{"questionDtos": self.questions}]}}

    def shared_save_answer(self, answer_item, recruit_id, *, exam):
        self._record("shared_save_answer", {"item": answer_item, "recruitId": recruit_id,
                                            "exam": exam})

    def shared_submit_homework(self, recruit_id, exam_id, stu_exam_id, count):
        self._record("shared_submit_homework", count)
        return self.submit_score or {"score": 18, "totalScore": 20}

    def shared_submit_exam(self, recruit_id, exam_id, stu_exam_id, count):
        self._record("shared_submit_exam", count)
        return {"statu": "1"}

    def names(self):
        return [name for name, _ in self.calls]

    def call(self, name):
        return [payload for called, payload in self.calls if called == name]


COURSE = {"courseName": "共享课", "zhs": {"kind": "shared", "recruitId": 9, "courseId": 3}}


def homework_item():
    return {"id": "shw-100-200", "name": "第一次作业",
            "zhs": {"kind": "shared_homework", "recruitId": 9, "courseId": 3,
                    "stuExamId": 100, "examId": 200}}


def exam_item():
    return {"id": "sex-300-400", "name": "期末考试",
            "zhs": {"kind": "shared_exam", "recruitId": 9, "courseId": 3,
                    "stuExamId": 300, "examId": 400}}


def runner(client, ai, auto_submit=True):
    return ZHSAnswerRunner(client, ai, auto_submit=auto_submit,
                           sleep=lambda seconds: None, rng=FixedRng())


class PrepareSharedTest(unittest.TestCase):
    def test_question_type_dict_or_number(self):
        prepared = prepare_shared(shared_question("e1", {"id": 1, "name": "单选题"}))
        self.assertEqual(prepared["qid"], "e1")
        self.assertEqual(prepared["kind"], 1)
        self.assertEqual(prepared["ids"], {"A": 101, "B": 102})
        self.assertEqual(prepare_shared(shared_question("e2", "2"))["kind"], 2)

    def test_missing_eid(self):
        raw = shared_question(None)
        with self.assertRaises(ValueError):
            prepare_shared(raw)
        del raw["eid"]
        with self.assertRaises(ValueError):
            prepare_shared(raw)

    def test_media_option_validation_reused(self):
        with self.assertRaises(ValueError):
            prepare_shared(shared_question("e3", name='<img src="x.png">'))

    def test_answer_encoding(self):
        single = prepare_shared(shared_question("e1"))
        self.assertEqual(",".join(str(x) for x in encode(single, "B", fill_sep="/")), "102")
        multi = prepare_shared(shared_question("e2", kind={"id": 2, "name": "多选题"}))
        self.assertEqual(",".join(str(x) for x in encode(multi, ["B", "A"], fill_sep="/")),
                         "102,101")
        fill = prepare_shared(shared_question("e3", kind=3, options=[{}, {}]))
        self.assertEqual(encode(fill, ["x", "y"], fill_sep="/"), ["x/y"])


class SharedHomeworkFlowTest(unittest.TestCase):
    def test_full_flow(self):
        client = FakeSharedClient([
            shared_question("e1"),
            shared_question("e2", kind=2),
            shared_question("e3", kind=3, options=[{}, {}]),
        ])
        ai = FakeAI(["B", ["A", "B"], ["x", "y"]])
        result = runner(client, ai).run_paper(COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "submitted", result)
        self.assertIn("得分 18/20", result["message"])
        names = client.names()
        self.assertEqual(names[0], "shared_exam_sso")
        self.assertLess(names.index("shared_do_homework"), names.index("shared_save_answer"))
        self.assertEqual(len(client.call("shared_school_id")), 1)
        self.assertEqual(
            client.call("shared_do_homework")[0], (9, 200, 100, "88", 3))
        saved = [call["item"] for call in client.call("shared_save_answer")]
        self.assertEqual([item["answer"] for item in saved], ["102", "101,102", "x/y"])
        self.assertEqual([item["eid"] for item in saved], ["e1", "e2", "e3"])
        self.assertEqual([item["questionType"] for item in saved], [1, 2, 3])
        first = saved[0]
        self.assertEqual(first["examType"], "")
        self.assertEqual(first["fromType"], 3)
        self.assertEqual(first["schoolId"], "88")
        self.assertEqual(first["deviceId"], "")
        self.assertEqual(first["dataIds"], "")
        self.assertEqual(first["stuExamId"], 100)
        self.assertEqual(first["examId"], 200)
        self.assertEqual(first["recruitId"], 9)
        self.assertFalse(client.call("shared_save_answer")[0]["exam"])
        self.assertEqual(client.call("shared_submit_homework"), [3])

    def test_preview_does_not_submit(self):
        client = FakeSharedClient([shared_question("e1")])
        result = runner(client, FakeAI(["B"]), auto_submit=False).run_paper(
            COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "preview")
        self.assertNotIn("shared_submit_homework", client.names())

    def test_bad_answer_stops_without_submit(self):
        client = FakeSharedClient([shared_question("e1"), shared_question("e2"),
                                   shared_question("e3")])
        result = runner(client, FakeAI(["B", "Z"])).run_paper(COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "error")
        self.assertIn("第 2 题", result["message"])
        self.assertEqual(len(client.call("shared_save_answer")), 1)
        self.assertNotIn("shared_submit_homework", client.names())

    def test_captcha_propagates(self):
        client = FakeSharedClient([shared_question("e1")])

        def slider(answer_item, recruit_id, *, exam):
            raise CaptchaRequired("滑块")

        client.shared_save_answer = slider
        with self.assertRaises(CaptchaRequired):
            runner(client, FakeAI(["B"])).run_paper(COURSE, homework_item(), 1)

    def test_submit_failure_is_unknown(self):
        client = FakeSharedClient([shared_question("e1")])

        def down(*args):
            raise PlatformUnavailable("down")

        client.shared_submit_homework = down
        result = runner(client, FakeAI(["B"])).run_paper(COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "submission_unknown")
        self.assertIn("未重试", result["message"])
        self.assertEqual(client.names().count("shared_submit_homework"), 0)

    def test_recheck_sees_submitted_paper(self):
        client = FakeSharedClient([shared_question("e1")])
        client.homework_rows = [dict(client.homework_rows[0], state=4)]
        result = runner(client, FakeAI(["B"])).run_paper(COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "error")
        self.assertIn("已提交", result["message"])
        self.assertNotIn("shared_do_homework", client.names())

    def test_empty_paper(self):
        client = FakeSharedClient([])
        result = runner(client, FakeAI([])).run_paper(COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "error")


class SharedExamFlowTest(unittest.TestCase):
    def test_exam_saves_with_exam_flag(self):
        client = FakeSharedClient([shared_question("e1")])
        result = runner(client, FakeAI(["B"])).run_paper(COURSE, exam_item(), 2)
        self.assertEqual(result["status"], "submitted", result)
        self.assertIn("成绩以平台为准", result["message"])
        self.assertEqual(client.call("shared_do_exam")[0], (9, 400, 300, "88", 3))
        saved = client.call("shared_save_answer")[0]
        self.assertTrue(saved["exam"])
        self.assertEqual(saved["item"]["examType"], 1)
        self.assertEqual(client.call("shared_submit_exam"), [1])

    def test_recheck_sees_locked_exam(self):
        client = FakeSharedClient([shared_question("e1")], lock=1)
        result = runner(client, FakeAI(["B"])).run_paper(COURSE, exam_item(), 2)
        self.assertEqual(result["status"], "error")
        self.assertIn("已锁定", result["message"])
        self.assertNotIn("shared_do_exam", client.names())


class CatalogSharedTest(unittest.TestCase):
    def make_client(self, **kwargs):
        client = FakeSharedClient([], **kwargs)
        return client

    def test_entries_and_ids(self):
        client = self.make_client()
        entries = catalog(client, COURSE)
        self.assertEqual(client.names()[0], "shared_exam_sso")
        self.assertEqual([category for _, _, category in entries], [1, 2])
        homework = entries[0][1]
        self.assertEqual(homework["id"], "shw-100-200")
        self.assertEqual(homework["zhs"], {"kind": "shared_homework", "recruitId": 9,
                                           "courseId": 3, "stuExamId": 100, "examId": 200})
        self.assertEqual(homework["_completion_status"], "not_started")
        self.assertNotIn("_unavailable", homework)
        self.assertEqual(homework["endTime"], "b")
        exam = entries[1][1]
        self.assertEqual(exam["id"], "sex-300-400")
        for _, item, _ in entries:
            self.assertNotIn("typeId", item)
            self.assertNotIn("status", item)

    def test_homework_unavailable_states(self):
        client = self.make_client()
        client.homework_rows = [
            {"id": 1, "examId": 11, "examName": "已交", "state": 4, "score": 100, "courseId": 3},
            {"id": 2, "examId": 12, "examName": "怪状态", "state": 9, "courseId": 3},
            {"id": 3, "examId": 13, "examName": "重置", "state": 5, "courseId": 3},
        ]
        client.exam_rows = []
        entries = catalog(client, COURSE)
        by_id = {item["id"]: item for _, item, _ in entries}
        self.assertEqual(by_id["shw-1-11"]["_unavailable"][0], "已提交")
        self.assertEqual(by_id["shw-1-11"]["_completion_status"], "submitted")
        self.assertEqual(by_id["shw-2-12"]["_unavailable"][0], "状态未知")
        self.assertNotIn("_unavailable", by_id["shw-3-13"])
        self.assertEqual(task_availability(by_id["shw-1-11"], 1).label, "已提交")

    def test_homework_skips_bad_rows(self):
        client = self.make_client()
        client.homework_rows = [
            {"id": None, "examId": 1, "examName": "缺 id", "state": 1, "courseId": 3},
            {"id": 5, "examId": None, "examName": "缺 exam", "state": 1, "courseId": 3},
            {"id": 6, "examId": 7, "examName": "别的课", "state": 1, "courseId": 999},
        ]
        client.exam_rows = []
        self.assertEqual(catalog(client, COURSE), [])

    def test_exam_unavailable_reasons(self):
        rows = [
            {"id": 1, "examId": 11, "examName": "已交", "state": 4},
            {"id": 2, "examId": 12, "examName": "未开考", "state": 1},
            {"id": 3, "examId": 13, "examName": "没次数", "state": 2,
             "studentStartTime": "s", "faStudentExamRemainCount": 0},
            {"id": 4, "examId": 14, "examName": "可考", "state": 2,
             "studentStartTime": "s", "faStudentExamRemainCount": 2},
        ]
        client = self.make_client(exam_rows=rows, lock=1)
        client.homework_rows = []
        by_id = {item["id"]: item for _, item, _ in catalog(client, COURSE)}
        self.assertEqual(by_id["sex-1-11"]["_unavailable"][0], "已提交")
        self.assertEqual(by_id["sex-1-11"]["_completion_status"], "submitted")
        self.assertEqual(by_id["sex-2-12"]["_unavailable"][0], "未开考")
        self.assertEqual(by_id["sex-3-13"]["_unavailable"][0], "无剩余次数")
        self.assertEqual(by_id["sex-4-14"]["_unavailable"][0], "已锁定")

    def test_answer_lock_failure_is_unknown(self):
        client = self.make_client()

        def down(exam_id, recruit_id):
            raise PlatformUnavailable("down")

        client.shared_answer_lock = down
        by_id = {item["id"]: item for _, item, _ in catalog(client, COURSE)}
        self.assertEqual(by_id["sex-300-400"]["_unavailable"][0], "状态未知")

    def test_exam_deduplicated(self):
        client = self.make_client()
        client.exam_rows = [{"id": 300, "examId": 400, "examName": "期末", "state": 2,
                             "studentStartTime": "s", "faStudentExamRemainCount": 3}]
        exams = [item for _, item, category in catalog(client, COURSE) if category == 2]
        self.assertEqual(len(exams), 1)

    def test_partial_and_total_failure(self):
        def fail_homework(*args):
            raise PlatformUnavailable("down")

        client = self.make_client()
        client.shared_homework_list = fail_homework
        entries = catalog(client, COURSE)
        self.assertEqual([category for _, _, category in entries], [2])

        client = self.make_client()
        client.shared_homework_list = fail_homework

        def fail_exam(*args):
            raise PlatformUnavailable("down")

        client.shared_exam_list = fail_exam
        with self.assertRaises(PlatformUnavailable):
            catalog(client, COURSE)

    def test_auth_error_propagates(self):
        client = self.make_client()

        def expired(*args):
            from base.auth import AuthenticationExpired
            raise AuthenticationExpired("expired")

        client.shared_homework_list = expired
        from base.auth import AuthenticationExpired
        with self.assertRaises(AuthenticationExpired):
            catalog(client, COURSE)


class UnavailableCapabilityTest(unittest.TestCase):
    def test_unavailable_item_is_not_selectable(self):
        item = {"_unavailable": ("已提交", "不自动重做")}
        availability = task_availability(item, 1)
        self.assertFalse(availability.available)
        self.assertEqual(availability.label, "已提交")
        self.assertEqual(availability.reason, "不自动重做")

    def test_attachment_still_wins(self):
        item = {"typeId": 3, "_unavailable": ("已提交", "不自动重做")}
        availability = task_availability(item, 1)
        self.assertEqual(availability.label, "暂不支持")


if __name__ == "__main__":
    unittest.main()
