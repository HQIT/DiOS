"""Real AMQP 0-9-1 delivery with explicit consumer acknowledgement.

Isolated local broker only. Channel delivery tags are NOT application IDs.
The configured queue/producer ACL supplies provenance; payloads do not.
"""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tmp/heterogeneity-deps"))
import pika
from file_ingress import normalize_envelope


def connect(port, *, username=None, password=None):
    return pika.BlockingConnection(pika.ConnectionParameters(
        host="127.0.0.1", port=port, virtual_host="e2ag",
        credentials=pika.PlainCredentials(username or os.environ["E2AG_AMQP_USER"],
                                          password or os.environ["E2AG_AMQP_PASSWORD"]),
        heartbeat=60, socket_timeout=5, stack_timeout=10, blocked_connection_timeout=10,
        connection_attempts=1))


class BrokerDelivery:
    def __init__(self, port, queue):
        if not queue.startswith("e2agq_"):
            raise ValueError("UNCONFIGURED_QUEUE")
        self.port, self.queue = port, queue
        self.connection = None
        self.acknowledged = False

    def __enter__(self):
        try:
            self.connection = connect(self.port)
            self.channel = self.connection.channel()
            self.channel.basic_qos(prefetch_count=1)
            self.method, self.properties, self.body = next(self.channel.consume(
                self.queue, auto_ack=False, inactivity_timeout=5))
            if self.method is None:
                raise RuntimeError("NO_DELIVERY")
            return self
        except Exception:
            self.close()
            raise

    def event(self):
        event = normalize_envelope(self.body, "fixture", "amqp")
        if self.properties.message_id != event["id"]:
            raise ValueError("MESSAGE_ID_ENVELOPE_MISMATCH")
        return event

    def acknowledge(self):
        self.channel.basic_ack(self.method.delivery_tag, multiple=False)
        # A synchronous method on this channel provides an ordering barrier.
        self.channel.basic_qos(prefetch_count=1)
        self.acknowledged = True

    def close(self):
        if self.connection is not None and self.connection.is_open:
            self.connection.close()

    def __exit__(self, *_):
        self.close()  # unacked delivery is requeued by the broker
