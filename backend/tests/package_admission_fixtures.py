"""Synthetic review evidence for package contract tests."""

from uuid import uuid4

from app.runtime.artifacts import LocalArtifacts
from app.runtime.objects import Objects
from app.runtime.package_admission import PREDICATE, STATEMENT, PackageAdmission
from app.runtime.store import digest


def declared(package):
    return dict(
        package,
        declaration={
            "source": {"uri": "https://example.test/source", "revision": "a" * 40},
            "license": "MIT",
            "capabilities": ["fixture.read"],
        },
    )


async def review_fixture(db, scope, root, package):
    evidence = await Objects(db, scope).publish(
        LocalArtifacts(root / "evidence"),
        [b'{"result":"passed","fixture":"package-contract"}'],
        str(uuid4()),
        "fixture-reviewer",
    )
    declaration = package["declaration"]
    checks = {
        key: {
            "result": "passed",
            "evidence": evidence["ref"],
            "digest": evidence["descriptor"]["digest"],
        }
        for key in [
            "source",
            "license",
            *("capability:" + c for c in declaration["capabilities"]),
        ]
    }
    statement = {
        "_type": STATEMENT,
        "subject": [{"name": package["name"], "digest": {"sha256": digest(package)}}],
        "predicateType": PREDICATE,
        "predicate": {"declaration": declaration, "checks": checks},
    }
    return statement


async def admit_fixture(db, scope, root, package):
    statement = await review_fixture(db, scope, root, package)
    return await PackageAdmission(db, scope).review(
        package,
        "admitted",
        statement,
        expected_version=0,
        key=str(uuid4()),
        actor="fixture-reviewer",
    )
