import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from rich.console import Console

import theme
from app import Application
from base.auth import PlatformUnavailable
from settings import SettingsStore
from ui import TerminalUI
from ZHSMain.qa import (CaptchaRequired, QaBridge, answer_questions, publish_questions,
                        write_questions)

LONG = "这是一段超过三十个字的回答，用来通过最小长度校验，确保回答不会为空。"
COURSE = {"courseName": "国家安全教育", "zhs": {"courseId": 1000119877, "recruitId": 426135}}


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
    return [{"id": i, "content": "问题 %d" % i} for i in range(1, count + 1)]


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
        ai = FakeAi(LONG)
        self.assertEqual(write_questions(ai, QUESTIONS, 2), [LONG])  # 重复的只发一条
        self.assertEqual(ai.calls, 2)

    def test_short_and_repeated_answers_are_skipped(self):
        self.assertEqual(write_questions(FakeAi("太短"), [], 2), [])
        self.assertEqual(len(write_questions(FakeAi(LONG), [], 3)), 1)

    def test_lead_ins_are_dropped(self):
        ai = FakeAi("老师好，请问合同效力的认定标准是什么？")
        self.assertEqual(write_questions(ai, [], 1), ["合同效力的认定标准是什么？"])

    def test_address_kept_when_it_is_part_of_the_question(self):
        text = "老师布置的作业要求和课程大纲不一致，以哪个为准？"
        self.assertEqual(write_questions(FakeAi(text), [], 1), [text])

    def test_reference_questions_are_used_as_topics(self):
        ai = FakeAi(LONG)
        write_questions(ai, QUESTIONS, 1, course_name="国家安全教育")
        title = ai.questions[0]["title"]
        self.assertIn("国家安全教育", title)
        for question in QUESTIONS:
            self.assertIn(question, title)

    def test_answer_matching_a_reference_is_skipped(self):
        self.assertEqual(write_questions(FakeAi(QUESTIONS[0]), QUESTIONS, 1), [])

    def test_reference_is_capped(self):
        ai = FakeAi(LONG)
        write_questions(ai, ["问题 %d 号？" % index for index in range(40)], 1)
        title = ai.questions[0]["title"]
        self.assertIn("问题 7 号？", title)
        self.assertNotIn("问题 8 号？", title)

    def test_prompt_forbids_address_and_identity_framing(self):
        ai = FakeAi(LONG)
        write_questions(ai, [], 1, course_name="国家安全教育")
        title = ai.questions[0]["title"]
        for banned in ("作为大学生", "请问", "老师", "序号", "Markdown"):
            self.assertIn(banned, title)

    def test_prompt_falls_back_to_course_topic(self):
        ai = FakeAi(LONG)
        write_questions(ai, [], 1, course_name="国家安全教育")
        title = ai.questions[0]["title"]
        self.assertIn("国家安全教育", title)
        self.assertIn("课程的核心概念", title)


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


class QaAskSettingsTest(unittest.TestCase):
    def view(self, answers):
        output = io.StringIO()
        inputs = iter(answers)
        return TerminalUI(console=Console(file=output, theme=theme.THEME, width=120),
                          input_fn=lambda: next(inputs)), output

    def test_manual_text_is_kept(self):
        view, _ = self.view(["1", "第一章的两个概念有什么区别？", "", "y"])
        options = view.qa_ask_settings({})
        self.assertEqual(options["source"], "manual")
        self.assertEqual(options["text"], "第一章的两个概念有什么区别？")
        self.assertEqual(options["interval"], 60)

    def test_short_text_is_asked_again(self):
        view, output = self.view(["1", "太短", "第一章的两个概念有什么区别？", "", "y"])
        self.assertEqual(view.qa_ask_settings({})["text"], "第一章的两个概念有什么区别？")
        self.assertIn("请输入 10–120 个字。", output.getvalue())

    def test_ai_mode_asks_only_count(self):
        view, _ = self.view(["2", "2", "", "y"])
        options = view.qa_ask_settings({})
        self.assertEqual((options["source"], options["count"]), ("ai", 2))
        self.assertNotIn("topic", options)

    def test_saved_values_become_defaults(self):
        view, _ = self.view(["2", "", "", "y"])
        saved = {"source": "ai", "count": 3, "interval": 30}
        options = view.qa_ask_settings(saved)
        self.assertEqual({key: options[key] for key in saved}, saved)

    def test_review_requires_confirmation(self):
        view, _ = self.view(["y"])
        self.assertEqual(view.qa_questions_review(QUESTIONS), QUESTIONS)
        view, _ = self.view(["n"])
        self.assertEqual(view.qa_questions_review(QUESTIONS), [])


