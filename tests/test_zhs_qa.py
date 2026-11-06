import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from rich.console import Console

import theme
from ai.client import AIResponseError, AITransientServiceError
from app import Application
from base.auth import PlatformUnavailable
from settings import SettingsStore
from ui import TerminalUI
from ZHSMain.qa import (CaptchaRequired, QaBridge, answer_questions, publish_questions,
                        write_questions)

LONG = "这是一段超过三十个字的回答，用来通过最小长度校验，确保回答不会为空。"
COURSE = {"courseName": "国家安全教育", "zhs": {"kind": "shared", "courseId": 1000119877, "recruitId": 426135}}


class FakeQa:
    def __init__(self, items, fail_on=None, captcha_on=None):
        self.items, self.published = items, []
        self.fail_on, self.captcha_on = fail_on, captcha_on

    def questions(self, max_answers=1, limit=10):
        return self.items[:limit]

    def publish(self, qid, text):
        if qid == self.captcha_on:
            raise CaptchaRequired("需要验证")
        if qid == self.fail_on:
            raise RuntimeError("平台拒绝")
        self.published.append((qid, text))
        return 1000 + qid


class FakeAi:
    def __init__(self, text=LONG):
        self.text, self.calls, self.questions = text, 0, []
        self.session = Mock()

    def answer(self, question):
        self.calls += 1
        self.questions.append(question)
        return {"answer": self.text}


def items(count):
    return [{"id": i, "content": "第 %d 节讲的分层设计为什么能降低维护成本？" % i} for i in range(1, count + 1)]


ASK = "大数据分析时，样本偏差会怎样误导企业的判断？"


class QaQueueTest(unittest.TestCase):
    def test_counts_and_limit(self):
        qa = FakeQa(items(5))
        stats = answer_questions(qa, FakeAi(), limit=3, interval=0)
        self.assertEqual(stats["answered"], 3)
        self.assertEqual([row[0] for row in qa.published], [1, 2, 3])

    def test_failure_does_not_stop_queue(self):
        qa = FakeQa(items(4), fail_on=2)
        stats = answer_questions(qa, FakeAi(), limit=4, interval=0)
        self.assertEqual((stats["answered"], stats["failed"]), (3, 1))
        self.assertEqual([row[0] for row in qa.published], [1, 3, 4])

    def test_short_answer_is_rejected(self):
        qa = FakeQa(items(2))
        stats = answer_questions(qa, FakeAi("太短"), limit=2, interval=0)
        self.assertEqual((stats["answered"], stats["failed"]), (0, 2))
        self.assertEqual(qa.published, [])

    def test_captcha_propagates(self):
        qa = FakeQa(items(3), captcha_on=2)
        with self.assertRaises(CaptchaRequired):
            answer_questions(qa, FakeAi(), limit=3, interval=0)
        self.assertEqual(len(qa.published), 1)

    def test_seen_ids_are_skipped_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "qa.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump([1, 2], handle)
            qa = FakeQa(items(5))
            stats = answer_questions(qa, FakeAi(), limit=3, interval=0, state_path=path)
            self.assertEqual([row[0] for row in qa.published], [3, 4, 5])
            self.assertEqual(stats["ids"], [3, 4, 5])
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), [1, 2, 3, 4, 5])


class AskTest(unittest.TestCase):
    def bridge(self, payload):
        qa = QaBridge.__new__(QaBridge)  # 不启动浏览器，只测响应处理
        qa.course_id, qa.recruit_id = 1, 2
        qa._eval = lambda script, arg: payload
        return qa

    def test_returns_question_id(self):
        qa = self.bridge({"status": "200", "rt": {"questionId": 99}})
        self.assertEqual(qa.ask("问题？"), 99)

    def test_captcha_is_raised(self):
        qa = self.bridge({"status": "200", "code": 1010})
        with self.assertRaises(CaptchaRequired):
            qa.ask("问题？")

    def test_failure_message(self):
        qa = self.bridge({"status": "500", "msg": "频率过快"})
        with self.assertRaises(Exception) as caught:
            qa.ask("问题？")
        self.assertIn("频率过快", str(caught.exception))


