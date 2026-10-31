import random
import threading
import unittest

from AIMoocMain.export_questions import paper_exam_id
from ai.client import AIError, AITransientServiceError
from base.auth import PlatformUnavailable
from course_flow import task_identity
from unittest.mock import Mock
from ZHSMain.answer import ZHSAnswerRunner, catalog, download_images, encode, prepare
from ZHSMain.api import CaptchaRequired


class FixedRng(random.Random):
    def uniform(self, a, b):
        return a


def raw_question(qid, kind=1, content="1+1=?", options=None, user_answer=None):
    if options is None:
        options = [
            {"id": 101, "content": "1", "isCorrect": 0},
            {"id": 102, "content": "2", "isCorrect": 1},
        ]
    return {
        "id": qid,
        "content": content,
        "questionType": kind,
        "optionVos": options,
        "userAnswerVo": user_answer,
    }


class FakeClient:
    def __init__(self, questions, sheet=None, detail=None):
        self.questions = dict(questions)
        self.sheet = sheet if sheet is not None else [
            {"questionId": qid, "version": 1} for qid in questions
        ]
        self.detail = detail or {}
        self.calls = []
        self.submitted = False
        self.heartbeats = 0

    def _record(self, name, payload=None):
        self.calls.append((name, payload))

    def exam_open(self, payload):
        self._record("exam_open", payload)

    def exam_sheet(self, test_id, paper_id, *, all_parts):
        self._record("exam_sheet", {"all_parts": all_parts})
        return list(self.sheet)

    def exam_question(self, test_id, paper_id, question_id, version):
        self._record("exam_question", question_id)
        question = dict(self.questions[question_id])
        if self.submitted:
            question["userAnswerVo"] = [{"isCorrect": 1, "answer": ""}]
        return question

    def exam_save_answer(self, payload):
        self._record("exam_save_answer", payload)

    def exam_save_batch(self, payload):
        self._record("exam_save_batch", payload)

    def exam_heartbeat(self, test_id, paper_id, seconds):
        self.heartbeats += 1

    def exam_submit(self, payload, *, expect_body=True):
        self._record("exam_submit", {"payload": payload, "expect_body": expect_body})
        self.submitted = True

    def ai_exam_pull_paper(self, course_id, class_id, exam_test_id):
        self._record("ai_exam_pull_paper", exam_test_id)

    def ai_exam_detail(self, payload):
        self._record("ai_exam_detail", payload)
        return dict(self.detail)

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


class ConcurrentFakeAI:
    """Answers every question with 'B' while recording peak in-flight calls."""

    def __init__(self):
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.overlap = threading.Event()
        self.asked = []

    def answer(self, question):
        with self.lock:
            self.asked.append(question)
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            if self.in_flight >= 2:
                self.overlap.set()
        self.overlap.wait(0.2)
        with self.lock:
            self.in_flight -= 1
        # Keep the task alive briefly so later questions are still queued.
        threading.Event().wait(0.01)
        return {"answer": "B", "explanation": ""}


class MapFakeAI:
    """Answers by question title so concurrent order does not matter."""

    def __init__(self, by_title):
        self.by_title = by_title
        self.lock = threading.Lock()

    def answer(self, question):
        with self.lock:
            result = self.by_title[question["title"]]
        if isinstance(result, Exception):
            raise result
        return {"answer": result, "explanation": ""}


def numbered_questions(count):
    questions = {}
    sheet = []
    for qid in range(1, count + 1):
        questions[qid] = raw_question(qid, content="Q%d" % qid)
        sheet.append({"questionId": qid, "version": 1})
    return questions, sheet


def homework_item():
    return {
        "name": "知识点 · 作业",
        "zhs": {"kind": "ai_homework", "courseId": 1, "classId": 2, "knowledgeId": 3,
                "examTestId": 11, "paperId": 22},
    }


def exam_item():
    return {
        "name": "期末考试",
        "zhs": {"kind": "ai_exam", "courseId": 1, "classId": 2, "examTestId": 11,
                "examPaperId": 22, "taskId": 7, "userId": "u1"},
    }


COURSE = {"courseName": "课程", "zhs": {"kind": "ai", "courseId": 1, "classId": 2}}


