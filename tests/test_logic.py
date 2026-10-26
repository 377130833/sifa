import unittest

from logic import grade_answer, validate_exam_config


class ExamLogicTests(unittest.TestCase):
    def test_multiple_choice_is_all_or_nothing_by_default(self):
        result = grade_answer("multiple", ["A"], "AC", 2)
        self.assertFalse(result["correct"])
        self.assertEqual(result["earned"], 0)

    def test_multiple_choice_partial_credit_can_be_enabled(self):
        result = grade_answer("multiple", ["C", "A"], "AC", 2, multiple_partial_credit=True)
        self.assertTrue(result["correct"])
        self.assertEqual(result["earned"], 2)
        partial = grade_answer("multiple", ["A"], "AC", 2, multiple_partial_credit=True)
        self.assertFalse(partial["correct"])
        self.assertEqual(partial["earned"], 1)
        self.assertEqual(grade_answer("multiple", ["A", "B"], "AC", 2, multiple_partial_credit=True)["earned"], 0)

    def test_fill_blank_partial_credit_is_per_answer_slot(self):
        result = grade_answer("fill_blank", ["北京", "错误"], "北京，上海", 4, fill_partial_credit=True)
        self.assertFalse(result["correct"])
        self.assertEqual(result["earned"], 2)

    def test_wrong_answer_penalty_applies_to_wrong_attempts_but_not_blank_answers(self):
        self.assertEqual(grade_answer("single", "B", "A", 1, wrong_penalty=0.25)["earned"], -0.25)
        self.assertEqual(grade_answer("single", None, "A", 1, wrong_penalty=0.25)["earned"], 0)
        self.assertEqual(grade_answer("multiple", ["A", "B"], "AC", 2, wrong_penalty=0.5)["earned"], -0.5)
        self.assertEqual(grade_answer("true_false", "错", "对", 1, wrong_penalty=0.25)["earned"], -0.25)
        self.assertEqual(grade_answer("fill_blank", "错误", "正确", 1, wrong_penalty=0.25)["earned"], -0.25)
        self.assertEqual(grade_answer("fill_blank", None, "正确", 1, wrong_penalty=0.25)["earned"], 0)

    def test_partial_credit_is_not_also_penalized(self):
        multiple = grade_answer("multiple", ["A"], "AC", 2, multiple_partial_credit=True, wrong_penalty=0.5)
        fill = grade_answer("fill_blank", ["北京", "错误"], "北京，上海", 4, fill_partial_credit=True, wrong_penalty=0.5)
        fill_all_wrong = grade_answer("fill_blank", ["错误", "也错"], "北京，上海", 4, fill_partial_credit=True, wrong_penalty=0.5)
        self.assertEqual(multiple["earned"], 1)
        self.assertEqual(fill["earned"], 2)
        self.assertEqual(fill_all_wrong["earned"], -0.5)

    def test_verified_config_requires_reference(self):
        config = {
            "duration_minutes": 90,
            "counts": {"single": 2, "multiple": 2, "true_false": 0, "fill_blank": 0},
            "points": {"single": 1, "multiple": 2, "true_false": 0, "fill_blank": 0},
            "official_verified": True,
            "official_reference": "正式通知.pdf",
        }
        self.assertTrue(validate_exam_config(config)["official_verified"])
        config["official_reference"] = ""
        with self.assertRaises(ValueError):
            validate_exam_config(config)

    def test_non_finite_scores_are_rejected(self):
        config = {
            "duration_minutes": 90,
            "counts": {"single": 1},
            "points": {"single": float("nan")},
        }
        with self.assertRaises(ValueError):
            validate_exam_config(config)

    def test_wrong_penalties_are_validated_and_default_to_zero(self):
        config = {
            "duration_minutes": 90,
            "counts": {"single": 1},
            "points": {"single": 1},
        }
        normalized = validate_exam_config(config)
        self.assertEqual(normalized["wrong_penalties"], {"single": 0, "multiple": 0, "true_false": 0, "fill_blank": 0})
        config["wrong_penalties"] = {"single": -1}
        with self.assertRaises(ValueError):
            validate_exam_config(config)


if __name__ == "__main__":
    unittest.main()
