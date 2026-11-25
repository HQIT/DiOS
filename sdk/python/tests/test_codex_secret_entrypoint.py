"""The application image projects a registered secret without replacing local state."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

path = (
    Path(__file__).resolve().parents[3]
    / "applications/static-review/examples/codex/secret-entrypoint.py"
)
spec = importlib.util.spec_from_file_location("codex_secret_entrypoint", path)
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class CodexSecretEntrypointTests(unittest.TestCase):
    def test_projection_is_idempotent_and_tracks_rotated_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "secret"
            secret.write_text("synthetic-v1")
            state = root / "state"
            state.mkdir()
            entrypoint.prepare_auth(secret, state)
            entrypoint.prepare_auth(secret, state)
            self.assertTrue((state / "auth.json").is_symlink())
            replacement = root / "replacement"
            replacement.write_text("synthetic-v2")
            replacement.replace(secret)
            self.assertEqual((state / "auth.json").read_text(), "synthetic-v2")

    def test_existing_state_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "secret"
            secret.write_text("synthetic-secret")
            target = root / "auth.json"
            target.write_text("existing-state")
            with self.assertRaisesRegex(ValueError, "CODEX_AUTH_REFERENCE_CONFLICT"):
                entrypoint.prepare_auth(secret, root)
            self.assertEqual(target.read_text(), "existing-state")
            target.unlink()
            other = root / "other"
            other.write_text("other-state")
            target.symlink_to(other)
            with self.assertRaisesRegex(ValueError, "CODEX_AUTH_REFERENCE_CONFLICT"):
                entrypoint.prepare_auth(secret, root)
            self.assertEqual(target.resolve(), other)

    def test_missing_secret_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "CODEX_AUTH_REFERENCE_MISSING"):
                entrypoint.prepare_auth(root / "absent", root)
            self.assertFalse((root / "auth.json").exists())
