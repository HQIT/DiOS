"""Compatibility imports for the former dedicated poller."""

from app.connectors.builtin.imap.poll import parse_message as _parse_message
from app.services.connector_poller import ConnectorPoller as ImapPoller
from app.services.connector_poller import connector_poller as imap_poller

POLL_INTERVAL = 60
__all__ = ["POLL_INTERVAL", "ImapPoller", "_parse_message", "imap_poller"]
