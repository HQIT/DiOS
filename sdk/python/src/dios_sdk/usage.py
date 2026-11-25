"""Whitelisted ACP telemetry. Context occupancy and billed tokens stay distinct."""

import re
from decimal import Decimal, InvalidOperation

TOKEN_FIELDS = (
    "inputTokens",
    "outputTokens",
    "totalTokens",
    "thoughtTokens",
    "cachedReadTokens",
    "cachedWriteTokens",
)


def usage_frame(session_id, sequence, kind, value):
    frame = {
        "type": "usage",
        "session_id": session_id,
        "sequence": sequence,
        "kind": kind,
        "quality": "reported",
        "values": {},
    }
    try:
        if not isinstance(value, dict):
            raise TypeError()
        fields = ("used", "size") if kind == "acp_session" else TOKEN_FIELDS
        required = fields if kind == "acp_session" else TOKEN_FIELDS[:3]
        for key in fields:
            v = value.get(key)
            if v is None and key not in required:
                continue
            if type(v) is not int or not 0 <= v <= 10**15:
                raise ValueError()
            frame["values"][key] = v
        if kind == "acp_session" and value.get("cost") is not None:
            cost = value["cost"]
            if not isinstance(cost, dict) or type(cost.get("amount")) not in (
                int,
                float,
                str,
            ):
                raise ValueError()
            amount = Decimal(str(cost["amount"]))
            unit = cost.get("currency")
            if (
                not amount.is_finite()
                or not 0 <= amount <= 10**12
                or not -18 <= amount.as_tuple().exponent <= 12
                or not isinstance(unit, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,15}", unit)
            ):
                raise ValueError()
            text = format(amount, "f")
            if len(text) > 64:
                raise ValueError()
            frame["values"]["cost"] = {"amount": text, "unit": unit}
    except (ValueError, TypeError, KeyError, InvalidOperation):
        frame["quality"] = "invalid"
        frame["values"] = {}
    return frame
