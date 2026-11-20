"""Fixed, synthetic inline inputs for the first operator task flow."""
import hashlib
from app.runtime.database import RuntimeErrorCode

SAMPLES = {
    'first-item': {'name':'空列表边界检查', 'filename':'sample.py',
        'source':'def first(items):\n    return items[0]\n'},
    'average': {'name':'平均值边界检查', 'filename':'sample.py',
        'source':'def average(values):\n    return sum(values) / len(values)\n'},
}


def sample(sample_id):
    if sample_id not in SAMPLES: raise RuntimeErrorCode('SAMPLE_NOT_FOUND')
    value = dict(SAMPLES[sample_id], id=sample_id)
    value['sha256'] = hashlib.sha256(value['source'].encode()).hexdigest()
    return value


def sample_prompt(value, goal, required_tool=None):
    numbered = '\n'.join(f'{n}: {line}' for n,line in enumerate(value['source'].splitlines(),1))
    tool_instruction = ('Do not use tools, files, commands or network. ' if required_tool is None else
        f'First call the governed MCP tool {required_tool} once with query={value["sha256"]}. '
        'Wait for its approval and result. If denied, stop and report insufficient evidence. '
        'Do not use any other tools, files, commands or network. ')
    return ('Review only the synthetic source below using static reasoning. '+tool_instruction+
        'Return ONLY a JSON object with summary (Chinese text) and '
        'findings (array). Each finding must contain title, line_start, line_end, condition, '
        'evidence (an exact contiguous substring of the specified source lines), and impact. '
        'Use an empty findings array if there is no supported finding. Never invent evidence.\n'
        f'User goal: {goal}\nSource: {value["filename"]} SHA256 {value["sha256"]}\n{numbered}')


def review_result(value, result):
    """Check source attribution/shape; business defect accuracy needs human review."""
    if not isinstance(result,dict) or not isinstance(result.get('summary'),str) or not result['summary'].strip():
        raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
    findings=result.get('findings')
    if not isinstance(findings,list) or len(findings)>20: raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
    lines=value['source'].splitlines();sections=['# 静态评审报告',result['summary']]
    for index,f in enumerate(findings,1):
        if not isinstance(f,dict): raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
        start,end=f.get('line_start'),f.get('line_end')
        if type(start) is not int or type(end) is not int or not 1<=start<=end<=len(lines):
            raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
        if any(not isinstance(f.get(k),str) or not f[k].strip() or len(f[k])>2000
               for k in ('title','condition','evidence','impact')):
            raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
        if f['evidence'] not in '\n'.join(lines[start-1:end]): raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
        sections.extend([f'## {index}. {f["title"]}',f'位置：{value["filename"]}:{start}–{end}',
            f'触发条件：{f["condition"]}',f'影响：{f["impact"]}', '证据：\n\n    '+f['evidence'].replace('\n','\n    ')])
    if not findings: sections.append('本次未报告有证据支持的问题。')
    sections.extend(['## 来源',f'{value["filename"]} · SHA256 `{value["sha256"]}`',
        '结论待人工复核；本次校验覆盖结果结构、行号与代码引用。'])
    return dict(summary=result['summary'],findings=findings,markdown='\n\n'.join(sections),
        evidence=[f'{value["filename"]}@{value["sha256"]}'],source=value,
        validation='source-references-checked',review_status='pending_review')


def main(acp_execute=None):
    """Application wrapper; ACP transport and runtime ownership remain generic."""
    import argparse
    import asyncio
    import json
    import os
    from app.runtime.acp_bridge import execute, diagnostic, emit
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True)
    parser.add_argument('--required-tool',choices=['repository_search'])
    args=parser.parse_args()
    try:
        raw=os.environ.get('DIOS_WORK_INPUT','')
        if len(raw.encode())>16384:raise RuntimeErrorCode('INVALID_WORK_INPUT')
        payload=json.loads(raw)
        supplied=payload.get('sample',{});value=sample(supplied.get('id'))
        if supplied!=value:raise RuntimeErrorCode('SAMPLE_DIGEST_MISMATCH')
        goal=payload.get('goal')
        if not isinstance(goal,str) or not 1<=len(goal.strip())<=2000:raise RuntimeErrorCode('INVALID_WORK_INPUT')
        return asyncio.run((acp_execute or execute)(args.config,sample_prompt(value,goal,args.required_tool),lambda result:review_result(value,result),
            required_tools=([{'tool':args.required_tool,'arguments':{'query':value['sha256']}}] if args.required_tool else [])))
    except asyncio.CancelledError:diagnostic('ACP_CANCELLED');return 130
    except RuntimeErrorCode as exc:
        from app.runtime.faults import CATEGORIES
        diagnostic(exc.code)
        emit({'type':'error','code':exc.code if exc.code in CATEGORIES else 'AGENT_EXECUTION_REJECTED'})
        return 2
    except Exception:
        diagnostic('REVIEW_BRIDGE_FAILED');emit({'type':'error','code':'REVIEW_BRIDGE_FAILED'});return 1

if __name__=='__main__':raise SystemExit(main())
