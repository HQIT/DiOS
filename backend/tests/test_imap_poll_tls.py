"""Real TLS/imaplib wire regression against a local protocol fixture."""

from __future__ import annotations

import asyncio
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.connectors import registry
from app.connectors.builtin.imap.poll import ImapAdapter
from app.connectors.contracts import PollInstance
from app.connectors.ownership import LocalPollOwnership
from app.db.database import Base
from app.models.tables import Connector, EventLog
from app.services.connector_poller import ConnectorPoller
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.test_connector_poller import eventually


class ImapTlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_tls_uid_collect_persist_and_ack_via_generic_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                    "-addext",
                    "subjectAltName=DNS:localhost",
                    "-keyout",
                    str(root / "key.pem"),
                    "-out",
                    str(root / "cert.pem"),
                ],
                capture_output=True,
            )
            self.assertEqual(0, result.returncode)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(root / "cert.pem", root / "key.pem")
            trust = ssl.create_default_context(cafile=str(root / "cert.pem"))
            raw = (
                b"From: local@example.test\r\nTo: local@example.test\r\nSubject: Fixture\r\n\r\nUTF-8: "
                + "中文证据".encode()
            )
            seen, errors, commands, handlers = [], [], [], set()
            engine = create_async_engine(f"sqlite+aiosqlite:///{root}/events.db")
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

            async def serve(reader, writer):
                task = asyncio.current_task()
                handlers.add(task)
                try:
                    writer.write(b"* OK local qualification mailbox\r\n")
                    await writer.drain()
                    while line := await reader.readline():
                        tag, rest = line.rstrip(b"\r\n").split(b" ", 1)
                        command = rest.split(b" ", 1)[0].upper()
                        commands.append(command.decode())
                        if command == b"CAPABILITY":
                            writer.write(b"* CAPABILITY IMAP4rev1\r\n")
                        elif command == b"LOGIN":
                            pass
                        elif command == b"SELECT":
                            writer.write(
                                b"* 1 EXISTS\r\n* OK [UIDVALIDITY 77] stable\r\n"
                            )
                        elif rest.startswith(b"UID SEARCH"):
                            writer.write(b"* SEARCH 101\r\n")
                        elif rest.startswith(b"UID FETCH"):
                            self.assertIn(b"BODY.PEEK[]", rest)
                            writer.write(
                                b"* 1 FETCH (UID 101 BODY[] {"
                                + str(len(raw)).encode()
                                + b"}\r\n"
                                + raw
                                + b")\r\n"
                            )
                        elif rest.startswith(b"UID STORE"):
                            async with sessions() as db:
                                log = await db.scalar(select(EventLog))
                                self.assertIsNotNone(log)
                                self.assertEqual(
                                    "allow", log.contract_decision["decision"]
                                )
                                self.assertIn(
                                    "中文证据", log.cloud_event["data"]["body_preview"]
                                )
                            seen.append("101")
                            writer.write(b"* 1 FETCH (FLAGS (\\Seen))\r\n")
                        elif command == b"LOGOUT":
                            writer.write(b"* BYE done\r\n" + tag + b" OK logout\r\n")
                            await writer.drain()
                            break
                        else:
                            raise AssertionError(command)
                        writer.write(tag + b" OK complete\r\n")
                        await writer.drain()
                except Exception as exc:  # noqa: BLE001 - isolate plugin failures; redact external replies
                    errors.append(repr(exc))
                finally:
                    writer.close()
                    await writer.wait_closed()
                    handlers.discard(task)

            server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=context)
            port = server.sockets[0].getsockname()[1]
            registry.reset_for_tests()
            registry.all_manifests()
            config = {
                "host": "localhost",
                "port": port,
                "user": "fixture",
                "password": "fixture",
            }
            async with sessions() as db:
                db.add(
                    Connector(
                        id="tls-fixture",
                        name="TLS fixture",
                        type="imap",
                        enabled=True,
                        config=config,
                    )
                )
                await db.commit()
            poller = ConnectorPoller(
                sessions=sessions,
                ownership=LocalPollOwnership(root / "locks"),
                scan_interval=0.01,
            )
            try:
                with patch(
                    "app.connectors.builtin.imap.poll.ssl.create_default_context",
                    return_value=trust,
                ):
                    await poller.start()

                    async def acknowledged():
                        return len(seen) == 1

                    await eventually(acknowledged, timeout=5)
                    await poller.stop()
                    # Repeat the source delivery after acknowledgement loss:
                    # native wire collection reuses the existing durable event.
                    await ImapAdapter().poll(
                        PollInstance("tls-fixture", config), poller._emit
                    )
                async with sessions() as db:
                    self.assertEqual(
                        1, len((await db.execute(select(EventLog))).scalars().all())
                    )
                self.assertEqual(["101", "101"], seen)
                self.assertEqual([], errors)
                self.assertIn("LOGOUT", commands)
            finally:
                await poller.stop()
                server.close()
                await server.wait_closed()
                await asyncio.gather(*handlers)
                registry.reset_for_tests()
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
