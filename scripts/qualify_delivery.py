"""Build tracked sources and gate a bounded Linux delivery, with no model credentials."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Deliberate compatibility baseline, never a mutable branch or moving image tag.
BASELINE = "33e898ba9bfe761cbe8fad29a45b3f61f1cc6c66"


def command(*args, **kwargs):
    return subprocess.check_output(args, cwd=ROOT, text=True, **kwargs).strip()


def accepted(report):
    """A partial reference suite is valid only for this explicitly bounded CI gate."""
    cases = {case["id"]: case for case in report.get("cases", [])}
    return (
        report.get("components_passed") is True
        and report.get("sources_unchanged") is True
        and report.get("selected_cases") == ["R13"]
        and cases.get("R13", {}).get("status") == "passed"
        and not cases["R13"].get("gaps")
        and bool(report.get("outcomes"))
        and all(value == "passed" for value in report["outcomes"].values())
        and any(
            row.get("case") == "R13"
            and row.get("actual_concurrency_before_drain") == 3
            and row.get("prompt_calls") == 4
            and row.get("failed_image_rolled_back") is True
            and row.get("e2ag") == "enforced"
            for row in report.get("observations", [])
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--export-images", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Linux required")
    if any(
        os.environ.get(key)
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DIOS_TEST_POSTGRES_URL")
    ):
        parser.error("disposable local Docker and SQLite required")
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    summary = {
        "passed": False,
        "baseline_commit": BASELINE,
        "scope": "Linux installation and compatible-schema cutover",
        "paid_model_calls": 0,
    }
    try:
        head = command("git", "rev-parse", "HEAD")
        summary["candidate_commit"] = head
        subprocess.run(
            [
                "git",
                "diff",
                "--exit-code",
                "HEAD",
                "--",
                "backend",
                "frontend",
                "deploy",
                "scripts/qualify_reference_faults.py",
            ],
            cwd=ROOT,
            check=True,
        )
        # Export tracked baseline files only; never copy the developer environment.
        with tempfile.TemporaryDirectory(prefix="dios-ci-baseline-") as directory:
            baseline = Path(directory)
            archive = baseline / "source.tar"
            subprocess.run(
                ["git", "archive", "--output", str(archive), BASELINE, "backend"],
                cwd=ROOT,
                check=True,
            )
            with tarfile.open(archive) as package:
                package.extractall(baseline, filter="data")
            candidate = baseline / "candidate"
            candidate.mkdir()
            subprocess.run(
                [
                    "git",
                    "archive",
                    "--output",
                    str(archive),
                    head,
                    "backend",
                    "frontend",
                    "deploy/ci",
                ],
                cwd=ROOT,
                check=True,
            )
            with tarfile.open(archive) as package:
                package.extractall(candidate, filter="data")
            tags = {
                name: f"dios-ci-{name}:{head[:12]}"
                for name in ("baseline", "candidate", "frontend", "rejected")
            }
            builds = (
                ("baseline", baseline / "backend", []),
                ("candidate", candidate / "backend", []),
                ("frontend", candidate / "frontend", []),
                (
                    "rejected",
                    candidate / "deploy/ci",
                    [
                        "-f",
                        str(candidate / "deploy/ci/failed-backend.Dockerfile"),
                        "--build-arg",
                        "BASE_IMAGE=" + tags["candidate"],
                    ],
                ),
            )
            for name, context, extra in builds:
                print("Building " + name, flush=True)
                subprocess.run(
                    ["docker", "build", *extra, "-t", tags[name], str(context)],
                    cwd=ROOT,
                    check=True,
                )
            images = {
                name: command("docker", "image", "inspect", tag, "--format", "{{.Id}}")
                for name, tag in tags.items()
            }
            summary["images"] = images
            summary["docker"] = command(
                "docker", "version", "--format", "{{.Server.Version}}"
            )
            summary["compose"] = command("docker", "compose", "version", "--short")
            summary["python"] = sys.version
            summary["dependencies"] = command(
                sys.executable, "-m", "pip", "freeze"
            ).splitlines()
            env = dict(
                os.environ,
                DIOS_TEST_DOCKER="1",
                DIOS_TEST_UPGRADE_OLD=images["baseline"],
                DIOS_TEST_UPGRADE_NEW=images["candidate"],
                DIOS_TEST_UPGRADE_BAD=images["rejected"],
                DIOS_TEST_FRONTEND_IMAGE=images["frontend"],
                DIOS_TEST_ACP_IMAGE=images["candidate"],
            )
            # The candidate contains the production ACP bridge; only its protocol peer is synthetic.
            with (output / "tests.log").open("w") as log:
                result = subprocess.run(
                    [
                        sys.executable,
                        "scripts/qualify_reference_faults.py",
                        "--cases",
                        "R13",
                        "--output",
                        str(output / "qualification"),
                    ],
                    cwd=ROOT,
                    check=False,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            print((output / "tests.log").read_text(), flush=True)
            evidence = output / "qualification/result.json"
            report = json.loads(evidence.read_text())
            summary["qualification_exit"] = result.returncode
            summary["qualification_sha256"] = hashlib.sha256(
                evidence.read_bytes()
            ).hexdigest()
            summary["passed"] = result.returncode in (0, 2) and accepted(report)
            summary["reference_suite_complete"] = report["complete"]
            summary["observations"] = report["observations"]
            summary["remaining"] = [
                "other reference fault scenarios",
                "real model qualification",
                "schema-changing upgrade",
                "physical multi-host qualification",
            ]
            if summary["passed"] and args.export_images:
                for name, image_name in (
                    ("backend", "candidate"),
                    ("frontend", "frontend"),
                ):
                    subprocess.run(
                        [
                            "docker",
                            "save",
                            "--output",
                            str(output / (name + ".tar")),
                            images[image_name],
                        ],
                        check=True,
                    )
        return 0 if summary["passed"] else 1
    finally:
        (output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
        )


if __name__ == "__main__":
    raise SystemExit(main())
