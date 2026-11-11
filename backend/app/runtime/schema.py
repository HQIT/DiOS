"""Versioned L1 persistence, independent of legacy ORM and business coverage data."""
from sqlalchemy import (MetaData, Table, Column as C, String as S, Integer as I,
                        Float as F, JSON as J, Boolean as B, ForeignKey as FK,
                        UniqueConstraint as U, Index)
metadata = MetaData()
versions = Table('runtime_schema', metadata, C('version', I, primary_key=True), C('checksum', S(64), nullable=False))
runs = Table('runtime_runs', metadata,
    C('id', S(64), primary_key=True), C('scope', S(160), nullable=False),
    C('idempotency_key', S(160), nullable=False), C('input_digest', S(64), nullable=False),
    C('input', J, nullable=False), C('state', S(40), nullable=False),
    C('version', I, nullable=False), C('epoch', I, nullable=False),
    C('attempt_id', S(64)), C('authorization_epoch', I, nullable=False),
    C('revoked', B, nullable=False), C('cancel_requested', B, nullable=False),
    C('created_at', F, nullable=False), C('updated_at', F, nullable=False),
    U('scope','idempotency_key', name='uq_runtime_submission'))
workers = Table('runtime_workers', metadata,
    C('id', S(64), primary_key=True), C('scope', S(160), nullable=False),
    C('instance', S(64), nullable=False), C('host', S(160), nullable=False),
    C('pid', I, nullable=False), C('slots', I, nullable=False),
    C('draining', B, nullable=False), C('heartbeat_at', F, nullable=False))
attempts = Table('runtime_attempts', metadata,
    C('id', S(64), primary_key=True), C('run_id', S(64), FK(runs.c.id), nullable=False),
    C('worker_id', S(64), FK(workers.c.id), nullable=False), C('worker_instance', S(64), nullable=False),
    C('epoch', I, nullable=False), C('authorization_epoch', I, nullable=False),
    C('lease_until', F, nullable=False), C('state', S(40), nullable=False),
    C('session_id', S(160)), C('process_id', I), C('process_instance', S(64)),
    C('last_sequence', I, nullable=False), C('report_digest', S(64)),
    C('remote', J, nullable=False), C('error', J), C('last_output_at', F),
    C('started_at', F, nullable=False), C('finished_at', F),
    U('run_id','epoch',name='uq_runtime_epoch'))
events = Table('runtime_events', metadata,
    C('id', S(64), primary_key=True), C('run_id', S(64), FK(runs.c.id), nullable=False),
    C('version', I, nullable=False), C('type', S(80), nullable=False),
    C('payload', J, nullable=False), C('digest', S(64), nullable=False),
    C('created_at', F, nullable=False), U('run_id','version',name='uq_runtime_event_version'))
acks = Table('runtime_event_acks', metadata, C('consumer', S(160), primary_key=True),
    C('event_id', S(64), FK(events.c.id), primary_key=True))
projections = Table('runtime_projections', metadata,
    C('consumer', S(160), primary_key=True), C('run_id', S(64), primary_key=True),
    C('version', I, nullable=False), C('digest', S(64), nullable=False),
    C('payload', J, nullable=False), C('observed_at', F, nullable=False))
artifacts = Table('runtime_artifacts', metadata,
    C('id', S(64), primary_key=True), C('run_id', S(64), FK(runs.c.id), nullable=False),
    C('attempt_id', S(64), FK(attempts.c.id), nullable=False),
    C('digest', S(64), nullable=False), C('size', I, nullable=False),
    C('provider', S(80), nullable=False), C('storage_key', S(80), nullable=False),
    C('created_at', F, nullable=False))
imports = Table('runtime_imports', metadata, C('scope', S(160), primary_key=True),
    C('source', S(160), primary_key=True), C('kind', S(40), primary_key=True),
    C('source_id', S(160), primary_key=True), C('target_id', S(64), nullable=False),
    C('digest', S(64), nullable=False), C('record', J, nullable=False),
    C('acceptance', S(40), nullable=False))
Index('ix_runtime_queue', runs.c.scope, runs.c.state, runs.c.created_at)
Index('ix_runtime_leases', attempts.c.state, attempts.c.lease_until)
Index('ix_runtime_events_created', events.c.created_at, events.c.id)
