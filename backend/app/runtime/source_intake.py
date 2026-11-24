"""Source enrollment through native KV v2 create-only records; no local catalog writes."""

import asyncio
import hashlib
import re
import time

from .database import RuntimeErrorCode
from .oauth_source import (
    OAuthSource,
    source_binding,
    validate_reference,
    validate_scopes,
)
from .openbao_secrets import OpenBaoSecrets
from .openbao_secrets import validate_config as validate_store
from .openbao_secrets import validate_reference as validate_secret
from .store import digest, identifier


def validate_config(value, provider):
    if not isinstance(value, dict) or set(value) != {
        "writer",
        "mount",
        "prefix",
        "timeout_seconds",
        "max_source_ttl_seconds",
    }:
        raise ValueError("intake config")
    validate_store(value["writer"])
    if "secret_store" not in provider or value["writer"]["address"].rstrip(
        "/"
    ) != provider["secret_store"]["address"].rstrip("/"):
        raise ValueError("intake store")
    validate_secret(
        {
            "mount": value["mount"],
            "path": value["prefix"] + "/x",
            "version": 1,
            "field": "value",
        }
    )
    if len(value["prefix"]) > 100:
        raise ValueError("intake prefix")
    if (
        type(value["timeout_seconds"]) not in (int, float)
        or not 0.05 <= value["timeout_seconds"] <= 10
    ):
        raise ValueError("intake deadline")
    if (
        type(value["max_source_ttl_seconds"]) is not int
        or not 60 <= value["max_source_ttl_seconds"] <= 86400
    ):
        raise ValueError("source lifetime")


def validate_identity(value, provider):
    if not isinstance(value, dict) or set(value) != {
        "issuer",
        "subject",
        "client_id",
        "required_scopes",
    }:
        raise ValueError("identity mapping")
    for key in ("issuer", "subject", "client_id"):
        identifier(value[key])
    if value["issuer"] != provider["issuer"]:
        raise ValueError("issuer mapping")
    validate_scopes(value["required_scopes"])


