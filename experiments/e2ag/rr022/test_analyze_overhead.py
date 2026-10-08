import unittest
from analyze_overhead import paired, proxy_intervals


class AnalysisTests(unittest.TestCase):
    def row(self, mode, p50):
        return dict(kind='issue', concurrency=1, repeat=0, mode=mode,
                    summary={'workflow': {'p50_ms': p50, 'n': 20}, 'errors': 0})

    def test_pair(self):
        result = paired([self.row('G2', 12), self.row('G0', 10)])[0]
        self.assertEqual(result['delta_p50_ms'], 2)
        self.assertEqual(result['delta_p50_pct'], 20)

    def test_incomplete(self):
        with self.assertRaises(ValueError):
            paired([self.row('G0', 10)])

    def test_duplicate(self):
        with self.assertRaises(ValueError):
            paired([self.row('G0', 10), self.row('G0', 11)])

    def test_intervals(self):
        rows = [{'request_id': 'measurement-0-1:read', 'stage': stage,
                 'start_ns': start, 'end_ns': end}
                for stage, start, end in [('http_route', 0, 10000000),
                                          ('upstream_roundtrip', 2000000, 7000000)]]
        result = proxy_intervals(rows)[0]
        self.assertEqual((result['pre_upstream_ms'], result['upstream_ms'], result['post_upstream_ms']), (2, 5, 3))


if __name__ == '__main__':
    unittest.main()
