import json
import os
import tempfile
import unittest

from ZHSMain.qa import CaptchaRequired, answer_questions

LONG = "这是一段超过三十个字的回答，用来通过最小长度校验，确保回答不会为空。"


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


if __name__ == "__main__":
    unittest.main()
