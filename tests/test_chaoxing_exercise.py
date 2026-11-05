import threading
import unittest

from ai.client import AIResponseError, AIServiceError
from ChaoxingMain.api import ChaoxingError, WorkUnavailable
from ChaoxingMain.exercise import (
    ChaoxingExerciseRunner,
    encode_answer,
    prompt_title,
    unsupported_reason,
)


def question_block(question_id, type_code, title, options=()):
    items = "".join('<li aria-label="%s"></li>' % text for text in options)
    return ('<div class="singleQuesId" data="%s"><div class="TiMu" data="%s">'
            '<div class="Zy_TItle"><span>%s</span></div>%s</div></div>'
            % (question_id, type_code, title, "<ul>%s</ul>" % items if items else ""))


def work_page(*blocks):
    return ('<form id="form1">'
            '<input type="hidden" name="courseId" value="111">'
            '<input type="hidden" name="workId" value="abc">'
            + "".join(blocks) + '</form>')


SINGLE = question_block("q1", "0", "1+1=?", ["A. 1", "B. 2"])
MULTIPLE = question_block("q2", "1", "哪些是偶数？", ["A. 1", "B. 2", "C. 4"])
JUDGEMENT = question_block("q3", "3", "地球是圆的")
COMPLETION = question_block("q4", "2", "中国的首都是____")
SHORTANSWER = question_block("q5", "4", "谈谈你的理解")
UNKNOWN = question_block("q6", "9", "新题型")

ANSWERS = {
    "1+1=?": "B",
    "哪些是偶数？": ["C", "B"],
    "地球是圆的": True,
    "中国的首都是____": ["北京"],
    "谈谈你的理解": "略",
}


class FakeClient:
    def __init__(self, page="", error=None, confirmation=None):
        self.page = page
        self.error = error
        self.confirmation = confirmation
        self.work_calls = []
        self.payloads = []

    def work_page(self, course, job, job_info):
        self.work_calls.append((course, job, job_info))
        if isinstance(self.error, Exception):
            raise self.error
        return self.page

    def submit_work(self, payload):
        self.payloads.append(payload)
        if isinstance(self.confirmation, Exception):
            raise self.confirmation
        return self.confirmation if self.confirmation is not None else {"status": True, "msg": "成功"}


class FakeAI:
    def __init__(self, answers=None, error=None):
        self.answers = answers if answers is not None else dict(ANSWERS)
        self.error = error
        self.asked = []

    def answer(self, question):
        self.asked.append(question)
        if self.error is not None:
            raise self.error
        value = self.answers[question["title"]]
        if isinstance(value, Exception):
            raise value
        return {"answer": value, "explanation": ""}


COURSE = {"title": "高等数学", "courseId": "111", "clazzId": "222", "cpi": "333"}
POINT = {"id": "123", "title": "1.1 函数"}
JOB = {"type": "workid", "jobid": "work-abc", "name": "1.1 章节测验", "enc": "e"}
JOB_INFO = {"knowledgeid": "123", "ktoken": "kt", "cpi": "333"}


def runner(client, ai, **kwargs):
    return ChaoxingExerciseRunner(client, ai, sleep=lambda _: None, **kwargs)


