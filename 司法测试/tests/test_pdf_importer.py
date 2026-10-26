import unittest
from pathlib import Path

from pdf_importer import EXPECTED_COUNTS, parse_question_bank


class BundledPdfTests(unittest.TestCase):
    def test_bundled_bank_has_expected_counts_and_pdf_order(self):
        pdf = Path(__file__).resolve().parents[1] / "题库（电子版）.pdf"
        questions = parse_question_bank(pdf.read_bytes())
        observed = {section: [q.number for q in questions if q.section == section] for section in EXPECTED_COUNTS}
        self.assertEqual({key: len(numbers) for key, numbers in observed.items()}, EXPECTED_COUNTS)
        for section, numbers in observed.items():
            self.assertEqual(numbers, list(range(1, EXPECTED_COUNTS[section] + 1)))
        self.assertEqual([q.section for q in questions[:EXPECTED_COUNTS["single"]]], ["single"] * EXPECTED_COUNTS["single"])
        self.assertEqual(questions[EXPECTED_COUNTS["single"]].section, "multiple")


if __name__ == "__main__":
    unittest.main()
