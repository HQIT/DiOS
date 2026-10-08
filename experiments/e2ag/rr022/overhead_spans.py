"""Experiment-only hierarchical wall-clock spans; no production dependencies."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from itertools import count
from time import perf_counter_ns


class Recorder:
    def __init__(self, enabled=True, clock=perf_counter_ns):
        self.enabled, self.clock = enabled, clock
        self.rows, self.ids = [], count(1)
        self.request = ContextVar('overhead_request', default='outside')
        self.parent = ContextVar('overhead_parent', default=None)

    @contextmanager
    def span(self, stage):
        if not self.enabled:
            yield
            return
        identifier = next(self.ids)
        row = dict(id=identifier, parent=self.parent.get(), request_id=self.request.get(),
                   stage=stage, start_ns=self.clock(), outcome='ok')
        token = self.parent.set(identifier)
        try:
            yield
        except BaseException:
            row['outcome'] = 'error'
            raise
        finally:
            row['end_ns'] = self.clock()
            self.rows.append(row)
            self.parent.reset(token)

    def wrap(self, function, stage, asynchronous=False):
        if asynchronous:
            @wraps(function)
            async def call(*args, **kwargs):
                with self.span(stage):
                    return await function(*args, **kwargs)
        else:
            @wraps(function)
            def call(*args, **kwargs):
                with self.span(stage):
                    return function(*args, **kwargs)
        return call


def union_ns(intervals):
    """Length of an interval union, never a sum of overlapping children."""
    total, left, right = 0, None, None
    for start, end in sorted(intervals):
        if end < start:
            raise ValueError('negative interval')
        if left is None:
            left, right = start, end
        elif start > right:
            total += right - left
            left, right = start, end
        else:
            right = max(right, end)
    return total + (right - left if left is not None else 0)


def exclusive_rows(rows):
    """Subtract child interval unions. Remaining wall time is not CPU time."""
    by_id = {r['id']: r for r in rows}
    if len(by_id) != len(rows):
        raise ValueError('duplicate span id')
    children = {}
    for row in rows:
        if row['end_ns'] < row['start_ns']:
            raise ValueError('negative span')
        parent_id = row['parent']
        if parent_id is not None:
            if parent_id not in by_id:
                raise ValueError('missing parent')
            parent = by_id[parent_id]
            if row['request_id'] != parent['request_id']:
                raise ValueError('cross-request parent')
            if not parent['start_ns'] <= row['start_ns'] <= row['end_ns'] <= parent['end_ns']:
                raise ValueError('asynchronous child outside parent; cannot subtract')
            children.setdefault(parent_id, []).append((row['start_ns'], row['end_ns']))
    return [{**r, 'inclusive_ns': r['end_ns'] - r['start_ns'],
             'exclusive_wall_ns': r['end_ns'] - r['start_ns'] - union_ns(children.get(r['id'], []))}
            for r in rows]
