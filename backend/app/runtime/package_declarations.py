"""Version-bound source, SPDX licence and capability declarations."""

import re
from urllib.parse import urlsplit

from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression

from .package_lock import fail


def declaration(value):
    if not isinstance(value, dict) or set(value) != {
        "source",
        "license",
        "capabilities",
    }:
        fail("PACKAGE_DECLARATION_INVALID")
    source = value["source"]
    if not isinstance(source, dict) or set(source) != {"uri", "revision"}:
        fail("PACKAGE_SOURCE_INVALID")
    uri, revision = source["uri"], source["revision"]
    if not isinstance(uri, str) or len(uri) > 2048 or not isinstance(revision, str):
        fail("PACKAGE_SOURCE_INVALID")
    try:
        parsed = urlsplit(uri)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(ord(c) < 33 for c in uri)
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision)
        ):
            fail("PACKAGE_SOURCE_INVALID")
    except ValueError:
        fail("PACKAGE_SOURCE_INVALID")
    license = value["license"]
    if not isinstance(license, str) or not 1 <= len(license) <= 512:
        fail("PACKAGE_LICENSE_INVALID")
    try:
        license = canonicalize_license_expression(license)
    except InvalidLicenseExpression:
        fail("PACKAGE_LICENSE_INVALID")
    capabilities = value["capabilities"]
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > 64
        or any(
            not isinstance(c, str) or not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,119}", c)
            for c in capabilities
        )
        or len(set(capabilities)) != len(capabilities)
    ):
        fail("PACKAGE_CAPABILITIES_INVALID")
    return {"source": source, "license": license, "capabilities": sorted(capabilities)}