class QaSettingsTest(unittest.TestCase):
    def view(self, answers):
        output = io.StringIO()
        inputs = iter(answers)
        return TerminalUI(console=Console(file=output, theme=theme.THEME, width=120),
                          input_fn=lambda: next(inputs)), output

    def test_enter_keeps_defaults(self):
        view, _ = self.view(["", "", "", "y"])
        self.assertEqual(view.qa_settings({}),
                         {"limit": 10, "max_answers": 1, "interval": 60})

    def test_invalid_number_is_asked_again(self):
        view, output = self.view(["99", "3", "0", "45", "y"])
        self.assertEqual(view.qa_settings({}),
                         {"limit": 3, "max_answers": 0, "interval": 45})
        self.assertIn("请输入 1–20 的整数。", output.getvalue())

    def test_saved_values_become_defaults(self):
        view, _ = self.view(["", "", "", "y"])
        saved = {"limit": 5, "max_answers": 2, "interval": 30}
        self.assertEqual(view.qa_settings(saved), saved)


class TuiExecuteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SettingsStore(Path(self.temp.name))
        self.view = TerminalUI(console=Console(file=io.StringIO(), theme=theme.THEME, width=120),
                               input_fn=lambda: "")
        self.app = Application(self.view, self.store)
        self.app.verify_ai = Mock(return_value=FakeAi())

    def run_qa(self, courses, qa):
        bridge = Mock()
        bridge.return_value.__enter__ = Mock(return_value=qa)
        bridge.return_value.__exit__ = Mock(return_value=False)
        with patch("ZHSMain.qa.QaBridge", bridge):
            stats = self.app.execute_zhs_qa(Mock(access_token="[]"), courses, Mock(),
                                            {"limit": 3, "max_answers": 1, "interval": 0})
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
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.view = TerminalUI(console=Console(file=io.StringIO(), theme=theme.THEME, width=120),
                               input_fn=lambda: "")
        self.app = Application(self.view, SettingsStore(Path(self.temp.name)))

    def publish(self, courses, qa):
        bridge = Mock()
        bridge.return_value.__enter__ = Mock(return_value=qa)
        bridge.return_value.__exit__ = Mock(return_value=False)
        with patch("ZHSMain.qa.QaBridge", bridge):
            stats = self.app.execute_zhs_ask(Mock(access_token="[]"), courses, QUESTIONS,
                                             {"interval": 0})
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
        ai = FakeAi(LONG)
        self.app.verify_ai = Mock(return_value=ai)
        qa = FakeQa([{"id": 1, "content": QUESTIONS[0]}, {"id": 2, "content": QUESTIONS[1]}])
        bridge = Mock()
        bridge.return_value.__enter__ = Mock(return_value=qa)
        bridge.return_value.__exit__ = Mock(return_value=False)
        with patch("ZHSMain.qa.QaBridge", bridge):
            texts = self.app.generate_zhs_questions(Mock(access_token="[]"), [COURSE], Mock(),
                                                    {"count": 1})
        self.assertEqual(texts, [LONG])
        self.assertIn(QUESTIONS[0], ai.questions[0]["title"])
        self.assertEqual(bridge.call_args.kwargs["recruit_id"], 426135)
        self.assertTrue(ai.session.close.called)

    def test_generate_without_qa_area_falls_back(self):
        ai = FakeAi(LONG)
        self.app.verify_ai = Mock(return_value=ai)
        with patch("ZHSMain.qa.QaBridge") as bridge:
            self.app.generate_zhs_questions(Mock(access_token="[]"), [{"courseName": "无问答区"}],
                                            Mock(), {"count": 1})
        bridge.assert_not_called()
        self.assertIn("课程的核心概念", ai.questions[0]["title"])