def runner(client, ai, auto_submit=True, **kwargs):
    kwargs.setdefault("heartbeat_interval", 3600)
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("rng", FixedRng())
    return ZHSAnswerRunner(client, ai, auto_submit=auto_submit, **kwargs)


class PrepareTest(unittest.TestCase):
    def test_image_urls_are_kept_for_the_model(self):
        raw = raw_question(1, content='概率为<img src="https://oss.example/p/q.png">',
                           options=[{"id": 1, "content": '<img src="https://oss.example/p/a.png?x=1">'},
                                    {"id": 2, "content": '<img src="http://oss.example/p/b.png">'}])
        prepared = prepare(raw, True)
        self.assertEqual(prepared["images"], {"q.png": "https://oss.example/p/q.png",
                                              "a.png": "https://oss.example/p/a.png?x=1"})
        self.assertIn("[图片:q.png]", prepared["public"]["title"])

    def test_download_images_rejects_non_images(self):
        def get(url, **kwargs):
            ok = url.endswith("ok.png")
            return Mock(status_code=200, content=b"x",
                        headers={"Content-Type": "image/png" if ok else "text/html"})
        self.assertEqual(download_images({"ok.png": "https://h/ok.png"}, get=get),
                         [("ok.png", "image/png", b"x")])
        self.assertIsNone(download_images({"ok.png": "https://h/ok.png", "bad": "https://h/bad"}, get=get))

    def test_single_choice(self):
        prepared = prepare(raw_question(1))
        self.assertEqual(prepared["public"]["type"], "单选题")
        self.assertEqual(prepared["public"]["options"][0], {"label": "A", "text": "1"})
        self.assertEqual(prepared["ids"], {"A": 101, "B": 102})
        self.assertEqual(encode(prepared, "B", fill_sep="#@#"), [102])

    def test_multiple_choice(self):
        prepared = prepare(raw_question(2, kind=2))
        self.assertEqual(prepared["public"]["type"], "多选题")
        self.assertEqual(encode(prepared, ["B", "A"], fill_sep="#@#"), [102, 101])

    def test_judgement(self):
        options = [{"id": 1, "content": "对"}, {"id": 2, "content": "错"}]
        prepared = prepare(raw_question(3, kind=14, options=options))
        self.assertEqual(prepared["public"]["type"], "单选题")
        self.assertEqual(encode(prepared, "A", fill_sep="#@#"), [1])

    def test_fill_blank(self):
        prepared = prepare(raw_question(4, kind=3, options=[{}, {}, {}]))
        self.assertEqual(prepared["public"]["type"], "填空题")
        self.assertEqual(prepared["blank_count"], 3)
        self.assertEqual(encode(prepared, [" x ", "y", "z"], fill_sep="#@#"), ["x#@#y#@#z"])
        self.assertEqual(encode(prepared, [" x ", "y", "z"], fill_sep="/"), ["x/y/z"])
        self.assertEqual(encode(prepare(raw_question(5, kind=3, options=[])), "a", fill_sep="/"),
                         ["a"])

    def test_media_and_errors(self):
        with self.assertRaises(ValueError):
            prepare(raw_question(6, content='看图<img src="x.png">'))
        with self.assertRaises(ValueError):
            prepare(raw_question(7, content=""))
        with self.assertRaises(ValueError):
            prepare(raw_question(8, kind=5))
        with self.assertRaises(ValueError):
            prepare(raw_question(9, options=[{"id": 1, "content": "only"}]))

    def test_encode_rejects_unknown_option(self):
        prepared = prepare(raw_question(10))
        with self.assertRaises(ValueError):
            encode(prepared, "C", fill_sep="#@#")
        with self.assertRaises(ValueError):
            encode(prepared, ["A", "A"], fill_sep="#@#")
        multi = prepare(raw_question(11, kind=2))
        with self.assertRaises(ValueError):
            encode(multi, [], fill_sep="#@#")


