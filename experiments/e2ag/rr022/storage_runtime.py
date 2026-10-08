"""Common experiment infrastructure, not an E2AG policy or production change."""


def pool_options(mode):
    if mode == 'parallel':
        return dict(pool_size=5, max_overflow=10, pool_timeout=30)
    if mode == 'queued':
        # SQLAlchemy's async pool queues transaction connection acquisition.
        # Reads also queue. Each commit/rollback returns the connection; external
        # tools must run after the transaction, not inside this queue's lease.
        return dict(pool_size=1, max_overflow=0, pool_timeout=30)
    raise ValueError('Unknown database admission mode')
