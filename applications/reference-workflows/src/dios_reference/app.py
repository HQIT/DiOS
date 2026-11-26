"""Deterministic synthetic business rules, owned by an independent application."""

import asyncio
import json
import math
import os
from uuid import uuid4

from dios_evidence.app import evidence_result, markdown_text
from dios_sdk import ToolChannelClient, WorkloadError, digest, emit

from . import __version__

KIND = "external.reference-workflow/v1"
FIELDS = {
    "id",
    "name",
    "scenario",
    "context_id",
    "query",
    "constraints",
    "source_refs",
    "sha256",
}


def validate_input(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise WorkloadError("INVALID_WORK_INPUT")
    if value["sha256"] != digest({k: v for k, v in value.items() if k != "sha256"}):
        raise WorkloadError("INPUT_DIGEST_MISMATCH")
    if not isinstance(value["scenario"], str) or value["scenario"] not in {
        "sales",
        "team",
        "education",
    }:
        raise WorkloadError("INVALID_WORK_INPUT")
    for key in ("id", "name", "context_id", "query"):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 160:
            raise WorkloadError("INVALID_WORK_INPUT")
    refs = value["source_refs"]
    if not isinstance(refs, dict) or not 1 <= len(refs) <= 3:
        raise WorkloadError("INVALID_WORK_INPUT")
    if any(
        not isinstance(k, str) or not isinstance(v, str) or len(v) != 64
        for k, v in refs.items()
    ):
        raise WorkloadError("INVALID_WORK_INPUT")
    constraints = value["constraints"]
    if value["scenario"] == "sales":
        if (
            not isinstance(constraints, dict)
            or set(constraints) != {"location", "battery_hours_min"}
            or not isinstance(constraints["location"], str)
            or type(constraints["battery_hours_min"]) not in (int, float)
            or not 0 <= constraints["battery_hours_min"] <= 1000
        ):
            raise WorkloadError("INVALID_WORK_INPUT")
    elif constraints != {}:
        raise WorkloadError("INVALID_WORK_INPUT")
    return value


def result(value, response, goal):
    value = validate_input(value)
    if not isinstance(goal, str) or not 1 <= len(goal) <= 2000:
        raise WorkloadError("INVALID_WORK_INPUT")
    # Reuse the independently packaged provider/citation protocol validator.
    evidence_input = {
        "id": value["id"],
        "name": value["name"],
        "filename": "reference.json",
        "source": "synthetic",
        "task_kind": "knowledge-evidence",
        "project_id": value["context_id"],
        "query": value["query"],
        "tool": "knowledge_search",
    }
    evidence_input["sha256"] = digest(evidence_input)
    verified = evidence_result(evidence_input, response, goal)
    citations = verified["citations"]
    if (
        verified["truncated"]
        or {c["id"]: c["digest"] for c in citations} != value["source_refs"]
    ):
        raise WorkloadError("EVIDENCE_INSUFFICIENT")
    try:
        documents = {c["id"]: json.loads(c["text"]) for c in citations}
        scenario = value["scenario"]
        if scenario == "sales":
            recommended, excluded = [], []
            for source_id, spec in documents.items():
                if (
                    set(spec) != {"id", "location", "battery_hours"}
                    or spec["id"] != source_id
                ):
                    raise ValueError()
                if (
                    type(spec["battery_hours"]) not in (int, float)
                    or not math.isfinite(spec["battery_hours"])
                    or not isinstance(spec["location"], str)
                ):
                    raise ValueError()
                constraints = value["constraints"]
                reasons = []
                if spec["location"] != constraints["location"]:
                    reasons.append("使用环境不匹配")
                if spec["battery_hours"] < constraints["battery_hours_min"]:
                    reasons.append("续航低于要求")
                (excluded if reasons else recommended).append(
                    {"id": source_id, "reasons": reasons}
                )
            output = {"recommended": recommended, "excluded": excluded}
            title = "有来源的选型建议"
        elif scenario == "team":
            plans, missing = [], []
            for source_id, plan in documents.items():
                if (
                    set(plan) != {"source", "work", "owner"}
                    or plan["source"] != source_id
                ):
                    raise ValueError()
                if (
                    not isinstance(plan["work"], str)
                    or not plan["work"]
                    or not (plan["owner"] is None or isinstance(plan["owner"], str))
                ):
                    raise ValueError()
                plans.append(plan)
                if not plan["owner"]:
                    missing.append(source_id)
            output = {
                "plans": plans,
                "sources_preserved": len(plans),
                "needs_owner": missing,
            }
            title = "岗位计划汇总与待补事项"
        else:
            sections = []
            for source_id, document in documents.items():
                if (
                    set(document) != {"id", "text", "audience"}
                    or document["id"] != source_id
                ):
                    raise ValueError()
                if (
                    not isinstance(document["audience"], list)
                    or "student" not in document["audience"]
                    or not isinstance(document["text"], str)
                    or not document["text"]
                ):
                    raise ValueError()
                sections.append({"source_id": source_id, "text": document["text"]})
            output = {
                "sections": sections,
                "source_ids": [s["source_id"] for s in sections],
            }
            title = "学生导读"
        paragraphs = ["# " + title, markdown_text(goal), "状态：待业务复核", "## 成果"]
        if scenario == "sales":
            paragraphs += [
                "建议："
                + "、".join(markdown_text(x["id"]) for x in output["recommended"])
            ]
            paragraphs += [
                markdown_text(x["id"]) + "：" + "、".join(x["reasons"])
                for x in output["excluded"]
            ]
        elif scenario == "team":
            paragraphs += [
                markdown_text(
                    x["source"]
                    + "："
                    + x["work"]
                    + "；负责人："
                    + (x["owner"] or "待指定")
                )
                for x in output["plans"]
            ]
        else:
            paragraphs += [markdown_text(x["text"]) for x in output["sections"]]
        paragraphs += ["## 来源"]
        paragraphs += [
            markdown_text(
                c["id"]
                + " · "
                + c["source"]
                + " · "
                + c["locator"]
                + " · "
                + c["version"]
            )
            for c in citations
        ]
        return {
            "summary": title,
            "scenario": scenario,
            "context_id": value["context_id"],
            "input_digest": value["sha256"],
            "application_version": __version__,
            "outcome": output,
            "citations": citations,
            "evidence": verified["evidence"],
            "review_status": "pending_review",
            "validation": "source-versions-and-reference-rules-checked",
            "markdown": "\n\n".join(paragraphs),
        }
    except (TypeError, ValueError, KeyError):
        raise WorkloadError("EVIDENCE_INSUFFICIENT") from None


async def execute(payload, client):
    value = validate_input(payload.get("sample"))
    session_id = "reference-" + str(uuid4())
    emit({"type": "session", "session_id": session_id})
    await client.ready(session_id)
    emit({"type": "output", "text": "等待授权资料，按来源版本生成待复核成果。"})
    arguments = {"query": value["query"]}
    response = await client.call("knowledge_search", arguments, "reference-source")
    await client.request(
        "verify_required",
        {"requirements": [{"tool": "knowledge_search", "arguments": arguments}]},
    )
    completed = result(value, response, payload.get("goal"))
    emit({"type": "result", "session_id": session_id, "result": completed})
    return completed


def main():
    try:
        raw = os.environ["DIOS_WORK_INPUT"]
        if len(raw.encode()) > 16384:
            raise WorkloadError("INVALID_WORK_INPUT")
        asyncio.run(
            execute(json.loads(raw), ToolChannelClient(os.environ["DIOS_TOOL_SOCKET"]))
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - emit bounded codes instead of provider diagnostics.
        code = getattr(exc, "code", "EVIDENCE_INSUFFICIENT")
        emit(
            {
                "type": "error",
                "code": code
                if code == "TOOL_APPROVAL_DENIED"
                else "EVIDENCE_INSUFFICIENT",
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
