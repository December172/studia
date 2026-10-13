"""Human answers are converted to the same typed values the AI path produces."""
import unittest

from ai.questions import manual_answer_value, prepare_question, question_fingerprint
from tests.test_ai import question


class FingerprintTests(unittest.TestCase):
    def prepared(self, kind=1, **changes):
        raw = dict(question(kind), **changes)
        return prepare_question(raw, 0)

    def test_the_same_question_has_the_same_fingerprint(self):
        self.assertEqual(question_fingerprint(self.prepared()), question_fingerprint(self.prepared()))

    def test_title_options_context_type_and_blank_count_change_the_fingerprint(self):
        base = question_fingerprint(self.prepared())
        changed = [
            self.prepared(title="改过的题干"),
            self.prepared(dataJson='[{"SortOrder": 3, "Content": "钢琴"}, {"SortOrder": 0, "Content": "椅子"}]'),
            prepare_question(question(1, "child"), 0, {"id": "parent", "title": "材料"}, 0),
            prepare_question(question(2), 0),
            prepare_question(question(4), 0),
        ]
        for variant in changed:
            with self.subTest(fingerprint=variant):
                self.assertNotEqual(base, question_fingerprint(variant))

    def test_fingerprint_records_child_mapping(self):
        child = prepare_question(question(1, "child"), 2, {"id": "parent", "title": "材料"}, 1)
        fingerprint = question_fingerprint(child)
        self.assertEqual((fingerprint["paper_id"], fingerprint["paper_sub_id"]), ("parent", "child"))
        self.assertEqual((fingerprint["number"], fingerprint["child_number"]), (2, 1))


class ManualValueTests(unittest.TestCase):
    def prepared(self, kind):
        return prepare_question(question(kind), 0)

    def test_single_choice_accepts_a_letter_that_is_not_a_back_key(self):
        q = self.prepared(1)
        self.assertEqual(manual_answer_value(q, "B"), "B")
        self.assertEqual(manual_answer_value(q, "b"), "B")
        self.assertEqual(manual_answer_value(q, " A "), "A")

    def test_single_choice_rejects_unknown_or_multiple_letters(self):
        q = self.prepared(1)
        for text in ("", "Z", "AB", "1"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                manual_answer_value(q, text)

    def test_multiple_choice_reads_letters_in_input_order(self):
        q = self.prepared(2)
        self.assertEqual(manual_answer_value(q, "ab"), ["A", "B"])
        self.assertEqual(manual_answer_value(q, "BA"), ["B", "A"])
        self.assertEqual(manual_answer_value(q, "a"), ["A"])
        for text in ("", "ac", "aa"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                manual_answer_value(q, text)

    def test_judgement_maps_words_to_booleans(self):
        q = self.prepared(3)
        for text in ("y", "Y", "yes", "对", "true", "1"):
            with self.subTest(text=text):
                self.assertIs(manual_answer_value(q, text), True)
        for text in ("n", "N", "no", "错", "false", "0"):
            with self.subTest(text=text):
                self.assertIs(manual_answer_value(q, text), False)
        with self.assertRaises(ValueError):
            manual_answer_value(q, "maybe")

    def test_blanks_split_on_the_pipe_and_must_fill_every_blank(self):
        q = self.prepared(4)
        self.assertEqual(manual_answer_value(q, "钢琴 | 小提琴"), ["钢琴", "小提琴"])
        for text in ("钢琴", "钢琴 | ", "a | b | c"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                manual_answer_value(q, text)

    def test_essay_keeps_the_typed_text_and_rejects_emptiness(self):
        q = self.prepared(6)
        self.assertEqual(manual_answer_value(q, " 说明 "), "说明")
        for text in ("", "   "):
            with self.subTest(text=text), self.assertRaises(ValueError):
                manual_answer_value(q, text)


if __name__ == "__main__":
    unittest.main()