class AnswerEncodingTest(unittest.TestCase):
    def question(self, type_code, options, kind):
        # 复用解码器，避免手写选项结构；页面只需要一道题。
        from ChaoxingMain.decode import decode_questions_info
        page = work_page(question_block("q1", type_code, "题", options))
        question = decode_questions_info(page)["questions"][0]
        self.assertEqual(question["type"], kind)
        return question

    def test_single_returns_option_letter(self):
        question = self.question("0", ["A. 甲", "B. 乙"], "single")
        self.assertEqual(encode_answer(question, "b"), "B")

    def test_multiple_is_sorted_and_deduplicated(self):
        question = self.question("1", ["A. 甲", "B. 乙", "C. 丙"], "multiple")
        self.assertEqual(encode_answer(question, ["C", "A"]), "AC")
        with self.assertRaises(ValueError):
            encode_answer(question, ["A", "A"])

    def test_multiple_accepts_comma_separated_text(self):
        question = self.question("1", ["A. 甲", "B. 乙"], "multiple")
        self.assertEqual(encode_answer(question, "B, A"), "AB")

    def test_judgement_accepts_bool_and_words(self):
        question = self.question("3", [], "judgement")
        self.assertEqual(encode_answer(question, True), "true")
        self.assertEqual(encode_answer(question, False), "false")
        self.assertEqual(encode_answer(question, "正确"), "true")
        self.assertEqual(encode_answer(question, "错误"), "false")
        with self.assertRaises(ValueError):
            encode_answer(question, "也许")

    def test_completion_joins_blanks(self):
        question = self.question("2", [], "completion")
        self.assertEqual(encode_answer(question, ["北京"]), "北京")
        self.assertEqual(encode_answer(question, ["甲", "乙"]), "甲乙")
        with self.assertRaises(ValueError):
            encode_answer(question, ["北京", ""])

    def test_shortanswer_is_stripped(self):
        question = self.question("4", [], "shortanswer")
        self.assertEqual(encode_answer(question, " 答案 "), "答案")
        with self.assertRaises(ValueError):
            encode_answer(question, "   ")

    def test_unknown_option_rejected(self):
        question = self.question("0", ["A. 甲", "B. 乙"], "single")
        with self.assertRaises(ValueError):
            encode_answer(question, "D")

    def test_prompt_title_strips_number_and_score(self):
        self.assertEqual(prompt_title("1.（2.0分）1+1=?"), "1+1=?")
        self.assertEqual(prompt_title("1. 1+1=?（2.0分）"), "1+1=?")


class UnsupportedTest(unittest.TestCase):
    def test_unknown_type(self):
        from ChaoxingMain.decode import decode_questions_info
        question = decode_questions_info(work_page(UNKNOWN))["questions"][0]
        self.assertIn("暂不支持题型", unsupported_reason(question))

    def test_audio_still_needs_human(self):
        from ChaoxingMain.decode import decode_questions_info
        page = work_page(question_block("q1", "0", "听音<audio src='https://x/1.mp3'></audio>", ["A. 甲", "B. 乙"]))
        question = decode_questions_info(page)["questions"][0]
        self.assertIn("音视频", unsupported_reason(question))

    def test_supported_question(self):
        from ChaoxingMain.decode import decode_questions_info
        question = decode_questions_info(work_page(SINGLE))["questions"][0]
        self.assertEqual(unsupported_reason(question), "")