class ConcurrencyTest(unittest.TestCase):
    def test_ai_runs_concurrently_and_saves_in_order(self):
        questions, sheet = numbered_questions(6)
        client = FakeClient(questions, sheet=sheet)
        ai = ConcurrentFakeAI()
        result = runner(client, ai, max_workers=4).run_paper(COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "submitted", result)
        self.assertGreater(ai.peak, 1)
        names = client.names()
        self.assertLessEqual(names.index("exam_save_answer"), names.index("exam_submit"))
        self.assertEqual(names.count("exam_save_answer"), 6)
        saved = client.call("exam_save_answer")
        self.assertEqual([item["questionId"] for item in saved], [1, 2, 3, 4, 5, 6])

    def test_one_failure_saves_nothing(self):
        questions, sheet = numbered_questions(4)
        client = FakeClient(questions, sheet=sheet)
        ai = MapFakeAI({"Q1": "B", "Q2": "B", "Q3": AIError("down"), "Q4": "B"})
        result = runner(client, ai, max_workers=4).run_paper(COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "ai_error")
        self.assertIn("第 3 题", result["message"])
        self.assertEqual(client.call("exam_save_answer"), [])
        self.assertNotIn("exam_submit", client.names())

    def test_earliest_failure_is_reported(self):
        questions, sheet = numbered_questions(4)
        client = FakeClient(questions, sheet=sheet)
        ai = MapFakeAI({"Q1": "B", "Q2": AIError("two"), "Q3": ValueError("three"), "Q4": "B"})
        result = runner(client, ai, max_workers=4).run_paper(COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "ai_error")
        self.assertIn("第 2 题", result["message"])
        self.assertEqual(client.call("exam_save_answer"), [])

    def test_cancel_event_raises_keyboard_interrupt(self):
        questions, sheet = numbered_questions(4)
        client = FakeClient(questions, sheet=sheet)
        cancel = threading.Event()

        class CancellingAI:
            def answer(self, question):
                cancel.set()
                return {"answer": "B", "explanation": ""}

        with self.assertRaises(KeyboardInterrupt):
            runner(client, CancellingAI(), max_workers=2,
                   cancel_event=cancel).run_paper(COURSE, homework_item(), 1)
        self.assertNotIn("exam_submit", client.names())


class PacingTest(unittest.TestCase):
    def test_fetch_and_save_sleeps(self):
        questions, sheet = numbered_questions(4)
        client = FakeClient(questions, sheet=sheet)
        sleeps = []
        rng = random.Random(0)
        result = runner(client, ConcurrentFakeAI(), max_workers=2,
                        sleep=sleeps.append, rng=rng).run_paper(
            COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "submitted", result)
        # 4 题之间 3 次拿题 + 4 次保存之间 3 次
        self.assertEqual(len(sleeps), 6)
        self.assertTrue(all(0.5 <= value <= 1 for value in sleeps[:3]))
        self.assertTrue(all(1 <= value <= 2 for value in sleeps[3:]))


class LogTest(unittest.TestCase):
    def test_answer_logs_progress(self):
        client = FakeClient({1: raw_question(1)})
        with self.assertLogs("ZHSMain.answer", "INFO") as captured:
            runner(client, FakeAI(["B"])).run_paper(COURSE, homework_item(), 1)
        text = "\n".join(captured.output)
        self.assertIn("AI 作答 1/", text)
        self.assertIn("已保存", text)


