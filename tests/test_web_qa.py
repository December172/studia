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


class FakeBridge:
    """Stands in for QaBridge: a board with questions by answer count; records what gets posted."""

    def __init__(self, board=None):
        self.board = board or [{"id": 1, "content": "已有一条回答的提问", "answers": 1},
                               {"id": 2, "content": "没人回答的提问", "answers": 0},
                               {"id": 3, "content": "回答很多的提问", "answers": 5}]
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
        with patch("web.server.QaBridge", return_value=bridge), patch("web.server.QA_PACE", (0, 0)):
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
        self.assertEqual([(p["kind"], p["question"]) for p in posts], [("answer", "已有一条回答的提问"), ("answer", "没人回答的提问")])
        self.assertEqual(posts[0]["text"], LONG)

    def test_asking_publishes_the_reviewed_texts(self):
        bridge = FakeBridge()
        job = self.run_job({"type": "ask", "texts": ["  请问 TCP 三次握手为什么不能是两次？ ", "云计算和边缘计算各适合什么场景？"]}, bridge)
        self.assertEqual(job["state"], "done")
        self.assertEqual(bridge.asked, ["TCP 三次握手为什么不能是两次？", "云计算和边缘计算各适合什么场景？"])
        self.assertEqual(job["summary"], "发布了 2 条提问")  # not to be mistaken for the answering job
        self.assertEqual(self.app.qa_posts("zhs", "shared-1000091138")["posts"][0]["kind"], "ask")

    def test_ask_texts_are_checked(self):
        for bad in ({}, {"texts": []}, {"texts": ["太短"]}, {"texts": ["合格的提问一二三四五"] * 6}):
            with self.assertRaises(HttpError):
                _ask_texts(bad)
        self.assertEqual(_ask_texts({"texts": ["同一个提问写了两遍", "同一个提问写了两遍"]}), ["同一个提问写了两遍"])

    def test_draft_writes_one_question_at_a_time_from_the_quiet_threads(self):
        ai = Mock(answer=Mock(return_value={"answer": "边缘计算会不会取代云计算？"}))
        self.app._ai = Mock(return_value=ai)
        with patch("web.server.QaBridge", return_value=FakeBridge()) as bridge:
            first = self.app.qa_draft("zhs", {"course": "shared-1000091138"})
            again = self.app.qa_draft("zhs", {"course": "shared-1000091138", "have": first["questions"]})
        self.assertEqual(first["questions"], ["边缘计算会不会取代云计算？"])
        self.assertEqual(again["questions"], [])  # the one the page already has is not repeated
        prompt = ai.answer.call_args.args[0]["title"]
        self.assertIn("没人回答的提问", prompt)
        self.assertIn("边缘计算会不会取代云计算？", prompt)  # shown to the model so it writes something else
        self.assertNotIn("回答很多的提问", prompt)
        self.assertEqual(bridge.call_count, 1)  # the board is read once, not per question
        self.assertFalse(JOBS)

    def test_draft_retries_a_broken_model_answer(self):
        ai = Mock()
        ai.answer = Mock(side_effect=[AIResponseError("AI 响应不是完整的答案 JSON"),
                                      {"answer": "边缘计算会不会取代云计算？", "explanation": "换个方向"}])
        self.app._ai = Mock(return_value=ai)
        with patch("web.server.QaBridge", return_value=FakeBridge()), \
                patch("ZHSMain.qa.time.sleep"):
            result = self.app.qa_draft("zhs", {"course": "shared-1000091138"})
        self.assertEqual(result["questions"], ["边缘计算会不会取代云计算？"])
        self.assertEqual(ai.answer.call_count, 2)

    def test_draft_failure_is_reported_instead_of_raising_a_traceback(self):
        ai = Mock(answer=Mock(side_effect=AIResponseError(
            "AI 响应不是完整的答案 JSON；本题未作答（finish_reason=length）")))
        self.app._ai = Mock(return_value=ai)
        with patch("web.server.QaBridge", return_value=FakeBridge()), \
                patch("ZHSMain.qa.time.sleep"):
            with self.assertRaises(HttpError) as caught:
                self.app.qa_draft("zhs", {"course": "shared-1000091138"})
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("没写出可用的提问", str(caught.exception))
        self.assertIn("finish_reason=length", str(caught.exception))

    def test_drafting_waits_for_slow_models_without_testing_first(self):
        config = Mock(timeout=120, api_format="chat_completions")
        self.app.store.ai_config = Mock(return_value=config)
        with patch("web.server.replace", side_effect=lambda c, **kw: kw) as copied, \
                patch("web.server.AIClient") as client:
            Studia._ai(self.app, "", timeout=300)
        self.assertEqual(copied.call_args.kwargs["timeout"], 300)
        client.return_value.check_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
