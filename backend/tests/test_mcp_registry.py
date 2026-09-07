import unittest

from app.api.os.mcp_registry import _dedupe_latest, _simplify


class McpRegistryTest(unittest.TestCase):
    def test_remote_metadata_is_ready_for_registration(self):
        result = _simplify({
            "server": {
                "name": "com.example/analytics",
                "title": "Analytics",
                "version": "2.0.0",
                "description": "Query analytics data",
                "remotes": [{
                    "type": "streamable-http",
                    "url": "https://example.com/mcp",
                    "headers": [{
                        "name": "X-API-Key",
                        "description": "API key",
                        "isRequired": True,
                        "isSecret": True,
                    }],
                }],
            },
            "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}},
        })

        self.assertEqual("Analytics", result["title"])
        self.assertEqual("streamable_http", result["transport"])
        self.assertEqual("https://example.com/mcp", result["url"])
        self.assertEqual({"X-API-Key": "API key（必填）"}, result["header_hints"])
        self.assertTrue(result["is_latest"])

    def test_package_metadata_keeps_install_hints(self):
        result = _simplify({"server": {
            "name": "io.github.example/local",
            "packages": [{
                "registryType": "npm",
                "identifier": "@example/local-mcp",
                "transport": {"type": "stdio"},
                "environmentVariables": [{"name": "TOKEN", "description": "Access token"}],
            }],
        }})

        self.assertEqual("npx", result["command"])
        self.assertEqual(["-y", "@example/local-mcp"], result["args"])
        self.assertEqual("npm", result["registry_type"])
        self.assertEqual({"TOKEN": "Access token"}, result["env_hints"])

    def test_latest_version_wins_deduplication(self):
        servers = [
            {"name": "com.example/server", "version": "1.0", "is_latest": False, "published_at": "2026-01-01"},
            {"name": "com.example/server", "version": "2.0", "is_latest": True, "published_at": "2026-02-01"},
        ]

        result = _dedupe_latest(servers)

        self.assertEqual(1, len(result))
        self.assertEqual("2.0", result[0]["version"])


if __name__ == "__main__":
    unittest.main()
