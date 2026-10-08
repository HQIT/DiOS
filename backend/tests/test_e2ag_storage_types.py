"""Dialect mapping regression; actual server checks live in qualification."""
import unittest
from sqlalchemy.dialects import postgresql, sqlite
from app.models.tables import EventLog


class StorageTypeTests(unittest.TestCase):
    def test_postgres_audit_equality_uses_jsonb(self):
        column = EventLog.__table__.c.audit_chain
        self.assertEqual("JSONB", column.type.compile(dialect=postgresql.dialect()))
        expression = str((EventLog.audit_chain == []).compile(dialect=postgresql.dialect()))
        self.assertIn("JSONB", expression)

    def test_sqlite_audit_representation_unchanged(self):
        self.assertEqual("JSON", EventLog.__table__.c.audit_chain.type.compile(dialect=sqlite.dialect()))
