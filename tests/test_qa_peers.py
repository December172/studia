"""People on one hosted site answer each other's questions first, and answering never holds up asking."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from settings import SettingsStore
from web.hosting import Hub
from web.server import PEER_DELAY, Studia, _Board, drain


def posts(hub, uid, rows):
    folder = Path(hub.data) / "users" / uid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "zhs-qa-posts.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


class PeerQuestionsTest(unittest.TestCase):
    def test_classmates_questions_with_how_many_of_us_answered(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        hub = Hub(directory.name, "http://127.0.0.1:1", key=os.urandom(32))
        ask = lambda qid, at, recruit=7: {"course": 1, "recruit": recruit, "kind": "ask", "question": "q%d" % qid, "id": qid, "at": at}
        answer = lambda qid: {"course": 1, "recruit": 7, "kind": "answer", "question": "", "id": 900 + qid, "qid": qid, "at": 5}
        posts(hub, "me", [ask(1, 10), answer(2)])                 # my own question is not a peer's
        posts(hub, "amy", [ask(2, 20), ask(3, 30, recruit=8)])    # another class's question is not on my board
        posts(hub, "bob", [answer(2), ask(4, 15)])
        self.assertEqual([(q["id"], q["ours"]) for q in hub.peer_questions("me", 1, 7)], [(4, 0), (2, 2)])


class BoardOrderTest(unittest.TestCase):
    def test_peer_questions_come_first_once_settled_and_until_answered_enough(self):
        now = time.time()
        bridge = Mock()
        bridge.questions.return_value = [{"id": 50, "content": "板上的提问为什么值得讨论？", "answers": 0}]
        peers = [{"id": 1, "text": "同学的提问一为什么重要？", "at": now - PEER_DELAY - 60, "ours": 0},
                 {"id": 2, "text": "刚发的提问为什么重要？", "at": now - 60, "ours": 0},
                 {"id": 3, "text": "答够了的提问为什么重要？", "at": now - PEER_DELAY - 60, "ours": 5}]
        board = _Board(bridge, Mock(), peers=lambda: peers)
        self.assertEqual([q["id"] for q in board.questions(limit=5)], [1, 50])


class AskWhileAnsweringTest(unittest.TestCase):
    def test_a_running_answering_job_does_not_block_asking(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = Studia(SettingsStore(directory.name), oauth=Mock(return_value=None))
        platform = app.platforms["zhs"]
        platform.rows = [{"courseName": "导论", "zhs": {"kind": "shared", "courseId": 1, "recruitId": 7}}]
        platform.client = Mock(access_token="[]")
        release = threading.Event()
        app._qa = lambda *args: release.wait(5) and "ok"
        app._ask = lambda *args: "ok"
        app.start_job({"platform": "zhs", "course": "shared-1", "type": "qa", "limit": 3})
        app.start_job({"platform": "zhs", "course": "shared-1", "type": "ask", "texts": ["边缘计算和云计算在工厂里应该怎样分工？"]})
        release.set()
        drain(5)
        self.assertEqual(sorted(h["kind"] for h in app.history), ["ask", "qa"])


if __name__ == "__main__":
    unittest.main()