class HomeworkFlowTest(unittest.TestCase):
    def test_full_flow(self):
        client = FakeClient({1: raw_question(1), 2: raw_question(2, kind=2)})
        ai = FakeAI(["B", ["A", "B"]])
        result = runner(client, ai).run_paper(COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["correct"], 2)
        self.assertEqual(result["total"], 2)
        self.assertIn("答对 2/2", result["message"])
        self.assertEqual(client.names()[:2], ["exam_open", "exam_sheet"])
        self.assertFalse(client.call("exam_sheet")[0]["all_parts"])
        saved = client.call("exam_save_answer")
        self.assertEqual(saved[0]["questionId"], 1)
        self.assertEqual(saved[0]["answer"], "102")
        self.assertEqual(saved[1]["answer"], "101#@#102")
        self.assertIsNone(saved[0]["dataVos"])
        submit = client.call("exam_submit")[0]
        self.assertEqual(submit["payload"]["courseType"], 8)
        self.assertEqual(submit["payload"]["aiKnlowledgeId"], 3)

    def test_preview_does_not_submit(self):
        client = FakeClient({1: raw_question(1)})
        result = runner(client, FakeAI(["B"]), auto_submit=False).run_paper(
            COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "preview")
        self.assertNotIn("exam_submit", client.names())

    def test_bad_answer_stops_without_saving_or_submit(self):
        client = FakeClient({1: raw_question(1), 2: raw_question(2), 3: raw_question(3)})
        ai = FakeAI(["Z"] * 9)
        result = runner(client, ai).run_paper(COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "error")
        self.assertIn("格式不符", result["message"])
        self.assertNotIn("exam_submit", client.names())
        self.assertEqual(len(client.call("exam_question")), 3)
        self.assertEqual(client.call("exam_save_answer"), [])

    def test_ai_error(self):
        client = FakeClient({1: raw_question(1)})
        result = runner(client, FakeAI([AIError("down")])).run_paper(
            COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "ai_error")
        self.assertNotIn("exam_submit", client.names())

    def test_transient_retries_then_succeeds(self):
        client = FakeClient({1: raw_question(1)})
        ai = FakeAI([AITransientServiceError("busy"), AITransientServiceError("busy"), "B"])
        result = runner(client, ai).run_paper(COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(len(ai.asked), 3)

    def test_captcha_propagates(self):
        client = FakeClient({1: raw_question(1)})
        client.exam_open = lambda payload: (_ for _ in ()).throw(CaptchaRequired("captcha"))
        with self.assertRaises(CaptchaRequired):
            runner(client, FakeAI(["B"])).run_paper(COURSE, homework_item(), 1)


class ExamFlowTest(unittest.TestCase):
    def test_batches_and_detail_hidden(self):
        questions = {qid: raw_question(qid) for qid in range(1, 6)}
        client = FakeClient(questions, detail={"isLookAnswer": 0, "isAllowShowDetail": 0})
        ai = FakeAI(["B"] * 5)
        result = runner(client, ai, batch_size=2).run_paper(COURSE, exam_item(), 2)
        self.assertEqual(result["status"], "submitted")
        self.assertNotIn("correct", result)
        self.assertIn("平台未开放答案", result["message"])
        names = client.names()
        self.assertLess(names.index("ai_exam_pull_paper"), names.index("exam_open"))
        self.assertTrue(client.call("exam_sheet")[0]["all_parts"])
        batches = client.call("exam_save_batch")
        self.assertEqual([len(batch["answerList"]) for batch in batches], [2, 2, 1])
        answer = batches[0]["answerList"][0]
        self.assertEqual(answer["questionType"], 1)
        self.assertEqual(answer["answer"], "102")
        self.assertIsNone(answer["dataVos"])
        submit = client.call("exam_submit")[0]
        self.assertFalse(submit["expect_body"])
        self.assertEqual(client.names().count("exam_question"), 5)

    def test_detail_visible_counts_correct(self):
        client = FakeClient({1: raw_question(1)}, detail={"isLookAnswer": 1})
        result = runner(client, FakeAI(["B"])).run_paper(COURSE, exam_item(), 2)
        self.assertEqual(result["correct"], 1)
        self.assertEqual(client.call("ai_exam_detail")[0]["studentId"], "u1")

    def test_submit_failure_is_unknown(self):
        client = FakeClient({1: raw_question(1)})
        client.exam_submit = lambda payload, *, expect_body=True: (_ for _ in ()).throw(
            PlatformUnavailable("down"))
        result = runner(client, FakeAI(["B"])).run_paper(COURSE, exam_item(), 2)
        self.assertEqual(result["status"], "submission_unknown")
        self.assertIn("未重试", result["message"])

    def test_open_failure_fails_only_this_paper(self):
        client = FakeClient({1: raw_question(1)})
        client.exam_open = lambda payload: (_ for _ in ()).throw(PlatformUnavailable("down"))
        result = runner(client, FakeAI(["B"])).run_paper(COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "error")
        self.assertIn("打开试卷失败", result["message"])
        self.assertNotIn("exam_submit", client.names())

    def test_option_without_id_is_rejected(self):
        raw = raw_question(1)
        raw["optionVos"][0]["id"] = None
        with self.assertRaises(ValueError):
            prepare(raw)

    def test_empty_sheet(self):
        client = FakeClient({}, sheet=[])
        result = runner(client, FakeAI([])).run_paper(COURSE, exam_item(), 2)
        self.assertEqual(result["status"], "error")


class CatalogTest(unittest.TestCase):
    def make_client(self):
        class CatalogClient:
            def __init__(self):
                self.basic = {"cakeThemeList": [{"knowledgeList": [
                    {"knowledgeId": 1, "knowledgeName": "知识点一"},
                    {"knowledgeId": 2, "knowledgeName": "知识点二"},
                ]}]}
                self.homework = {
                    1: {"examTestId": 11, "paperId": 22, "highMasteryScore": 100},
                    2: {"examTestId": 0, "paperId": 22, "highMasteryScore": 0},
                }
                self.tasks = [
                    {"taskType": 1, "taskName": "期末", "examTestId": 5, "examPaperId": 6,
                     "id": 7, "userId": "u1", "startTime": "a", "endTime": "b", "score": 10},
                    {"taskType": 1, "taskName": "缺 ID", "examTestId": None,
                     "examPaperId": 6, "id": 8, "userId": "u1"},
                ]
                self.fail_basic = False
                self.fail_tasks = False

            def ai_course_basic(self, course_id, class_id):
                if self.fail_basic:
                    raise PlatformUnavailable("down")
                return self.basic

            def ai_homework_info(self, course_id, class_id, knowledge_id):
                return self.homework[knowledge_id]

            def ai_exam_tasks(self, course_id):
                if self.fail_tasks:
                    raise PlatformUnavailable("down")
                return self.tasks

        return CatalogClient()

    def test_entries(self):
        entries = catalog(self.make_client(), COURSE)
        self.assertEqual([category for _, _, category in entries], [1, 2])
        homework = entries[0][1]
        self.assertEqual(homework["name"], "知识点一 · 作业")
        self.assertEqual(homework["score"], 100)
        self.assertEqual(homework["_completion_status"], "submitted")
        self.assertEqual(homework["zhs"]["kind"], "ai_homework")
        exam = entries[1][1]
        self.assertEqual(exam["score"], 10)
        self.assertEqual(exam["startTime"], "a")
        self.assertEqual(exam["zhs"]["taskId"], 7)
        for _, item, _ in entries:
            self.assertNotIn("typeId", item)
            self.assertNotIn("status", item)

    def test_entries_have_stable_unique_ids(self):
        client = self.make_client()
        other = dict(COURSE, courseName="课程二", zhs={"kind": "ai", "courseId": 9, "classId": 2})
        first = {task_identity(task) for task in catalog(client, COURSE)}
        second = {task_identity(task) for task in catalog(client, other)}
        self.assertTrue(first.isdisjoint(second))
        ids = [paper_exam_id(item) for _, item, _ in catalog(client, COURSE)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[0], "hw-1-11-22")
        self.assertEqual(ids[1], "exam-7-5-6")

    def test_partial_failure(self):
        client = self.make_client()
        client.fail_basic = True
        entries = catalog(client, COURSE)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][2], 2)

    def test_both_fail(self):
        client = self.make_client()
        client.fail_basic = True
        client.fail_tasks = True
        with self.assertRaises(PlatformUnavailable):
            catalog(client, COURSE)


