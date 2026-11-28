"""Arrival cutoff jitter must not hide overload or partial accepted workflows."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from qualify_control_load import qualifies


class LoadVerdictTests(unittest.TestCase):
    def fixture(self, **changes):
        return dict(dict(intended_iterations=100, completed_iterations=100, arrival_rate=10,
            k6_exit=0, semantic_errors=0, accounting_consistent=True,
            operation_success_rate=1, dropped_iterations=0), **changes)

    def test_k6_cutoff_extra_iteration_is_counted_not_failed(self):
        self.assertTrue(qualifies(self.fixture(completed_iterations=101)))
        self.assertFalse(qualifies(self.fixture(completed_iterations=102)))
        self.assertFalse(qualifies(self.fixture(arrival_rate=0, completed_iterations=101)))

    def test_missing_and_dropped_work_cannot_pass(self):
        self.assertFalse(qualifies(self.fixture(completed_iterations=99)))
        self.assertFalse(qualifies(self.fixture(dropped_iterations=1)))

    def test_partial_work_and_errors_cannot_pass_despite_complete_count(self):
        for change in ({'accounting_consistent':False}, {'operation_success_rate':.99},
                       {'semantic_errors':1}, {'k6_exit':99}):
            with self.subTest(change=change):
                self.assertFalse(qualifies(self.fixture(**change)))


if __name__ == '__main__':
    unittest.main()
