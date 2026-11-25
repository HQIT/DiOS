"""Capability-only example: heterogeneous Harness delegation with verified child evidence."""

import argparse
import asyncio
import json
import os

from dios_sdk import WorkloadError, work_input
from dios_sdk.acp import diagnostic, emit
from dios_sdk.tools import ToolChannelClient


async def verified_child_result(value, channel):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("summary"), str)
        or not value["summary"].strip()
        or not isinstance(value.get("child_run_id"), str)
        or not 1 <= len(value["child_run_id"]) <= 160
    ):
        raise WorkloadError("EVIDENCE_INSUFFICIENT")
    child = await channel.child(value["child_run_id"])
    artifact = next(
        (a for a in child.get("artifacts", []) if a["kind"] == "result"), None
    )
    if child["state"] != "succeeded" or artifact is None or child.get("result") is None:
        raise WorkloadError("EVIDENCE_INSUFFICIENT")
    return {
        "summary": value["summary"],
        "child_run_id": child["id"],
        "child_result": child["result"],
        "evidence": ["dios:run:" + child["id"], "sha256:" + artifact["digest"]],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--harness", choices=["cursor", "codex", "dsh"], required=True)
    parser.add_argument("--receive-child", action="store_true")
    args = parser.parse_args()
    from dios_sdk.codex import codex_execute
    from dios_sdk.cursor import cursor_execute
    from dios_sdk.dsh import dsh_execute

    execute = {"cursor": cursor_execute, "codex": codex_execute, "dsh": dsh_execute}[
        args.harness
    ]
    payload = work_input()
    source = payload["sample"]
    prompt = (
        "Use only the DiOS MCP task tools. Submit exactly one child using dios_task_submit "
        "with the profile alias and sample_id below. Save its run ID. Repeatedly use dios_task_get "
        "to observe that same run until succeeded, with wait_seconds=20. A wait timeout is not "
        "task failure. Do not submit another child. The child may wait for human tool approval. "
        "Do not finish before it succeeds. If it fails, report failure. "
        "Do not use filesystem, shell, web, or any other tool. Once succeeded, return ONLY a JSON "
        "object with summary (Chinese summary of the returned child result) and child_run_id.\n"
        + json.dumps(
            {
                "profile": source["child_profile"],
                "sample_id": source["child_input"],
                "goal": payload["goal"],
            },
            ensure_ascii=False,
        )
    )
    if args.receive_child:
        prompt = (
            "The application is assigning exactly one independently authenticated child to this parent. "
            "Use dios_task_list with wait_seconds=20 until it returns a child. Then repeatedly call "
            "dios_task_get on that same run ID with wait_seconds=20 until succeeded. "
            "Do not submit a child yourself. Empty waits are normal. Human tool approval may take time. "
            "If the child fails, report failure. Use only these DiOS MCP tools, no filesystem, shell or web. "
            "After success, return ONLY JSON with summary (Chinese summary of the returned child result) "
            "and child_run_id. Goal: " + payload["goal"]
        )
    try:
        channel = ToolChannelClient(os.environ["DIOS_TOOL_SOCKET"])
        return asyncio.run(
            execute(
                args.config, prompt, lambda value: verified_child_result(value, channel)
            )
        )
    except asyncio.CancelledError:
        diagnostic("ACP_CANCELLED")
        return 130
    except Exception as exc:  # noqa: BLE001 -- normalize untrusted provider errors without leaking credentials
        diagnostic(exc.code if isinstance(exc, WorkloadError) else "COORDINATOR_FAILED")
        emit({"type": "error", "code": "AGENT_EXECUTION_REJECTED"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
