import asyncio
import unittest
from overhead_spans import Recorder, exclusive_rows, union_ns


class SpanTests(unittest.TestCase):
    def test_union_overlap(self):
        self.assertEqual(union_ns([(1, 4), (3, 8), (10, 12)]), 9)

    def test_nested_not_double_counted(self):
        r = Recorder(clock=iter([0, 1, 3, 5]).__next__)
        with r.span('parent'):
            with r.span('child'):
                pass
        rows = {x['stage']: x for x in exclusive_rows(r.rows)}
        self.assertEqual(rows['parent']['exclusive_wall_ns'], 3)
        self.assertEqual(rows['child']['exclusive_wall_ns'], 2)

    def test_siblings_overlap(self):
        rows = [dict(id=1, parent=None, request_id='a', start_ns=0, end_ns=10),
                dict(id=2, parent=1, request_id='a', start_ns=1, end_ns=7),
                dict(id=3, parent=1, request_id='a', start_ns=3, end_ns=9)]
        self.assertEqual(exclusive_rows(rows)[0]['exclusive_wall_ns'], 2)

    def test_uncontained_child_rejected(self):
        rows = [dict(id=1, parent=None, request_id='a', start_ns=0, end_ns=10),
                dict(id=2, parent=1, request_id='a', start_ns=1, end_ns=11)]
        with self.assertRaises(ValueError):
            exclusive_rows(rows)

    def test_exception_recorded(self):
        r = Recorder()
        with self.assertRaises(RuntimeError):
            with r.span('failure'):
                raise RuntimeError('test')
        self.assertEqual(r.rows[0]['outcome'], 'error')
        self.assertIsNone(r.parent.get())

    def test_disabled_no_clock(self):
        r = Recorder(enabled=False, clock=lambda: self.fail('clock used'))
        with r.span('off'):
            pass
        self.assertEqual(r.rows, [])

    def test_concurrent_requests_do_not_cross(self):
        r = Recorder()
        async def worker(name):
            token = r.request.set(name)
            try:
                with r.span('request'):
                    await asyncio.sleep(0)
                    with r.span('child'):
                        await asyncio.sleep(0)
            finally:
                r.request.reset(token)
        async def run():
            await asyncio.gather(worker('a'), worker('b'))
        asyncio.run(run())
        self.assertEqual(len(exclusive_rows(r.rows)), 4)


if __name__ == '__main__':
    unittest.main()
