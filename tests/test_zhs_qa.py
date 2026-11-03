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
from settings import SettingsStore
from ui import TerminalUI
from ZHSMain.qa import CaptchaRequired, QaBridge, answer_questions

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
        self.text, self.calls = text, 0
        self.session = Mock()

    def answer(self, question):
        self.calls += 1
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
        workspace = CourseWorkspace(app)
        workspace.view, workspace.store, workspace.platform = view, app.store, 2
        workspace.client = Mock()
        workspace.course_loop([COURSE])
        app.execute_zhs_qa.assert_called_once()
        self.assertEqual(view.qa_settings.call_args.args[0], {})
        self.assertEqual(app.execute_zhs_qa.call_args.args[1], [COURSE])
        self.assertEqual(app.store.set.call_args.args,
                         ("qa", {"limit": 2, "max_answers": 0, "interval": 30}))


if __name__ == "__main__":
    unittest.main()
