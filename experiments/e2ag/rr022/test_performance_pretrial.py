import unittest
from performance_pretrial import percentile, stop_reason, summarize


class MeasurementTests(unittest.TestCase):
    def test_nearest_rank_empty_and_small_sample(self):
        self.assertIsNone(percentile([], .95))
        self.assertEqual(percentile([4, 1, 2, 3], .5), 2)
        self.assertEqual(percentile([4, 1, 2, 3], .95), 4)

    def test_disk_stops_immediately(self):
        self.assertEqual(stop_reason(4_999_999_999, .5, 0, None, None, 0)[0], 'disk_below_5GB')

    def test_memory_requires_ten_continuous_seconds(self):
        reason, low, high = stop_reason(6e9, .1, 0, None, None, 1)
        self.assertIsNone(reason)
        self.assertIsNone(stop_reason(6e9, .1, 0, low, high, 10.9)[0])
        self.assertEqual(stop_reason(6e9, .1, 0, low, high, 11)[0], 'memory_below_15pct_for_10s')
        self.assertIsNone(stop_reason(6e9, .2, 0, low, high, 12)[1])

    def test_error_rate_requires_ten_seconds_and_resets(self):
        _, low, high = stop_reason(6e9, .5, .06, None, None, 4)
        self.assertEqual(stop_reason(6e9, .5, .06, low, high, 14)[0], 'error_rate_above_5pct_for_10s')
        self.assertIsNone(stop_reason(6e9, .5, .05, low, high, 14)[2])

    def test_drained_work_not_counted_as_inside_window_throughput(self):
        raw = {'windows':[dict(phase='measurement', start_ns=0, configured_seconds=1, drain_end_ns=2_000_000_000)],
               'records':[dict(phase='measurement',success=True,planned_ns=0,send_ns=0,admitted_ns=1,end_ns=end,calls=[]) for end in [500_000_000, 2_000_000_000]]}
        value = summarize(raw)
        self.assertEqual(value['completed'], 2)
        self.assertEqual(value['completed_inside_window'], 1)
        self.assertEqual(value['completion_rate_inside_window'], 1)
        self.assertEqual(value['drain_seconds'], 1)

    def test_failed_and_warm_rows_not_hidden_or_in_success_latency(self):
        raw = {'windows':[], 'records':[dict(phase='measurement',success=False,error='ReadTimeout: x'),dict(phase='warmup',success=True)]}
        value = summarize(raw)
        self.assertEqual(value['errors'], 1)
        self.assertEqual(value['warmup_attempts'], 1)
        self.assertEqual(value['errors_by_type'], {'ReadTimeout':1})
        self.assertIsNone(value['workflow']['p50_ms'])


if __name__ == '__main__': unittest.main()
