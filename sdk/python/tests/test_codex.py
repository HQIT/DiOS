"""Codex provider mapping cannot grant native execution or another MCP route."""

import unittest

from dios_sdk.codex import proxy_permission, tool_activity
from dios_sdk.protocol import WorkloadError


class CodexMetadataTests(unittest.TestCase):
    def test_mcp_evidence_is_mapped_without_changing_arguments(self):
        call = {
            "status": "completed",
            "rawInput": {
                "server": "dios",
                "tool": "repository_search",
                "arguments": {"query": "中文🙂"},
            },
        }
        self.assertEqual(
            proxy_permission(call),
            {"name": "mcp__dios__repository_search", "arguments": {"query": "中文🙂"}},
        )
        self.assertEqual(
            tool_activity(call),
            {"name": "mcp__dios__repository_search", "rawInput": {"query": "中文🙂"}},
        )

    def test_native_unknown_or_ambiguous_metadata_is_denied(self):
        for raw in (
            {"command": "echo unsafe"},
            {"server": "other", "tool": "search", "arguments": {}},
            {"server": "dios", "tool": "search", "arguments": "{}"},
            {"server": "dios", "tool": "../shell", "arguments": {}},
            {"server": "dios", "tool": "search", "arguments": {}, "command": "shell"},
            None,
        ):
            with self.subTest(raw=raw), self.assertRaises(WorkloadError):
                proxy_permission({"rawInput": raw})

    def test_started_tool_does_not_prove_completion(self):
        for status in ("pending", "in_progress", None):
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(WorkloadError, "ACP_TOOL_ACTIVITY_UNVERIFIED"),
            ):
                tool_activity(
                    {
                        "status": status,
                        "rawInput": {
                            "server": "dios",
                            "tool": "search",
                            "arguments": {},
                        },
                    }
                )


if __name__ == "__main__":
    unittest.main()
