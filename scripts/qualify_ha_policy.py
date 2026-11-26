"""Verify HA ordering with the native Pacemaker scheduler in an offline container."""

import argparse
import hashlib
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ["dios-volume", "dios-service", "dios-address"]


def graph(path):
    actions, dependencies = {}, {}
    for synapse in ET.parse(path).getroot().findall("synapse"):
        incoming = {e.attrib["id"] for e in synapse.findall("inputs/trigger/*")}
        for action in synapse.findall("action_set/*"):
            key = action.attrib["id"]
            actions[key] = dict(action.attrib, kind=action.tag)
            dependencies[key] = incoming

    def ancestors(key, visited=None):
        visited = set() if visited is None else visited
        for parent in dependencies.get(key, set()):
            if parent not in visited:
                visited.add(parent)
                ancestors(parent, visited)
        return visited

    return actions, ancestors


def operation(actions, resource, name):
    return [
        key
        for key, action in actions.items()
        if action["kind"] == "rsc_op"
        and action.get("operation_key") == resource + "_" + name + "_0"
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    image = subprocess.check_output(
        ["docker", "image", "inspect", args.tools_image, "--format", "{{.Id}}"],
        text=True,
    ).strip()
    command = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--network",
        "none",
        "--env",
        "HOME=/tmp",
        "-v",
        f"{ROOT / 'deploy/ha'}:/input:ro",
        "-v",
        f"{output}:/out",
        image,
    ]

    def run(name, *argv):
        value = subprocess.run(
            [*command, *argv], capture_output=True, text=True, timeout=30, check=False
        )
        (output / (name + ".log")).write_text(value.stdout + value.stderr)
        if value.returncode:
            raise RuntimeError(f"{name} failed; inspect private log")
        return value.stdout

    def simulate(name, source, *events):
        run(
            name,
            "crm_simulate",
            "-x",
            source,
            "-S",
            *events,
            "-O",
            f"/out/{name}.xml",
            "-G",
            f"/out/{name}.graph.xml",
            "-D",
            f"/out/{name}.dot",
        )
        return graph(output / (name + ".graph.xml"))

    versions = run(
        "versions",
        "dpkg-query",
        "-W",
        "pacemaker-cli-utils",
        "resource-agents-base",
        "resource-agents-extra",
        "fence-agents-virsh",
    )
    agent_metadata = {}
    for agent in (
        "ocf:heartbeat:Filesystem",
        "ocf:heartbeat:IPaddr2",
        "stonith:fence_virsh",
    ):
        metadata = run(
            "metadata-" + agent.rsplit(":", 1)[-1],
            "crm_resource",
            "--show-metadata",
            agent,
        )
        document = ET.fromstring(metadata)
        assert document.tag == "resource-agent"
        if agent.startswith("ocf:"):
            assert document.find("actions/action[@name='start']") is not None
        agent_metadata[agent] = hashlib.sha256(metadata.encode()).hexdigest()
    actions, ancestors = simulate(
        "started",
        "/input/pacemaker.example.xml",
        "-u",
        "control-a",
        "-u",
        "control-b",
        "-u",
        "control-c",
        "-q",
        "true",
    )
    start = [operation(actions, r, "start") for r in RESOURCES]
    assert all(len(x) == 1 for x in start)
    assert len({actions[x[0]]["on_node"] for x in start}) == 1
    assert start[0][0] in ancestors(start[1][0]) and start[1][0] in ancestors(
        start[2][0]
    )
    run("verify", "crm_verify", "-x", "/out/started.xml", "-VVV")
    checks = ["native-schema-and-policy", "ordered-colocated-start"]
    observations = []
    for name, injection in [
        ("old-primary-failed", ["-f", "control-a"]),
        ("service-stop-failed", ["--op-inject", "dios-service_stop_0@control-a=1"]),
        ("volume-stop-failed", ["--op-inject", "dios-volume_stop_0@control-a=1"]),
    ]:
        actions, ancestors = simulate(name, "/out/started.xml", *injection)
        fences = {
            k
            for k, v in actions.items()
            if v.get("operation_key") == "stonith-control-a-off"
        }
        assert len(fences) == 1
        targets = []
        for resource in RESOURCES:
            starts = operation(actions, resource, "start")
            assert len(starts) == 1
            assert actions[starts[0]]["on_node"] != "control-a"
            assert fences <= ancestors(starts[0])
            targets.append(actions[starts[0]]["on_node"])
        assert len(set(targets)) == 1
        checks.append(name + "-fenced-before-takeover")
        observations.append(
            {
                "scenario": name,
                "fence": "off control-a",
                "successor": targets[0],
                "fence_precedes": RESOURCES,
            }
        )
    tree = ET.parse(output / "started.xml")
    tree.getroot().set("have-quorum", "false")
    tree.write(output / "quorum-lost-input.xml", encoding="utf-8")
    fixture = output / "quorum-lost-input.xml"
    fixture.write_text(
        "\n".join(line.rstrip() for line in fixture.read_text().splitlines()) + "\n"
    )
    actions, ancestors = simulate("quorum-lost", "/out/quorum-lost-input.xml")
    assert not any(operation(actions, r, "start") for r in RESOURCES)
    stops = [operation(actions, r, "stop") for r in RESOURCES]
    assert all(len(x) == 1 for x in stops)
    assert stops[2][0] in ancestors(stops[1][0]) and stops[1][0] in ancestors(
        stops[0][0]
    )
    checks.append("quorum-lost-reverse-stop-no-start")
    tree = ET.parse(output / "started.xml")
    resources = tree.find("configuration/resources")
    resources.remove(resources.find("primitive[@id='dios-fence']"))
    constraints = tree.find("configuration/constraints")
    for rule in list(constraints):
        if rule.get("rsc") == "dios-fence":
            constraints.remove(rule)
    # Model a missing provider, including its registrations in node executor state.
    # An orphaned but still registered device remains a usable provider to Pacemaker.
    for registered in tree.findall("status/node_state/lrm/lrm_resources"):
        for resource in list(registered):
            if resource.get("id") == "dios-fence":
                registered.remove(resource)
    tree.write(output / "fence-missing-input.xml", encoding="utf-8")
    fixture = output / "fence-missing-input.xml"
    fixture.write_text(
        "\n".join(line.rstrip() for line in fixture.read_text().splitlines()) + "\n"
    )
    actions, _ = simulate(
        "fence-missing", "/out/fence-missing-input.xml", "-f", "control-a"
    )
    assert not any(operation(actions, r, "start") for r in RESOURCES)
    checks.append("missing-fencing-blocks-takeover")
    report = {
        "work_item": "D01-3.01",
        "passed": True,
        "qualification": "offline native Pacemaker transition policy",
        "tools_image": image,
        "package_versions": versions.splitlines(),
        "checks": checks,
        "agent_metadata_sha256": agent_metadata,
        "observations": observations,
        "source_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                ROOT / "deploy/ha/pacemaker.example.xml",
                Path(__file__).resolve(),
            ]
        },
        "artifacts": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in output.iterdir()
            if p.suffix in {".xml", ".dot"}
        },
        "physical_fencing_executed": False,
        "services_started": False,
        "replication_rpo_verified": False,
        "complete": False,
    }
    (output / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps({"passed": True, "checks": checks, "complete": False}))


if __name__ == "__main__":
    main()
