import unittest
from resume_performance_pretrial import remaining_order


class ResumeSelectionTests(unittest.TestCase):
    def test_preserves_order_and_repeats_only_stopped_or_unrun(self):
        source = dict(planned_order=[['issue','G0',1], ['issue','G2',32], ['pr','G0',1]], rows=[
            dict(kind='issue', mode='G0', concurrency=1, status='pretrial_checked', verification={'verified': True}),
            dict(kind='issue', mode='G2', concurrency=32, status='pretrial_failed_or_stopped', stop_reason='memory', verification={'verified': True})])
        self.assertEqual(remaining_order(source), source['planned_order'][1:])

    def test_unverified_cell_is_not_silently_skipped(self):
        source = dict(planned_order=[['issue','G0',1]], rows=[dict(kind='issue', mode='G0', concurrency=1, status='pretrial_checked', verification={'verified': False})])
        self.assertEqual(remaining_order(source), source['planned_order'])

    def test_all_checked_is_empty(self):
        source = dict(planned_order=[['issue','G0',1]], rows=[dict(kind='issue', mode='G0', concurrency=1, status='pretrial_checked', verification={'verified': True})])
        self.assertEqual(remaining_order(source), [])


if __name__ == '__main__':
    unittest.main()