class SourceIntake:
    def __init__(self, document):
        self.document = document
        self.config = document["oauth_intake"]
        self.provider = document["oauth2"]

    def owner(self, pid, row):
        return {
            "issuer": self.document["issuer"],
            "audience": self.document["audience"],
            "id": pid,
            **{k: row[k] for k in ("subject", "actor", "scope", "revision")},
            "oauth_identity": dict(
                row["oauth_identity"],
                required_scopes=sorted(row["oauth_identity"]["required_scopes"]),
            ),
        }

    def key(self, owner, token_digest):
        authority = {
            k: self.provider[k] for k in ("issuer", "audience", "endpoint", "client_id")
        }
        return digest(
            [
                owner,
                authority,
                self.provider["secret_store"]["address"].rstrip("/"),
                token_digest,
            ]
        )

    def secret(self, key):
        return {
            "mount": self.config["mount"],
            "path": self.config["prefix"] + "/" + key,
            "version": 1,
            "field": "value",
        }

    def validated(self, record, owner, key):
        try:
            if (
                not isinstance(record, dict)
                or set(record) != {"format", "principal", "source", "value"}
                or type(record["format"]) is not int
                or record["format"] != 1
                or record["principal"] != owner
            ):
                raise ValueError("source record")
            source = record["source"]
            validate_reference(source, self.provider)
            if (
                source["secret_ref"] != self.secret(key)
                or source["ref"] != "enrolled/" + key
                or source["version"] != "v1"
            ):
                raise ValueError("source location")
            if self.key(owner, source["sha256"]) != key:
                raise ValueError("source binding")
            identity = owner["oauth_identity"]
            if any(
                source[k] != identity[k] for k in ("issuer", "subject", "client_id")
            ) or source["required_scopes"] != sorted(identity["required_scopes"]):
                raise ValueError("source identity")
            token = record["value"]
            if (
                not isinstance(token, str)
                or not 1 <= len(token) <= 16384
                or not token.isascii()
                or any(c.isspace() for c in token)
            ):
                raise ValueError("source credential")
            if hashlib.sha256(token.encode()).hexdigest() != source["sha256"]:
                raise ValueError("source digest")
            return source
        except (ValueError, KeyError, TypeError, RuntimeErrorCode):
            raise RuntimeErrorCode("AUTH_PRINCIPAL_REVOKED") from None

    def authenticate(self, token, scope):
        try:
            return asyncio.run(self._authenticate(token, scope))
        except TimeoutError:
            raise RuntimeErrorCode("AUTH_PROVIDER_UNAVAILABLE") from None

    async def _authenticate(self, token, scope):
        async with asyncio.timeout(self.config["timeout_seconds"]):
            claims = await OAuthSource(self.provider).inspect_async(token)
            matched = []
            for pid, row in self.document["principals"].items():
                expected = row.get("oauth_identity")
                if (
                    expected
                    and row["scope"] == scope
                    and row["enabled"]
                    and row["expires_at"] > time.time()
                    and all(
                        expected[k] == claims[k]
                        for k in ("issuer", "subject", "client_id")
                    )
                    and set(expected["required_scopes"]) <= set(claims["scopes"])
                ):
                    matched.append((pid, row))
            if len(matched) != 1:
                raise RuntimeErrorCode("AUTH_UNAUTHENTICATED")
            pid, row = matched[0]
            if (
                claims["expires_at"] - time.time()
                > self.config["max_source_ttl_seconds"]
            ):
                raise RuntimeErrorCode("AUTH_UNAUTHENTICATED")
            owner = self.owner(pid, row)
            token_digest = hashlib.sha256(token.encode()).hexdigest()
            key = self.key(owner, token_digest)
            reference = {
                "ref": "enrolled/" + key,
                "version": "v1",
                "sha256": token_digest,
                **{
                    k: claims[k]
                    for k in ("issuer", "subject", "client_id", "expires_at")
                },
                "audience": self.provider["audience"],
                "required_scopes": sorted(row["oauth_identity"]["required_scopes"]),
                "secret_ref": self.secret(key),
                **{k:claims[k] for k in ('session_id','session_started_at') if k in claims},
            }
            record = {
                "format": 1,
                "principal": owner,
                "source": reference,
                "value": token,
            }
            try:
                stored = await OpenBaoSecrets(
                    self.provider["secret_store"]
                ).read_record_async(reference["secret_ref"])
            except RuntimeErrorCode as exc:
                if exc.code != "SOURCE_SECRET_UNAVAILABLE":
                    raise
                stored = await OpenBaoSecrets(self.config["writer"]).create_once_async(
                    reference["secret_ref"], record
                )
            return pid, self.validated(stored, owner, key)

    def resolve(self, pid, row, binding):
        try:
            ref = binding.get("ref", "")
            if not isinstance(ref, str) or not re.fullmatch(
                r"enrolled/[0-9a-f]{64}", ref
            ):
                raise RuntimeErrorCode("AUTH_PRINCIPAL_REVOKED")
            key = ref.split("/")[1]
            return asyncio.run(self._resolve(self.owner(pid, row), key, binding))
        except TimeoutError:
            raise RuntimeErrorCode("AUTH_PROVIDER_UNAVAILABLE") from None

    async def _resolve(self, owner, key, binding):
        async with asyncio.timeout(self.config["timeout_seconds"]):
            try:
                record = await OpenBaoSecrets(
                    self.provider["secret_store"]
                ).read_record_async(self.secret(key))
            except RuntimeErrorCode as exc:
                if exc.code == "SOURCE_SECRET_UNAVAILABLE":
                    raise RuntimeErrorCode("AUTH_PRINCIPAL_REVOKED") from None
                raise
            source = self.validated(record, owner, key)
            if source_binding(source, self.provider) != binding:
                raise RuntimeErrorCode("AUTH_PRINCIPAL_REVOKED")
            return source
