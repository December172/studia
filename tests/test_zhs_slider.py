"""滑块缺口定位自检：用真实易盾图片，识别结果必须稳定且落在已知缺口处。"""
import os
import unittest
from unittest.mock import Mock

import cv2

from ZHSMain import slider

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class GapTest(unittest.TestCase):
    def _images(self):
        bg = cv2.imread(os.path.join(FIXTURES, "yidun_bg.jpg"), cv2.IMREAD_COLOR)
        piece = cv2.imread(os.path.join(FIXTURES, "yidun_piece.png"), cv2.IMREAD_UNCHANGED)
        self.assertIsNotNone(bg, "缺少滑块背景样本")
        self.assertIsNotNone(piece, "缺少滑块拼图样本")
        return bg, piece

    def test_real_captcha_gap_is_stable(self):
        bg, piece = self._images()
        left, confidence = slider.find_gap(bg, piece)
        # 该样本经逐像素比对确认缺口在 x=119；重算必须复现同一位置。
        self.assertEqual(left, 119)
        self.assertGreater(confidence, 0.2)

    def test_rejects_piece_without_alpha(self):
        bg, piece = self._images()
        left, confidence = slider.find_gap(bg, piece[:, :, :3])
        self.assertEqual((left, confidence), (0, 0.0))

    def test_image_click_uses_only_dialog_and_rejects_out_of_bounds(self):
        page = Mock()
        modal = page.locator.return_value.first
        modal.bounding_box.return_value = {"x": 30, "y": 40, "width": 320, "height": 200}
        modal.screenshot.return_value = b"dialog-only"
        vision = Mock()
        vision.answer.return_value = {"answer": [[321, 40]]}
        self.assertFalse(slider._click_image(page, vision, 10))
        page.mouse.click.assert_not_called()
        self.assertEqual(vision.answer.call_args.kwargs["images"][0][2], b"dialog-only")
        vision.answer.return_value = {"answer": [[120, 80]]}
        self.assertTrue(slider._click_image(page, vision, 10))
        page.mouse.click.assert_called_once_with(150, 120)


if __name__ == "__main__":
    unittest.main()