class BridgeLifecycleTest(unittest.TestCase):
    """__enter__ 失败时 with 不会调用 __exit__：浏览器和事件循环必须在这里关掉。"""

    def test_failed_enter_releases_playwright(self):
        qa = QaBridge("[]", course_id=1, recruit_id=2)
        qa._prepare = Mock(side_effect=PlatformUnavailable("页面没有加载完成"))
        playwright = Mock()
        with patch("playwright.sync_api.sync_playwright") as start:
            start.return_value.start.return_value = playwright
            with self.assertRaises(PlatformUnavailable):
                qa.__enter__()
        playwright.chromium.launch.return_value.close.assert_called_once()
        playwright.stop.assert_called_once()

    def test_launch_failure_still_stops_playwright(self):
        qa = QaBridge("[]", course_id=1, recruit_id=2)
        playwright = Mock()
        playwright.chromium.launch.side_effect = RuntimeError("没有可用的浏览器")
        with patch("playwright.sync_api.sync_playwright") as start:
            start.return_value.start.return_value = playwright
            with self.assertRaises(RuntimeError):
                qa.__enter__()
        playwright.stop.assert_called_once()


class FlakyAi(FakeAi):
    """先回坏 JSON 再正常，模拟推理模型偶尔只回思考、正文为空。"""

    def __init__(self, text=ASK, failures=1):
        super().__init__(text)
        self.failures = failures

    def answer(self, question):
        self.calls += 1
        self.questions.append(question)
        if self.failures:
            self.failures -= 1
            raise AIResponseError("AI 响应不是完整的答案 JSON；本题未作答（finish_reason=length）")
        return {"answer": self.text}


class FakeAsk:
    """只实现 ask() 的桥替身，用来验证提问队列。"""

    def __init__(self, fail_on=None, captcha_on=None):
        self.published, self.fail_on, self.captcha_on = [], fail_on, captcha_on

    def ask(self, text, chapter_id=0):
        if text == self.captcha_on:
            raise CaptchaRequired("需要验证")
        if text == self.fail_on:
            raise ZHSApiError("500", "平台拒绝")
        self.published.append(text)
        return 5000 + len(self.published)


QUESTIONS = ["第一章里的两个概念有什么区别？", "为什么这里要用比例而不是绝对值？"]


