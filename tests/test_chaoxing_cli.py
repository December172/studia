import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from ChaoxingMain.__main__ import main, parse_args


class CliTest(unittest.TestCase):
    def test_only_explicit_answer_opens_work(self):
        for flags in ([], ["--list"], ["--study"], ["--answer"], ["--study", "--answer"]):
            with self.subTest(flags=flags):
                client = Mock()
                client.course_list.return_value = [{"courseId": "1", "title": "课程"}]
                client.course_points.return_value = {"points": []}
                answer = Mock(counts={"submitted": 0, "saved": 0, "skipped": 0,
                                      "error": 0, "submission_unknown": 0})
                with patch("ChaoxingMain.__main__.ChaoxingApi", return_value=client), \
                        patch("ChaoxingMain.__main__.ChaoxingStudyHandler") as study, \
                        patch("ChaoxingMain.__main__.ChaoxingHandler", return_value=answer) as handler, \
                        patch("ChaoxingMain.__main__.AIClient") as ai, \
                        patch("ChaoxingMain.__main__.SettingsStore"), redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--username", "u", "--password", "p"] + flags), 0)
                self.assertEqual(handler.called, "--answer" in flags)
                self.assertEqual(ai.called, "--answer" in flags)
                self.assertEqual(study.called, "--study" in flags)
                client.close.assert_called_once()

    def test_submit_requires_explicit_answer(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            parse_args(["--submit"])
        self.assertEqual(caught.exception.code, 2)
        self.assertTrue(parse_args(["--answer", "--submit"]).answer)


if __name__ == "__main__":
    unittest.main()