class WorkspaceTest(unittest.TestCase):
    def test_course_menu_enters_qa_area(self):
        from course_flow import CourseWorkspace
        from ui import Back

        view = MagicMock()
        view.course_menu.side_effect = [4, Back()]
        view.qa_settings.return_value = {"limit": 2, "max_answers": 0, "interval": 30}
        app = MagicMock()
        app.store.get.return_value = {}
        app.remember.side_effect = lambda operation: operation()
        # 统计字典不是文件路径：汇总页不能把它当成 answered/failed/skipped 三个文件。
        app.execute_zhs_qa.return_value = {"answered": 9, "failed": 0, "skipped": 0}
        workspace = CourseWorkspace(app)
        workspace.view, workspace.store, workspace.platform = view, app.store, 2
        workspace.client = Mock()
        workspace.course_loop([COURSE])
        app.execute_zhs_qa.assert_called_once()
        self.assertEqual(view.qa_settings.call_args.args[0], {})
        self.assertEqual(app.execute_zhs_qa.call_args.args[1], [COURSE])
        self.assertEqual(view.operation_result.call_args.args[0], [])
        self.assertEqual(app.store.set.call_args.args,
                         ("qa", {"limit": 2, "max_answers": 0, "interval": 30}))

    def test_course_menu_publishes_questions(self):
        from course_flow import CourseWorkspace
        from ui import Back

        view = MagicMock()
        view.course_menu.side_effect = [5, Back()]
        view.qa_ask_settings.return_value = {"source": "manual", "text": QUESTIONS[0],
                                             "count": 1, "interval": 30}
        view.qa_questions_review.return_value = [QUESTIONS[0]]
        app = MagicMock()
        app.store.get.return_value = {}
        app.remember.side_effect = lambda operation: operation()
        workspace = CourseWorkspace(app)
        workspace.view, workspace.store, workspace.platform = view, app.store, 2
        workspace.client = Mock()
        workspace.course_loop([COURSE])
        app.execute_zhs_ask.assert_called_once()
        self.assertEqual(app.execute_zhs_ask.call_args.args[2], [QUESTIONS[0]])
        self.assertEqual(view.operation_result.call_args.args[0], [])

    def test_prepare_questions_manual_mode_skips_ai(self):
        from course_flow import CourseWorkspace

        view, app = MagicMock(), MagicMock()
        view.qa_questions_review.return_value = [QUESTIONS[0]]
        workspace = CourseWorkspace(app)
        workspace.view = view
        texts = workspace.prepare_questions([COURSE], {"source": "manual", "text": QUESTIONS[0]})
        self.assertEqual(texts, [QUESTIONS[0]])
        app.ai_configuration.assert_not_called()

    def test_generation_error_is_shown_and_retryable(self):
        from course_flow import CourseWorkspace

        view, app = MagicMock(), MagicMock()
        app.ai_configuration.side_effect = [RuntimeError("AI 服务 HTTP 503"), MagicMock()]
        app.generate_zhs_questions.return_value = [QUESTIONS[0]]
        view.confirm.return_value = True
        view.qa_questions_review.return_value = [QUESTIONS[0]]
        workspace = CourseWorkspace(app)
        workspace.view = view
        texts = workspace.prepare_questions([COURSE], {"source": "ai", "count": 1})
        self.assertEqual(texts, [QUESTIONS[0]])
        self.assertEqual(view.confirm.call_count, 1)
        self.assertTrue(any("503" in str(call.args[0]) for call in view.note.call_args_list))

    def test_generation_error_can_be_abandoned(self):
        from course_flow import CourseWorkspace

        view, app = MagicMock(), MagicMock()
        app.ai_configuration.side_effect = RuntimeError("AI 服务 HTTP 503")
        view.confirm.return_value = False
        workspace = CourseWorkspace(app)
        workspace.view = view
        self.assertEqual(workspace.prepare_questions([COURSE], {"source": "ai", "count": 1}), [])
        view.qa_questions_review.assert_not_called()

    def test_cancelled_generation_stays_in_menu(self):
        from course_flow import CourseWorkspace

        view, app = MagicMock(), MagicMock()
        app.ai_configuration.side_effect = KeyboardInterrupt
        workspace = CourseWorkspace(app)
        workspace.view = view
        texts = workspace.prepare_questions([COURSE], {"source": "ai", "topic": "主题", "count": 1})
        self.assertEqual(texts, [])
        view.qa_questions_review.assert_not_called()


if __name__ == "__main__":
    unittest.main()