class WriteQuestionsTest(unittest.TestCase):
    def test_writes_requested_questions(self):
        ai = FakeAi(ASK)
        self.assertEqual(write_questions(ai, QUESTIONS, 2), [ASK])  # the second one repeats: three tries, then given up
        self.assertEqual(ai.calls, 4)

    def test_a_question_the_board_would_reject_is_rewritten_with_the_reason(self):
        ai = Mock(answer=Mock(side_effect=[{"answer": "云计算好吗？"}, {"answer": ASK}]))
        self.assertEqual(write_questions(ai, [], 1), [ASK])
        self.assertIn("是非题", ai.answer.call_args.args[0]["title"])

    def test_gives_up_after_three_bad_tries(self):
        ai = FakeAi("太短")
        self.assertEqual(write_questions(ai, [], 1), [])
        self.assertEqual(ai.calls, 3)

    def test_lead_ins_are_dropped(self):
        ai = FakeAi("老师好，请问合同效力为什么要看双方的真实意思表示？")
        self.assertEqual(write_questions(ai, [], 1), ["合同效力为什么要看双方的真实意思表示？"])

    def test_address_kept_when_it_is_part_of_the_question(self):
        text = "老师布置的作业要求和课程大纲不一致时，应当以哪个为准？"
        self.assertEqual(write_questions(FakeAi(text), [], 1), [text])

    def test_existing_questions_are_only_something_to_avoid(self):
        ai = FakeAi(ASK)
        write_questions(ai, QUESTIONS, 1, course_name="国家安全教育")
        title = ai.questions[0]["title"]
        self.assertIn("国家安全教育", title)
        self.assertIn("不要重复", title)
        for question in QUESTIONS:
            self.assertIn(question, title)

    def test_answer_matching_a_reference_is_skipped(self):
        self.assertEqual(write_questions(FakeAi(QUESTIONS[0]), QUESTIONS, 1), [])

    def test_reference_is_capped(self):
        ai = FakeAi(ASK)
        write_questions(ai, ["问题 %d 号？" % index for index in range(40)], 1)
        title = ai.questions[0]["title"]
        self.assertIn("问题 7 号？", title)
        self.assertNotIn("问题 8 号？", title)

    def test_broken_response_is_asked_again(self):
        ai = FlakyAi()
        with patch("ZHSMain.qa.time.sleep"):
            self.assertEqual(write_questions(ai, [], 1), [ASK])
        self.assertEqual(ai.calls, 2)

    def test_repeated_broken_response_gives_up_after_three_tries(self):
        ai = FlakyAi(failures=5)
        self.assertEqual(write_questions(ai, [], 1), [])
        self.assertEqual(ai.calls, 3)

    def test_broken_reply_counts_as_a_try(self):
        broken = AIResponseError("AI 响应不是完整的答案 JSON")
        ai = Mock(answer=Mock(side_effect=[broken, {"answer": ASK}]))
        self.assertEqual(write_questions(ai, [], 1), [ASK])
        ai = Mock(answer=Mock(side_effect=[broken, broken, broken, {"answer": ASK}]))
        self.assertEqual(write_questions(ai, [], 2), [ASK])  # the first question gives up, the second still comes
        ai = Mock(answer=Mock(side_effect=AITransientServiceError("AI 连接失败或超时")))
        with self.assertRaises(AITransientServiceError):
            write_questions(ai, [], 1)
        self.assertEqual(ai.answer.call_count, 1)  # a timeout is not waited out twice

    def test_progress_callback_reports_each_question(self):
        seen = []
        write_questions(FakeAi(ASK), [], 2, on_progress=lambda index, total: seen.append((index, total)))
        self.assertEqual(seen, [(1, 2), (2, 2)])

    def test_prompt_carries_the_board_rules(self):
        ai = FakeAi(ASK)
        write_questions(ai, [], 1, course_name="国家安全教育")
        title = ai.questions[0]["title"]
        for rule in ("一个问号", "请问", "老师", "序号", "是非题", "名词解释", "括号"):
            self.assertIn(rule, title)

    def test_each_question_takes_its_own_lesson(self):
        ai = FakeAi(ASK)
        lessons = ["1.1 物联网的体系结构", "2.3 云计算的服务模式"]
        write_questions(ai, [], 2, course_name="新一代信息技术导论", topics=lessons)
        first = next(lesson for lesson in lessons if lesson in ai.questions[0]["title"])
        second = next(lesson for lesson in lessons if lesson in ai.questions[1]["title"])
        self.assertNotEqual(first, second)

    def test_prompt_falls_back_to_the_course_as_a_whole(self):
        ai = FakeAi(ASK)
        write_questions(ai, [], 1, course_name="国家安全教育")
        self.assertIn("课程的核心内容", ai.questions[0]["title"])


class ReplyVoiceTest(unittest.TestCase):
    PLAIN = "我觉得缓冲最关键，CPU 太快外设太慢，没有它就得干等着。"

    def test_a_listed_or_stock_answer_is_rewritten_as_one_plain_paragraph(self):
        from ZHSMain.qa import write_reply
        ai = Mock(answer=Mock(side_effect=[{"answer": "1. 处理机管理。2. 存储器管理。"},
                                           {"answer": "主要体现在以下几个方面：效率和安全"},
                                           {"answer": self.PLAIN}]))
        self.assertEqual(write_reply(ai, "操作系统有哪些功能？", voice=2), self.PLAIN)
        contexts = [call.args[0]["context"] for call in ai.answer.call_args_list]
        self.assertIn("不要分点编号", contexts[1])
        self.assertIn("套话", contexts[2])

    def test_gives_up_after_three_tries(self):
        from ZHSMain.qa import write_reply
        ai = Mock(answer=Mock(return_value={"answer": "首先要看效率，其次看安全。"}))
        with self.assertRaises(ValueError):
            write_reply(ai, "操作系统有哪些功能？")
        self.assertEqual(ai.answer.call_count, 3)

    def test_each_account_keeps_its_voice_and_steers_clear_of_answers_already_there(self):
        from ZHSMain.qa import REPLY_VOICES, write_reply
        ai = FakeAi(self.PLAIN)
        write_reply(ai, "操作系统有哪些功能？", seen=["我觉得是管好 CPU 和内存。"], voice=3)
        context = ai.questions[0]["context"]
        self.assertIn(REPLY_VOICES[3][0], context)
        self.assertIn("我觉得是管好 CPU 和内存。", context)

    def test_answering_uses_the_accounts_voice(self):
        from ZHSMain.qa import REPLY_VOICES
        ai = FakeAi(LONG)
        answer_questions(FakeQa(items(1)), ai, limit=1, interval=0, voice=1)
        self.assertIn(REPLY_VOICES[1][0], ai.questions[0]["context"])

    def test_our_own_questions_stay_short_and_plain(self):
        from ZHSMain.qa import own_question_flaw
        self.assertIsNone(own_question_flaw(ASK))
        self.assertIsNotNone(own_question_flaw("在端边协同的AI推理场景中，深度学习模型分割通常依据哪些策略划分端侧与边缘侧的计算图以平衡开销？"))
        self.assertIsNotNone(own_question_flaw("LoRa 和 NB-IoT 哪个更省电？"))