class RunnerTest(unittest.TestCase):
    def test_submits_answered_paper(self):
        client, ai = FakeClient(work_page(SINGLE, MULTIPLE, JUDGEMENT, COMPLETION, SHORTANSWER)), FakeAI()
        result = runner(client, ai, auto_submit=True).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(result["status"], "submitted")
        payload = client.payloads[0]
        self.assertEqual(payload["pyFlag"], "")
        self.assertEqual(payload["answerq1"], "B")
        self.assertEqual(payload["answerq2"], "BC")
        self.assertEqual(payload["answerq3"], "true")
        self.assertEqual(payload["answerq4"], "北京")
        self.assertEqual(payload["answerq5"], "略")
        self.assertEqual(payload["answertypeq1"], "0")
        self.assertEqual(payload["answertypeq5"], "4")
        self.assertEqual(payload["answerwqbid"], "q1,q2,q3,q4,q5,")
        self.assertEqual(payload["courseId"], "111")

    def test_title_scaffolding_is_removed_before_asking_ai(self):
        page = work_page(question_block("q1", "0", "1.（2.0分）1+1=?", ["A. 1", "B. 2"]))
        ai = FakeAI({"1+1=?": "B"})
        runner(FakeClient(page), ai, auto_submit=True).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(ai.asked[0]["title"], "1+1=?")
        self.assertEqual(ai.asked[0]["options"], [{"label": "A", "text": "1"}, {"label": "B", "text": "2"}])

    def test_saves_when_coverage_below_threshold(self):
        page = work_page(SINGLE, MULTIPLE, SHORTANSWER)
        ai = FakeAI({"1+1=?": "B", "哪些是偶数？": AIResponseError("格式错误"), "谈谈你的理解": "略"})
        client = FakeClient(page)
        result = runner(client, ai, auto_submit=True).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(result["status"], "saved")
        self.assertIn("覆盖率 67%", result["message"])
        self.assertEqual(client.payloads[0]["pyFlag"], "1")
        self.assertEqual(client.payloads[0]["answerq2"], "")

    def test_saves_on_exact_threshold_submits(self):
        page = work_page(SINGLE, MULTIPLE, JUDGEMENT, COMPLETION, SHORTANSWER)
        answers = dict(ANSWERS, **{"谈谈你的理解": AIResponseError("超时")})
        client = FakeClient(page)
        result = runner(client, FakeAI(answers), auto_submit=True, cover_rate=0.8).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(client.payloads[0]["pyFlag"], "")
        self.assertEqual(client.payloads[0]["answerq5"], "")

    def test_unusable_answer_is_asked_again_for_a_best_guess(self):
        class GuessingAI(FakeAI):
            def answer(self, question, guess=False):
                self.asked.append(guess)
                return {"answer": "B" if guess else None, "explanation": ""}

        client, ai = FakeClient(work_page(SINGLE)), GuessingAI()
        result = runner(client, ai, auto_submit=True).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(ai.asked, [False, True])
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(client.payloads[0]["answerq1"], "B")

    def test_saves_only_when_auto_submit_off(self):
        client = FakeClient(work_page(SINGLE))
        result = runner(client, FakeAI({"1+1=?": "B"}), auto_submit=False).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(result["status"], "saved")
        self.assertIn("未交卷", result["message"])
        self.assertEqual(client.payloads[0]["pyFlag"], "1")

    def test_unsupported_question_is_not_asked(self):
        client, ai = FakeClient(work_page(SINGLE, UNKNOWN)), FakeAI()
        result = runner(client, ai, auto_submit=True).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(len(ai.asked), 1)
        self.assertEqual(result["status"], "saved")
        self.assertIn("1 题未作答", result["message"])
        self.assertEqual(client.payloads[0]["answerq6"], "")

    def test_platform_rejection_is_error(self):
        client = FakeClient(work_page(SINGLE), confirmation={"status": False, "msg": "作业已提交"})
        result = runner(client, FakeAI(), auto_submit=True).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(result["status"], "error")
        self.assertIn("作业已提交", result["message"])

    def test_http_failure_while_submitting_is_unknown(self):
        client = FakeClient(work_page(SINGLE), confirmation=ChaoxingError("连接中断"))
        result = runner(client, FakeAI(), auto_submit=True).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(result["status"], "submission_unknown")
        self.assertIn("交卷结果未知", result["message"])

    def test_unavailable_work_is_skipped(self):
        client = FakeClient(error=WorkUnavailable("教师未创建完成该测验"))
        result = runner(client, FakeAI()).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("未创建完成", result["message"])
        self.assertEqual(client.payloads, [])

    def test_encrypted_font_fails_closed(self):
        page = work_page(SINGLE).replace(
            "<form", "<style id=\"cxSecretStyle\">@font-face{src:url('data:application/font-ttf;"
                      "charset=utf-8;base64,bm90LWEtZm9udA==')}</style><form")
        ai = FakeAI()
        result = runner(FakeClient(page), ai, auto_submit=True).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(result["status"], "error")
        self.assertIn("字体", result["message"])
        self.assertEqual(ai.asked, [])

    def test_page_without_questions(self):
        result = runner(FakeClient(work_page()), FakeAI()).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(result["status"], "error")
        self.assertIn("没有读到题目", result["message"])

    def test_no_answers_is_error_and_not_submitted(self):
        client = FakeClient(work_page(UNKNOWN))
        result = runner(client, FakeAI()).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(result["status"], "error")
        self.assertIn("没有生成任何答案", result["message"])
        self.assertEqual(client.payloads, [])

    def test_ai_service_failure_stops_the_run(self):
        client = FakeClient(work_page(SINGLE))
        ai = FakeAI(error=AIServiceError("AI 服务不可用"))
        with self.assertRaises(AIServiceError):
            runner(client, ai, auto_submit=True).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(client.payloads, [])

    def test_cancel_before_answering_submits_nothing(self):
        cancel = threading.Event()
        cancel.set()
        client = FakeClient(work_page(SINGLE))
        result = runner(client, FakeAI(), cancel_event=cancel).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(client.payloads, [])


if __name__ == "__main__":
    unittest.main()
