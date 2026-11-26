"""Run exact isolated component checks; retain unmet end-to-end requirements."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unittest
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/design/fixtures/reference-faults.json"
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scenario_status(case, outcomes):
    states = [outcomes.get(name, "not_run") for name in case["tests"]]
    if any(state in {"failed", "error"} for state in states):
        return "failed"
    if case["gaps"] or not states or any(state != "passed" for state in states):
        return "blocked"
    return "passed"


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}
        self.elapsed = {}

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        self.elapsed[test.id()] = time.monotonic() - self.started
        super().stopTest(test)

    def addSuccess(self, test):
        self.outcomes.setdefault(test.id(), "passed")
        super().addSuccess(test)

    def addFailure(self, test, err):
        self.outcomes[test.id()] = "failed"
        super().addFailure(test, err)

    def addError(self, test, err):
        self.outcomes[test.id()] = "error"
        super().addError(test, err)

    def addSkip(self, test, reason):
        parent = getattr(test, "test_case", test)
        self.outcomes[parent.id()] = "skipped"
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self.outcomes[test.id()] = "blocked"
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self.outcomes[test.id()] = "failed"
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.outcomes[test.id()] = "failed"
        super().addSubTest(test, subtest, err)


def materialize_review(directory, review):
    """Create real Git object identities for synthetic text, without executing it."""
    directory.mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")

    def git(*args):
        return subprocess.check_output(
            [
                "git",
                "-c",
                "user.name=DiOS Synthetic Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=directory,
            env=env,
            text=True,
            stderr=subprocess.PIPE,
        ).strip()

    git("init", "--quiet", "--initial-branch=fixture")
    versions = {}
    for label in ("defect-v1", "fix-v2"):
        source = review["input"]["files"][label + "/service.ts"]
        (directory / "service.ts").write_text(source, encoding="utf-8")
        git("add", "service.ts")
        git("commit", "--quiet", "-m", label)
        sha = git("rev-parse", "HEAD")
        assert git("show", sha + ":service.ts") == source.strip()
        versions[label] = {
            "sha": sha,
            "blob": git("rev-parse", sha + ":service.ts"),
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        }
    for branch in review["input"]["branches"]:
        git("branch", branch["name"], versions[branch["version_label"]]["sha"])
    return {
        "versions": versions,
        "branches": review["input"]["branches"],
        "contexts": review["input"]["contexts"],
        "business_code_executed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="new private evidence directory"
    )
    parser.add_argument(
        "--list", action="store_true", help="validate selectors without execution"
    )
    parser.add_argument(
        "--cases", nargs="+", choices=[f"R{i:02}" for i in range(1, 19)],
        help="run only affected scenarios; unselected cases remain not run in this report",
    )
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    assert [c["id"] for c in manifest["cases"]] == [f"R{i:02}" for i in range(1, 19)]
    text = (ROOT / manifest["requirements"]).read_text()
    rows = {
        parts[1].split()[0]: parts
        for line in text.splitlines()
        if re.match(r"\| R\d{2} ", line)
        for parts in [[x.strip() for x in line.split("|")]]
    }
    for case in manifest["cases"]:
        row = rows[case["id"]]
        case.update(title=row[1], injection=row[2], assertions=row[3])
    selected = [c for c in manifest["cases"] if not args.cases or c["id"] in args.cases]
    selectors = sorted(
        {s for c in selected for s in c["tests"]}
        | set(manifest["harness_tests"])
    )
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(selectors)
    if loader.errors or suite.countTestCases() != len(selectors):
        parser.error("invalid or non-exact test selector: " + "\n".join(loader.errors))
    if args.list:
        print(
            json.dumps(
                {
                    "scenarios": len(rows),
                    "selected_cases": [c["id"] for c in selected],
                    "unique_tests": len(selectors),
                    "selectors": selectors,
                },
                indent=2,
            )
        )
        return 0
    if sys.platform != "linux" or os.environ.get("DIOS_TEST_DOCKER") != "1":
        parser.error("Linux and DIOS_TEST_DOCKER=1 required for native checks")
    if os.environ.get("DIOS_TEST_POSTGRES_URL"):
        parser.error(
            "this qualification uses disposable SQLite; unset DIOS_TEST_POSTGRES_URL"
        )
    # Native CLI Worker subprocesses must import the same checkout as the runner.
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "backend"), str(ROOT / "backend/tests")]
    )
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    paths = set((ROOT / "backend").rglob("*.py"))
    paths = {p for p in paths if ".venv" not in p.parts}
    paths.update((ROOT / "scripts").glob("*.py"))
    paths.update((ROOT / "examples/runtime").rglob("*"))
    paths.update([MANIFEST, ROOT / manifest["input"], ROOT / manifest["requirements"]])
    hashes = {str(p.relative_to(ROOT)): digest(p) for p in sorted(paths) if p.is_file()}
    review = next(
        x
        for x in json.loads((ROOT / manifest["input"]).read_text())["scenarios"]
        if x["id"] == "review"
    )
    materialized = materialize_review(args.output / "source", review)
    started = datetime.now(UTC).isoformat()
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    stable = all(digest(ROOT / p) == sha for p, sha in hashes.items())
    cases = [
        dict(c, status=scenario_status(c, result.outcomes)) for c in manifest["cases"]
    ]
    components_passed = (
        result.wasSuccessful()
        and stable
        and all(result.outcomes.get(s) == "passed" for s in selectors)
    )
    report = {
        "work_item": "F03-2.02",
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "environment": "Linux/WSL; SQLite; synthetic processes and Docker; no real model",
        "source_sha256": hashes,
        "sources_unchanged": stable,
        "materialized_review": materialized,
        "tests_run": result.testsRun,
        "selected_cases": [c["id"] for c in selected],
        "outcomes": result.outcomes,
        "test_seconds": result.elapsed,
        "components_passed": components_passed,
        "cases": cases,
        "scenario_counts": dict(Counter(c["status"] for c in cases)),
        "cross_agent_remaining": manifest["cross_agent_remaining"],
        "complete": components_passed
        and all(c["status"] == "passed" for c in cases)
        and not manifest["cross_agent_remaining"],
        "observations": [row for name in ("test_reference_faults", "test_reference_native_acp", "test_reference_remote_consistency", "test_reference_network", "test_reference_upgrade", "test_reference_unattended", "test_reference_observation")
                         if name in sys.modules for row in sys.modules[name].OBSERVATIONS],
    }
    target = args.output / "result.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    target.chmod(0o600)
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "tests_run",
                    "components_passed",
                    "scenario_counts",
                    "complete",
                )
            }
        )
    )
    # A partial qualification is a distinct non-success exit, never a green release gate.
    return 0 if report["complete"] else 2 if components_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