class RulesTest(unittest.TestCase):
    def test_flaws_the_board_rules_name(self):
        from ZHSMain.qa import question_flaw
        self.assertIsNone(question_flaw(ASK))
        self.assertIsNone(question_flaw("为什么是边缘计算而不是云计算更适合工厂产线？"))
        for text in ("请问大数据分析会怎样误导企业判断？", "物联网安全为什么难做？？", "云计算好吗？",
                     "什么是物联网的感知层？", "企业上云最大的阻力是什么？", "人工智能会不会取代程序员？",
                     "5G 是哪一年开始商用的呢？", "大数据（数据集）会怎样影响企业决策？", "大数据怎样影响决策"):
            self.assertIsNotNone(question_flaw(text), text)

    def test_threads_worth_answering(self):
        from ZHSMain.qa import worth_answering
        now = 1_800_000_000_000
        good = {"id": 1, "content": ASK, "asker": 7, "at": now - 3600_000}
        burst = [{"id": 10 + i, "content": ASK, "asker": 8, "at": now - 60_000 + i * 5000} for i in range(3)]
        mine = dict(good, id=2, mine=True)
        old = dict(good, id=3, asker=9, at=now - 200 * 24 * 3600 * 1000)
        junk = dict(good, id=4, asker=10, content="云计算好吗？")
        self.assertEqual([q["id"] for q in worth_answering([good, mine, old, junk] + burst, now=now)], [1])

    def test_room_is_the_smaller_of_cap_and_daily_limit(self):
        from ZHSMain.qa import qa_room
        self.assertEqual(qa_room(5, 0, 20, 10), 10)
        self.assertEqual(qa_room(15, 2, 20, 10), 3)
        self.assertEqual(qa_room(None, 10, 20, 10), 0)
        self.assertEqual(qa_room(25, 0, 20, 10), 0)


class PublishQuestionsTest(unittest.TestCase):
    def test_publishes_all_questions(self):
        qa = FakeAsk()
        stats = publish_questions(qa, QUESTIONS, interval=0)
        self.assertEqual((stats["published"], stats["failed"]), (2, 0))
        self.assertEqual(qa.published, QUESTIONS)
        self.assertEqual(stats["ids"], [5001, 5002])

    def test_failure_does_not_stop_queue(self):
        qa = FakeAsk(fail_on=QUESTIONS[0])
        stats = publish_questions(qa, QUESTIONS, interval=0)
        self.assertEqual((stats["published"], stats["failed"]), (1, 1))
        self.assertEqual(qa.published, [QUESTIONS[1]])

    def test_captcha_propagates(self):
        qa = FakeAsk(captcha_on=QUESTIONS[1])
        with self.assertRaises(CaptchaRequired):
            publish_questions(qa, QUESTIONS, interval=0)
        self.assertEqual(qa.published, [QUESTIONS[0]])

    def test_missing_id_counts_as_failure(self):
        qa = FakeAsk()
        qa.ask = lambda text, chapter_id=0: None
        stats = publish_questions(qa, QUESTIONS, interval=0)
        self.assertEqual((stats["published"], stats["failed"]), (0, 2))


