"""Parser/dedup regression only; these are not real IMAP endpoint tests."""
import unittest

from app.services.imap_poller import _parse_message
from app.services.event_normalizer import compute_dedup_hash, _make_event


def identity(raw):
    data = _parse_message(raw)
    event = _make_event(source="imap/qualification", event_type="email.received",
                        subject=data["subject"], data=data)
    return data, compute_dedup_hash(event)


class ImapMessageIdentityTests(unittest.TestCase):
    def test_preserves_message_id(self):
        data, _ = identity(b"Message-ID: <a@example.invalid>\r\nSubject: same\r\n\r\nfirst")
        self.assertEqual("<a@example.invalid>", data["message_id"])

    def test_distinct_messages_with_same_subject(self):
        a = b"Message-ID: <a@example.invalid>\r\nSubject: same\r\n\r\nfirst"
        b = b"Message-ID: <b@example.invalid>\r\nSubject: same\r\n\r\nsecond"
        self.assertNotEqual(identity(a)[1], identity(b)[1])
        self.assertEqual(identity(a)[1], identity(a)[1])

    def test_missing_message_id_uses_exact_bytes(self):
        a = b"Subject: same\r\n\r\nfirst"
        b = b"Subject: same\r\n\r\nsecond"
        self.assertTrue(identity(a)[0]["message_id"].startswith("raw-sha256:"))
        self.assertNotEqual(identity(a)[1], identity(b)[1])
        self.assertEqual(identity(a)[1], identity(a)[1])

    def test_blank_message_id_uses_fallback(self):
        data, _ = identity(b"Message-ID:  \r\nSubject: same\r\n\r\nbody")
        self.assertTrue(data["message_id"].startswith("raw-sha256:"))


if __name__ == "__main__":
    unittest.main()
