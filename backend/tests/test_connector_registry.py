from __future__ import annotations

import json
import unittest

from fastapi import HTTPException
from jsonschema.exceptions import SchemaError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.os.connectors import create_connector, list_connector_types, update_connector
from app.api.os.events import _webhook_secrets, event_catalog
from app.connectors import registry
from app.connectors.contracts import (
    CAPABILITY_WEBHOOK,
    ConnectorManifest,
    EventSourceDecl,
    EventTypeDecl,
)
from app.db.database import Base
from app.models.schemas import ConnectorCreate, ConnectorUpdate
from app.models.tables import Connector


class ConnectorRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        registry.reset_for_tests()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        registry.reset_for_tests()
        await self.engine.dispose()

    @staticmethod
    def _calendar_manifest() -> ConnectorManifest:
        return ConnectorManifest(
            type="calendar_webhook",
            label="Calendar Webhook",
            description="Test-only calendar event source",
            capabilities=(CAPABILITY_WEBHOOK,),
            config_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["calendar_id"],
                "properties": {
                    "calendar_id": {"type": "string", "title": "Calendar ID"},
                    "secret": {"type": "string", "title": "Secret"},
                },
            },
            secret_fields=("secret",),
            event_sources=(EventSourceDecl(id="calendar", name="Calendar"),),
            event_types=(EventTypeDecl(type="calendar.changed", description="Changed"),),
            accepted_source_patterns=("calendar/*",),
        )

    async def test_type_catalog_is_manifest_driven_and_serializable(self) -> None:
        descriptors = await list_connector_types()
        by_type = {item["type"]: item for item in descriptors}

        self.assertEqual({"generic", "git_webhook", "imap"}, set(by_type))
        self.assertNotIn("internal", by_type)
        self.assertTrue(by_type["git_webhook"]["config_schema"]["properties"]["secret"]["writeOnly"])
        json.dumps(descriptors)

    async def test_new_manifest_uses_generic_api_without_router_changes(self) -> None:
        registry.all_manifests()
        registry.register(self._calendar_manifest())

        descriptors = await list_connector_types()
        self.assertIn("calendar_webhook", {item["type"] for item in descriptors})

        async with self.sessions() as db:
            connector = await create_connector(
                ConnectorCreate(
                    type="calendar_webhook",
                    name="Team Calendar",
                    config={"calendar_id": "engineering"},
                ),
                db,
            )
            self.assertEqual("calendar_webhook", connector.type)

            catalog = await event_catalog(db)
            self.assertTrue(catalog["connector_status"]["calendar"])
            self.assertIn("calendar.changed", {item["type"] for item in catalog["event_types"]})

    async def test_invalid_type_and_config_are_rejected(self) -> None:
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as unknown:
                await create_connector(
                    ConnectorCreate(type="unknown", name="Unknown", config={}),
                    db,
                )
            self.assertEqual(400, unknown.exception.status_code)

            with self.assertRaises(HTTPException) as internal:
                await create_connector(
                    ConnectorCreate(type="internal", name="Internal", config={}),
                    db,
                )
            self.assertEqual(400, internal.exception.status_code)

            with self.assertRaises(HTTPException) as invalid:
                await create_connector(
                    ConnectorCreate(
                        type="git_webhook",
                        name="Bad Git",
                        config={"platform": "bitbucket"},
                    ),
                    db,
                )
            self.assertEqual(400, invalid.exception.status_code)
            self.assertIn("config.platform", invalid.exception.detail)

    async def test_invalid_manifest_schema_is_rejected_at_registration(self) -> None:
        manifest = ConnectorManifest(
            type="broken",
            label="Broken",
            config_schema={"type": "not-a-json-schema-type"},
        )
        with self.assertRaises(SchemaError):
            registry.register(manifest)

    async def test_legacy_alias_keeps_working_through_manifest(self) -> None:
        async with self.sessions() as db:
            connector = Connector(
                type="github",
                name="Legacy GitHub",
                enabled=True,
                config={"secret": "legacy-secret"},
            )
            db.add(connector)
            await db.commit()
            await db.refresh(connector)

            updated = await update_connector(
                connector.id,
                ConnectorUpdate(name="Legacy GitHub Updated"),
                db,
            )
            self.assertEqual("github", updated.type)
            self.assertEqual("Legacy GitHub Updated", updated.name)
            self.assertEqual("legacy-secret", (await _webhook_secrets(db))["github"])

            catalog = await event_catalog(db)
            self.assertTrue(catalog["connector_status"]["git"])
            self.assertTrue(catalog["connector_status"]["manual"])
            self.assertTrue(catalog["connector_status"]["cron"])


if __name__ == "__main__":
    unittest.main()
