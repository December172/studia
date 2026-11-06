import json
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ai.client import AIResponseError
from settings import SettingsStore
from web.server import JOBS, HttpError, Studia, _ask_texts, _Board, drain

ROW = {"courseName": "新一代信息技术导论", "zhs": {"kind": "shared", "courseId": 1000091138, "recruitId": 444872}}
LONG = "这是一段足够长的回答，讲清楚了概念和它的用途。" * 3
DRAFT = "边缘计算和云计算各自适合什么样的业务场景？"


class FakeBridge:
    """Stands in for QaBridge: a board with questions by answer count; records what gets posted."""

    def __init__(self, board=None):
        self.board = board or [{"id": 1, "content": "分层架构为什么能让系统更容易维护？", "answers": 1},
                               {"id": 2, "content": "边缘计算和云计算在工厂里应该怎样分工？", "answers": 0},
                               {"id": 3, "content": "物联网设备的安全为什么总是难以保障？", "answers": 5}]
        self.published, self.asked = [], []

    def __enter__(self): return self
    def __exit__(self, *exc): pass

    def questions(self, max_answers=1, limit=10, **_):
        return [dict(q) for q in self.board if q["answers"] <= max_answers][:limit]

    def publish(self, qid, text):
        self.published.append((qid, text))
        return 9000 + qid

    def ask(self, text):
        self.asked.append(text)
        return 7000 + len(self.asked)


class WebQaTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = Studia(SettingsStore(directory.name), oauth=Mock(return_value=None))
        platform = self.app.platforms["zhs"]
        platform.rows, platform.client = [ROW], Mock(access_token="[]")
        self.app._ai = Mock(return_value=Mock(answer=Mock(return_value={"answer": LONG})))
        self.addCleanup(drain, 5)

    def run_job(self, body, bridge):
        with patch("web.server.QaBridge", return_value=bridge), patch("web.server.QA_PACE", (0, 0)), \
                patch("web.server.ASK_PACE", (0, 0)):
            self.app.start_job(dict(body, platform="zhs", course="shared-1000091138"))
            deadline = time.time() + 5
            while self.app.jobs and time.time() < deadline:
                time.sleep(0.05)
        return self.app.history[0]

    def test_unanswered_questions_go_first_and_every_post_is_listed(self):
        bridge = FakeBridge()
        job = self.run_job({"type": "qa", "limit": 2}, bridge)
        self.assertEqual(job["state"], "done")
        self.assertEqual([qid for qid, _ in bridge.published], [2, 1])
        posts = self.app.qa_posts("zhs", "shared-1000091138")["posts"]
        self.assertEqual([(p["kind"], p["question"]) for p in posts], [("answer", "分层架构为什么能让系统更容易维护？"), ("answer", "边缘计算和云计算在工厂里应该怎样分工？")])
        self.assertEqual(posts[0]["text"], LONG)

    def test_asking_publishes_the_reviewed_texts(self):
        bridge = FakeBridge()
        job = self.run_job({"type": "ask", "texts": ["  请问 TCP 三次握手为什么不能是两次？ ", "云计算和边缘计算各适合什么场景？"]}, bridge)
        self.assertEqual(job["state"], "done")
        self.assertEqual(bridge.asked, ["TCP 三次握手为什么不能是两次？", "云计算和边缘计算各适合什么场景？"])
        self.assertEqual(job["summary"], "发布了 2 条提问")  # not to be mistaken for the answering job
        self.assertEqual(self.app.qa_posts("zhs", "shared-1000091138")["posts"][0]["kind"], "ask")

    def test_ask_texts_are_checked(self):
        for bad in ({}, {"texts": []}, {"texts": ["太短？"]}, {"texts": [DRAFT] * 6},
                    {"texts": ["云计算好吗？"]}, {"texts": ["物联网安全为什么难做？？"]}):
            with self.assertRaises(HttpError):
                _ask_texts(bad)
        self.assertEqual(_ask_texts({"texts": [DRAFT, DRAFT]}), [DRAFT])

    def test_draft_writes_one_question_at_a_time_from_the_quiet_threads(self):
        ai = Mock(answer=Mock(return_value={"answer": "边缘计算和云计算各自适合什么样的业务场景？"}))
        self.app._ai = Mock(return_value=ai)
        with patch("web.server.QaBridge", return_value=FakeBridge()) as bridge:
            first = self.app.qa_draft("zhs", {"course": "shared-1000091138"})
            again = self.app.qa_draft("zhs", {"course": "shared-1000091138", "have": first["questions"]})
        self.assertEqual(first["questions"], ["边缘计算和云计算各自适合什么样的业务场景？"])
        self.assertEqual(again["questions"], [])  # the one the page already has is not repeated
        prompt = ai.answer.call_args.args[0]["title"]
        self.assertIn("边缘计算和云计算在工厂里应该怎样分工？", prompt)
        self.assertIn("边缘计算和云计算各自适合什么样的业务场景？", prompt)  # shown to the model so it writes something else
        self.assertNotIn("物联网设备的安全为什么总是难以保障？", prompt)
        self.assertEqual(bridge.call_count, 1)  # the board is read once, not per question
        self.assertFalse(JOBS)

    def test_draft_retries_a_broken_model_answer(self):
        ai = Mock()
        ai.answer = Mock(side_effect=[AIResponseError("AI 响应不是完整的答案 JSON"),
                                      {"answer": "边缘计算和云计算怎样分工才能降低工厂的延迟？", "explanation": "换个方向"}])
        self.app._ai = Mock(return_value=ai)
        with patch("web.server.QaBridge", return_value=FakeBridge()), \
                patch("ZHSMain.qa.time.sleep"):
            result = self.app.qa_draft("zhs", {"course": "shared-1000091138"})
        self.assertEqual(result["questions"], ["边缘计算和云计算怎样分工才能降低工厂的延迟？"])
        self.assertEqual(ai.answer.call_count, 2)

    def test_draft_gives_up_after_repeated_broken_responses(self):
        ai = Mock(answer=Mock(side_effect=AIResponseError(
            "AI 响应不是完整的答案 JSON；本题未作答（finish_reason=length）")))
        self.app._ai = Mock(return_value=ai)
        with patch("web.server.QaBridge", return_value=FakeBridge()):
            result = self.app.qa_draft("zhs", {"course": "shared-1000091138"})
        self.assertEqual(result["questions"], [])
        self.assertEqual(ai.answer.call_count, 3)
        ai.session.close.assert_called_once()

    def test_daily_limit_and_cap_hold_back_jobs(self):
        for n in range(10):  # ten answers already posted today
            self.app._record_post(1000091138, "answer", "q", "a", n)
        with self.assertRaises(HttpError) as refused:
            self.app.start_job({"platform": "zhs", "course": "shared-1000091138", "type": "qa", "limit": 5})
        self.assertIn("明天再来", str(refused.exception))
        self.app.platforms["zhs"].client.score_earned.return_value = {"qa": {"questions": 8, "answers": 0}}
        self.app.platforms["zhs"].cache.clear()  # the score the first try read
        room = self.app.qa_posts("zhs", "shared-1000091138")["room"]
        self.assertEqual((room["ask"], room["answer"]), (2, 0))  # 10 questions score, 8 already count
        with self.assertRaises(HttpError) as refused:
            self.app.start_job({"platform": "zhs", "course": "shared-1000091138", "type": "ask", "texts": [DRAFT, LONG[:20] + "为什么？", "云计算的弹性扩展为什么会让成本难以预估？"]})
        self.assertIn("还能发 2 条", str(refused.exception))
        self.assertFalse(JOBS)

    def test_drafting_waits_for_slow_models_without_testing_first(self):
        config = Mock(timeout=120, api_format="chat_completions")
        self.app.store.ai_config = Mock(return_value=config)
        with patch("web.server.replace", side_effect=lambda c, **kw: kw) as copied, \
                patch("web.server.AIClient") as client:
            Studia._ai(self.app, "", timeout=300)
        self.assertEqual(copied.call_args.kwargs["timeout"], 300)
        client.return_value.check_connection.assert_not_called()


def setUpModule():
    # Task tests have a daytime clock; curfew boundaries live in test_quiet_hours.py.
    daytime = unittest.mock.patch("web.server.quiet_hours", return_value={"active": False})
    daytime.start()
    unittest.addModuleCleanup(daytime.stop)


if __name__ == "__main__":
    unittest.main()
