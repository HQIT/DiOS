"""Decode a complete structured result at the end of ACP text output."""

import json


def final_object(text):
    text = text.rstrip()
    if text.endswith("```"):
        text = text[:-3].rstrip()
    decoder = json.JSONDecoder()
    for offset in range(len(text) - 1, -1, -1):
        if text[offset] != "{":
            continue
        try:
            value, end = decoder.raw_decode(text, offset)
        except ValueError:
            continue
        if end == len(text) and isinstance(value, dict):
            return value
    raise ValueError("No complete final JSON object")
