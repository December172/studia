import base64
import hashlib
import io
import unittest
from unittest import mock

from ChaoxingMain import font


def build_font():
    """真实的最小 TrueType 字体，用来验证「读字体 → 算字形哈希」这一段。"""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    pen = TTGlyphPen(None)
    pen.moveTo((0, 0))
    pen.lineTo((100, 0))
    pen.lineTo((100, 100))
    pen.closePath()
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", "uni2F00"])
    builder.setupCharacterMap({0x2F00: "uni2F00"})
    builder.setupGlyf({".notdef": TTGlyphPen(None).glyph(), "uni2F00": pen.glyph()})
    builder.setupHorizontalMetrics({".notdef": (1000, 0), "uni2F00": (1000, 0)})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Test", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    builder.setupMaxp()
    buffer = io.BytesIO()
    builder.save(buffer)
    return buffer.getvalue()


def encrypted_page(font_bytes):
    return ('<style id="cxSecretStyle">@font-face{font-family:"cx";'
            "src:url('data:application/font-ttf;charset=utf-8;base64,%s')}</style>"
            % base64.b64encode(font_bytes).decode())


class GlyphHashTest(unittest.TestCase):
    def test_hash_uses_coordinates_and_on_curve_flags(self):
        class Glyph:
            numberOfContours = 1
            endPtsOfContours = [1]
            coordinates = [(1, 2), (3, 4)]
            flags = [1, 0]

        self.assertEqual(font.glyph_hash(Glyph()), hashlib.md5(b"121340").hexdigest())

    def test_glyph_without_contours_has_no_hash(self):
        class Glyph:
            numberOfContours = 0

        self.assertEqual(font.glyph_hash(Glyph()), "")


class MappingTest(unittest.TestCase):
    def test_bundled_table_is_indexed_by_hash(self):
        table = font.mapping()
        self.assertGreater(len(table), 30000)
        self.assertRegex(next(iter(table)), r"^[0-9a-f]{32}$")

    def test_decrypt_maps_hash_back_to_character(self):
        table = font.mapping()
        digest = next(digest for digest, name in table.items() if name == "uni2F00")
        # 康熙部首「⼀」在平台字体里换成原字形，最后按部首表还原成「一」。
        self.assertEqual(font.decrypt({"uni2F00": digest}, chr(0x2F00)), "一")

    def test_unknown_character_is_kept(self):
        self.assertEqual(font.decrypt({}, "abc"), "abc")


class FontFileTest(unittest.TestCase):
    def test_font_map_and_decoder_end_to_end(self):
        data = build_font()
        glyphs = font.font_map(io.BytesIO(data))
        self.assertEqual(list(glyphs), ["uni2F00"])
        digest = glyphs["uni2F00"]
        self.assertRegex(digest, r"^[0-9a-f]{32}$")

        decoder = font.FontDecoder(encrypted_page(data))
        with mock.patch.object(font, "mapping", return_value={digest: "uni2F00"}):
            self.assertEqual(decoder.decode(chr(0x2F00)), "一")

    def test_encrypted_detection(self):
        self.assertTrue(font.encrypted(encrypted_page(b"x")))
        self.assertFalse(font.encrypted('<style id="other">x</style>'))
        self.assertFalse(font.encrypted(""))

    def test_decoder_rejects_page_without_font(self):
        with self.assertRaises(font.FontDecodeError):
            font.FontDecoder("<html>没有加密字体</html>")

    def test_decoder_rejects_broken_font_data(self):
        page = ('<style id="cxSecretStyle">'
                "src:url('data:application/font-ttf;charset=utf-8;base64,bm90LWEtZm9udA==')</style>")
        with self.assertRaises(font.FontDecodeError):
            font.FontDecoder(page)


if __name__ == "__main__":
    unittest.main()
