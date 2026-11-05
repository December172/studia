import unittest
from unittest.mock import Mock

import requests
from requests.cookies import RequestsCookieJar

from ai.client import AIServiceError
from ChaoxingMain.api import ChaoxingApi
from ChaoxingMain.decode import _choice_text, _document, _title_text, decode_questions_info
from ZHSMain.answer import IMAGE_LIMIT, download_images
from tests.test_chaoxing_exercise import COURSE, JOB, JOB_INFO, POINT, FakeClient, question_block, runner, work_page


def response(chunks=(b"picture",), status=200, mime="image/png"):
    return Mock(status_code=status, headers={"Content-Type": mime},
                iter_content=Mock(return_value=iter(chunks)))


class ImageDownloadTest(unittest.TestCase):
    def setUp(self):
        self.session = Mock(headers={}, cookies=RequestsCookieJar())
        self.session.get.return_value = response()
        self.api = ChaoxingApi(cookies={"UID": "synthetic-only"}, session=self.session)
        self.addCleanup(self.api.close)

    def test_authenticated_image_download_is_streamed_without_redirects(self):
        url = "https://p.ananas.chaoxing.com/star3/origin/diagram.png"
        self.assertEqual(download_images({"figure": url}, get=self.api.work_image), [("figure", "image/png", b"picture")])
        self.session.get.assert_called_once_with(url, headers={"Referer": "https://mooc1.chaoxing.com/"},
                                                 timeout=(5, 15), allow_redirects=False, stream=True)
        self.assertEqual(self.session.cookies.get("UID"), "synthetic-only")
        self.session.get.return_value.close.assert_called_once()

    def test_external_local_plain_http_and_userinfo_urls_never_receive_the_session(self):
        for url in ("https://evil.example/x", "https://chaoxing.com.evil.example/x", "https://evilchaoxing.com/x",
                    "http://p.ananas.chaoxing.com/x", "https://127.0.0.1/x", "https://localhost/x",
                    "file:///etc/passwd", "https://u:p@mooc1.chaoxing.com/x", "https://mooc1.chaoxing.com:8443/x"):
            with self.subTest(url=url), self.assertRaises(requests.RequestException):
                self.api.work_image(url)
        self.session.get.assert_not_called()

    def test_redirect_and_html_are_not_passed_to_ai(self):
        for bad in (response(status=302), response(mime="text/html")):
            with self.subTest(status=bad.status_code):
                self.session.get.return_value = bad
                self.assertIsNone(download_images({"figure": "https://mooc1.chaoxing.com/x"}, get=self.api.work_image))
                bad.iter_content.assert_not_called()
                bad.close.assert_called_once()

    def test_size_limit_stops_reading_and_closes_the_response(self):
        def chunks():
            yield b"x" * IMAGE_LIMIT
            yield b"x"
            raise AssertionError("oversized image must not be read to the end")
        data = response(chunks())
        self.assertIsNone(download_images({"figure": "https://mooc1.chaoxing.com/x"}, get=Mock(return_value=data)))
        data.close.assert_called_once()


class ImageQuestionTest(unittest.TestCase):
    def page(self):
        return work_page('<div class="singleQuesId" data="q1"><div class="TiMu" data="0">'
                         '<input name="answerq1" value="">'
                         '<div class="Zy_TItle">看图<img src="//p.ananas.chaoxing.com/a.png?x=1&amp;y=2"></div>'
                         '<ul><li><input type="radio" name="answerq1" value="B">'
                         '<img src="https://p.ananas.chaoxing.com/b.png"></li>'
                         '<li aria-label="A. 第一幅"><input type="radio" name="answerq1" value="A">'
                         '<img data-src="//p.ananas.chaoxing.com/a.png?x=1&amp;y=2"></li></ul></div></div>')

    def test_title_and_image_only_options_keep_native_labels_and_deduplicate_downloads(self):
        sheet = decode_questions_info(self.page())
        self.assertEqual([o["label"] for o in sheet["questions"][0]["options"]], ["A", "B"])
        client = FakeClient(self.page())
        client.work_image = Mock(side_effect=lambda *a, **kw: response())
        ai = Mock(answer=Mock(return_value={"answer": "B"}))
        runner(client, ai).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(client.payloads[0]["answerq1"], "B")
        self.assertEqual(client.work_image.call_count, 2)
        urls = [c.args[0] for c in client.work_image.call_args_list]
        self.assertEqual(urls[0], "https://p.ananas.chaoxing.com/a.png?x=1&y=2")
        prompt = ai.answer.call_args.args[0]
        self.assertIn("[图片:cx-image-1]", prompt["title"])
        self.assertIn("[图片:cx-image-1]", prompt["options"][0]["text"])
        self.assertIn("[图片:cx-image-2]", prompt["options"][1]["text"])
        self.assertNotIn("https:", str(prompt))
        self.assertEqual(len(ai.answer.call_args.kwargs["images"]), 2)

    def test_download_failure_never_turns_an_image_question_into_a_text_guess(self):
        client = FakeClient(self.page())
        client.work_image = Mock(return_value=response(status=403))
        ai = Mock()
        result = runner(client, ai).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(result["status"], "error")
        ai.answer.assert_not_called()
        self.assertEqual(client.payloads, [])

    def test_vision_provider_failure_does_not_save_or_submit(self):
        client = FakeClient(self.page())
        client.work_image = Mock(side_effect=lambda *a, **kw: response())
        ai = Mock(answer=Mock(side_effect=AIServiceError("model does not support images")))
        with self.assertRaises(AIServiceError):
            runner(client, ai).run(COURSE, POINT, JOB, JOB_INFO)
        self.assertEqual(client.payloads, [])

    def test_retry_reuses_the_same_images_and_does_not_download_again(self):
        client = FakeClient(self.page())
        client.work_image = Mock(side_effect=lambda *a, **kw: response())
        ai = Mock(answer=Mock(side_effect=[{"answer": None}, {"answer": "B"}]))
        runner(client, ai).run(COURSE, POINT, JOB, JOB_INFO)
        first, again = ai.answer.call_args_list
        self.assertEqual(first.kwargs["images"], again.kwargs["images"])
        self.assertTrue(again.kwargs["guess"])
        self.assertEqual(client.work_image.call_count, 2)

    def test_font_decoding_never_changes_image_urls_or_markers(self):
        root = _document('<div>cipher<img src="https://p.ananas.chaoxing.com/cipher.png"></div>')
        node = root.xpath(".//div")[0]
        decoder = Mock(decode=Mock(side_effect=lambda s: s.replace("cipher", "明文")))
        for decode in (_title_text, _choice_text):
            text = decode(node, decoder)
            self.assertIn("明文", text)
            self.assertIn("/cipher.png", text)


if __name__ == "__main__":
    unittest.main()
