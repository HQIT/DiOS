"""Upper-platform SDK consumes the real isolated API with CLI-equivalent results."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk/python/src"))
from dios_sdk.tasks import TaskClient, ApiError

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
args = parser.parse_args()
client = TaskClient("http://127.0.0.1:13344", "catalog-fixture-token")
checks = []
for filters in (
    {},
    {"q": "catalog", "version": "1.0.0", "admission": "admitted"},
    {"kind": "skill"},
    {"admission": "revoked"},
    {"q": "missing"},
):
    value = client.package_catalog(**filters)
    command = [
        str(ROOT / "backend/.venv/bin/python"),
        "-m",
        "app.runtime.cli",
        "--database-url",
        "sqlite+aiosqlite:///" + str(args.root / "state.sqlite"),
        "--scope",
        "catalog-fixture",
        "registry",
        "catalog",
    ]
    for key, item in filters.items():
        command.extend(["--query" if key == "q" else "--" + key, item])
    cli = subprocess.run(
        command, cwd=ROOT / "backend", capture_output=True, text=True, check=True
    )
    assert json.loads(cli.stdout) == value
    checks.append({"filters": filters, "count": len(value["items"])})
reference = client.package_catalog(version="1")["items"][0]["digest"]
assert client.package_detail(reference)["digest"] == reference
assert client.package_releases(reference) == {"items": [], "next_after": None}
for call, expected in [
    (lambda: client.package_catalog(kind="invalid"), "INVALID_PACKAGE_FILTER"),
    (lambda: client.package_detail("sha256:" + "f" * 64), "NOT_FOUND"),
]:
    try:
        call()
    except ApiError as exc:
        assert str(exc) == expected
    else:
        raise AssertionError("Expected API error")
report = {
    "passed": True,
    "filter_comparisons": checks,
    "detail": True,
    "related_revisions": True,
    "error_codes": True,
}
(args.root / "sdk.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
