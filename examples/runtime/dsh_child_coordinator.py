"""Open-Harness example: a model delegates through the governed MCP task channel."""
import argparse
import asyncio
import json
from dios_sdk import work_input, WorkloadError
from dios_sdk.dsh import dsh_execute
from dios_sdk.acp import diagnostic, emit


def decode(value):
    if (not isinstance(value, dict) or not isinstance(value.get('summary'), str)
            or not value['summary'].strip() or not isinstance(value.get('child_run_id'), str)
            or not 1 <= len(value['child_run_id']) <= 160):
        raise WorkloadError('EVIDENCE_INSUFFICIENT')
    return dict(summary=value['summary'], child_run_id=value['child_run_id'],
                evidence=['dios:run:' + value['child_run_id']])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    payload = work_input()
    source = payload['sample']
    prompt = ('Use only the DiOS MCP task tools. Submit exactly one child using dios_task_submit '
              'with the profile alias and sample_id below. Save its run ID. Repeatedly use dios_task_get '
              'to observe that same run until succeeded. Do not submit another child. The child may '
              'wait for human tool approval. Do not finish before it succeeds. If it fails, report failure. '
              'Do not use filesystem, shell, web, or any other tool. Once succeeded, return ONLY a JSON '
              'object with summary (Chinese summary of the returned child result) and child_run_id.\n'
              + json.dumps(dict(profile=source['child_profile'], sample_id=source['child_input'],
                                goal=payload['goal']), ensure_ascii=False))
    try:
        return asyncio.run(dsh_execute(args.config, prompt, decode))
    except asyncio.CancelledError:
        diagnostic('ACP_CANCELLED')
        return 130
    except Exception as exc:
        diagnostic(exc.code if isinstance(exc, WorkloadError) else 'COORDINATOR_FAILED')
        emit({'type':'error','code':'AGENT_EXECUTION_REJECTED'})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