class QaDraftsTest(unittest.TestCase):
    def view(self, answers):
        output = io.StringIO()
        inputs = iter(answers)
        return TerminalUI(console=Console(file=output, theme=theme.THEME, width=120),
                          input_fn=lambda: next(inputs)), output

    def test_enter_publishes_them_all(self):
        view, _ = self.view([""])
        self.assertEqual(view.qa_drafts(QUESTIONS), QUESTIONS)

    def test_a_number_drops_that_one(self):
        view, _ = self.view(["1", ""])
        self.assertEqual(view.qa_drafts(QUESTIONS), QUESTIONS[1:])

    def test_e_and_a_number_edits_it(self):
        view, output = self.view(["e2", "太短", "e2", "数据和信息到底有什么区别？", ""])
        self.assertEqual(view.qa_drafts(QUESTIONS), [QUESTIONS[0], "数据和信息到底有什么区别？"])
        self.assertIn("这条没改", output.getvalue())

    def test_r_asks_for_another_batch(self):
        view, _ = self.view(["r"])
        self.assertIsNone(view.qa_drafts(QUESTIONS))

    def test_escape_or_dropping_all_posts_nothing(self):
        view, _ = self.view(["\x1b"])
        self.assertEqual(view.qa_drafts(QUESTIONS), [])
        view, _ = self.view(["1", "1"])
        self.assertEqual(view.qa_drafts(QUESTIONS), [])

    def test_size_defaults_to_the_last_choice(self):
        view, _ = self.view([""])
        self.assertEqual(view.qa_size("条数", (5, 10, 20), 20), 20)
        view, _ = self.view([""])
        self.assertEqual(view.qa_size("条数", (5, 10, 20), 7), 10)
        view, _ = self.view(["3"])  # 编号是第几项，不是条数
        self.assertEqual(view.qa_size("条数", (5, 10, 20), None), 20)


class TuiExecuteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SettingsStore(Path(self.temp.name))
        self.view = TerminalUI(console=Console(file=io.StringIO(), theme=theme.THEME, width=120),
                               input_fn=lambda: "")
        self.app = Application(self.view, self.store)
        self.app.verify_ai = Mock(return_value=FakeAi())
        for name in ("ZHSMain.qa.QA_PACE", "ZHSMain.qa.ASK_PACE"):
            pace = patch(name, (0, 0))
            pace.start()
            self.addCleanup(pace.stop)

    def run_qa(self, courses, qa):
        bridge = Mock()
        bridge.return_value.__enter__ = Mock(return_value=qa)
        bridge.return_value.__exit__ = Mock(return_value=False)
        with patch("ZHSMain.qa.QaBridge", bridge):
            stats = self.app.execute_zhs_qa(Mock(access_token="[]"), courses, Mock(),
                                            {"limit": 3})
        return stats, bridge

    def test_answers_selected_questions(self):
        qa = FakeQa(items(5))
        stats, _ = self.run_qa([COURSE], qa)
        self.assertEqual(stats, {"answered": 3, "failed": 0, "skipped": 0})
        self.assertEqual([row[0] for row in qa.published], [1, 2, 3])

    def test_course_without_qa_area_is_skipped(self):
        stats, bridge = self.run_qa([{"courseName": "无问答区"}], FakeQa(items(2)))
        self.assertEqual(stats, {"answered": 0, "failed": 0, "skipped": 0})
        bridge.assert_not_called()

    def test_captcha_stops_remaining_courses(self):
        qa = FakeQa(items(3), captcha_on=2)
        stats, bridge = self.run_qa([COURSE, dict(COURSE, courseName="课程二")], qa)
        self.assertEqual(stats["answered"], 1)
        self.assertEqual(bridge.call_count, 1)

    def test_shared_course_passes_recruit_id(self):
        _, bridge = self.run_qa([COURSE], FakeQa(items(1)))
        self.assertEqual(bridge.call_args.kwargs["recruit_id"], 426135)
        self.assertEqual(bridge.call_args.kwargs["course_id"], 1000119877)

    def test_ledger_is_written_next_to_settings(self):
        self.run_qa([COURSE], FakeQa(items(2)))
        path = Path(self.store.path).parent / "zhs-qa-answers.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [1, 2])


