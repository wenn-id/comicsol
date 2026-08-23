from __future__ import annotations

import unittest

from scripts.check_coverage import (
    BRANCH_COVERAGE_FLOOR,
    LINE_COVERAGE_FLOOR,
    check_coverage,
    coverage_percentages,
)


class CoverageGateTests(unittest.TestCase):
    @staticmethod
    def _report(*, lines: tuple[int, int], branches: tuple[int, int]):
        return {
            "totals": {
                "covered_lines": lines[0],
                "num_statements": lines[1],
                "covered_branches": branches[0],
                "num_branches": branches[1],
            }
        }

    def test_floors_are_locked_to_documented_application_baseline(self):
        self.assertEqual(82.0, LINE_COVERAGE_FLOOR)
        self.assertEqual(72.0, BRANCH_COVERAGE_FLOOR)

    def test_gate_checks_line_and_branch_percentages_independently(self):
        report = self._report(lines=(82, 100), branches=(72, 100))
        self.assertEqual((82.0, 72.0), check_coverage(report))

        with self.assertRaisesRegex(ValueError, "line 81.00%"):
            check_coverage(self._report(lines=(81, 100), branches=(100, 100)))
        with self.assertRaisesRegex(ValueError, "branch 71.00%"):
            check_coverage(self._report(lines=(100, 100), branches=(71, 100)))

    def test_malformed_or_empty_totals_fail_closed(self):
        for report in ({}, {"totals": {}}, self._report(lines=(0, 0), branches=(0, 0))):
            with self.subTest(report=report), self.assertRaises(ValueError):
                coverage_percentages(report)


if __name__ == "__main__":
    unittest.main()