if __name__ == "__main__":
    unittest.main()


class ImageQuestionTest(unittest.TestCase):
    def test_images_become_named_placeholders_only_when_allowed(self):
        raw = raw_question(1, content='看图<img src="https://x.test/a/fig1.png?v=2">选择')
        with self.assertRaises(ValueError):
            prepare(raw)
        prepared = prepare(raw, allow_images=True)
        self.assertIn("[图片:fig1.png]", prepared["public"]["title"])
        options = [{"id": 1, "content": '<img src="/p/one.png">'}, {"id": 2, "content": '<img src="/p/two.png">'}]
        texts = [o["text"] for o in prepare(raw_question(2, options=options), allow_images=True)["public"]["options"]]
        self.assertEqual(texts, ["[图片:one.png]", "[图片:two.png]"])

    def test_audio_is_still_rejected(self):
        with self.assertRaises(ValueError):
            prepare(raw_question(1, content='<audio src="a.mp3"></audio>听'), allow_images=True)

    def test_ai_homework_answers_image_questions_but_exam_does_not(self):
        raw = raw_question(1, content='看图<img src="/fig.png">')
        client = FakeClient({1: raw})
        result = runner(client, FakeAI(["B"])).run_paper(COURSE, homework_item(), 1)
        self.assertEqual(result["status"], "submitted", result)
        client = FakeClient({1: raw})
        result = runner(client, FakeAI(["B"])).run_paper(COURSE, exam_item(), 2)
        self.assertEqual(result["status"], "error")