class TuiAskTest(unittest.TestCase):
    @staticmethod
    def config(format="chat_completions"):
        from ai.client import AIConfig
        return AIConfig("https://example.org/v1", "model", "key", api_format=format)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.view = TerminalUI(console=Console(file=io.StringIO(), theme=theme.THEME, width=120),
                               input_fn=lambda: "")
        self.app = Application(self.view, SettingsStore(Path(self.temp.name)))
        for name in ("ZHSMain.qa.QA_PACE", "ZHSMain.qa.ASK_PACE"):
            pace = patch(name, (0, 0))
            pace.start()
            self.addCleanup(pace.stop)

    def publish(self, courses, qa):
        bridge = Mock()
        bridge.return_value.__enter__ = Mock(return_value=qa)
        bridge.return_value.__exit__ = Mock(return_value=False)
        with patch("ZHSMain.qa.QaBridge", bridge):
            stats = self.app.execute_zhs_ask(Mock(access_token="[]"), courses, QUESTIONS)
        return stats, bridge

    def test_publishes_questions(self):
        qa = FakeAsk()
        stats, bridge = self.publish([COURSE], qa)
        self.assertEqual(stats, {"published": 2, "failed": 0})
        self.assertEqual(qa.published, QUESTIONS)
        self.assertEqual(bridge.call_args.kwargs["recruit_id"], 426135)

    def test_captcha_stops_remaining_courses(self):
        qa = FakeAsk(captcha_on=QUESTIONS[1])
        stats, bridge = self.publish([COURSE, dict(COURSE, courseName="课程二")], qa)
        self.assertEqual(stats["published"], 1)
        self.assertEqual(bridge.call_count, 1)

    def test_generate_reads_reference_questions(self):
        ai = FakeAi(ASK)
        qa = FakeQa([{"id": 1, "content": QUESTIONS[0]}, {"id": 2, "content": QUESTIONS[1]}])
        bridge = Mock()
        bridge.return_value.__enter__ = Mock(return_value=qa)
        bridge.return_value.__exit__ = Mock(return_value=False)
        with patch("ai.client.AIClient", return_value=ai) as client_class, \
                patch("ZHSMain.qa.QaBridge", bridge):
            texts = self.app.generate_zhs_questions(Mock(access_token="[]"), [COURSE], self.config(),
                                                    {"count": 1})
        self.assertEqual(texts, [ASK])
        self.assertIn(QUESTIONS[0], ai.questions[0]["title"])
        self.assertEqual(bridge.call_args.kwargs["recruit_id"], 426135)
        self.assertTrue(ai.session.close.called)
        # 推理型模型出题常超过 120 秒：生成阶段单独放宽超时。
        self.assertGreaterEqual(client_class.call_args.args[0].timeout, 300)

    def test_generate_probes_unknown_api_format(self):
        ai = FakeAi(LONG)
        bridge = Mock()
        bridge.return_value.__enter__ = Mock(return_value=FakeQa([]))
        bridge.return_value.__exit__ = Mock(return_value=False)
        self.app.verify_ai = Mock(return_value=ai)
        with patch("ai.client.AIClient", return_value=ai), \
                patch("ZHSMain.qa.QaBridge", bridge):
            self.app.generate_zhs_questions(Mock(access_token="[]"), [COURSE],
                                            self.config("auto"), {"count": 1})
        self.app.verify_ai.assert_called_once()

    def test_generate_without_qa_area_falls_back(self):
        ai = FakeAi(LONG)
        with patch("ai.client.AIClient", return_value=ai), \
                patch("ZHSMain.qa.QaBridge") as bridge:
            self.app.generate_zhs_questions(Mock(access_token="[]"), [{"courseName": "无问答区"}],
                                            self.config(), {"count": 1})
        bridge.assert_not_called()
        self.assertIn("课程的核心内容", ai.questions[0]["title"])


class WorkspaceTest(unittest.TestCase):
    def workspace(self, view):
        from course_flow import CourseWorkspace

        app = MagicMock()
        app.store.get.return_value = {}
        app.remember.side_effect = lambda operation: operation()
        workspace = CourseWorkspace(app)
        workspace.view, workspace.store, workspace.platform = view, app.store, 2
        workspace.client = Mock()
        workspace.client.score_shares.return_value = {"interactive": 10}
        workspace.client.score_earned.return_value = {
            "interactive": 2.0, "qa": {"answers": 3, "questions": 1, "received": 2}}
        return workspace, app

    def test_answering_asks_only_how_many(self):
        from ui import Back

        view = MagicMock()
        view.course_menu.side_effect = [4, Back()]
        view.qa_menu.side_effect = [1, Back()]
        view.qa_size.return_value = 10
        workspace, app = self.workspace(view)
        # 统计字典不是文件路径：汇总页不能把它当成 answered/failed/skipped 三个文件。
        app.execute_zhs_qa.return_value = {"answered": 9, "failed": 0, "skipped": 0}
        workspace.course_loop([COURSE])
        self.assertEqual(app.execute_zhs_qa.call_args.args[1:], ([COURSE], app.ai_configuration(), {"limit": 10}))
        self.assertEqual(view.operation_result.call_args.args[0], [])
        self.assertEqual(app.store.set.call_args.args, ("qa", {"limit": 10}))
        self.assertEqual(view.qa_menu.call_args.args[0],
                         ["国家安全教育：互动分 2/10 · 提问 1/10 · 回答 3/20 · 收获回答 2（次日上午更新）"])

    def test_answering_stops_short_of_the_cap(self):
        from ui import Back

        view = MagicMock()
        view.course_menu.side_effect = [4, Back()]
        view.qa_menu.side_effect = [1, Back()]
        view.qa_size.return_value = 10
        workspace, app = self.workspace(view)
        workspace.client.score_earned.return_value = {
            "interactive": 3.6, "qa": {"answers": 18, "questions": 0, "received": 0}}
        workspace.course_loop([COURSE])
        self.assertEqual(app.execute_zhs_qa.call_args.args[3], {"limit": 2})  # 20 answers score, 18 already do

    def test_asking_publishes_what_was_kept(self):
        from ui import Back

        view = MagicMock()
        view.course_menu.side_effect = [4, Back()]
        view.qa_menu.side_effect = [2, Back()]
        view.qa_size.return_value = 3
        view.qa_drafts.side_effect = [None, [QUESTIONS[0]]]  # 先换一批，再留一条发
        workspace, app = self.workspace(view)
        app.generate_zhs_questions.return_value = QUESTIONS
        workspace.course_loop([COURSE])
        self.assertEqual(app.generate_zhs_questions.call_count, 2)
        self.assertEqual(app.generate_zhs_questions.call_args.args[3], {"count": 3})
        app.execute_zhs_ask.assert_called_once_with(workspace.client, [COURSE], [QUESTIONS[0]])
        self.assertEqual(app.store.set.call_args.args, ("qa_ask", {"count": 3}))

    def test_generation_error_is_shown_and_retryable(self):
        view = MagicMock()
        workspace, app = self.workspace(view)
        app.ai_configuration.side_effect = [RuntimeError("AI 服务 HTTP 503"), MagicMock()]
        app.generate_zhs_questions.return_value = [QUESTIONS[0]]
        view.confirm.return_value = True
        view.qa_drafts.return_value = [QUESTIONS[0]]
        self.assertEqual(workspace.prepare_questions([COURSE], {"count": 1}), [QUESTIONS[0]])
        self.assertEqual(view.confirm.call_count, 1)
        self.assertTrue(any("503" in str(call.args[0]) for call in view.note.call_args_list))

    def test_generation_error_can_be_abandoned(self):
        view = MagicMock()
        workspace, app = self.workspace(view)
        app.ai_configuration.side_effect = RuntimeError("AI 服务 HTTP 503")
        view.confirm.return_value = False
        self.assertEqual(workspace.prepare_questions([COURSE], {"count": 1}), [])
        view.qa_drafts.assert_not_called()

    def test_cancelled_generation_stays_in_menu(self):
        view = MagicMock()
        workspace, app = self.workspace(view)
        app.ai_configuration.side_effect = KeyboardInterrupt
        self.assertEqual(workspace.prepare_questions([COURSE], {"count": 1}), [])
        view.qa_drafts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
